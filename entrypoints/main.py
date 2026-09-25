"""
This is the long-running main entry point for the whole application. It runs at startup and gets minimized to the
system tray.
"""
import datetime
import multiprocessing
import os
import sys

multiprocessing.freeze_support()

import ctypes
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

from league_vcs.config import Config
from league_vcs.gui import GUI
from league_vcs.gui.utils import Singleton

if getattr(sys, 'frozen', False):
    _file_ = sys.executable
else:
    _file_ = __file__

dir_ = os.path.dirname(os.path.realpath(_file_))
os.makedirs(os.path.join(dir_, 'logs'), exist_ok=True)


# never let a weird character in a riot id kill the app just because it got printed
class DuplicateStream:
    def __init__(self, *streams):
        self.streams = [stream for stream in streams if stream is not None]

    def write(self, s: str) -> int:
        for stream in self.streams:
            try:
                stream.write(s)
                stream.flush()
            except (UnicodeError, OSError, ValueError):
                pass
        return len(s)

    def flush(self) -> None:
        for stream in self.streams:
            try:
                stream.flush()
            except (OSError, ValueError):
                pass


log_name = datetime.datetime.now().isoformat().replace(':', '-').replace('.', '-') + '.log'
log_file = open(os.path.join(dir_, 'logs', log_name), 'w', encoding='utf-8', errors='replace')
sys.__stdout__ = sys.stdout = DuplicateStream(log_file, sys.stdout)
sys.__stderr__ = sys.stderr = DuplicateStream(log_file, sys.stderr)

config_path = os.path.join(dir_, 'user_settings.json')
config = Config(config_path)

print('Loaded config', config)

if __name__ == '__main__':
    replay_args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not replay_args:
        singleton = Singleton()
        if singleton.should_close():
            if '--background' not in sys.argv:
                singleton.notify_existing()
            print('Already running; asked it to show its window.')
            sys.exit(0)

        gui = GUI(config)
        singleton.listen(lambda: gui.show_window())
        gui.start(show_window='--background' not in sys.argv)
    else:
        gui = GUI(config)
        gui.watch(replay_args[0])
