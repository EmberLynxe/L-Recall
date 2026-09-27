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


class Redacted:
    """what goes in the log file. people attach these to github issues, so your windows username (it's in
    every path, sometimes in windows' short JOHNSM~1 form too) comes out"""

    def __init__(self, stream):
        import re
        self.stream = stream
        home = os.path.expanduser('~')
        homes = {home}
        try:
            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.kernel32.GetShortPathNameW(home, buf, 260):
                homes.add(buf.value)
        except (OSError, AttributeError):
            pass
        names = {os.path.basename(h) for h in homes} - {''}
        paths = '|'.join(re.escape(h).replace(r'\\', r'[\\/]') for h in sorted(homes, key=len, reverse=True))
        self.path_re = re.compile(paths, re.I)
        self.name_re = re.compile('|'.join(re.escape(n) for n in sorted(names, key=len, reverse=True)), re.I) if names else None

    def write(self, s):
        s = self.path_re.sub('%USERPROFILE%', s)
        if self.name_re:
            s = self.name_re.sub('<you>', s)
        return self.stream.write(s)

    def flush(self):
        return self.stream.flush()


def _tidy_logs(folder, days=14):
    """logs older than two weeks go"""
    cutoff = datetime.datetime.now().timestamp() - days * 86400
    try:
        for e in os.scandir(folder):
            if e.is_file() and e.name.endswith('.log') and e.stat().st_mtime < cutoff:
                try:
                    os.unlink(e.path)
                except OSError:
                    pass
    except OSError:
        pass


_tidy_logs(os.path.join(dir_, 'logs'))
log_name = datetime.datetime.now().isoformat().replace(':', '-').replace('.', '-') + '.log'
log_file = Redacted(open(os.path.join(dir_, 'logs', log_name), 'w', encoding='utf-8', errors='replace'))
sys.__stdout__ = sys.stdout = DuplicateStream(log_file, sys.stdout)
sys.__stderr__ = sys.stderr = DuplicateStream(log_file, sys.stderr)

config_path = os.path.join(dir_, 'user_settings.json')
config = Config(config_path)

# not the whole config. it has your riot ids and folders in it, and this ends up in a log file
print(f"Started, {'set up' if config.get('configured') else 'not set up yet'}, "
      f"{len(config.get('game_paths') or [])} game install(s), {len(config.get('replay_folders') or [])} replay folder(s)")

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
