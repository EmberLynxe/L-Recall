import ctypes
import os
import sys
import threading
import webbrowser
from typing import Optional

# per monitor v2, so dragging a window to a screen with different scaling redraws it properly
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import win32api
import win32com.client
import win32process
import wx

from .frames import WatchReplayFrame, InitialConfigFrame, SettingsFrame
from .tray_item import TrayItem
from .. import core, updates, winproc
from ..config import Config
from ..exceptions import UserInputException


class GUI:
    SCAN_INTERVAL = 1 * 60 * 60 * 1000
    UPDATE_INTERVAL = 12 * 60 * 60 * 1000

    def __init__(self, config: Config):
        self.config = config
        core.keep_prepared = config.get('keep_prepared', 2)
        core.quick_start = config.get('quick_start', False)
        self.app = wx.App()
        try:
            self.app.MSWEnableDarkMode(wx.App.DarkMode_Always)
        except Exception:
            pass
        self.auto_scan_interval: Optional[wx.CallLater] = None
        self.update_interval: Optional[wx.CallLater] = None
        self.update_info = None
        self._told_about = None
        self.tray_icon: Optional[TrayItem] = None

        self.scan_lock = threading.Lock()
        self.replay_frame = None
        self.settings_frame = None

        self.process_handle = win32api.GetCurrentProcess()

    def set_priority(self, priority):
        win32process.SetPriorityClass(self.process_handle, priority)

    def set_low_priority(self):
        self.set_priority(win32process.IDLE_PRIORITY_CLASS)

    def set_high_priority(self):
        self.set_priority(win32process.ABOVE_NORMAL_PRIORITY_CLASS)

    def start(self, show_window=True):
        if self.config['configured']:
            self.start_daemon()
            if show_window:
                wx.CallAfter(self.open_settings)
        else:
            frame = InitialConfigFrame(self.config, on_complete=self._setup_done, on_cancel=self.exit)
            frame.Show()
        self.app.MainLoop()

    def _setup_done(self):
        # used to only start the tray, so continue looked like it closed the app
        self.start_daemon()
        wx.CallAfter(self.open_settings)

    def watch(self, replay):
        if not self.config['configured']:
            wx.MessageBox('Please first launch the application directly to complete configuration!', 'Error',
                          style=wx.ICON_ERROR)
            sys.exit(1)

        WatchReplayFrame(replay, self.config['repository'])
        self.app.MainLoop()

    def start_daemon(self):
        # nothing set, or league got moved/reinstalled somewhere else. go find it
        paths = self.config.get('game_paths') or []
        if not any(os.path.isfile(p) for p in paths):
            exe = core.detect_game_exe()
            if exe:
                self.config['game_paths'] = [exe]
                self.config.save()
        self.install_startup()
        self.set_low_priority()
        self.tray_icon = TrayItem([], on_click=self.open_settings)
        self.update_tray_menu()
        self.auto_scan_loop()
        self.update_check_loop()

    def install_startup(self):
        if not getattr(sys, 'frozen', False):
            return
        shell = win32com.client.Dispatch('WScript.Shell')
        startup_path = shell.SpecialFolders('Startup')
        path = os.path.join(startup_path, 'L-Recall.lnk')
        # remove the old shortcut too, otherwise two copies start at boot
        for name in ('League VCS.lnk', 'L-Recall.lnk'):
            old = os.path.join(startup_path, name)
            if os.path.exists(old):
                os.unlink(old)
        target = sys.executable
        wDir = os.path.dirname(target)

        shortcut = shell.CreateShortCut(path)
        shortcut.Targetpath = target
        shortcut.Arguments = '--background'
        shortcut.WorkingDirectory = wDir
        shortcut.IconLocation = sys.executable
        shortcut.save()

    def update_tray_menu(self):
        scan_option = ('Scanning...', None) if self.scan_lock.locked() else ('Scan Now', self.scan_game_directories)
        update_option = ((f"Update to {self.update_info['version']}", self.open_update), None) \
            if self.update_info else ()
        tray_menu = (
            *update_option,
            ('Browse Replays', self.open_settings),
            ('Watch Replay...', self.open_replay_picker),
            scan_option,
            None,
            ('Settings', self.open_settings),
            ('Exit', self.ask_exit)
        )
        self.tray_icon.menu_options = tray_menu

    def auto_scan_loop(self):
        threading.Thread(target=self._scan_game_directories, daemon=True, args=(False,)).start()
        self.auto_scan_interval = wx.CallLater(self.SCAN_INTERVAL, self.auto_scan_loop)

    def update_check_loop(self):
        if self.config.get('check_updates', True):
            threading.Thread(target=self._check_for_update, daemon=True).start()
        self.update_interval = wx.CallLater(self.UPDATE_INTERVAL, self.update_check_loop)

    def _check_for_update(self):
        release = updates.newer_than_running()
        self.update_info = release
        wx.CallAfter(self.update_tray_menu)
        # one balloon per version, and none for a version they already said no to
        if release and release['version'] not in (self._told_about, self.config.get('dismissed_update')):
            self._told_about = release['version']
            wx.CallAfter(self.tray_icon.ShowBalloon,
                         title=f"L-Recall {release['version']} is out",
                         text='Open L-Recall and hit Update now.')

    def notify(self, title, text):
        """tray popup, any thread"""
        if self.tray_icon:
            wx.CallAfter(self.tray_icon.ShowBalloon, title=title, text=text)

    def open_update(self, *_):
        # the built app updates itself from the banner. from source there's nothing to swap, so the page
        if getattr(sys, 'frozen', False):
            self.open_settings()
        else:
            webbrowser.open(self.update_info['url'] if self.update_info else updates.RELEASES_URL)

    def ask_exit(self, *_):
        """exit from the tray. if something's still running, ask, then stop it properly first"""
        frame = self.settings_frame
        try:
            busy = bool(frame and frame.busy())
        except RuntimeError:
            busy = False
        if not busy:
            return self.exit()
        answer = wx.MessageBox('L-Recall is still working on something. Stop it and quit?\n\n'
                               'Stopping is safe, anything half done gets tidied up next time.',
                               'L-Recall', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
        if answer != wx.YES:
            return
        frame.cancel_op()

        def wait(tries=40):
            try:
                still = frame.busy()
            except RuntimeError:
                still = False
            if still and tries:
                wx.CallLater(250, wait, tries - 1)
            else:
                self.exit()
        wait()

    def exit(self, *_):
        if self.tray_icon:
            self.tray_icon.RemoveIcon()
            self.tray_icon.Destroy()
        if self.auto_scan_interval:
            self.auto_scan_interval.Stop()
        if self.update_interval:
            self.update_interval.Stop()
        self.app.Destroy()
        sys.exit()

    def open_replay_picker(self, *_):
        if self.replay_frame:
            return
        self.set_high_priority()
        self.replay_frame = wx.FileDialog(None, 'Select a replay file',
                                          wildcard='Replay of LoL (*.rofl)|*.rofl', style=wx.FD_OPEN)
        if self.replay_frame.ShowModal() == wx.ID_CANCEL:
            self.replay_frame = None
            self.set_low_priority()
            return
        file = self.replay_frame.GetPath()
        self.replay_frame = None

        WatchReplayFrame(file, self.config['repository']).on_complete(self.set_low_priority)

    def show_window(self):
        """any thread"""
        if self.config['configured']:
            wx.CallAfter(self.open_settings)

    def open_settings(self, *_):
        if self.settings_frame:
            self.settings_frame.Iconize(False)
            self.settings_frame.Show(True)
            self.settings_frame.Raise()
            return

        self.set_high_priority()
        frame = self.settings_frame = SettingsFrame(self.config, quit_app=self.exit, notify=self.notify)
        frame.Show()
        frame.on_close(self._on_settings_closed)

    def _on_settings_closed(self):
        self.settings_frame = None
        self.set_low_priority()

    def scan_game_directories(self, *_):
        threading.Thread(target=self._scan_game_directories, daemon=True, args=(True,)).start()

    def _scan_game_directories(self, user_initiated=False):
        if winproc.game_running():
            if user_initiated:
                wx.CallAfter(self.tray_icon.ShowBalloon,
                             title='In game',
                             text="Patch scanning waits until you're out of game.")
            return

        if self.scan_lock.locked():
            if user_initiated:
                wx.CallAfter(self.tray_icon.ShowBalloon,
                             title='Already scanning.',
                             text='Try again in a bit.')
            return

        with self.scan_lock:
            wx.CallAfter(self.update_tray_menu)

            try:
                core.set_repo_path(self.config['repository'])
            except Exception as e:
                print(f'Error accessing repository: {e}')
                if user_initiated:
                    wx.CallAfter(self.tray_icon.ShowBalloon,
                                 title='Repository error',
                                 text=str(e))
                return

            to_update = []
            new_versions = set()
            for game_path in self.config['game_paths']:
                try:
                    version, is_new = core.can_update(game_path)
                except Exception as e:
                    print(f'Error checking {game_path}: {e}')
                    continue
                if is_new and version not in new_versions:
                    to_update.append((game_path, version))
                    new_versions.add(version)
                elif not is_new:
                    try:
                        core.top_up(os.path.dirname(game_path))
                    except Exception as e:
                        print(f'Top-up of {version} failed: {e}')

            if new_versions:
                wx.CallAfter(self.tray_icon.ShowBalloon,
                             title=f"Found new patch{'es' if len(new_versions) > 1 else ''}!",
                             text=f"Storing {', '.join(new_versions)} in the background.")
            elif user_initiated:
                wx.CallAfter(self.tray_icon.ShowBalloon,
                             title='No new patches found.',
                             text='Your repository is up to date!')

            for game_path, version in to_update:
                try:
                    core.add(os.path.dirname(game_path))
                except UserInputException as e:
                    print(f'Skipped {version}: {e}')  # still updating, or changed mid-copy. next scan retries
                    continue
                except Exception as e:
                    print(f'Skipped {version}: {e}')
                    self.notify(f"Couldn't store patch {version}", 'Check the log in the logs folder for details.')
                    continue
                wx.CallAfter(self.tray_icon.ShowBalloon,
                             title=f'Stored patch {version}',
                             text='Replays from this patch can now be watched.')

        wx.CallAfter(self.update_tray_menu)
