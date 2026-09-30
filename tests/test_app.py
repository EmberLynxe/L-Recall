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


class QuickStartRetryTest(TempDirTest):
    ZED = r'DATA\FINAL\Champions\Zed.wad.client'

    def setUp(self):
        super().setUp()
        from fixtures import make_wad, random_assets
        from large_vcs import LargeVCS
        inst = os.path.join(self.tmp, 'inst')
        os.makedirs(os.path.join(inst, 'DATA', 'FINAL', 'Champions'))
        with open(os.path.join(inst, 'League of Legends.exe'), 'wb') as f:
            f.write(b'exe')
        make_wad(os.path.join(inst, self.ZED), random_assets(5, seed=31), seed=31)
        self.repo = LargeVCS.load_or_create(os.path.join(self.tmp, 'repo'))
        self.repo.add(inst, '16.1.1.1')
        self.old_repo = core.repo
        core.repo = self.repo

    def tearDown(self):
        core.repo = self.old_repo
        self.repo.release()
        for root, _, files in os.walk(self.tmp):
            for f in files:
                os.chmod(os.path.join(root, f), 0o666)
        super().tearDown()

    def watch(self, *runs, running=(), ask=None):
        """how long each launch runs for. running: game_running answers after the two checks before launch"""
        clock = iter([t for i, r in enumerate(runs) for t in (i * 1000, i * 1000 + r)])
        answers = iter([False, False, *running])
        rofl = mock.Mock(version='16.1.1.1')
        rofl.info.players = []
        with mock.patch.object(core, 'ROFLParser', return_value=rofl), \
                mock.patch.object(core, 'quick_start_skip', return_value={self.ZED}), \
                mock.patch.object(core, 'quick_start', True), mock.patch.object(core, 'use_my_settings', False), \
                mock.patch.object(core.winproc, 'game_running', side_effect=lambda: next(answers, False)), \
                mock.patch.object(core, 'vanguard_preflight', return_value=(None, None)), \
                mock.patch.object(core.signing, 'check_game_folder', return_value=None), \
                mock.patch.object(core.time, 'monotonic', side_effect=lambda: next(clock)), \
                mock.patch.object(core.subprocess, 'Popen') as popen:
            core.watch('x.rofl', ask_retry=ask)
        return popen.call_count

    def test_closing_straight_away_builds_the_rest_and_tries_again(self):
        self.assertEqual(self.watch(3, 3), 2)
        self.assertEqual(self.repo.stubs(), set())
        with open(self.repo.current_path(self.ZED), 'rb') as f:
            self.assertNotEqual(f.read(), __import__('large_vcs').STUB_WAD)
        self.assertFalse(core.needs_everything('x.rofl'))  # closed quickly both times, so nothing learned

    def test_a_replay_that_actually_played_is_left_alone(self):
        self.assertEqual(self.watch(600), 1)
        self.assertEqual(self.repo.stubs(), {self.ZED})

    def test_asks_before_rebuilding_and_leaves_it_if_told_to(self):
        asked = []
        self.assertEqual(self.watch(3, ask=lambda: asked.append(1) or False), 1)
        self.assertEqual((asked, self.repo.stubs()), ([1], {self.ZED}))
        self.assertEqual(self.watch(3, 600, ask=lambda: True), 2)
        self.assertEqual(self.repo.stubs(), set())

    def test_no_rebuild_when_a_real_game_started(self):
        self.assertEqual(self.watch(3, running=[True]), 1)
        self.assertEqual(self.repo.stubs(), {self.ZED})

    def test_a_replay_that_needed_everything_gets_everything_next_time(self):
        self.assertEqual(self.watch(3, 600), 2)
        self.assertTrue(core.needs_everything(r'C:\somewhere\X.rofl'))
        self.repo.restore('16.1.1.1', skip={self.ZED}, trim=True)
        self.assertEqual(self.watch(600), 1)
        self.assertEqual(self.repo.stubs(), set())

    def test_a_second_watch_stops_at_the_lock_if_the_first_is_running(self):
        with mock.patch.object(core.winproc, 'game_running', return_value=True), \
                self.assertRaisesRegex(core.UserInputException, 'already running'):
            core._launch('16.1.1.1', 'x.rofl', None, lambda: None)


class PrepareNewestTest(TempDirTest):
    def setUp(self):
        super().setUp()
        from large_vcs import LargeVCS
        self.repo = LargeVCS.load_or_create(os.path.join(self.tmp, 'repo'))
        for tag in ('16.1.1.1', '16.2.1.1', '16.10.1.1'):
            inst = os.path.join(self.tmp, tag)
            os.makedirs(inst)
            with open(os.path.join(inst, 'a.dll'), 'w') as f:
                f.write(tag)
            self.repo.add(inst, tag)
        self.old_repo, core.repo = core.repo, self.repo

    def tearDown(self):
        core.repo = self.old_repo
        self.repo.release()
        for root, _, files in os.walk(self.tmp):
            for f in files:
                os.chmod(os.path.join(root, f), 0o666)
        super().tearDown()

    def test_newest_gets_ready_into_a_free_slot(self):
        self.repo.keep_prepared = 2
        self.assertTrue(core.prepare_newest_patch())
        self.assertEqual(self.repo.current(), '16.10.1.1')  # 16.10 is newer than 16.2
        self.assertFalse(core.prepare_newest_patch())  # already there

    def test_never_pushes_out_what_you_got_ready(self):
        self.repo.keep_prepared = 2
        self.repo.restore('16.1.1.1')
        self.repo.restore('16.2.1.1')  # 16.1 kept, 16.2 current, both slots used
        self.assertFalse(core.prepare_newest_patch())
        self.assertEqual((self.repo.current(), self.repo.kept()), ('16.2.1.1', ['16.1.1.1']))

    def test_never_replaces_your_patch_when_the_drive_is_low(self):
        self.repo.keep_prepared = 2
        self.repo.restore('16.1.1.1')
        low = shutil._ntuple_diskusage(1, 1, 0)
        with mock.patch('large_vcs.shutil.disk_usage', return_value=low):
            self.assertFalse(core.prepare_newest_patch())
        self.assertEqual((self.repo.current(), self.repo.kept()), ('16.1.1.1', []))

    def test_waits_while_a_game_is_running(self):
        with mock.patch.object(core.winproc, 'game_running', return_value=True):
            self.assertFalse(core.prepare_newest_patch())
        self.assertIsNone(self.repo.current())

    def test_can_be_turned_off(self):
        with mock.patch.object(core, 'prepare_newest', False):
            self.assertFalse(core.prepare_newest_patch())
        self.assertIsNone(self.repo.current())


class SamePatchTest(unittest.TestCase):
    TAGS = ['16.18.817.5716', '16.19.820.7193', '16.19.821.7343', '16.19.822.100']

    def test_exact_build_first(self):
        self.assertEqual(core.client_for('16.19.820.7193', self.TAGS), '16.19.820.7193')

    def test_otherwise_newest_build_of_the_same_patch(self):
        self.assertEqual(core.client_for('16.19.819.1', self.TAGS), '16.19.822.100')

    def test_never_a_different_patch(self):
        self.assertIsNone(core.client_for('16.17.1.1', self.TAGS))
        self.assertIsNone(core.client_for('16.1.800.1', self.TAGS))  # 16.1 isn't 16.19


class LockedFolderTest(unittest.TestCase):
    def test_says_which_app_is_holding_the_files(self):
        import large_vcs
        repo = mock.Mock()
        repo.restore.side_effect = ValueError(large_vcs.STILL_CLOSING)
        with mock.patch.object(core, 'repo', repo), \
                mock.patch.object(core.winproc, 'game_running', return_value=False), \
                mock.patch.object(core.winproc, 'apps_using', return_value=['Tracker.gg']):
            with self.assertRaisesRegex(core.UserInputException, 'Tracker.gg'):
                core.restore('16.18.1.1')

    def test_really_still_closing_keeps_the_old_message(self):
        import large_vcs
        repo = mock.Mock()
        repo.restore.side_effect = ValueError(large_vcs.STILL_CLOSING)
        with mock.patch.object(core, 'repo', repo), \
                mock.patch.object(core.winproc, 'game_running', return_value=False), \
                mock.patch.object(core.winproc, 'apps_using', return_value=[]):
            with self.assertRaisesRegex(ValueError, 'still closing'):
                core.restore('16.18.1.1')


class PrivacyTest(unittest.TestCase):
    def test_log_files_never_get_the_windows_username(self):
        import ctypes
        src = open(os.path.join(os.path.dirname(__file__), '..', 'entrypoints', 'main.py'), encoding='utf-8').read()
        ns = {'os': os, 'ctypes': ctypes}
        exec(src[src.index('class Redacted:'):src.index('def _tidy_logs')], ns)
        out = io.StringIO()
        log = ns['Redacted'](out)
        home = os.path.expanduser('~')
        log.write(os.path.join(home, 'Documents', 'x.rofl') + ' ' + home.upper().replace(os.sep, '/') + ' '
                  + os.path.basename(home) + ' E:' + os.sep + 'games')
        self.assertNotIn(os.path.basename(home).lower(), out.getvalue().lower())
        self.assertIn('%USERPROFILE%', out.getvalue())
        self.assertIn('E:' + os.sep + 'games', out.getvalue())

    def test_config_isnt_dumped_into_the_log(self):
        src = open(os.path.join(os.path.dirname(__file__), '..', 'entrypoints', 'main.py'), encoding='utf-8').read()
        self.assertNotIn("print('Loaded config', config)", src)

    def test_documents_is_asked_for_not_guessed(self):
        from league_vcs.config import documents_folders
        folders = documents_folders()
        self.assertTrue(folders)
        self.assertEqual(len({os.path.normcase(f) for f in folders}), len(folders))


class DiskFullTest(unittest.TestCase):
    def test_full_drive_gets_a_plain_message(self):
        repo = mock.Mock(root='E:\\old riot')
        repo.restore.side_effect = OSError(28, 'No space left on device')
        with mock.patch.object(core, 'repo', repo), mock.patch.object(core, '_free', return_value=2 * 1024 ** 3):
            with self.assertRaisesRegex(core.DiskFull, 'Ran out of space on E:.*2.0 GB free'):
                core.restore('16.18.1.1')

    def test_other_errors_come_through_untouched(self):
        repo = mock.Mock(root='E:\\old riot')
        repo.restore.side_effect = OSError(13, 'Access is denied')
        with mock.patch.object(core, 'repo', repo), self.assertRaises(PermissionError):
            core.restore('16.18.1.1')

    def test_all_three_ways_windows_says_it(self):
        self.assertTrue(core.disk_full(OSError(28, 'x')))
        full = OSError(0, 'x')
        full.winerror = 112
        self.assertTrue(core.disk_full(full))
        self.assertFalse(core.disk_full(OSError(13, 'x')))


class LeanDefaultsTest(TempDirTest):
    def test_old_settings_move_to_one_patch_and_champions_only_once(self):
        path = os.path.join(self.tmp, 'user_settings.json')
        with open(path, 'w') as f:
            f.write('{"keep_prepared": 3, "quick_start": false}')
        c = Config(path)
        self.assertEqual((c['keep_prepared'], c['quick_start']), (1, True))
        c['keep_prepared'] = 2  # changed back afterwards by hand
        c.save()
        self.assertEqual(Config(path)['keep_prepared'], 2)

    def test_skipping_every_champion_for_a_patch_nobodys_watched_yet(self):
        pairs = [('a', r'DATA\FINAL\Champions\Ahri.wad.client'), ('b', r'DATA\FINAL\Champions\Ahri.en_US.wad.client'),
                 ('c', r'DATA\FINAL\Champions\TFTChampion.en_US.wad.client'), ('d', r'DATA\FINAL\Maps\Shipping\Map11.wad.client')]
        repo = mock.Mock()
        repo.pairs.return_value = pairs
        with mock.patch.object(core, 'repo', repo), \
                mock.patch.object(assets, 'champion_names', return_value=['Ahri', 'Zed']), \
                mock.patch.object(assets, 'version_for', return_value='16.1.1'):
            skip = core.quick_start_skip('16.1.1.1', [], everyone=True)
            self.assertEqual(skip, {pairs[0][1], pairs[1][1]})  # tft's placeholder and the map stay
            self.assertEqual(core.replay_map([{'summoner_spells': [30, 31]}] * 10), 'map12')  # poro king
            self.assertEqual(core.quick_start_skip('16.1.1.1', []), set())

    FILES = [r'DATA\FINAL\Champions\Ahri.wad.client', r'DATA\FINAL\Champions\Zed.wad.client',
             r'DATA\FINAL\Companions.wad.client', r'DATA\FINAL\TFTSet13.wad.client', r'DATA\FINAL\TFTCommon.wad.client',
             r'DATA\FINAL\Global.wad.client', r'DATA\FINAL\Maps\Shipping\Common.wad.client',
             r'DATA\FINAL\Maps\Shipping\Map11.wad.client', r'DATA\FINAL\Maps\Shipping\Map12.wad.client',
             r'DATA\FINAL\Maps\Shipping\Map12.en_US.wad.client', r'DATA\FINAL\Maps\Shipping\Map30.wad.client']

    def skipped(self, players, champions=('Ahri', 'Zed')):
        repo = mock.Mock()
        repo.pairs.return_value = [(str(i), rel) for i, rel in enumerate(self.FILES)]
        with mock.patch.object(core, 'repo', repo), \
                mock.patch.object(assets, 'champion_names', return_value=list(champions)), \
                mock.patch.object(assets, 'version_for', return_value='16.1.1'):
            skip = core.quick_start_skip('16.1.1.1', players)
        return sorted(os.path.basename(r).split('.')[0] for r in skip)

    def test_only_the_replays_map_gets_built_and_never_tft(self):
        rift = [{'champion': 'Ahri', 'summoner_spells': [4, 14]}] * 10
        self.assertEqual(self.skipped(rift), ['Companions', 'Map12', 'Map12', 'Map30', 'TFTSet13', 'Zed'])
        aram = [{'champion': 'Ahri', 'summoner_spells': [4, 32]}] + rift[1:]
        self.assertEqual(self.skipped(aram), ['Companions', 'Map11', 'Map30', 'TFTSet13', 'Zed'])
        arena = [{'champion': 'Ahri', 'summoner_spells': [4, 14], 'subteam': 3}] * 16
        self.assertEqual(self.skipped(arena), ['Companions', 'Map11', 'Map12', 'Map12', 'TFTSet13', 'Zed'])

    def test_an_unknown_champion_or_player_count_leaves_those_parts_alone(self):
        new_champ = [{'champion': 'Brandnew', 'summoner_spells': [4, 14]}] * 10
        self.assertEqual(self.skipped(new_champ), ['Companions', 'Map12', 'Map12', 'Map30', 'TFTSet13'])
        three = [{'champion': 'Ahri', 'summoner_spells': [4, 14]}] * 3
        self.assertEqual(self.skipped(three), ['Companions', 'TFTSet13', 'Zed'])


class BadReplayTest(TempDirTest):
    def test_a_broken_replay_is_skipped_not_fatal(self):
        from league_vcs.parsers.rofl import scan_replays
        make_rofl(os.path.join(self.tmp, 'good.rofl'))
        open(os.path.join(self.tmp, 'empty.rofl'), 'wb').close()
        with open(os.path.join(self.tmp, 'junk.rofl'), 'wb') as f:
            f.write(b'nope, not a replay at all')
        self.assertEqual([r.filename for r in scan_replays([self.tmp])], ['good.rofl'])

    def test_its_error_is_an_ordinary_exception(self):
        # as a BaseException it went straight past every "except Exception" and left windows hanging
        self.assertTrue(issubclass(core.UserInputException, Exception))


class ClearReadyTest(unittest.TestCase):
    def test_not_while_the_game_is_running(self):
        repo = mock.Mock()
        with mock.patch.object(core, 'repo', repo), \
                mock.patch.object(core.winproc, 'game_running', return_value=True), \
                self.assertRaisesRegex(core.UserInputException, 'League is running'):
            core.clear_ready()
        repo.clear_prepared.assert_not_called()


class UninstallTest(TempDirTest):
    def test_only_takes_folders_that_are_ours(self):
        from league_vcs import uninstall
        os.makedirs(os.path.join(self.tmp, 'lib', 'league_vcs'))
        os.makedirs(os.path.join(self.tmp, 'logs'))
        open(os.path.join(self.tmp, 'logs', '2026-09-27T10-00-00-000000.log'), 'w').close()
        self.assertEqual(uninstall._shipped(self.tmp)[0], ['lib', 'logs'])
        # someone's own logs folder in Downloads, and a lib that isn't ours
        open(os.path.join(self.tmp, 'logs', 'my notes.txt'), 'w').close()
        os.makedirs(os.path.join(self.tmp, 'lib.old'))
        self.assertEqual(uninstall._shipped(self.tmp)[0], ['lib'])


class ReportTest(TempDirTest):
    def test_the_zip_has_the_newest_logs_and_nothing_personal(self):
        import zipfile
        from league_vcs import report
        home = os.path.expanduser('~')
        logs = os.path.join(self.tmp, 'logs')
        os.makedirs(logs)
        for i in range(5):
            path = os.path.join(logs, f'2026-09-2{i}T10-00-00-000000.log')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(f'log {i}: opened {home}\\Documents and {home.replace(chr(92), "/").upper()}/x\n')
            os.utime(path, (1000 + i, 1000 + i))
        path = report.make(logs, 'L-Recall test\n', dest=self.tmp)
        with zipfile.ZipFile(path) as z:
            names = sorted(z.namelist())
            text = ''.join(z.read(n).decode() for n in names)
        self.assertEqual(names, ['logs/2026-09-22T10-00-00-000000.log', 'logs/2026-09-23T10-00-00-000000.log',
                                 'logs/2026-09-24T10-00-00-000000.log', 'summary.txt'])
        self.assertNotIn(os.path.basename(home).lower(), text.lower())
        self.assertIn('<you>', text)


class SaveStatusTest(TempDirTest):
    def test_percent_for_the_window_and_tray(self):
        import types
        from league_vcs.gui import GUI
        status = lambda saving: GUI.save_status(types.SimpleNamespace(saving=saving))
        self.assertIsNone(status(None))
        self.assertEqual(status({'version': '16.20', 'done': 0, 'total': 0}), {'version': '16.20', 'percent': 0})
        self.assertEqual(status({'version': '16.20', 'done': 50, 'total': 200}), {'version': '16.20', 'percent': 25})
        # never says 100 while it's still going, the last bit is writing the patch list
        self.assertEqual(status({'version': '16.20', 'done': 200, 'total': 200})['percent'], 99)

    def test_saving_new_patches_is_on_unless_turned_off(self):
        path = os.path.join(self.tmp, 'user_settings.json')
        with open(path, 'w') as f:
            f.write('{"configured": true}')
        self.assertTrue(Config(path)['save_new_patches'])


class FailedJobTest(unittest.TestCase):
    def run_job(self, func):
        from league_vcs.gui.frames import settings
        window, log, done = [], [], []
        fake = mock.Mock(_push_console=window.append, _console_done=done.append)
        with mock.patch.object(settings, '_log_only', log.append), mock.patch.object(settings.wx, 'CallAfter'), \
                mock.patch.object(settings.winproc, 'trim_memory'):
            settings.SettingsFrame._run_console_op_inner(fake, 'r1', func)
        return ''.join(window), ''.join(log), done

    def test_a_surprise_gets_a_plain_message_and_the_details_go_in_the_log(self):
        def prepare():
            {}['missing']
        window, log, done = self.run_job(prepare)
        self.assertEqual(done, ['failed'])  # which is what shows the report button
        self.assertIn('Save a problem report', window)
        self.assertNotIn('Traceback', window)
        self.assertIn('Traceback', log)
        self.assertIn('prepare failed', log)

    def test_a_known_problem_says_just_the_problem(self):
        def prepare():
            raise core.UserInputException('League is running. Close the game (and any replay) first.')
        window, log, done = self.run_job(prepare)
        self.assertEqual(window.strip(), 'League is running. Close the game (and any replay) first.')
        self.assertIn('Traceback', log)  # still logged in full


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
    """replays don't need vanguard. it only gets in the way when it's loaded as the game starts"""

    def status(self, installed=True, running=True, mode='boot'):
        return {'installed': installed, 'running': running, 'mode': mode}

    def preflight(self, version, live=None, **st):
        with mock.patch.object(vanguard, 'status', return_value=self.status(**st)), \
                mock.patch.object(core, 'installed_version', return_value=live):
            return core.vanguard_preflight(version)

    def test_nothing_to_say_when_vanguard_isnt_loaded(self):
        # pre-check waiting for a game, exited from the tray, or not installed at all. any patch
        for st in ({'running': False, 'mode': 'on_demand'}, {'running': False, 'mode': 'boot'},
                   {'installed': False, 'running': False, 'mode': None}):
            for version in ('14.1.1', '16.19.1'):
                self.assertEqual(self.preflight(version, **st), (None, None))

    def test_always_on_gives_both_ways_out(self):
        level, message = self.preflight('16.10.1', live='16.19.1')
        self.assertEqual(level, 'warn')
        self.assertIn('Pre-Check', message)
        self.assertIn('tray icon', message)
        self.assertNotIn('League client', message)
        self.assertIn('League client', self.preflight('16.19.1', live='16.19.1')[1])

    def test_pre_check_but_running_right_now(self):
        level, message = self.preflight('16.10.1', mode='on_demand')
        self.assertEqual(level, 'warn')
        self.assertIn('tray icon', message)

    def test_no_patch_is_treated_differently(self):
        # the old "needs vanguard from 14.9" split was wrong, exiting vanguard works for new patches too
        self.assertEqual(self.preflight('14.1.1')[0], self.preflight('16.19.1')[0])
        self.assertNotIn('needs Riot Vanguard', self.preflight('16.19.1')[1])

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
        self.assertIn('tray icon', self.refused('16.10.1', live='16.19.1'))
        self.assertIn('League client', self.refused('16.19.1', live='16.19.1'))
        self.assertIn('tray icon', self.refused('16.10.1', mode='on_demand'))
        self.assertIn('Antivirus', self.refused('16.10.1', running=False))

    def test_process_list_needs_no_handles(self):
        self.assertIn('explorer.exe', winproc.process_names())


if __name__ == '__main__':
    unittest.main()
