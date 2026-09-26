import base64
import glob
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from multiprocessing import Pool

from .progress import step, track

from .packs import PackStore, PackWriter

WORKERS = os.cpu_count() or 4
BLOCK_SIZE = 1048576
# check the first bytes instead of loading the file. some of these are 3gb
MANIFEST_PROBE = b'{"__wad_manifest__"'
COMMIT_BYTES = 4 * 1024 ** 3
MIN_FREE_BYTES = 8 * 1024 ** 3
BUNDLE_MAX = 64 * 1024
# room to leave on the drive when keeping an old patch ready means building the new one from scratch
KEEP_MARGIN = 5 * 1024 ** 3

# riot's own empty archive, byte for byte. they ship it as Audio.wad.client and a few others,
# so the game's fine with it. quick start uses it for champions that aren't in the replay
STUB_WAD = base64.b64decode(
    'UlcDBBpO+D8pVKe2WRyMSJWV0ZSIP1V3mhGD0fTF61eS3cyOXf7BTuDPueoSDZscdLucYKdup5A4jQtnm3kwnO2MMB0Z'
    'vrvYuGji8bjfdmhM+sAbOgm5Gg/5JYE5RKgoyQqyziaIsb9kWXWNS5CuEDuMs2CT0kGeyk0/LdjLfc/5W2h4DVl2JgDt'
    'QJLNZRHSvoUghDYuiMVzZUaLOGNfqcVRdb6/yl+ilkD21rR/iDBOnkd/+K8svDeJzTYfnouABNpq/85oppVbwcrzxxq5'
    'Qj9nueOodSQvzrTQl4OoJz055+xE4HAtqObGaaMiL3bSbSikOVjRQdKlqh1QjrLjBS4Z5maZ6dhRN9tG7wAAAAA=')

_pack_stores = {}
_cost_cache = {}

_locks = {}


class _RepoLock:
    """shared by every thread and every process using this repo. named mutex"""

    def __init__(self, root):
        import win32event
        self._ev = win32event
        key = hashlib.sha1(os.path.normcase(os.path.abspath(root)).encode()).hexdigest()
        self._handle = win32event.CreateMutex(None, False, 'Local\\league_vcs_repo_' + key)

    def __enter__(self):
        self._ev.WaitForSingleObject(self._handle, self._ev.INFINITE)
        return self

    def __exit__(self, *exc):
        self._ev.ReleaseMutex(self._handle)


def initializer():
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def hash_file(fp):
    file_hash = hashlib.sha256()
    with open(fp, 'rb') as f:
        fb = f.read(BLOCK_SIZE)
        while fb:
            file_hash.update(fb)
            fb = f.read(BLOCK_SIZE)
    return file_hash.hexdigest()


def save_to_repo(src, dst):
    if os.path.exists(dst):
        return
    # own tmp name per worker, two of them on the same file used to stomp each other
    tmp = f'{dst}.{os.getpid()}.{threading.get_ident()}.tmp'
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
        os.chmod(dst, stat.S_IREAD)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if not os.path.exists(dst):
            raise


def load_from_repo(src, dst):
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def _is_wad_path(rel_path):
    return rel_path.lower().endswith('.wad.client')


def _write_json_atomic(path, data, **kw):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, **kw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# patch json could come from anyone's storage folder. check it before it turns into a path
_DIGEST = re.compile(r'[0-9a-f]{64}')
_TAG = re.compile(r'[0-9A-Za-z][0-9A-Za-z._-]*')


def _check_digest(digest):
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError(f'Bad file hash in storage: {digest!r}')
    return digest


def _check_rel(rel):
    if (not isinstance(rel, str) or not rel or os.path.isabs(rel) or os.path.splitdrive(rel)[0]
            or rel[0] in '\\/' or '..' in re.split(r'[\\/]', rel)):
        raise ValueError(f'Bad file path in storage: {rel!r}')
    return rel


def _check_tag(tag):
    if not isinstance(tag, str) or not _TAG.fullmatch(tag):
        raise ValueError(f'Bad patch name: {tag!r}')
    return tag


def _version_key(tag):
    return [int(p) if p.isdigit() else 0 for p in tag.split('.')]


def _check_manifest(manifest, checksum):
    try:
        if 'segments' in manifest:
            for digest, size in manifest['segments']:
                if digest is not None:
                    _check_digest(digest)
                if not isinstance(size, int) or size < 0:
                    raise ValueError
        else:
            for entry in manifest['entries']:
                _check_digest(entry['data_hash'])
    except (ValueError, TypeError, KeyError):
        raise ValueError(f'Stored manifest {checksum} is corrupt')
    return manifest


def _load_manifest(repo_files_dir, checksum):
    """manifest json, or None if it's just a whole file"""
    path = os.path.join(repo_files_dir, _check_digest(checksum))
    try:
        with open(path, 'rb') as f:
            if f.read(len(MANIFEST_PROBE)) != MANIFEST_PROBE:
                return None
            f.seek(0)
            raw = f.read()
    except FileNotFoundError:
        return None
    try:
        return _check_manifest(json.loads(raw), checksum)
    except (ValueError, UnicodeDecodeError):
        raise ValueError(f'Stored manifest {checksum} is corrupt')


def _collect_all_hashes(patch, repo_files_dir):
    """everything a patch needs, wad pieces included"""
    from league_vcs.parsers.wad import manifest_hashes

    all_hashes = set(patch.keys())
    for checksum, rel_path in patch.items():
        if _is_wad_path(rel_path):
            manifest = _load_manifest(repo_files_dir, checksum)
            if manifest:
                all_hashes |= manifest_hashes(manifest)
    return all_hashes


def _store_manifest(files_dir, manifest):
    manifest_json = json.dumps(manifest, separators=(',', ':'), sort_keys=True)
    manifest_hash = hashlib.sha256(manifest_json.encode()).hexdigest()
    dst = os.path.join(files_dir, manifest_hash)
    if not os.path.exists(dst):
        tmp = f'{dst}.{threading.get_ident()}.tmp'
        try:
            with open(tmp, 'w') as f:
                f.write(manifest_json)
            os.replace(tmp, dst)
            os.chmod(dst, stat.S_IREAD)
        except (PermissionError, FileExistsError):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return manifest_hash


def _unlink_blob(path):
    try:
        os.chmod(path, stat.S_IWRITE)
        os.unlink(path)
    except FileNotFoundError:
        pass


def _delete_tree(folders, label):
    """rmtree but one file at a time so there's something to show. stored files are read only"""
    files = [os.path.join(root, f) for folder in folders for root, _, names in os.walk(folder) for f in names]
    for path in track(files, label=label):
        try:
            _unlink_blob(path)
        except OSError:
            pass  # in use, rmtree skips it too
    for folder in folders:
        shutil.rmtree(folder, ignore_errors=True)


class LargeVCS:
    def __init__(self, root, repo_name='lvcs', current_name='current', do_copy=False):
        self.root = os.path.abspath(root)
        self.repo_name = repo_name
        self.current_name = current_name
        self.do_copy = do_copy
        self.current_patch_path = self.repo_path('current.json')
        # how many prepared patches to keep, the current one included. 1 = old behaviour
        self.keep_prepared = 1

    def path(self, *parts):
        return os.path.abspath(os.path.join(self.root, *parts))

    def repo_path(self, *parts):
        return self.path(self.repo_name, *parts)

    def current_path(self, *parts):
        return self.path(self.current_name, *parts)

    def kept_path(self, *parts):
        """older prepared patches, parked so switching back is a rename"""
        return self.path('prepared', *parts)

    def kept(self):
        """tags of the parked ones, newest first"""
        try:
            names = [e for e in os.scandir(self.kept_path()) if e.is_dir() and _TAG.fullmatch(e.name)]
        except FileNotFoundError:
            return []
        return [e.name for e in sorted(names, key=lambda e: e.stat().st_mtime, reverse=True)]

    def _stubs_file(self, tag=None):
        return self.kept_path(_check_tag(tag) + '.stubs.json') if tag else self.repo_path('current.stubs.json')

    def stubs(self, tag=None):
        """paths in a prepared folder that are only placeholders (quick start)"""
        try:
            with open(self._stubs_file(tag)) as f:
                return {_check_rel(r) for r in json.load(f)}
        except (FileNotFoundError, ValueError, TypeError):
            return set()

    def _save_stubs(self, stubs, tag=None):
        if stubs:
            _write_json_atomic(self._stubs_file(tag), sorted(stubs))
        else:
            try:
                os.unlink(self._stubs_file(tag))
            except FileNotFoundError:
                pass

    @property
    def files_dir(self):
        return self.repo_path('files')

    @property
    def lock(self):
        lock = _locks.get(self.root)
        if lock is None:
            lock = _locks[self.root] = _RepoLock(self.root)
        return lock

    @property
    def packs(self):
        store = _pack_stores.get(self.files_dir)
        if store is None:
            store = _pack_stores[self.files_dir] = PackStore(self.files_dir)
        else:
            store.reload()
        return store

    def _blob_size(self, digest, packs=None):
        packs = packs or self.packs
        if digest in packs:
            return packs.size(digest)
        try:
            return os.path.getsize(os.path.join(self.files_dir, digest))
        except OSError:
            return 0

    def ensure_repo(self):
        assert os.path.exists(self.path(self.repo_name)), 'Not in repository!'

    def _patch_path(self, tag, ext='.json'):
        return self.repo_path('patches', _check_tag(tag) + ext)

    def get_patch(self, tag):
        try:
            with open(self._patch_path(tag)) as patch_file:
                patch = json.load(patch_file)
        except FileNotFoundError:
            return None
        if not isinstance(patch, dict):
            raise ValueError(f'Patch {tag} is corrupt')
        for checksum, rel_path in patch.items():
            _check_digest(checksum)
            _check_rel(rel_path)
        return patch

    def _save_patch(self, tag, patch):
        _write_json_atomic(self._patch_path(tag), patch)

    # riot ships a bunch of identical placeholder wads under different names. evil
    # patches are {hash: path}, so extra paths for the same hash go in <tag>.json.dups
    def _dups_path(self, tag):
        return self._patch_path(tag, '.json.dups')

    def get_dups(self, tag):
        try:
            with open(self._dups_path(tag)) as f:
                dups = json.load(f)
        except (FileNotFoundError, ValueError):
            return {}
        if not isinstance(dups, dict):
            return {}
        for checksum, paths in dups.items():
            _check_digest(checksum)
            if not isinstance(paths, list):
                raise ValueError(f'Patch {tag} is corrupt')
            for rel_path in paths:
                _check_rel(rel_path)
        return dups

    def _save_dups(self, tag, dups):
        if dups:
            _write_json_atomic(self._dups_path(tag), dups)
        else:
            try:
                os.unlink(self._dups_path(tag))
            except FileNotFoundError:
                pass

    @staticmethod
    def _record(patch, dups, checksum, rel):
        if checksum in patch and os.path.normcase(patch[checksum]) != os.path.normcase(rel):
            paths = dups.setdefault(checksum, [])
            if rel not in paths:
                paths.append(rel)
        else:
            patch[checksum] = rel

    def pairs(self, tag, patch=None):
        """every (hash, path) incl the dupes"""
        patch = patch if patch is not None else (self.get_patch(tag) or {})
        out = set(patch.items())
        for checksum, paths in self.get_dups(tag).items():
            out.update((checksum, p) for p in paths)
        return out

    def current(self):
        try:
            with open(self.current_patch_path) as file:
                return json.load(file)
        except FileNotFoundError:
            return None

    def clean(self):
        with self.lock:
            self._clean()

    def _clean(self):
        _delete_tree([self.current_path()], 'Removing prepared files')
        self._save_stubs(set())
        try:
            os.unlink(self.current_patch_path)
        except FileNotFoundError:
            pass

    def _drop_kept(self, tags=None):
        for tag in (self.kept() if tags is None else tags):
            if os.path.isdir(self.kept_path(tag)):
                _delete_tree([self.kept_path(tag)], f'Removing kept copy of {tag}')
            self._save_stubs(set(), tag)

    def _park(self, tag, room_needed):
        """move the prepared folder aside instead of throwing it away. false if we're only keeping
        one, or the drive's too full to build the next one next to it"""
        if self.keep_prepared <= 1 or not os.path.isdir(self.current_path()):
            return False
        if shutil.disk_usage(self.root).free < room_needed + KEEP_MARGIN:
            return False
        self._drop_kept([tag])
        os.makedirs(self.kept_path(), exist_ok=True)
        os.rename(self.current_path(), self.kept_path(tag))
        os.utime(self.kept_path(tag))
        self._save_stubs(self.stubs(), tag)
        self._save_stubs(set())
        os.unlink(self.current_patch_path)
        return True

    def _unpark(self, tag):
        if not os.path.isdir(self.kept_path(tag)):
            return False
        if os.path.exists(self.current_path()):
            self._clean()
        os.rename(self.kept_path(tag), self.current_path())
        self._save_stubs(self.stubs(tag))
        self._save_stubs(set(), tag)
        # it was complete when it got parked
        _write_json_atomic(self.current_patch_path, tag)
        return True

    def _wad_size(self, checksum):
        m = _load_manifest(self.files_dir, checksum)
        if m is None:
            return os.path.getsize(os.path.join(self.files_dir, checksum))
        return m.get('size') or 1

    @classmethod
    def load_or_create(cls, root):
        repo = cls(root)
        os.makedirs(repo.repo_path('files'), exist_ok=True)
        os.makedirs(repo.repo_path('patches'), exist_ok=True)
        return repo

    @staticmethod
    def _hash_file(params):
        full_path, rel_path = params
        return full_path, rel_path, hash_file(full_path)

    @staticmethod
    def _add_file(params):
        full_path, _, _, dest_path = params
        save_to_repo(full_path, dest_path)

    def _known_blobs(self):
        return set(os.listdir(self.files_dir)) | set(self.packs.index)

    def _store_wad(self, full_path, known, expected_sha=None, writer=None):
        """exact pieces if we can, whole file if we can't"""
        from league_vcs.parsers.wad import unpack_wad_exact

        try:
            manifest = unpack_wad_exact(full_path, self.files_dir, known, expected_sha, writer)
        except ValueError:
            manifest = None
        if manifest:
            return _store_manifest(self.files_dir, manifest)
        checksum = expected_sha or hash_file(full_path)
        save_to_repo(full_path, os.path.join(self.files_dir, checksum))
        return checksum

    def add(self, target, tag):
        self.ensure_repo()
        with self.lock:
            patch_path = self._patch_path(tag)
            assert not os.path.exists(patch_path), f'Patch {tag} already exists!'

            regular_files, wad_files = [], []
            print('Retrieving file listing...')
            for root, _, files in os.walk(target):
                for file in files:
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, target)
                    (wad_files if _is_wad_path(rel_path) else regular_files).append((full_path, rel_path))

            print(f'Found {len(regular_files)} regular files and {len(wad_files)} WAD archives.')
            patch, dups = {}, {}

            if regular_files:
                pool = Pool(WORKERS, initializer=initializer)
                try:
                    print('Hashing regular files...')
                    with_hash = list(track(pool.imap_unordered(self._hash_file, regular_files),
                                           total=len(regular_files), label='Hashing files'))
                    to_add, queued = [], set()
                    for fp, rp, cs in with_hash:
                        dest = self.repo_path('files', cs)
                        if cs not in queued and not os.path.exists(dest):
                            queued.add(cs)
                            to_add.append((fp, rp, cs, dest))
                    if to_add:
                        print(f'Storing {len(to_add)} new regular files...')
                        list(track(pool.imap_unordered(self._add_file, to_add), total=len(to_add), label='Storing files'))
                except KeyboardInterrupt:
                    pool.terminate()
                    pool.join()
                    raise
                finally:
                    pool.close()
                for _, rel_path, checksum in with_hash:
                    self._record(patch, dups, checksum, rel_path)

            if wad_files:
                known = self._known_blobs()
                writer = PackWriter(self.packs)
                wad_files.sort(key=lambda w: os.path.getsize(w[0]), reverse=True)
                print(f'Storing {len(wad_files)} WAD archives ({WORKERS} threads)...')
                try:
                    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
                        futures = {executor.submit(self._store_wad, fp, known, None, writer): rp
                                   for fp, rp in wad_files}
                        for future in track(as_completed(futures), total=len(futures), label='Storing archives'):
                            self._record(patch, dups, future.result(), futures[future])
                finally:
                    writer.seal()

            self._save_dups(tag, dups)
            self._save_patch(tag, patch)

    def top_up(self, target, tag):
        """grab files riot added after we first stored this version"""
        self.ensure_repo()
        with self.lock:
            patch = self.get_patch(tag)
            if patch is None:
                return 0
            dups = self.get_dups(tag)
            have = {os.path.normcase(rp) for _, rp in self.pairs(tag, patch)}
            missing = []
            for root, _, files in os.walk(target):
                for f in files:
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, target)
                    key = os.path.normcase(rel)
                    if key not in have:
                        missing.append((full, rel))
            if not missing:
                return 0

            print(f'Patch {tag}: storing {len(missing)} files added since it was first stored...')
            known = self._known_blobs()
            writer = PackWriter(self.packs)
            added = []
            try:
                for full, rel in missing:
                    if _is_wad_path(rel):
                        checksum = self._store_wad(full, known, None, writer)
                    else:
                        checksum = hash_file(full)
                        save_to_repo(full, os.path.join(self.files_dir, checksum))
                    self._record(patch, dups, checksum, rel)
                    added.append((checksum, rel))
            finally:
                writer.seal()
            self._save_dups(tag, dups)
            self._save_patch(tag, patch)

            if self.current() == tag and os.path.isdir(self.current_path()):
                for checksum, rel in added:
                    out = self.current_path(rel)
                    if os.path.exists(out):
                        continue
                    if _is_wad_path(rel):
                        self._build_wad(checksum, out)
                    else:
                        self._restore_file((checksum, rel))
            return len(added)

    def list(self):
        files = glob.glob(self.repo_path('patches', '*.json'))
        tags = (os.path.splitext(os.path.basename(fp))[0] for fp in files)
        return sorted(t for t in tags if _TAG.fullmatch(t))

    def drop(self, tag, collect=True):
        """collect=False when dropping several, then gc() once at the end. it reads every patch"""
        self.ensure_repo()
        with self.lock:
            assert self.current() != tag, f"Can't delete patch {tag} as it's the current one."
            patch = self.get_patch(tag)
            assert patch is not None, f'Patch {tag} does not exist!'

            os.unlink(self._patch_path(tag))
            self._save_dups(tag, {})
            self._drop_kept([tag])
            if collect:
                self.gc()

    def _restore_file(self, params):
        checksum, rel_path = params
        full_path = self.current_path(rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        load_from_repo(self.repo_path('files', checksum), full_path)

    def _build_wad(self, checksum, output_path):
        from league_vcs.parsers.wad import pack_wad, pack_wad_exact

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        manifest = _load_manifest(self.files_dir, checksum)
        if manifest is None:
            load_from_repo(os.path.join(self.files_dir, checksum), output_path)
        elif 'segments' in manifest:
            pack_wad_exact(manifest, self.files_dir, output_path, self.packs)
        else:
            pack_wad(manifest, self.files_dir, output_path)

    def restore(self, tag, clean=False, skip=()):
        """make tag the prepared patch. skip = wad paths that can be placeholders this time (quick start)"""
        self.ensure_repo()
        with self.lock:
            try:
                self._restore(tag, clean, skip)
            finally:
                # let go of the mmaps so other processes can compact
                self.packs.close()

    def _restore(self, tag, clean, skip):
        patch = self.get_patch(tag)
        assert patch is not None, f'Patch {tag} does not exist!'
        wanted = self.pairs(tag, patch)
        skip = {os.path.normcase(r) for r in skip}
        skip = {r for _, r in wanted if _is_wad_path(r) and os.path.normcase(r) in skip}

        current = self.current()
        if clean:
            self._clean()
            current = None
        elif current is not None and not os.path.isdir(self.current_path()):
            current = None

        if current != tag:
            wad_bytes = sum(self._wad_size(h) for h, r in wanted if _is_wad_path(r) and r not in skip)
            if current and self._park(current, wad_bytes):
                current = None
            if current is None and self._unpark(tag):
                current = tag
                print(f'Patch {tag} was kept ready.')

        stubs = self.stubs() if current else set()
        if current == tag:
            # already here. fill in whatever's missing, and real files for placeholders that are needed now
            todo = {(h, r) for h, r in wanted
                    if (r in stubs and r not in skip) or not os.path.exists(self.current_path(r))}
            if not todo:
                print(f'Already on {tag}.')
                return
        else:
            current_patch = self.get_patch(current) if current else None
            if current_patch is None:
                self._clean()
                stubs = set()
            # compare (hash, path), not just hash. files move between patches. placeholders never count
            have = {(h, r) for h, r in self.pairs(current, current_patch) if r not in stubs} if current_patch else set()
            stale = sorted({r for _, r in have - wanted} | stubs)
            stubs = set()
            todo = wanted - have
            if stale:
                print(f'Removing {len(stale)} outdated files...')
                for rel_path in stale:
                    full_path = self.current_path(rel_path)
                    if os.path.exists(full_path):
                        os.chmod(full_path, stat.S_IWRITE)
                        os.unlink(full_path)

        # write None first so a crash mid-restore gets rebuilt next time
        _write_json_atomic(self.current_patch_path, None)

        regular = sorted(t for t in todo if not _is_wad_path(t[1]))
        wads = sorted(t for t in todo if _is_wad_path(t[1]))

        if regular:
            print(f'Linking {len(regular)} files...')
            for item in track(regular, label='Linking files'):
                self._restore_file(item)

        placeholders = [t for t in wads if t[1] in skip]
        wads = [t for t in wads if t[1] not in skip]
        for _, rel in placeholders:
            out = self.current_path(rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, 'wb') as f:
                f.write(STUB_WAD)
            stubs.add(rel)
        if placeholders:
            print(f'Quick start: {len(placeholders)} archives for champions not in this game left as placeholders.')

        wads = self._link_from_kept(tag, wads, stubs)
        if wads:
            sizes = {h: self._wad_size(h) for h, _ in wads}
            wads.sort(key=lambda x: sizes[x[0]], reverse=True)
            total = sum(sizes.values())
            print(f'Building {len(wads)} WAD archives, {total / 1024 ** 3:.1f} GB ({WORKERS} threads)...')
            done = 0
            step(0, total, 'Building archives')
            with ThreadPoolExecutor(max_workers=WORKERS) as executor:
                futures = {executor.submit(self._build_wad, cs, self.current_path(rp)): (cs, rp) for cs, rp in wads}
                for future in as_completed(futures):
                    future.result()
                    cs, rp = futures[future]
                    stubs.discard(rp)
                    done += sizes[cs]
                    step(done, total, 'Building archives')

        self._save_stubs(stubs)
        _write_json_atomic(self.current_patch_path, tag)
        # keep_prepared counts the current one
        self._drop_kept(self.kept()[max(self.keep_prepared - 1, 0):])
        print(f'Restored patch {tag}!')

    def _link_from_kept(self, tag, wads, stubs):
        """identical archives in a kept patch get hard linked instead of rebuilt. returns what's left"""
        have = {}
        for other in self.kept():
            if other == tag:
                continue
            other_stubs = self.stubs(other)
            for h, r in self.pairs(other):
                if _is_wad_path(r) and r not in other_stubs:
                    have.setdefault((h, os.path.normcase(r)), self.kept_path(other, r))
        left, linked = [], 0
        for h, r in wads:
            src = have.get((h, os.path.normcase(r)))
            out = self.current_path(r)
            if src and os.path.isfile(src):
                try:
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    if os.path.exists(out):
                        os.chmod(out, stat.S_IWRITE)
                        os.unlink(out)
                    os.link(src, out)
                    stubs.discard(r)
                    linked += 1
                    continue
                except OSError:
                    pass
            left.append((h, r))
        if linked:
            print(f'Reused {linked} archives from kept patches.')
        return left

    def export(self, tag, destination):
        self.ensure_repo()
        with self.lock:
            try:
                self._export(tag, destination)
            finally:
                self.packs.close()

    def _export(self, tag, destination):
        patch = self.get_patch(tag)
        assert patch is not None, f'Patch {tag} does not exist!'

        items = sorted(self.pairs(tag, patch), key=lambda x: os.path.getsize(os.path.join(self.files_dir, x[0])),
                       reverse=True)
        print(f'Exporting {len(items)} files ({WORKERS} threads)...')

        def _one(item):
            checksum, rel_path = item
            dst = os.path.join(destination, rel_path)
            if _is_wad_path(rel_path):
                self._build_wad(checksum, dst)
            else:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                load_from_repo(os.path.join(self.files_dir, checksum), dst)

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = [executor.submit(_one, item) for item in items]
            for future in track(as_completed(futures), total=len(futures), label='Exporting files'):
                future.result()

    # storage format stuff

    def storage_report(self):
        """what format each patch is in"""
        report = []
        for tag in self.list():
            patch = self.get_patch(tag) or {}
            kinds = {'exact': 0, 'legacy': 0, 'whole': 0}
            whole_bytes = 0
            for checksum, rel_path in patch.items():
                if not _is_wad_path(rel_path):
                    continue
                path = os.path.join(self.files_dir, checksum)
                try:
                    with open(path, 'rb') as f:
                        probe = f.read(64)
                except FileNotFoundError:
                    continue
                if not probe.startswith(MANIFEST_PROBE):
                    kinds['whole'] += 1
                    whole_bytes += os.path.getsize(path)
                elif b'"format":2' in probe:
                    kinds['exact'] += 1
                else:
                    kinds['legacy'] += 1
            report.append({'tag': tag, **kinds, 'whole_bytes': whole_bytes})
        return report

    def total_size(self):
        total = self.packs.total_bytes()
        with os.scandir(self.files_dir) as it:
            for entry in it:
                if entry.is_file():
                    total += entry.stat().st_size
        return total

    def _segment_hashes(self):
        """what's allowed in bundles. patch-level files and legacy stuff get read as loose files,
        so they stay out or restores break"""
        segs, loose_only = set(), set()
        for tag in self.list():
            patch = self.get_patch(tag) or {}
            loose_only.update(patch)
            for checksum, rel_path in patch.items():
                if _is_wad_path(rel_path):
                    m = _load_manifest(self.files_dir, checksum)
                    if not m:
                        continue
                    if 'segments' in m:
                        segs.update(d for d, _ in m['segments'] if d)
                    else:
                        loose_only.update(e['data_hash'] for e in m['entries'])
        return segs - loose_only

    def repack(self, log=print):
        """rewrite the bundles so each archive's pieces sit together, in the order a rebuild reads them.
        rebuilding then reads long straight runs instead of hopping across tens of thousands of files.
        loose copies only go once their new bundle is written, old bundles only at the very end,
        so stopping halfway loses nothing"""
        from .packs import PACK_TARGET
        self.ensure_repo()
        with self.lock:
            packs = self.packs
            segs = self._segment_hashes()
            order, seen = [], set()
            for tag in sorted(self.list(), key=_version_key, reverse=True):
                wads = sorted(((c, r) for c, r in (self.get_patch(tag) or {}).items() if _is_wad_path(r)),
                              key=lambda x: x[1].lower())
                for checksum, _ in wads:
                    m = _load_manifest(self.files_dir, checksum)
                    for d, _ in (m or {}).get('segments', ()):
                        if d and d in segs and d not in seen:
                            seen.add(d)
                            order.append(d)
            if not order:
                log('Nothing to rearrange.')
                return
            old_index = dict(packs.index)
            old_bundles = sorted({base for base, _, _ in old_index.values()})
            old_bytes = sum(size for _, _, size in old_index.values())
            free = shutil.disk_usage(self.files_dir).free
            if free < old_bytes + MIN_FREE_BYTES:
                raise ValueError(f'Need about {(old_bytes + MIN_FREE_BYTES) / 1024 ** 3:.0f} GB free to rearrange '
                                 f'storage (have {free / 1024 ** 3:.1f} GB).')
            log(f'Rearranging {len(order):,} pieces into playback order...')
            os.makedirs(packs.dir, exist_ok=True)
            batch, batch_bytes, loose_done = [], 0, []

            def flush():
                nonlocal batch_bytes
                if batch:
                    packs._write_one(batch)
                    packs.reload(force=True)
                    # sealed and indexed, the loose copies can go now
                    for d in loose_done:
                        _unlink_blob(os.path.join(self.files_dir, d))
                batch.clear()
                loose_done.clear()
                batch_bytes = 0

            for d in track(order, label='Rearranging storage'):
                if d in old_index:
                    base, off, size = old_index[d]
                    data = bytes(packs._map(base)[off:off + size])
                    if os.path.exists(os.path.join(self.files_dir, d)):
                        loose_done.append(d)  # stray loose copy of something already bundled
                else:
                    try:
                        with open(os.path.join(self.files_dir, d), 'rb') as f:
                            data = f.read()
                    except FileNotFoundError:
                        continue
                    loose_done.append(d)
                batch.append((d, data))
                batch_bytes += len(data)
                if batch_bytes >= PACK_TARGET:
                    flush()
            flush()

            # every piece in the old bundles has a new home now
            packs.close()
            for base in old_bundles:
                try:
                    os.unlink(os.path.join(packs.dir, base + '.idx'))
                    os.unlink(os.path.join(packs.dir, base + '.pack'))
                except PermissionError:
                    pass  # something still has it mapped. idx is gone, next tidy up removes it
                except FileNotFoundError:
                    pass
            packs.reload(force=True)
            packs._remove_orphans()
            log(f'Done. {len(order):,} pieces in {len(packs._current_stamp())} bundles.')

    def bundle(self, log=print):
        """bundle small files. windows hates hundreds of thousands of tiny files"""
        with self.lock:
            packs = self.packs
            segs = self._segment_hashes()
            todo, already = [], []
            with os.scandir(self.files_dir) as it:
                for e in it:
                    if not e.is_file() or e.name not in segs:
                        continue
                    if e.name in packs:
                        already.append(e.path)
                    elif e.stat().st_size <= BUNDLE_MAX:
                        todo.append((e.name, e.path))
            for path in already:
                _unlink_blob(path)
            if not todo:
                return 0
            log(f'Bundling {len(todo):,} small files...')
            written = set(packs.add_files(todo))
            for digest, path in track(todo, label='Removing bundled copies'):
                if digest in written:
                    _unlink_blob(path)
            log(f'  {len(written):,} files moved into {len(packs._current_stamp())} bundles.')
            return len(written)

    def patch_costs(self):
        """size per patch + how much deleting it actually frees"""
        tags = self.list()
        stamp = tuple((t, os.path.getmtime(self.repo_path('patches', t + '.json'))) for t in tags)
        cached = _cost_cache.get(self.root)
        if cached and cached[0] == stamp:
            return cached[1]

        sizes = {}
        packs = self.packs

        def size_of(d, known=None):
            if d not in sizes:
                sizes[d] = known if known is not None else self._blob_size(d, packs)
            return sizes[d]

        manifests = {}
        per_patch = {}
        for tag in tags:
            refs = set()
            for checksum, rel_path in (self.get_patch(tag) or {}).items():
                refs.add(checksum)
                size_of(checksum)
                if not _is_wad_path(rel_path):
                    continue
                if checksum not in manifests:
                    m = _load_manifest(self.files_dir, checksum)
                    if m is None:
                        manifests[checksum] = ()
                    elif 'segments' in m:
                        manifests[checksum] = tuple((d, s) for d, s in m['segments'] if d)
                    else:
                        manifests[checksum] = tuple((e['data_hash'], None) for e in m['entries'])
                for d, s in manifests[checksum]:
                    refs.add(d)
                    size_of(d, s)
            per_patch[tag] = refs

        owners = {}
        for tag, refs in per_patch.items():
            for d in refs:
                owners[d] = owners.get(d, 0) + 1
        result = {tag: {'total': sum(sizes[d] for d in refs),
                        'unique': sum(sizes[d] for d in refs if owners[d] == 1)}
                  for tag, refs in per_patch.items()}
        _cost_cache[self.root] = (stamp, result)
        return result

    def _whole_wads(self):
        whole = {}
        for tag in self.list():
            for checksum, rel_path in (self.get_patch(tag) or {}).items():
                if not _is_wad_path(rel_path) or checksum in whole:
                    continue
                path = os.path.join(self.files_dir, checksum)
                try:
                    with open(path, 'rb') as f:
                        if f.read(2) == b'RW':
                            whole[checksum] = rel_path
                except FileNotFoundError:
                    pass
        return whole

    def estimate_optimized(self):
        """estimate the size after optimize from the wad tables only"""
        from league_vcs.parsers.wad import estimate_toc

        whole = self._whole_wads()
        if not whole:
            return None
        current = self.total_size()
        saved_from = 0
        seen = set()
        after = 0

        def _one(checksum):
            path = os.path.join(self.files_dir, checksum)
            try:
                toc, keys = estimate_toc(path)
            except (ValueError, OSError):
                return checksum, os.path.getsize(path), None
            return checksum, toc, keys

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            for checksum, toc, keys in executor.map(_one, whole):
                saved_from += os.path.getsize(os.path.join(self.files_dir, checksum))
                after += toc
                if keys:
                    new = keys - seen
                    after += sum(size for _, size in new)
                    seen |= new
        return {'current': current, 'after': current - saved_from + after, 'wads': len(whole)}

    def optimize(self, log=print):
        """old whole-file wads -> shared pieces.

        every wad gets checked against its original sha256 before anything is deleted.
        no match, leave it alone. kill this check and people lose patches"""
        self.ensure_repo()
        from league_vcs import winproc
        if winproc.game_running():
            raise ValueError('League is running. Close the game (and any replay) before optimizing.')
        with self.lock:
            whole = self._whole_wads()
            if not whole:
                log('Everything is already in the optimized format.')
                return

            free = shutil.disk_usage(self.files_dir).free
            if free < MIN_FREE_BYTES:
                raise ValueError(f'Need at least {MIN_FREE_BYTES // 1024 ** 3} GB free to optimize '
                                 f'(have {free / 1024 ** 3:.1f} GB).')

            if os.path.isdir(self.current_path()) or self.kept():
                log('Clearing prepared patches (they get rebuilt when you next watch)...')
                self._clean()
                self._drop_kept()

            tags = self.list()
            patches = {t: self.get_patch(t) for t in tags}
            known = self._known_blobs()
            sizes = {c: os.path.getsize(os.path.join(self.files_dir, c)) for c in whole}
            order = sorted(whole, key=sizes.get, reverse=True)
            total = sum(sizes.values())
            log(f'Converting {len(order)} WAD archives ({total / 1024 ** 3:.1f} GB) '
                f'across {len(tags)} patches using {WORKERS} threads.')

            pending, pending_bytes, done_bytes, kept = {}, 0, 0, 0
            writer = PackWriter(self.packs)

            def commit():
                nonlocal pending, pending_bytes
                if not pending:
                    return
                writer.seal()
                for tag, patch in patches.items():
                    dups = self.get_dups(tag)
                    if any(c in pending for c in dups):
                        self._save_dups(tag, {pending.get(c, c): ps for c, ps in dups.items()})
                    if any(c in pending for c in patch):
                        patches[tag] = {pending.get(c, c): rp for c, rp in patch.items()}
                        self._save_patch(tag, patches[tag])
                # an empty wad's only piece is the whole file, so the old copy is also a piece.
                # only delete what nothing points at any more
                in_use = set()
                for patch in patches.values():
                    in_use |= _collect_all_hashes(patch, self.files_dir)
                for old in pending:
                    if old not in in_use:
                        _unlink_blob(os.path.join(self.files_dir, old))
                pending, pending_bytes = {}, 0

            def convert(checksum):
                from league_vcs.parsers.wad import unpack_wad_exact
                manifest = unpack_wad_exact(os.path.join(self.files_dir, checksum),
                                            self.files_dir, known, expected_sha=checksum, writer=writer)
                return _store_manifest(self.files_dir, manifest) if manifest else None

            with ThreadPoolExecutor(max_workers=WORKERS) as executor:
                futures = {executor.submit(convert, c): c for c in order}
                for i, future in enumerate(as_completed(futures), 1):
                    checksum = futures[future]
                    try:
                        manifest_hash = future.result()
                    except Exception as e:
                        manifest_hash = None
                        log(f'  kept {whole[checksum]} unchanged ({e})')
                    if manifest_hash:
                        pending[checksum] = manifest_hash
                        pending_bytes += sizes[checksum]
                    else:
                        kept += 1
                    done_bytes += sizes[checksum]
                    # by bytes, not count. the big archives take forever and the bar should show it
                    step(done_bytes, total, 'Converting archives')
                    if i % 25 == 0 or i == len(order):
                        log(f'  {i}/{len(order)} archives, {100 * done_bytes / total:.0f}%')
                    if pending_bytes >= COMMIT_BYTES:
                        commit()
            commit()
            writer.seal()

            removed = self.gc()
            self.bundle(log)
            log(f'Done. {len(order) - kept} archives converted'
                + (f', {kept} left as-is' if kept else '')
                + (f', {removed} unused files removed' if removed else '') + '.')

    def gc(self):
        """delete anything no patch points at (plus junk from crashed writes)"""
        with self.lock:
            referenced = set()
            for tag in track(self.list(), label='Checking which files are still used'):
                referenced |= _collect_all_hashes(self.get_patch(tag) or {}, self.files_dir)
            with os.scandir(self.files_dir) as it:
                unused = [e.path for e in it if e.is_file() and e.name not in referenced]
            for path in track(unused, label='Deleting unused files'):
                _unlink_blob(path)
            self.packs.compact(referenced)
            return len(unused)

    def wipe(self):
        """every stored patch. just lvcs\\ and current\\, not the whole folder"""
        with self.lock:
            self.packs.close()
            _pack_stores.pop(self.files_dir, None)
            _delete_tree([self.repo_path(), self.current_path(), self.kept_path()], 'Deleting stored patches')
            try:
                os.rmdir(self.root)  # only goes if it's empty
            except OSError:
                pass
