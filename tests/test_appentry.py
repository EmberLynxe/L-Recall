import os
import shutil
import tempfile
import unittest
import winreg
from unittest import mock

import win32api
import win32com.client

from league_vcs import appentry, uninstall

TEST_KEY = r'Software\L-Recall-tests\Uninstall\L-Recall'


class TestAppEntry(unittest.TestCase):
    """the real thing, but into a temp folder and a throwaway registry key"""

    def setUp(self):
        self.tmp = win32api.GetLongPathName(tempfile.mkdtemp())  # temp can be an 8.3 name, the shortcut stores the long one
        self.app = os.path.join(self.tmp, 'app')
        os.makedirs(os.path.join(self.app, 'lib', 'league_vcs'))
        self.exe = os.path.join(self.app, 'L-Recall.exe')
        with open(self.exe, 'wb') as f:
            f.write(b'x' * 4096)
        with open(os.path.join(self.app, 'lib', 'league_vcs', 'a.pyc'), 'wb') as f:
            f.write(b'x' * 8192)
        with open(os.path.join(self.app, 'someone elses.iso'), 'wb') as f:
            f.write(b'x' * 100000)
        self.menu = os.path.join(self.tmp, 'Programs')
        patches = [mock.patch.object(appentry, 'KEY', TEST_KEY),
                   mock.patch.object(appentry, 'start_menu_folder', lambda: self.menu)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        for key in (TEST_KEY, r'Software\L-Recall-tests\Uninstall', r'Software\L-Recall-tests'):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def value(self, name):
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as key:
            return winreg.QueryValueEx(key, name)[0]

    def test_shortcut_and_listing(self):
        appentry.register(self.exe, win32com.client.Dispatch('WScript.Shell'))
        link = os.path.join(self.menu, 'L-Recall.lnk')
        self.assertTrue(os.path.isfile(link))
        target = win32com.client.Dispatch('WScript.Shell').CreateShortCut(link).Targetpath
        self.assertEqual(os.path.normcase(target), os.path.normcase(self.exe))
        self.assertEqual(self.value('DisplayName'), 'L-Recall')
        self.assertEqual(self.value('UninstallString'), f'"{self.exe}" --uninstall')
        self.assertEqual(self.value('InstallLocation'), self.app)
        # 12 KB of ours, not the 100 KB file someone left next to it
        self.assertEqual(self.value('EstimatedSize'), 12)

    def test_moving_the_folder_just_updates_it(self):
        shell = win32com.client.Dispatch('WScript.Shell')
        appentry.register(self.exe, shell)
        moved = os.path.join(self.tmp, 'moved')
        shutil.move(self.app, moved)
        appentry.register(os.path.join(moved, 'L-Recall.exe'), shell)
        self.assertEqual(self.value('InstallLocation'), moved)
        self.assertEqual(os.listdir(self.menu), ['L-Recall.lnk'])

    def test_uninstall_takes_both_away(self):
        appentry.register(self.exe, win32com.client.Dispatch('WScript.Shell'))
        with mock.patch.object(uninstall, '_startup_folder', lambda: self.tmp), \
                mock.patch.object(uninstall.assets, 'ROOT', os.path.join(self.tmp, 'nothing', 'here')):
            uninstall.remove_data(log=lambda *_: None)
        self.assertFalse(os.path.exists(os.path.join(self.menu, 'L-Recall.lnk')))
        with self.assertRaises(OSError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY)


if __name__ == '__main__':
    unittest.main()
