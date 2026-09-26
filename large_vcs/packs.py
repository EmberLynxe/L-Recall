"""bundles of small files.

pack-NNNNNN.pack = blobs back to back, pack-NNNNNN.idx = 44 byte records (sha256, offset, size).
idx gets written last. no idx means the bundle doesn't exist, which is the whole crash safety plan
"""
import hashlib
import mmap
import os
import struct
import threading

from .progress import track

RECORD = struct.Struct('<32sQI')
PACK_TARGET = 256 * 1024 ** 2


def _positional_reader():
    """ReadFile with the offset handed in. one handle per bundle is then fine from every thread at once,
    no shared file position. plain reads like this beat mmap by about a fifth on a cold build, windows
    reads ahead properly instead of faulting pages in one at a time"""
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [('Internal', ctypes.c_void_p), ('InternalHigh', ctypes.c_void_p),
                    ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD), ('hEvent', wintypes.HANDLE)]

    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.ReadFile.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED))
    k32.ReadFile.restype = wintypes.BOOL

    def read(f, offset, size):
        buf = bytearray(size)
        got = wintypes.DWORD()
        ov = OVERLAPPED(Offset=offset & 0xFFFFFFFF, OffsetHigh=offset >> 32)
        ptr = ctypes.addressof(ctypes.c_char.from_buffer(buf)) if size else None  # one type, not one per size
        if size and not k32.ReadFile(f.handle, ptr, size, ctypes.byref(got), ctypes.byref(ov)):
            raise ctypes.WinError(ctypes.get_last_error())
        if got.value != size:
            raise ValueError('Bundle is shorter than its index says')
        return buf
    return read


_read_at = _positional_reader()


class _Stream:
    __slots__ = ('file', 'base', 'records', 'offset')

    def __init__(self, file, base):
        self.file, self.base, self.records, self.offset = file, base, bytearray(), 0


class PackWriter:
    """writes small blobs straight into a bundle.

    one bundle per storing thread, so each archive's pieces end up together and in order instead of
    shuffled in with whatever the other threads were doing. reading them back is then mostly straight
    runs, which is most of what rearrange storage buys you.

    nothing is readable until seal(). seal before saving anything that points at them,
    otherwise patches end up pointing at nothing"""

    # pieces up to this go in bundles, bigger ones stay separate files. at 64 KB a patch was ~60,000 files,
    # at 4 MB it's ~900 and stores about a third faster. those get read into memory whole anyway
    max_size = 4 * 1024 * 1024

    def __init__(self, store):
        self.store = store
        self._lock = threading.Lock()
        self._streams = {}  # thread id -> _Stream

    def add(self, digest, data):
        tid = threading.get_ident()
        st = self._streams.get(tid)
        if st is None:
            with self._lock:
                os.makedirs(self.store.dir, exist_ok=True)
                # the .tmp exists before the lock goes, so the next thread's _next_base skips this number
                base = self.store._next_base()
                f = open(os.path.join(self.store.dir, base + '.pack.tmp'), 'wb', buffering=8 * 1024 * 1024)
                st = self._streams[tid] = _Stream(f, base)
        st.file.write(data)
        st.records += RECORD.pack(bytes.fromhex(digest), st.offset, len(data))
        st.offset += len(data)
        if st.offset >= PACK_TARGET:
            with self._lock:
                self._streams.pop(tid, None)
            self._seal_stream(st)

    def _seal_stream(self, st):
        st.file.flush()
        os.fsync(st.file.fileno())
        st.file.close()
        path = os.path.join(self.store.dir, st.base)
        os.replace(path + '.pack.tmp', path + '.pack')
        with open(path + '.idx.tmp', 'wb') as out:
            out.write(st.records)
            out.flush()
            os.fsync(out.fileno())
        os.replace(path + '.idx.tmp', path + '.idx')

    def seal(self):
        with self._lock:
            streams, self._streams = list(self._streams.values()), {}
        for st in streams:
            self._seal_stream(st)
        self.store.reload(force=True)


class PackStore:
    def __init__(self, files_dir):
        self.dir = os.path.join(files_dir, 'packs')
        self.index = {}
        self._maps = {}
        self._files = {}
        self._lock = threading.Lock()
        self._stamp = None
        self.reload()

    def _current_stamp(self):
        try:
            with os.scandir(self.dir) as it:
                return tuple(sorted((e.name, e.stat().st_mtime_ns) for e in it if e.name.endswith('.idx')))
        except FileNotFoundError:
            return ()

    def reload(self, force=False):
        stamp = self._current_stamp()
        if not force and stamp == self._stamp:
            return
        # don't close maps here. bundles are append-only, only compact() deletes
        index = {}
        for name, _ in stamp:
            base = name[:-4]
            if not os.path.exists(os.path.join(self.dir, base + '.pack')):
                continue
            with open(os.path.join(self.dir, name), 'rb') as f:
                data = f.read()
            for pos in range(0, len(data) - len(data) % RECORD.size, RECORD.size):
                digest, off, size = RECORD.unpack_from(data, pos)
                index[digest.hex()] = (base, off, size)
        self.index = index
        self._stamp = stamp

    def __contains__(self, digest):
        return digest in self.index

    def size(self, digest):
        return self.index[digest][2]

    def _map(self, base):
        with self._lock:
            entry = self._maps.get(base)
            if entry is None:
                f = open(os.path.join(self.dir, base + '.pack'), 'rb')
                entry = self._maps[base] = (f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ))
            return entry[1]

    def read(self, digest):
        base, off, size = self.index[digest]
        if _read_at is None:
            return self._map(base)[off:off + size]
        return _read_at(self._file(base), off, size)

    def _file(self, base):
        with self._lock:
            f = self._files.get(base)
            if f is None:
                f = self._files[base] = open(os.path.join(self.dir, base + '.pack'), 'rb', buffering=0)
                if _read_at is not None:
                    import msvcrt
                    f.handle = msvcrt.get_osfhandle(f.fileno())
            return f

    def close(self, base=None):
        with self._lock:
            for key in ([base] if base else list(self._maps)):
                entry = self._maps.pop(key, None)
                if entry:
                    entry[1].close()
                    entry[0].close()
            for key in ([base] if base else list(self._files)):
                f = self._files.pop(key, None)
                if f:
                    f.close()

    def total_bytes(self):
        total = 0
        try:
            with os.scandir(self.dir) as it:
                for e in it:
                    if e.is_file():
                        total += e.stat().st_size
        except FileNotFoundError:
            pass
        return total

    def _next_base(self):
        taken = [int(n[5:11]) for n in os.listdir(self.dir) if n.startswith('pack-') and n[5:11].isdigit()]
        return f'pack-{max(taken, default=0) + 1:06d}'

    def _write_one(self, blobs):
        """(digest, bytes) in, digests out"""
        base = self._next_base()
        pack_path = os.path.join(self.dir, base + '.pack')
        idx_path = os.path.join(self.dir, base + '.idx')
        records = bytearray()
        written = []
        with open(pack_path + '.tmp', 'wb') as out:
            offset = 0
            for digest, data in blobs:
                out.write(data)
                records += RECORD.pack(bytes.fromhex(digest), offset, len(data))
                offset += len(data)
                written.append(digest)
            out.flush()
            os.fsync(out.fileno())
        os.replace(pack_path + '.tmp', pack_path)
        with open(idx_path + '.tmp', 'wb') as out:
            out.write(records)
            out.flush()
            os.fsync(out.fileno())
        os.replace(idx_path + '.tmp', idx_path)
        return written

    def add_files(self, items):
        """(digest, path) in. checks each file against its name before packing it"""
        os.makedirs(self.dir, exist_ok=True)
        written, batch, batch_bytes = [], [], 0
        for digest, path in track(items, label='Bundling small files'):
            try:
                with open(path, 'rb') as f:
                    data = f.read()
            except OSError:
                continue
            if hashlib.sha256(data).hexdigest() != digest:
                continue
            batch.append((digest, data))
            batch_bytes += len(data)
            if batch_bytes >= PACK_TARGET:
                written += self._write_one(batch)
                batch, batch_bytes = [], 0
        if batch:
            written += self._write_one(batch)
        self.reload(force=True)
        return written

    def compact(self, referenced, min_dead=0.3):
        """throw out blobs nobody uses. returns bytes freed"""
        # every bundle on disk, not just the ones the index points at. a stopped repack leaves old
        # bundles whose pieces all live somewhere newer, and those never show up in the index
        by_pack = {name[:-4]: [] for name, _ in self._current_stamp()}
        for digest, (base, _, size) in self.index.items():
            by_pack.setdefault(base, []).append((digest, size))
        freed = 0
        for base, entries in track(list(by_pack.items()), label='Tidying bundles'):
            live = [d for d, _ in entries if d in referenced]
            try:
                total = os.path.getsize(os.path.join(self.dir, base + '.pack')) or 1
            except OSError:
                continue
            dead_bytes = total - sum(s for d, s in entries if d in referenced)
            if dead_bytes <= 0 or (live and dead_bytes / total < min_dead):
                continue
            self.close(base)
            pack = os.path.join(self.dir, base + '.pack')
            try:
                # can't delete a file another process has mapped. renaming it and back is the cheapest check
                os.rename(pack, pack + '.probe')
                os.rename(pack + '.probe', pack)
            except PermissionError:
                continue
            if live:
                self._write_one((d, bytes(self.read(d))) for d in live)
                self.close(base)
            os.unlink(os.path.join(self.dir, base + '.idx'))
            try:
                os.unlink(os.path.join(self.dir, base + '.pack'))
            except PermissionError:
                pass  # idx is gone so nothing can reach this. next pass cleans it up
            freed += dead_bytes
            self.reload(force=True)
        self._remove_orphans()
        return freed

    def _remove_orphans(self):
        try:
            names = os.listdir(self.dir)
        except FileNotFoundError:
            return
        for name in names:
            if name.endswith('.pack') and name[:-5] + '.idx' not in names:
                try:
                    os.unlink(os.path.join(self.dir, name))
                except OSError:
                    pass
