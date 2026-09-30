"""storage. every rebuild has to match byte for byte"""
import json
import os
import shutil
import stat
import struct
import tempfile
import threading
import unittest
from unittest import mock

import large_vcs
from large_vcs import LargeVCS, _store_manifest, hash_file
from league_vcs.parsers.wad import parse_wad, unpack_wad

from fixtures import make_wad, random_assets


def wipe(path):
    for root, _, files in os.walk(path):
        for f in files:
            os.chmod(os.path.join(root, f), stat.S_IWRITE)
    shutil.rmtree(path, ignore_errors=True)


class RepoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='lrecall-test-')
        self.repo = LargeVCS.load_or_create(os.path.join(self.tmp, 'repo'))
        self._min_free = large_vcs.MIN_FREE_BYTES
        large_vcs.MIN_FREE_BYTES = 0

    def tearDown(self):
        large_vcs.MIN_FREE_BYTES = self._min_free
        self.repo.packs.close()
        large_vcs._pack_stores.clear()
        wipe(self.tmp)

    def install(self, name, files):
        """{path: bytes | ('wad', assets, seed)}"""
        root = os.path.join(self.tmp, name)
        for rel, content in files.items():
            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if isinstance(content, tuple):
                make_wad(path, content[1], seed=content[2])
            else:
                with open(path, 'wb') as f:
                    f.write(content)
        return root

    def assertStagedEqual(self, install_root):
        want = {}
        for root, _, files in os.walk(install_root):
            for f in files:
                full = os.path.join(root, f)
                want[os.path.relpath(full, install_root)] = hash_file(full)
        got = {}
        for root, _, files in os.walk(self.repo.current_path()):
            for f in files:
                full = os.path.join(root, f)
                got[os.path.relpath(full, self.repo.current_path())] = hash_file(full)
        self.assertEqual(want, got)


class StorageTest(RepoTest):

    def test_store_and_restore_is_byte_identical(self):
        shared = random_assets(30, seed=1)
        inst = self.install('p1', {
            'League of Legends.exe': b'exe v1',
            r'DATA\FINAL\Champions\Ahri.wad.client': ('wad', shared[:20], 1),
            r'DATA\FINAL\Maps\Map11.wad.client': ('wad', shared[10:], 2),
        })
        self.repo.add(inst, 'P1')
        self.repo.restore('P1')
        self.assertStagedEqual(inst)
        report = self.repo.storage_report()[0]
        self.assertEqual((report['exact'], report['whole']), (2, 0))

    def test_page_answers_are_cached_until_a_patch_changes(self):
        p1 = self.install('p1', {'a.dll': b'1', r'DATA\x.wad.client': ('wad', random_assets(5, seed=13), 13)})
        self.repo.add(p1, 'P1')
        first = self.repo.patch_costs()
        large_vcs._cost_cache.clear()  # like starting the app again
        with mock.patch.object(self.repo, '_patch_costs', side_effect=AssertionError('recomputed')):
            self.assertEqual(self.repo.patch_costs(), first)
        p2 = self.install('p2', {'a.dll': b'2'})
        self.repo.add(p2, 'P2')
        self.assertEqual(set(self.repo.patch_costs()), {'P1', 'P2'})
        self.assertEqual([r['tag'] for r in self.repo.storage_report()], ['P1', 'P2'])

    def test_people_only_see_the_patch_not_the_build(self):
        self.assertEqual(large_vcs.show('16.19.821.7343'), '16.19')
        self.assertEqual(large_vcs.show('P1'), 'P1')
        shared = {'a.dll': b'same', r'DATA\x.wad.client': ('wad', random_assets(5, seed=14), 14)}
        self.repo.add(self.install('b1', {**shared, 'game.exe': b'build 1'}), '16.19.820.1')
        self.repo.add(self.install('b2', {**shared, 'game.exe': b'build 2'}), '16.19.821.1')
        costs = self.repo.patch_costs()
        # each build alone frees next to nothing, the patch as a whole frees everything
        self.assertLess(costs['16.19.820.1']['unique'], costs['16.19']['unique'])
        self.assertEqual(costs['16.19']['unique'], costs['16.19']['total'])

    def test_building_reports_bytes_as_it_goes(self):
        inst = self.install('p1', {r'DATA\big.wad.client': ('wad', random_assets(60, seed=7, size=(40, 400000)), 7)})
        self.repo.add(inst, 'P1')
        checksum = next(iter(self.repo.get_patch('P1')))
        calls = []
        out = os.path.join(self.tmp, 'out.wad.client')
        self.repo._build_wad(checksum, out, calls.append)
        self.assertGreater(len(calls), 1)
        self.assertEqual(sum(calls), os.path.getsize(out))

    def test_switching_patches_handles_changed_and_moved_files(self):
        assets = random_assets(25, seed=2)
        p1 = self.install('p1', {'a\\foo.dll': b'same bytes', r'DATA\x.wad.client': ('wad', assets, 3)})
        changed = assets[:24] + [b'patched asset' * 50]
        p2 = self.install('p2', {'b\\foo.dll': b'same bytes', r'DATA\x.wad.client': ('wad', changed, 3)})
        self.repo.add(p1, 'P1')
        self.repo.add(p2, 'P2')
        for tag, inst in (('P1', p1), ('P2', p2), ('P1', p1)):
            self.repo.restore(tag)
            self.assertStagedEqual(inst)

    def test_identical_files_at_several_paths_are_all_restored(self):
        stub = random_assets(1, seed=4)
        inst = self.install('p1', {
            r'DATA\FINAL\Audio.wad.client': ('wad', stub, 9),
            r'DATA\FINAL\Online.wad.client': ('wad', stub, 9),
            'one.dll': b'dup', 'two.dll': b'dup',
        })
        self.repo.add(inst, 'P1')
        self.assertEqual(len(self.repo.pairs('P1')), 4)
        self.repo.restore('P1')
        self.assertStagedEqual(inst)

    def test_optimize_converts_old_format_without_changing_bytes(self):
        inst = self.install('p1', {
            'game.dll': b'regular file',
            r'DATA\a.wad.client': ('wad', random_assets(40, seed=5), 5),
            r'DATA\b.wad.client': ('wad', random_assets(40, seed=6), 6),
        })
        patch = {}
        for rel in ('game.dll', r'DATA\a.wad.client', r'DATA\b.wad.client'):
            src = os.path.join(inst, rel)
            digest = hash_file(src)
            shutil.copyfile(src, os.path.join(self.repo.files_dir, digest))
            patch[digest] = rel
        with open(self.repo.repo_path('patches', 'OLD.json'), 'w') as f:
            json.dump(patch, f)
        whole = [c for c, r in patch.items() if r.endswith('.wad.client')]

        # optimize won't run with league open, and league being open on this pc shouldn't fail the test
        with mock.patch('league_vcs.winproc.game_running', return_value=False):
            self.repo.optimize(log=lambda *_: None)

        report = self.repo.storage_report()[0]
        self.assertEqual((report['exact'], report['whole']), (2, 0))
        for digest in whole:
            self.assertFalse(os.path.exists(os.path.join(self.repo.files_dir, digest)))
        self.repo.restore('OLD')
        self.assertStagedEqual(inst)

    def test_optimize_keeps_empty_placeholder_wads(self):
        # an empty wad is one piece that's the whole file, so the piece and the old copy are the same file
        empty = b'RW' + bytes([3, 4]) + bytes(256) + struct.pack('<QI', 0, 0)
        inst = self.install('p1', {
            r'DATA\Audio.wad.client': empty, r'DATA\Online.wad.client': empty,
            r'DATA\real.wad.client': ('wad', random_assets(10, seed=12), 12),
        })
        patch, dups = {}, {}
        for rel in (r'DATA\Audio.wad.client', r'DATA\Online.wad.client', r'DATA\real.wad.client'):
            src = os.path.join(inst, rel)
            digest = hash_file(src)
            dst = os.path.join(self.repo.files_dir, digest)
            if not os.path.exists(dst):
                shutil.copyfile(src, dst)
            LargeVCS._record(patch, dups, digest, rel)
        with open(self.repo.repo_path('patches', 'OLD.json'), 'w') as f:
            json.dump(patch, f)
        with open(self.repo.repo_path('patches', 'OLD.json.dups'), 'w') as f:
            json.dump(dups, f)
        with mock.patch('league_vcs.winproc.game_running', return_value=False):
            self.repo.optimize(log=lambda *_: None)
        self.repo.restore('OLD')
        self.assertStagedEqual(inst)

    def test_dropping_a_patch_keeps_the_other_intact(self):
        shared = random_assets(30, seed=7)
        p1 = self.install('p1', {r'DATA\x.wad.client': ('wad', shared, 7), 'a.dll': b'one'})
        p2 = self.install('p2', {r'DATA\x.wad.client': ('wad', shared[:29] + [b'new' * 100], 7), 'a.dll': b'two'})
        self.repo.add(p1, 'P1')
        self.repo.add(p2, 'P2')
        costs = self.repo.patch_costs()
        self.assertLess(costs['P1']['unique'], costs['P1']['total'])
        self.repo.drop('P1')
        self.repo.restore('P2')
        self.assertStagedEqual(p2)

    def test_top_up_adds_new_files_once(self):
        inst = self.install('p1', {'a.dll': b'a'})
        self.repo.add(inst, 'P1')
        self.repo.restore('P1')
        self.install('p1', {r'DATA\late.wad.client': ('wad', random_assets(5, seed=8), 8), 'copy.dll': b'a'})
        self.assertEqual(self.repo.top_up(inst, 'P1'), 2)
        self.assertEqual(self.repo.top_up(inst, 'P1'), 0)
        self.assertStagedEqual(inst)

    def test_bundling_leaves_legacy_manifests_readable(self):
        assets = random_assets(20, seed=10, size=(40, 400))
        legacy_inst = self.install('legacy', {r'DATA\l.wad.client': ('wad', assets, 10)})
        manifest, _ = unpack_wad(os.path.join(legacy_inst, r'DATA\l.wad.client'), self.repo.files_dir)
        with open(self.repo.repo_path('patches', 'LEGACY.json'), 'w') as f:
            json.dump({_store_manifest(self.repo.files_dir, manifest): r'DATA\l.wad.client'}, f)

        exact = self.install('p2', {r'DATA\l.wad.client': ('wad', assets, 10)})
        self.repo.add(exact, 'P2')
        self.repo.bundle(log=lambda *_: None)

        self.repo.restore('LEGACY')
        _, _, _, entries = parse_wad(self.repo.current_path(r'DATA\l.wad.client'))
        self.assertEqual(len(entries), len(assets) + 1)


class PlaceholderTest(RepoTest):
    """patches from the original league vcs kept one name for riot's empty archive and lost the rest.
    the game won't start without Audio.wad.client"""
    AUDIO, TFT = r'DATA\FINAL\Audio.wad.client', r'DATA\FINAL\Champions\TFTChampion.en_US.wad.client'
    ONLINE, OLD_NAME = r'DATA\FINAL\Online.wad.client', r'DATA\FINAL\TFTSet10.wad.client'

    def make_old(self, tag):
        """no .dups file at all, like the ones the original tool stored"""
        try:
            os.unlink(self.repo._dups_path(tag))
        except FileNotFoundError:
            pass

    def test_old_patch_gets_its_missing_names_back(self):
        new = self.install('new', {'a.dll': b'new', self.AUDIO: large_vcs.STUB_WAD, self.TFT: large_vcs.STUB_WAD})
        old = self.install('old', {'a.dll': b'old', self.AUDIO: large_vcs.STUB_WAD, self.TFT: large_vcs.STUB_WAD})
        self.repo.add(new, 'P2')
        self.repo.add(old, 'P1')
        # one name kept (tft, like 16.18 on disk), the rest gone
        patch = self.repo.get_patch('P1')
        key = next(c for c, r in patch.items() if r in (self.AUDIO, self.TFT))
        patch[key] = self.TFT
        self.repo._save_patch('P1', patch)
        self.make_old('P1')
        self.assertNotIn(self.AUDIO, {r for _, r in self.repo.pairs('P1')})
        self.repo.restore('P1')
        for rel in ('a.dll', self.AUDIO, self.TFT):
            self.assertEqual(hash_file(self.repo.current_path(rel)), hash_file(os.path.join(old, rel)))
        # fixed for good, nothing more to add next time
        self.assertEqual(self.repo.fill_placeholders(), {})

    def test_properly_recorded_patch_is_left_alone(self):
        # riot really did ship it under one name here. current code marks that, so nothing gets added
        p = self.install('p1', {self.TFT: large_vcs.STUB_WAD, 'a.dll': b'x'})
        self.repo.add(p, 'P1')
        self.assertEqual(self.repo.fill_placeholders(), {})
        self.repo.restore('P1')
        self.assertStagedEqual(p)

    def test_real_files_are_never_swapped_for_the_empty_one(self):
        real = self.install('p1', {self.TFT: large_vcs.STUB_WAD, self.ONLINE: ('wad', random_assets(5, seed=9), 9)})
        self.repo.add(real, 'P1')
        self.make_old('P1')
        self.repo.restore('P1')
        self.assertTrue(os.path.exists(self.repo.current_path(self.AUDIO)))
        self.assertEqual(hash_file(self.repo.current_path(self.ONLINE)), hash_file(os.path.join(real, self.ONLINE)))

    def test_found_by_what_it_is_not_its_name(self):
        # whole file format, under a name that isn't one of the 16.19 ones
        with open(os.path.join(self.repo.files_dir, large_vcs.STUB_SHA), 'wb') as f:
            f.write(large_vcs.STUB_WAD)
        self.repo._save_patch('P1', {large_vcs.STUB_SHA: self.OLD_NAME})
        self.repo.restore('P1')
        for rel in (self.OLD_NAME, self.AUDIO):
            with open(self.repo.current_path(rel), 'rb') as f:
                self.assertEqual(f.read(), large_vcs.STUB_WAD)

    def test_old_manifest_format_too(self):
        path = os.path.join(self.tmp, 'empty.wad.client')
        with open(path, 'wb') as f:
            f.write(large_vcs.STUB_WAD)
        manifest, _ = unpack_wad(path, self.repo.files_dir)
        self.repo._save_patch('P1', {_store_manifest(self.repo.files_dir, manifest): self.OLD_NAME})
        self.repo.restore('P1')
        self.assertEqual(parse_wad(self.repo.current_path(self.AUDIO))[3], [])

    def test_patch_without_the_empty_archive_is_left_alone(self):
        p = self.install('p1', {'a.dll': b'x'})
        self.repo.add(p, 'P1')
        self.make_old('P1')
        self.assertEqual(self.repo.fill_placeholders(), {})


class CrashSafetyTest(RepoTest):
    def test_switch_that_dies_halfway_gets_rebuilt_properly(self):
        # shared.dll changes P1 -> P2 and changes back in P3. a switch that died after deleting it but
        # still said P1 meant P3 trusted it was there
        self.repo.keep_prepared = 1
        p1 = self.install('p1', {'shared.dll': b'A', 'only1.dll': b'1', 'zz.dll': b'z'})
        p2 = self.install('p2', {'shared.dll': b'B'})
        p3 = self.install('p3', {'shared.dll': b'A', 'only3.dll': b'3'})
        for tag, inst in (('P1', p1), ('P2', p2), ('P3', p3)):
            self.repo.add(inst, tag)
        self.repo.restore('P1')
        real = large_vcs._unlink_blob

        def fails_on_zz(path):
            if path.endswith('zz.dll'):
                raise PermissionError(5, 'Access is denied')
            return real(path)
        with mock.patch('large_vcs._unlink_blob', side_effect=fails_on_zz), self.assertRaisesRegex(ValueError, 'still closing'):
            self.repo.restore('P2')
        self.assertIsNone(self.repo.current())
        self.repo.restore('P3')
        self.assertStagedEqual(p3)

    def test_retrying_while_the_game_holds_a_file_never_mixes_patches(self):
        self.repo.keep_prepared = 1
        p1 = self.install('p1', {'a.dll': b'1', 'z.dll': b'z'})
        p3 = self.install('p3', {'a.dll': b'3'})
        self.repo.add(p1, 'P1')
        self.repo.add(p3, 'P3')
        self.repo.restore('P1')
        held = open(self.repo.current_path('z.dll'), 'rb')  # the game, not quite closed yet
        try:
            with mock.patch('large_vcs.time.sleep'):
                for _ in range(2):
                    with self.assertRaisesRegex(ValueError, 'still closing'):
                        self.repo.restore('P3')
            self.assertNotEqual(self.repo.current(), 'P3')
        finally:
            held.close()
        self.repo.restore('P3')
        self.assertStagedEqual(p3)
        self.assertFalse(os.path.exists(self.repo.path('trash')))

    def test_build_that_dies_leaves_no_cut_off_archive(self):
        inst = self.install('p1', {r'DATA\x.wad.client': ('wad', random_assets(10, seed=12), 12)})
        self.repo.add(inst, 'P1')

        def half(manifest, files_dir, out, *_):
            with open(out, 'wb') as f:
                f.write(b'half an archive')
            raise OSError('disk full')
        with mock.patch('league_vcs.parsers.wad.pack_wad_exact', side_effect=half), self.assertRaises(OSError):
            self.repo.restore('P1')
        self.assertFalse(os.path.exists(self.repo.current_path(r'DATA\x.wad.client')))
        self.assertFalse(os.path.exists(self.repo.current_path(r'DATA\x.wad.client.part')))
        self.repo.restore('P1')
        self.assertStagedEqual(inst)

    def test_waiting_on_storage_can_be_cancelled(self):
        from large_vcs.progress import Cancelled, reporting
        held, done = threading.Event(), threading.Event()

        def hold():
            with self.repo.lock:
                held.set()
                done.wait(10)
        t = threading.Thread(target=hold)
        t.start()
        held.wait(5)
        stop = threading.Event()
        stop.set()
        try:
            with reporting(lambda *_: None, stop), self.assertRaises(Cancelled):
                with self.repo.lock:
                    pass
        finally:
            done.set()
            t.join()


class BundleWriterTest(RepoTest):
    def test_sealing_while_threads_are_still_adding_loses_nothing(self):
        # optimize seals mid run. every piece add() said was stored has to be readable afterwards
        import hashlib
        import random
        from large_vcs.packs import PackWriter
        writer = PackWriter(self.repo.packs)
        stored, lock = {}, threading.Lock()

        def worker(seed):
            rnd = random.Random(seed)
            for _ in range(300):
                data = rnd.randbytes(rnd.randint(1, 3000))
                digest = hashlib.sha256(data).hexdigest()
                writer.add(digest, data)
                with lock:
                    stored[digest] = data

        with mock.patch('large_vcs.packs.PACK_TARGET', 64 * 1024):  # lots of bundles filling up on their own too
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
            for t in threads:
                t.start()
            while any(t.is_alive() for t in threads):
                writer.seal()
            for t in threads:
                t.join()
            writer.seal()
        packs = self.repo.packs
        missing = [d for d in stored if d not in packs]
        self.assertEqual(missing, [])
        wrong = [d for d, data in stored.items() if bytes(packs.read(d)) != data]
        self.assertEqual(wrong, [])

    def test_seal_waits_for_a_bundle_thats_sealing_itself(self):
        # a thread fills its bundle and starts sealing it. a seal() at that moment has to wait for it,
        # otherwise optimize saves patches pointing at a bundle that isn't indexed yet
        import hashlib
        from large_vcs.packs import PackWriter
        writer = PackWriter(self.repo.packs)
        in_fsync, let_go, seen = threading.Event(), threading.Event(), {}
        real_fsync = os.fsync
        a, b = b'a' * 600, b'b' * 600

        def slow_fsync(fd):
            if threading.current_thread().name == 'filler' and not in_fsync.is_set():
                in_fsync.set()
                let_go.wait(5)
            return real_fsync(fd)

        def filler():
            writer.add(hashlib.sha256(a).hexdigest(), a)
            writer.add(hashlib.sha256(b).hexdigest(), b)  # crosses the limit, seals itself

        def sealer():
            writer.seal()
            seen['a'] = hashlib.sha256(a).hexdigest() in self.repo.packs

        with mock.patch('large_vcs.packs.PACK_TARGET', 1000), mock.patch('large_vcs.packs.os.fsync', slow_fsync):
            t = threading.Thread(target=filler, name='filler')
            t.start()
            self.assertTrue(in_fsync.wait(5))
            s = threading.Thread(target=sealer)
            s.start()
            s.join(0.3)
            let_go.set()
            s.join(5)
            t.join(5)
        self.assertTrue(seen.get('a'))

    def test_a_bundle_that_cant_be_saved_stops_the_commit(self):
        import hashlib
        from large_vcs.packs import PackWriter
        writer = PackWriter(self.repo.packs)
        data = b'z' * 100
        writer.add(hashlib.sha256(data).hexdigest(), data)
        with mock.patch('large_vcs.packs.os.fsync', side_effect=OSError(28, 'No space left on device')):
            with self.assertRaises(OSError):
                writer.seal()

    def test_a_failed_write_doesnt_shift_the_pieces_after_it(self):
        import hashlib
        from large_vcs.packs import PackWriter
        writer = PackWriter(self.repo.packs)
        good = [bytes([i]) * (100 + i) for i in range(5)]
        for data in good[:2]:
            writer.add(hashlib.sha256(data).hexdigest(), data)
        stream = writer._streams[threading.get_ident()]
        real_write = stream.file.write

        def half_then_fail(data):
            real_write(data[:len(data) // 2])
            raise OSError(28, 'No space left on device')
        stream.file.write = half_then_fail
        bad = b'x' * 500
        with self.assertRaises(OSError):
            writer.add(hashlib.sha256(bad).hexdigest(), bad)
        for data in good[2:]:
            writer.add(hashlib.sha256(data).hexdigest(), data)
        writer.seal()
        for data in good:
            self.assertEqual(bytes(self.repo.packs.read(hashlib.sha256(data).hexdigest())), data)
        self.assertNotIn(hashlib.sha256(bad).hexdigest(), self.repo.packs)


class DamageTest(RepoTest):
    """storage that got damaged somehow (bad disk, antivirus, whatever) has to be caught, not launched"""

    def setUp(self):
        super().setUp()
        self.inst = self.install('p1', {'a.dll': b'fine', r'DATA\x.wad.client': ('wad', random_assets(8, seed=41), 41)})
        self.repo.add(self.inst, 'P1')

    def damage_a_piece(self):
        packs = self.repo.packs
        digest = next(iter(packs.index))
        base, off, size = packs.index[digest]
        self.repo.release()
        large_vcs._pack_stores.clear()
        with open(os.path.join(packs.dir, base + '.pack'), 'r+b') as f:
            f.seek(off)
            first = f.read(1)
            f.seek(off)
            f.write(bytes([first[0] ^ 0xFF]))

    def test_a_damaged_archive_is_caught_while_building(self):
        self.damage_a_piece()
        with self.assertRaisesRegex(large_vcs.PatchDamaged, 'damaged in storage'):
            self.repo.restore('P1')
        self.assertIsNone(self.repo.current())  # never marked ready
        self.assertFalse(os.path.exists(self.repo.current_path(r'DATA\x.wad.client')))

    def test_check_storage_finds_it(self):
        self.assertEqual(self.repo.verify(log=lambda *_: None), {})
        self.damage_a_piece()
        problems = self.repo.verify(log=lambda *_: None)
        self.assertIn('P1', problems)
        self.assertIn('x.wad.client', problems['P1'][0])

    def test_a_bundle_cut_short_is_damage_not_a_crash(self):
        packs = self.repo.packs
        base = next(iter(packs.index.values()))[0]
        self.repo.release()
        large_vcs._pack_stores.clear()
        with open(os.path.join(packs.dir, base + '.pack'), 'r+b') as f:
            f.truncate(10)
        problems = self.repo.verify(log=lambda *_: None)
        self.assertIn('cut short', problems['P1'][0])
        with self.assertRaises(large_vcs.PatchDamaged):
            self.repo.restore('P1')

    def test_check_storage_finds_a_damaged_regular_file(self):
        blob = os.path.join(self.repo.files_dir, hash_file(os.path.join(self.inst, 'a.dll')))
        os.chmod(blob, stat.S_IWRITE)
        with open(blob, 'wb') as f:
            f.write(b'not fine')
        problems = self.repo.verify(log=lambda *_: None)
        self.assertEqual([p.split(':')[0] for p in problems['P1']], ['a.dll'])


class GameWritesTest(RepoTest):
    """older builds save their settings into files in the game folder. that must never reach storage"""
    CFG = r'DATA\cfg\game.cfg'

    def setUp(self):
        super().setUp()
        self.inst = self.install('p1', {'League of Legends.exe': b'exe', self.CFG: b'[General]\nWidth=800\n'})
        self.repo.add(self.inst, 'P1')

    def test_settings_files_get_their_own_copy(self):
        self.repo.restore('P1')
        cfg = self.repo.current_path(self.CFG)
        self.assertEqual(os.stat(cfg).st_nlink, 1)
        self.assertGreater(os.stat(self.repo.current_path('League of Legends.exe')).st_nlink, 1)
        with open(cfg, 'w') as f:
            f.write('[General]\nWidth=2560\n')  # the game saving its settings
        self.assertEqual(self.repo.verify(log=lambda *_: None), {})

    def test_old_prepared_folders_get_unlinked_before_launch(self):
        self.repo.restore('P1')
        cfg = self.repo.current_path(self.CFG)
        os.unlink(cfg)
        os.link(os.path.join(self.repo.files_dir, hash_file(os.path.join(self.inst, self.CFG))), cfg)  # the way prepared folders used to link it
        self.assertEqual(self.repo.detach_game_writable(), 1)
        self.assertEqual(os.stat(cfg).st_nlink, 1)
        with open(cfg, 'rb') as f:
            self.assertEqual(f.read(), b'[General]\nWidth=800\n')

    def test_settings_the_game_already_changed_get_kept(self):
        blob = os.path.join(self.repo.files_dir, hash_file(os.path.join(self.inst, self.CFG)))
        os.chmod(blob, stat.S_IWRITE)
        with open(blob, 'wb') as f:
            f.write(b'[General]\nWidth=2560\n')
        problems = self.repo.verify(log=lambda *_: None)
        self.assertEqual(problems, {'P1': [self.CFG + ': ' + large_vcs.CHANGED_SETTINGS]})
        self.assertEqual(self.repo.keep_changed_settings(problems), 1)
        self.assertEqual(self.repo.verify(log=lambda *_: None), {})
        self.repo.restore('P1')
        with open(self.repo.current_path(self.CFG), 'rb') as f:
            self.assertEqual(f.read(), b'[General]\nWidth=2560\n')


class DeleteTest(RepoTest):
    def test_wipe_only_takes_its_own_folders(self):
        inst = self.install('p1', {'a.dll': b'a', r'DATA\x.wad.client': ('wad', random_assets(5, seed=11), 11)})
        self.repo.add(inst, 'P1')
        self.repo.restore('P1')
        mine = os.path.join(self.repo.root, 'my other stuff.txt')
        with open(mine, 'w') as f:
            f.write('keep')
        self.repo.wipe()
        self.assertEqual(os.listdir(self.repo.root), ['my other stuff.txt'])

    def test_dropping_several_then_one_gc(self):
        for i in range(3):
            self.repo.add(self.install(f'p{i}', {'a.dll': bytes([i]) * 10}), f'P{i}')
        self.repo.drop('P0', collect=False)
        self.repo.drop('P1', collect=False)
        self.assertEqual(self.repo.gc(), 2)
        self.repo.restore('P2')

    def test_progress_reaches_the_end(self):
        from large_vcs.progress import reporting
        seen = []
        inst = self.install('p1', {f'f{i}.dll': bytes([i]) * 10 for i in range(50)})
        self.repo.add(inst, 'P1')
        with reporting(lambda done, total, label: seen.append((done, total, label))):
            self.repo.restore('P1')
        linking = [s for s in seen if s[2] == 'Linking files']
        self.assertEqual((linking[0][0], linking[-1]), (0, (50, 50, 'Linking files')))


class CancelTest(RepoTest):
    def cancel_after(self, n):
        from large_vcs.progress import reporting
        ev, seen = threading.Event(), []

        def cb(done, total, label):
            seen.append(label)
            if len(seen) >= n:
                ev.set()
        return reporting(cb, ev)

    def test_cancelled_restore_gets_rebuilt_properly_next_time(self):
        from large_vcs.progress import Cancelled
        inst = self.install('p1', {f'DATA\\w{i}.wad.client': ('wad', random_assets(5, seed=40 + i), 40 + i) for i in range(12)})
        self.repo.add(inst, 'P1')
        with self.cancel_after(1), self.assertRaises(Cancelled):
            self.repo.restore('P1')
        self.assertIsNone(self.repo.current())
        self.repo.restore('P1')
        self.assertStagedEqual(inst)

    def test_store_stopped_by_a_game_starting_is_redone_cleanly(self):
        from large_vcs.progress import Cancelled
        files = {f'f{i}.dll': bytes([i]) * 50 for i in range(30)}
        files.update({f'DATA\\w{i}.wad.client': ('wad', random_assets(5, seed=60 + i), 60 + i) for i in range(6)})
        inst = self.install('p1', files)
        with self.cancel_after(1), self.assertRaises(Cancelled):
            self.repo.add(inst, 'P1', workers=2)
        self.assertNotIn('P1', self.repo.list())
        self.repo.add(inst, 'P1', workers=2)
        self.repo.restore('P1')
        self.assertStagedEqual(inst)

    def test_cancelled_optimize_keeps_what_it_finished(self):
        from large_vcs.progress import Cancelled
        inst = self.install('p1', {f'DATA\\w{i}.wad.client': ('wad', random_assets(10, seed=50 + i), 50 + i) for i in range(8)})
        patch = {}
        for i in range(8):
            src = os.path.join(inst, f'DATA\\w{i}.wad.client')
            digest = hash_file(src)
            shutil.copyfile(src, os.path.join(self.repo.files_dir, digest))
            patch[digest] = f'DATA\\w{i}.wad.client'
        with open(self.repo.repo_path('patches', 'OLD.json'), 'w') as f:
            json.dump(patch, f)
        with mock.patch('league_vcs.winproc.game_running', return_value=False), \
                self.cancel_after(1), self.assertRaises(Cancelled):
            self.repo.optimize(log=lambda *_: None)
        report = self.repo.storage_report()[0]
        self.assertGreater(report['exact'], 0)
        self.repo.restore('OLD')
        self.assertStagedEqual(inst)


class RekeyTest(RepoTest):
    def test_two_keys_becoming_one_keeps_both_paths(self):
        a, b, c = 'a' * 64, 'b' * 64, 'c' * 64
        self.repo.ensure_repo()
        self.repo._save_patch('P1', {a: r'DATA\x.wad.client', b: r'DATA\y.wad.client'})
        self.repo._save_dups('P1', {b: [r'DATA\z.wad.client']})
        self.repo._rekey('P1', {b: a, c: a})
        self.assertEqual(sorted(r for _, r in self.repo.pairs('P1')),
                         [r'DATA\x.wad.client', r'DATA\y.wad.client', r'DATA\z.wad.client'])
        self.assertEqual({h for h, _ in self.repo.pairs('P1')}, {a})


class LeftoverBundleFilesTest(RepoTest):
    def test_a_bundle_stuck_mid_check_gets_its_name_back(self):
        inst = self.install('p1', {r'DATA\x.wad.client': ('wad', random_assets(8, seed=51), 51)})
        self.repo.add(inst, 'P1')
        packs = self.repo.packs
        base = next(iter(packs.index.values()))[0]
        pack = os.path.join(packs.dir, base + '.pack')
        packs.close()
        os.rename(pack, pack + '.probe')
        with open(os.path.join(packs.dir, 'pack-999999.pack.tmp'), 'wb') as f:
            f.write(b'half')
        packs._remove_orphans()
        self.assertEqual(sorted(os.listdir(packs.dir)), sorted([base + '.pack', base + '.idx']))
        packs.reload(force=True)
        self.repo.restore('P1')
        self.assertStagedEqual(inst)


class DriveTest(RepoTest):
    def setUp(self):
        super().setUp()
        self.inst = self.install('p1', {'a.dll': b'one',
                                        r'DATA\x.wad.client': ('wad', random_assets(40, seed=61, size=(40, 90000)), 61),
                                        r'DATA\y.wad.client': ('wad', random_assets(30, seed=62, size=(40, 200000)), 62)})
        self.repo.add(self.inst, 'P1')

    def test_every_drive_builds_the_same_files(self):
        for kind in (large_vcs.drives.NVME, large_vcs.drives.SSD, large_vcs.drives.HDD):
            self.repo.drive = kind
            self.repo.restore('P1', clean=True)
            self.assertStagedEqual(self.inst)

    def test_big_reads_across_back_to_back_pieces_come_out_the_same(self):
        from league_vcs.parsers.wad import _exact_chunks
        self.repo.repack(log=lambda *_: None)  # puts each archive's pieces back to back
        for checksum, rel in self.repo.get_patch('P1').items():
            if not rel.endswith('.wad.client'):
                continue
            m = large_vcs._load_manifest(self.repo.files_dir, checksum)
            # 16 KB is smaller than most pieces here, so the bit at a time path gets used too
            small_reads = b''.join(_exact_chunks(m, self.repo.files_dir, self.repo.packs, run=16 * 1024))
            in_runs = b''.join(_exact_chunks(m, self.repo.files_dir, self.repo.packs, run=64 * 1024 * 1024))
            with open(os.path.join(self.inst, rel), 'rb') as f:
                self.assertEqual(small_reads, f.read())
            self.assertEqual(in_runs, small_reads)

    def test_a_drive_windows_wont_describe_counts_as_fast(self):
        self.assertIsNone(large_vcs.drives.kind('\\\\nowhere\\share'))
        with mock.patch.object(large_vcs.drives, 'kind', return_value=None):
            self.assertEqual(self.repo.drive_kind(), large_vcs.drives.NVME)

    def test_clearing_ready_files_leaves_the_saved_patch(self):
        self.repo.keep_prepared = 2
        p2 = self.install('p2', {'a.dll': b'two'})
        self.repo.add(p2, 'P2')
        self.repo.restore('P1')
        self.repo.restore('P2')
        self.assertEqual(self.repo.kept(), ['P1'])
        self.repo.clear_prepared()
        self.assertEqual((self.repo.current(), self.repo.kept()), (None, []))
        self.assertFalse(os.path.exists(self.repo.current_path()))
        self.assertEqual(self.repo.space_used()['ready'], 0)
        self.repo.restore('P1')
        self.assertStagedEqual(self.inst)


class SavingProgressTest(RepoTest):
    def test_saving_counts_bytes_and_ends_at_the_total(self):
        from large_vcs.progress import reporting
        inst = self.install('p1', {r'DATA\big.wad.client': ('wad', random_assets(40, seed=71, size=(20000, 90000)), 71),
                                   r'DATA\small.wad.client': ('wad', random_assets(3, seed=72), 72)})
        seen = []
        with reporting(lambda done, total, label: seen.append((done, total, label))):
            self.repo.add(inst, 'P1')
        mine = [(d, t) for d, t, label in seen if label == 'Storing patch P1']
        size = sum(os.path.getsize(os.path.join(inst, 'DATA', f)) for f in ('big.wad.client', 'small.wad.client'))
        self.assertTrue(mine)
        self.assertEqual(mine[-1], (size, size))
        self.assertTrue(all(t == size for _, t in mine))


class ListFormatTest(RepoTest):
    """some league vcs builds saved patches as {hash: [every path]}. people point l-recall at that storage"""

    def to_list_format(self, tag):
        lists = {}
        for checksum, rel in sorted(self.repo.pairs(tag), key=lambda p: p[1]):
            lists.setdefault(checksum, []).append(rel)
        with open(self.repo._patch_path(tag), 'w') as f:
            json.dump(lists, f)
        os.unlink(self.repo._dups_path(tag))
        return lists

    def setUp(self):
        super().setUp()
        same = large_vcs.STUB_WAD  # riot's empty archive, the same file under several names
        self.inst = self.install('p1', {'a.dll': b'one', 'code-metadata.json': b'{"v": 1}',
                                        r'DATA\FINAL\Audio.wad.client': same, r'DATA\FINAL\Online.wad.client': same,
                                        r'DATA\FINAL\Champions\Ahri.wad.client': ('wad', random_assets(5, seed=81), 81)})
        self.repo.add(self.inst, 'P1')

    def test_it_builds_and_counts_every_name(self):
        before = self.repo.pairs('P1')
        lists = self.to_list_format('P1')
        self.assertTrue(any(len(v) > 1 for v in lists.values()))  # the identical archives, under both names
        self.assertEqual(self.repo.pairs('P1'), before)
        self.repo.restore('P1')
        self.assertStagedEqual(self.inst)
        self.assertEqual(self.repo.verify(log=lambda *_: None), {})

    def test_saving_it_again_turns_it_into_ours(self):
        before = self.repo.pairs('P1')
        self.to_list_format('P1')
        self.repo._rekey('P1', {'0' * 64: '1' * 64})  # nothing to rename, just loads and leaves it
        with open(os.path.join(self.inst, 'new.txt'), 'wb') as f:
            f.write(b'riot added this later')
        self.repo.top_up(self.inst, 'P1')  # a real save
        with open(self.repo._patch_path('P1')) as f:
            self.assertTrue(all(isinstance(v, str) for v in json.load(f).values()))
        self.assertEqual(self.repo.pairs('P1') - before, {(hash_file(os.path.join(self.inst, 'new.txt')), 'new.txt')})

    def test_a_really_broken_one_names_the_patch(self):
        with open(self.repo._patch_path('P1'), 'w') as f:
            json.dump({'a' * 64: [5]}, f)
        with self.assertRaisesRegex(ValueError, 'Patch P1 has a damaged file list'):
            self.repo.get_patch('P1')


class RepackTest(RepoTest):
    def test_repack_keeps_every_patch_byte_identical(self):
        shared = random_assets(30, seed=30, size=(40, 90000))
        p1 = self.install('p1', {'a.dll': b'one', r'DATA\x.wad.client': ('wad', shared, 30),
                                 r'DATA\y.wad.client': ('wad', random_assets(20, seed=31, size=(40, 200000)), 31)})
        p2 = self.install('p2', {'a.dll': b'two', r'DATA\x.wad.client': ('wad', shared[:25] + [b'new' * 5000], 30)})
        self.repo.add(p1, 'P1')
        self.repo.add(p2, 'P2')
        segs = self.repo._segment_hashes()
        self.repo.repack(log=lambda *_: None)
        # every shareable piece is bundled now, nothing loose left over
        loose = set(os.listdir(self.repo.files_dir))
        self.assertFalse(segs & loose)
        self.assertTrue(segs <= set(self.repo.packs.index))
        for tag, inst in (('P1', p1), ('P2', p2)):
            self.repo.restore(tag)
            self.assertStagedEqual(inst)
        # running it again is fine
        self.repo.repack(log=lambda *_: None)
        self.repo.restore('P1', clean=True)
        self.assertStagedEqual(p1)

    def test_stopped_repack_doesnt_leave_a_second_copy_behind(self):
        from large_vcs.progress import Cancelled, reporting
        p1 = self.install('p1', {r'DATA\x.wad.client': ('wad', random_assets(40, seed=33, size=(40, 60000)), 33)})
        self.repo.add(p1, 'P1')
        self.repo.repack(log=lambda *_: None)
        one_copy = self.repo.packs.total_bytes()
        ev, write_one = threading.Event(), self.repo.packs._write_one

        def stop_after_a_few(blobs):
            out = write_one(blobs)
            if len(self.repo.packs._current_stamp()) >= 4:
                ev.set()
            return out
        # tiny bundles so some get written before it stops
        with mock.patch('large_vcs.packs.PACK_TARGET', 4096), \
                mock.patch.object(self.repo.packs, '_write_one', stop_after_a_few), \
                reporting(lambda *_: None, ev), self.assertRaises(Cancelled):
            self.repo.repack(log=lambda *_: None)
        self.assertGreater(self.repo.packs.total_bytes(), one_copy)
        self.repo.packs.compact(self.repo._segment_hashes(), min_dead=0)
        self.assertLessEqual(self.repo.packs.total_bytes(), one_copy)
        self.repo.restore('P1', clean=True)
        self.assertStagedEqual(p1)


class KeepReadyTest(RepoTest):
    def setUp(self):
        super().setUp()
        self.repo.keep_prepared = 2
        # make_wad signs with random bytes, so build the shared one once and copy the bytes
        path = os.path.join(self.tmp, 'shared.wad.client')
        make_wad(path, random_assets(10, seed=20), seed=20)
        with open(path, 'rb') as f:
            shared = f.read()
        self.p1 = self.install('p1', {'a.dll': b'one', r'DATA\same.wad.client': shared,
                                      r'DATA\FINAL\Champions\Ahri.wad.client': ('wad', random_assets(5, seed=21), 21),
                                      r'DATA\FINAL\Champions\Zed.wad.client': ('wad', random_assets(5, seed=22), 22)})
        self.p2 = self.install('p2', {'a.dll': b'two', r'DATA\same.wad.client': shared,
                                      r'DATA\FINAL\Champions\Ahri.wad.client': ('wad', random_assets(5, seed=23), 23)})
        self.repo.add(self.p1, 'P1')
        self.repo.add(self.p2, 'P2')

    def test_switching_back_is_a_rename(self):
        self.repo.restore('P1')
        self.repo.restore('P2')
        self.assertStagedEqual(self.p2)
        self.assertEqual(self.repo.kept(), ['P1'])
        # identical archive came from the kept copy, same file on disk
        self.assertGreater(os.stat(self.repo.current_path(r'DATA\same.wad.client')).st_nlink, 1)
        with mock.patch('league_vcs.parsers.wad.pack_wad_exact', side_effect=AssertionError('rebuilt')):
            self.repo.restore('P1')
        self.assertStagedEqual(self.p1)
        self.assertEqual(self.repo.kept(), ['P2'])

    def test_only_keeps_as_many_as_asked(self):
        p3 = self.install('p3', {'a.dll': b'three'})
        self.repo.add(p3, 'P3')
        for tag in ('P1', 'P2', 'P3'):
            self.repo.restore(tag)
        self.assertEqual(self.repo.kept(), ['P2'])
        self.repo.keep_prepared = 1
        self.repo.restore('P1')
        self.assertEqual(self.repo.kept(), [])
        self.assertStagedEqual(self.p1)

    def test_a_kept_copy_in_use_stays_whole(self):
        zed = r'DATA\FINAL\Champions\Zed.wad.client'
        self.repo.restore('P1', skip={zed})
        self.repo.restore('P2')
        self.assertEqual((self.repo.kept(), self.repo.stubs('P1')), (['P1'], {zed}))
        self.repo.keep_prepared = 1
        # something's got a file in it open, so it can't go yet. it used to get half deleted and lose its
        # placeholder list, and then riot's empty archive got linked in as the real zed
        with open(self.repo.kept_path('P1', 'a.dll'), 'rb'):
            self.repo.restore('P2')
            self.assertEqual((self.repo.kept(), self.repo.stubs('P1')), (['P1'], {zed}))
        self.repo.restore('P2')  # lowering keep takes effect without anything to build
        self.assertEqual(self.repo.kept(), [])
        self.repo.restore('P1')
        self.assertStagedEqual(self.p1)

    def test_dropping_a_patch_drops_its_kept_copy(self):
        self.repo.restore('P1')
        self.repo.restore('P2')
        self.repo.drop('P1')
        self.assertEqual(self.repo.kept(), [])
        self.assertFalse(os.path.exists(self.repo.kept_path('P1')))

    def test_quick_start_placeholders_get_filled_in_later(self):
        zed = r'DATA\FINAL\Champions\Zed.wad.client'
        self.repo.restore('P1', skip={zed})
        with open(self.repo.current_path(zed), 'rb') as f:
            self.assertEqual(f.read(), large_vcs.STUB_WAD)
        self.assertEqual(self.repo.stubs(), {zed})
        # same patch again, zed still not needed: nothing to do
        self.repo.restore('P1', skip={zed})
        self.assertEqual(self.repo.stubs(), {zed})
        # a full prepare swaps the placeholder for the real thing
        self.repo.restore('P1')
        self.assertStagedEqual(self.p1)
        self.assertEqual(self.repo.stubs(), set())
        # and asking for quick start after that doesn't downgrade anything
        self.repo.restore('P1', skip={zed})
        self.assertStagedEqual(self.p1)

    def full_drive(self):
        return mock.patch('large_vcs.shutil.disk_usage', return_value=shutil._ntuple_diskusage(1, 1, 0))

    def test_kept_patch_is_used_even_with_the_drive_full(self):
        self.repo.restore('P1')
        self.repo.restore('P2')
        with self.full_drive(), \
                mock.patch('league_vcs.parsers.wad.pack_wad_exact', side_effect=AssertionError('rebuilt')):
            self.repo.restore('P1')
        self.assertStagedEqual(self.p1)
        # no room to keep P2 around as well
        self.assertEqual(self.repo.kept(), [])

    def test_kept_copies_make_room_for_a_build(self):
        self.repo.keep_prepared = 3
        p3 = self.install('p3', {'a.dll': b'three'})
        self.repo.add(p3, 'P3')
        self.repo.restore('P1')
        self.repo.restore('P2')
        with self.full_drive():
            self.repo.restore('P3')
        self.assertStagedEqual(p3)
        self.assertEqual(self.repo.kept(), [])

    def test_clearing_prepared_files_leaves_stored_ones_read_only(self):
        self.repo.keep_prepared = 1
        self.repo.restore('P1')
        self.repo.restore('P2')
        stored = os.path.join(self.repo.files_dir, hash_file(os.path.join(self.p1, 'a.dll')))
        self.assertFalse(os.stat(stored).st_mode & stat.S_IWRITE)
        self.repo.clean()
        stored = os.path.join(self.repo.files_dir, hash_file(os.path.join(self.p2, 'a.dll')))
        self.assertFalse(os.stat(stored).st_mode & stat.S_IWRITE)

    def test_game_still_holding_files_leaves_everything_alone(self):
        self.repo.restore('P1')
        real_rename = os.rename

        def locked(src, dst):
            if 'prepared' in dst:
                raise PermissionError(5, 'Access is denied')
            return real_rename(src, dst)
        with mock.patch('large_vcs.os.rename', side_effect=locked), mock.patch('large_vcs.time.sleep'), \
                self.assertRaisesRegex(ValueError, 'still closing'):
            self.repo.restore('P2')
        # nothing touched, P1's still there and ready
        self.assertEqual(self.repo.current(), 'P1')
        self.assertStagedEqual(self.p1)
        self.assertEqual(self.repo.kept(), [])
        self.repo.restore('P2')
        self.assertStagedEqual(self.p2)

    def test_overlay_apps_holding_game_logs_dont_block_switching(self):
        # tracker.gg and co keep the game's log open long after it closes
        self.repo.restore('P1')
        self.assertTrue(self.repo.redirect_logs())
        # straight into Logs. making folders through a junction doesn't work inside windows' temp folder
        # (it does everywhere else, checked on C:, E: and a hard drive), and the tests live in temp
        log = self.repo.current_path('Logs', 'r3dlog.txt')
        held = open(log, 'w')
        try:
            held.write('game log')
            held.flush()
            self.repo.restore('P2')  # parks P1, logs and all
            self.assertStagedEqual(self.p2)
            self.repo.keep_prepared = 1
            self.repo.restore('P1')  # and drops the kept copy
        finally:
            held.close()
        self.assertTrue(os.path.exists(os.path.join(self.repo.path('game logs'), 'r3dlog.txt')))
        # P1's back with its Logs junction still pointing at the shared logs
        self.assertTrue(os.path.isjunction(self.repo.current_path('Logs')))
        with open(self.repo.current_path('a.dll'), 'rb') as f:
            self.assertEqual(f.read(), b'one')

    def test_a_real_logs_folder_from_before_moves_over(self):
        self.repo.restore('P1')
        os.makedirs(self.repo.current_path('Logs'))
        with open(self.repo.current_path('Logs', 'old.txt'), 'w') as f:
            f.write('x')
        self.assertTrue(self.repo.redirect_logs())
        self.assertTrue(os.path.isjunction(self.repo.current_path('Logs')))
        moved = [d for d in os.listdir(self.repo.path('game logs')) if d.startswith('older')]
        self.assertEqual(len(moved), 1)

    def test_old_game_logs_get_cleared(self):
        folder = self.repo.path('game logs', 'GameLogs', 'old run')
        os.makedirs(folder)
        old, new = os.path.join(folder, 'r3dlog.txt'), self.repo.path('game logs', 'today.txt')
        for f in (old, new):
            with open(f, 'w') as fh:
                fh.write('x')
        month_ago = __import__('time').time() - 30 * 86400
        os.utime(old, (month_ago, month_ago))
        self.assertEqual(self.repo.tidy_game_logs(), 1)
        self.assertFalse(os.path.exists(folder))  # emptied folders go too
        self.assertTrue(os.path.exists(new))

    def test_space_used_counts_shared_copies_once(self):
        self.repo.restore('P1')
        one = self.repo.space_used()['ready']
        self.repo.restore('P2')  # P1 kept, same.wad.client hard linked between them
        both = self.repo.space_used()
        self.assertGreater(both['ready'], one)
        shared = os.path.getsize(self.repo.current_path(r'DATA\same.wad.client'))
        wads = sum(os.path.getsize(os.path.join(r, f)) for folder in (self.repo.current_path(), self.repo.kept_path('P1'))
                   for r, _, fs in os.walk(folder) for f in fs if f.endswith('.wad.client'))
        self.assertEqual(both['ready'], wads - shared)
        self.assertEqual(both['total'], both['storage'] + both['ready'])

    def test_log_cleanup_never_follows_a_junction_out(self):
        import _winapi
        outside = os.path.join(self.tmp, 'somebody elses stuff')
        os.makedirs(outside)
        precious = os.path.join(outside, 'old but mine.txt')
        with open(precious, 'w') as f:
            f.write('keep me')
        month_ago = __import__('time').time() - 30 * 86400
        os.utime(precious, (month_ago, month_ago))
        os.makedirs(self.repo.path('game logs'))
        _winapi.CreateJunction(outside, self.repo.path('game logs', 'sneaky'))
        self.repo.tidy_game_logs()
        self.assertTrue(os.path.exists(precious))

    def test_only_the_last_replays_champions_stay_built(self):
        ahri, zed = r'DATA\FINAL\Champions\Ahri.wad.client', r'DATA\FINAL\Champions\Zed.wad.client'
        self.repo.keep_prepared = 1
        self.repo.restore('P1', skip={zed}, trim=True)  # a replay with ahri in it
        self.assertEqual(self.repo.stubs(), {zed})
        self.repo.restore('P1', skip={ahri}, trim=True)  # then one with zed
        self.assertEqual(self.repo.stubs(), {ahri})
        with open(self.repo.current_path(ahri), 'rb') as f:
            self.assertEqual(f.read(), large_vcs.STUB_WAD)
        self.assertNotEqual(os.path.getsize(self.repo.current_path(zed)), len(large_vcs.STUB_WAD))
        # and a full build when it's needed still fills everything in
        self.repo.restore('P1')
        self.assertStagedEqual(self.p1)

    def test_switching_patch_in_place_trims_too(self):
        ahri, zed = r'DATA\FINAL\Champions\Ahri.wad.client', r'DATA\FINAL\Champions\Zed.wad.client'
        self.repo.keep_prepared = 1
        self.repo.restore('P1')  # everything built
        self.repo.restore('P2', skip={ahri}, trim=True)  # P2 has only ahri, and this replay doesn't need her
        self.assertEqual(self.repo.kept(), [])
        self.assertEqual(self.repo.stubs(), {ahri})
        self.assertFalse(os.path.exists(self.repo.current_path(zed)))  # not part of P2 at all
        self.repo.restore('P2')
        self.assertStagedEqual(self.p2)

    def test_placeholders_follow_a_kept_patch_around(self):
        zed = r'DATA\FINAL\Champions\Zed.wad.client'
        self.repo.restore('P1', skip={zed})
        self.repo.restore('P2')
        self.assertEqual(self.repo.stubs('P1'), {zed})
        self.repo.restore('P1')
        self.assertStagedEqual(self.p1)
        self.assertEqual(self.repo.stubs(), set())


class HostileStorageTest(RepoTest):
    """storage folders from someone else. none of this should touch anything outside the repo"""

    def write_patch(self, tag, patch, dups=None):
        with open(self.repo.repo_path('patches', tag + '.json'), 'w') as f:
            json.dump(patch, f)
        if dups:
            with open(self.repo.repo_path('patches', tag + '.json.dups'), 'w') as f:
                json.dump(dups, f)

    def blob(self, data):
        path = os.path.join(self.tmp, 'blob')
        with open(path, 'wb') as f:
            f.write(data)
        digest = hash_file(path)
        shutil.copyfile(path, os.path.join(self.repo.files_dir, digest))
        return digest

    def test_paths_outside_staging_are_refused(self):
        digest = self.blob(b'payload')
        escape = os.path.join(self.tmp, 'escaped.txt')
        for bad in (r'..\..\escaped.txt', escape, r'\escaped.txt', 'C:escaped.txt', 'a/../../escaped.txt'):
            self.write_patch('BAD', {digest: bad})
            with self.assertRaises(ValueError):
                self.repo.restore('BAD')
        self.write_patch('BAD', {digest: 'fine.txt'}, dups={digest: [r'..\..\escaped.txt']})
        with self.assertRaises(ValueError):
            self.repo.restore('BAD')
        self.assertFalse(os.path.exists(escape))

    def test_hash_that_is_really_a_path_is_refused(self):
        self.write_patch('BAD', {r'..\..\..\secret': 'a.dll'})
        with self.assertRaises(ValueError):
            self.repo.restore('BAD')

    def test_manifest_pointing_outside_files_is_refused(self):
        manifest = {'__wad_manifest__': True, 'format': 2, 'sha256': '0' * 64, 'size': 4,
                    'segments': [[r'..\..\..\secret', 4]]}
        self.write_patch('BAD', {_store_manifest(self.repo.files_dir, manifest): r'DATA\x.wad.client'})
        with self.assertRaises(ValueError):
            self.repo.restore('BAD')

    def test_current_json_cant_point_outside(self):
        victim = os.path.join(self.tmp, 'victim')
        os.makedirs(victim)
        with open(os.path.join(victim, 'keep.txt'), 'w') as f:
            f.write('mine')
        inst = self.install('p1', {'a.dll': b'a'})
        self.repo.add(inst, 'P1')
        os.makedirs(self.repo.current_path(), exist_ok=True)
        with open(self.repo.current_patch_path, 'w') as f:
            json.dump('../victim', f)
        self.assertIsNone(self.repo.current())
        self.repo.restore('P1')
        self.assertTrue(os.path.exists(os.path.join(victim, 'keep.txt')))

    def test_patch_names_cant_be_paths(self):
        for bad in (r'..\..\x', 'a/b', 'C:x', ''):
            with self.assertRaises(ValueError):
                self.repo.get_patch(bad)
        with open(self.repo.repo_path('patches', 'odd name.json'), 'w') as f:
            f.write('{}')
        self.assertNotIn('odd name', self.repo.list())


if __name__ == '__main__':
    unittest.main()
