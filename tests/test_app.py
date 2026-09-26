"""replays, config, notes, output capture, vanguard check"""
import io
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from league_vcs import assets, core, signing, vanguard, winproc
from league_vcs.config import Config
from league_vcs.gui.utils.output import capture
from league_vcs.notes import Notes, replay_key
from league_vcs.parsers.rofl import ROFLParser

from fixtures import make_rofl


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='lrecall-test-')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class ReplayTest(TempDirTest):
    def test_parses_version_length_players_runes_and_stats(self):
        path = os.path.join(self.tmp, 'EUW1-123456.rofl')
        make_rofl(path)
        info = ROFLParser(path).info
        self.assertEqual(info.version, '16.19.821.7343')
        self.assertEqual(info.game_length, 1800)
        self.assertEqual([p['champion'] for p in info.players], ['Ahri', 'Zed'])
        ahri = info.players[0]
        self.assertEqual((ahri['kills'], ahri['deaths'], ahri['assists']), (7, 2, 9))
        self.assertEqual(ahri['runes']['perks'][0], 8112)
        self.assertEqual(ahri['stats']['phys_dealt'], 1234)
        self.assertTrue(info.blue_win)


class AssetPathTest(unittest.TestCase):
    def test_names_from_replays_stay_inside_the_cache(self):
        self.assertTrue(assets.local_path('15.14.1', 'champion', 'MonkeyKing').startswith(assets.ROOT))
        for version, name in (('15.14.1', r'..\..\x'), ('15.14.1', 'a/b'), (r'..\x', 'Ahri'), ('15..1', 'Ahri')):
            with self.assertRaises(ValueError):
                assets.local_path(version, 'champion', name)
        # bad names get skipped, not fetched
        with mock.patch.object(assets, '_fetch') as fetch:
            self.assertEqual(assets.ensure('15.14.1', champions=[r'..\..\evil']), 0)
            fetch.assert_not_called()


class AssetVersionsTest(TempDirTest):
    def test_saved_patch_list_doesnt_wait_for_the_network(self):
        with open(os.path.join(self.tmp, 'versions.json'), 'w') as f:
            f.write('["16.19.1", "16.18.1"]')
        slow = threading.Event()
        with mock.patch.object(assets, 'ROOT', self.tmp), mock.patch.object(assets, '_versions', None), \
                mock.patch.object(assets, '_get', side_effect=lambda *a, **k: slow.wait(5) or b'[]'):
            t = time.perf_counter()
            self.assertEqual(assets.versions(), ['16.19.1', '16.18.1'])
            self.assertLess(time.perf_counter() - t, 1)
            slow.set()


class IconSharingTest(TempDirTest):
    def test_same_icon_in_two_patches_is_one_file(self):
        png = b'\x89PNG fake icon'
        with mock.patch.object(assets, 'ROOT', self.tmp), mock.patch.object(assets, '_get', return_value=png):
            assets._fetch('16.18.1', 'champion', 'Ahri')
            assets._fetch('16.19.1', 'champion', 'Ahri')
            a, b = assets.local_path('16.18.1', 'champion', 'Ahri'), assets.local_path('16.19.1', 'champion', 'Ahri')
            self.assertTrue(os.path.samefile(a, b))

    def test_old_duplicates_get_merged(self):
        with mock.patch.object(assets, 'ROOT', self.tmp):
            for v, data in (('16.17.1', b'same'), ('16.18.1', b'same'), ('16.19.1', b'changed')):
                os.makedirs(os.path.join(self.tmp, v, 'item'))
                with open(os.path.join(self.tmp, v, 'item', '3031.png'), 'wb') as f:
                    f.write(data)
            self.assertEqual(assets.share_duplicates(), 1)
            p = lambda v: os.path.join(self.tmp, v, 'item', '3031.png')
            self.assertTrue(os.path.samefile(p('16.17.1'), p('16.18.1')))
            self.assertFalse(os.path.samefile(p('16.18.1'), p('16.19.1')))


class SigningTest(TempDirTest):
    def game(self, *names):
        for n in names:
            path = os.path.join(self.tmp, n)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as f:
                f.write(b'MZ' + os.urandom(512))
        return self.tmp

    def test_unsigned_file_has_no_signer(self):
        self.game('fake.exe')
        self.assertIsNone(signing.signer(os.path.join(self.tmp, 'fake.exe')))

    def test_folder_rules(self):
        folder = self.game('League of Legends.exe', 'Vendor.dll', r'sub\thing.dll', 'readme.txt')
        signers = {'League of Legends.exe': signing.RIOT, 'Vendor.dll': 'Microsoft Corporation',
                   'thing.dll': signing.RIOT}
        fake = lambda p: signers.get(os.path.basename(p))
        with mock.patch.object(signing, '_cached_signer', side_effect=fake):
            self.assertIsNone(signing.check_game_folder(folder, 'League of Legends.exe'))
            signers['thing.dll'] = None
            self.assertIn('thing.dll', signing.check_game_folder(folder, 'League of Legends.exe'))
            signers['League of Legends.exe'] = 'Someone Else'
            self.assertIn('Riot', signing.check_game_folder(folder, 'League of Legends.exe'))


class DetectTest(TempDirTest):
    def test_finds_live_installs_once_and_skips_pbe(self):
        roots = []
        for name in ('League of Legends', 'League of Legends (PBE)', 'Not League'):
            root = os.path.join(self.tmp, name)
            roots.append(root)
            if name != 'Not League':
                os.makedirs(os.path.join(root, 'Game'))
                open(os.path.join(root, 'Game', 'League of Legends.exe'), 'w').close()
        live = os.path.join(roots[0], 'Game', 'League of Legends.exe')
        # riot's files use forward slashes and trailing ones, same install shows up a few times
        seen = [roots[0].replace('\\', '/') + '/', roots[1], roots[0], roots[2]]
        with mock.patch.object(core, '_install_roots', return_value=iter(seen)):
            self.assertEqual(core.detect_game_exes(), [os.path.normpath(live)])

    def test_real_lookup_doesnt_blow_up(self):
        self.assertIsInstance(core.detect_game_exes(), list)


class MySettingsTest(TempDirTest):
    def test_settings_come_along_without_touching_the_real_ones(self):
        league = os.path.join(self.tmp, 'League of Legends')
        os.makedirs(os.path.join(league, 'Game'))
        os.makedirs(os.path.join(league, 'Config'))
        exe = os.path.join(league, 'Game', 'League of Legends.exe')
        open(exe, 'wb').close()
        for name, text in (('game.cfg', '[General]\nWidth=2560\n'), ('input.ini', '[GameEvents]\n'),
                           ('PersistedSettings.json', '{"files": []}')):
            with open(os.path.join(league, 'Config', name), 'w') as f:
                f.write(text)
        persisted = os.path.join(league, 'Config', 'PersistedSettings.json')
        os.chmod(persisted, 0o444)  # people lock it so the client stops resetting it
        game = os.path.join(self.tmp, 'current')
        try:
            with mock.patch.object(core, 'game_exes', [exe]), mock.patch.object(core, 'detect_game_exes', return_value=[]):
                self.assertEqual(core.copy_settings(game), 3)
                self.assertEqual(core.copy_settings(game), 3)  # again, over last time's copies
            with open(os.path.join(game, 'Config', 'game.cfg')) as f:
                self.assertIn('Width=2560', f.read())
            self.assertFalse(os.stat(persisted).st_mode & 0o200)  # the real one's still locked
        finally:
            os.chmod(persisted, 0o666)

    def test_no_league_install_means_defaults(self):
        with mock.patch.object(core, 'game_exes', []), mock.patch.object(core, 'detect_game_exes', return_value=[]):
            self.assertEqual(core.copy_settings(os.path.join(self.tmp, 'current')), 0)


class ConfigTest(TempDirTest):
    def test_corrupt_config_is_set_aside(self):
        path = os.path.join(self.tmp, 'user_settings.json')
        with open(path, 'w') as f:
            f.write('{"configured": tr')
        self.assertFalse(Config(path)['configured'])
        self.assertTrue(os.path.exists(path + '.corrupt'))

    def test_non_ascii_round_trips(self):
        path = os.path.join(self.tmp, 'user_settings.json')
        c = Config(path)
        c['player_names'] = ['정글러#KR1']
        c.save()
        self.assertEqual(Config(path)['player_names'], ['정글러#KR1'])


class NotesTest(TempDirTest):
    def test_notes_follow_the_game_id_not_the_filename(self):
        notes = Notes(os.path.join(self.tmp, 'notes.json'))
        notes.set('euw1-555.rofl', ['#clutch', 'clutch', ' review '], 'baron call')
        self.assertEqual(replay_key('EUW1-555 (1).rofl'), 'EUW1-555')
        self.assertEqual(notes.get('EUW1-555 (1).rofl'), {'tags': ['clutch', 'review'], 'note': 'baron call'})
        notes.set('euw1-555.rofl', [], '')
        self.assertEqual(notes.get('euw1-555.rofl'), {})


class OutputCaptureTest(unittest.TestCase):
    def test_each_thread_captures_its_own_prints(self):
        bufs = [io.StringIO(), io.StringIO()]

        def work(buf, text):
            with capture(buf):
                print(text)

        threads = [threading.Thread(target=work, args=(b, t)) for b, t in zip(bufs, 'AB')]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual([b.getvalue() for b in bufs], ['A\n', 'B\n'])


class VanguardCheckTest(unittest.TestCase):
    def status(self, installed=True, running=True, mode='boot'):
        return {'installed': installed, 'running': running, 'mode': mode}

    def test_patch_cutoff(self):
        self.assertTrue(core.needs_vanguard('14.9.581.1'))
        self.assertFalse(core.needs_vanguard('14.8.1'))

    def test_new_patch_blocked_when_vanguard_off(self):
        with mock.patch.object(vanguard, 'status', return_value=self.status(running=False)):
            self.assertEqual(core.vanguard_preflight('16.19.1')[0], 'block')

    def test_pre_check_not_running_yet_is_fine(self):
        with mock.patch.object(vanguard, 'status', return_value=self.status(running=False, mode='on_demand')):
            self.assertEqual(core.vanguard_preflight('16.19.1'), (None, None))

    def test_nothing_in_here_changes_vanguard(self):
        # vanguard stays read only
        self.assertFalse([n for n in dir(vanguard) if n.startswith(('set', 'stop', 'start', 'enable', 'disable'))])
        self.assertTrue(vanguard.status()['installed'] in (True, False, None))

    def refused(self, version, live=None, **st):
        with mock.patch.object(vanguard, 'status', return_value=self.status(**st)), \
                mock.patch.object(core, 'installed_version', return_value=live):
            return core.launch_refused(version)

    def test_blocked_launch_says_why(self):
        self.assertIn('Pre-Check', self.refused('16.10.1', live='16.19.1'))
        self.assertNotIn('tray icon', self.refused('16.10.1', live='16.19.1'))
        self.assertIn('League client', self.refused('16.19.1', live='16.19.1'))
        self.assertIn('untested', self.refused('14.1.1'))
        self.assertIn('Antivirus', self.refused('16.10.1', running=False))

    def test_vanguard_from_boot_always_warns(self):
        with mock.patch.object(vanguard, 'status', return_value=self.status()), \
                mock.patch.object(core, 'installed_version', return_value='16.19.1'):
            level, message = core.vanguard_preflight('14.1.1', warn_old=False)
            self.assertEqual(level, 'warn')
            self.assertIn('untested', message)
            level, message = core.vanguard_preflight('16.10.1')
            self.assertIn('Pre-Check', message)
            self.assertNotIn('League client', message)
            self.assertIn('League client', core.vanguard_preflight('16.19.1')[1])

    def test_old_patch_warns_only_when_asked(self):
        with mock.patch.object(vanguard, 'status', return_value=self.status(mode='on_demand')):
            self.assertEqual(core.vanguard_preflight('14.1.1')[0], 'warn')
            self.assertEqual(core.vanguard_preflight('14.1.1', warn_old=False), (None, None))

    def test_all_clear(self):
        with mock.patch.object(vanguard, 'status', return_value=self.status(mode='on_demand')):
            self.assertEqual(core.vanguard_preflight('16.19.1'), (None, None))

    def test_process_list_needs_no_handles(self):
        self.assertIn('explorer.exe', winproc.process_names())


if __name__ == '__main__':
    unittest.main()
