"""storage. every rebuild has to match byte for byte"""
import json
import os
import shutil
import stat
import struct
import tempfile
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

    def test_patch_names_cant_be_paths(self):
        for bad in (r'..\..\x', 'a/b', 'C:x', ''):
            with self.assertRaises(ValueError):
                self.repo.get_patch(bad)
        with open(self.repo.repo_path('patches', 'odd name.json'), 'w') as f:
            f.write('{}')
        self.assertNotIn('odd name', self.repo.list())


if __name__ == '__main__':
    unittest.main()
