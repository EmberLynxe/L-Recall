import os
import re
import subprocess
import time
from typing import Optional

from large_vcs import LargeVCS

from league_vcs import signing, winproc
from league_vcs.exceptions import UserInputException
from league_vcs.parsers import ROFLParser, GameParser
from league_vcs.parsers.rofl import scan_replays

repo: Optional[LargeVCS] = None


def set_repo_path(path):
    global repo
    repo = LargeVCS.load_or_create(path)


def can_update(game_path):
    game = GameParser(game_path)
    version = game.version
    return version, version not in repo.list()


SETTLE_SECONDS = 10 * 60


def detect_game_exe():
    """find the league install from the riot client's own metadata"""
    meta = os.path.join(os.environ.get('PROGRAMDATA', r'C:\ProgramData'), 'Riot Games', 'Metadata',
                        'league_of_legends.live', 'league_of_legends.live.product_settings.yaml')
    candidates = []
    try:
        with open(meta, encoding='utf-8') as f:
            m = re.search(r'^product_install_full_path:\s*"?([^"\r\n]+)"?', f.read(), re.M)
        if m:
            candidates.append(os.path.join(m.group(1), 'Game', GameParser.executable_name))
    except OSError:
        pass
    candidates.append(os.path.join(r'C:\Riot Games\League of Legends\Game', GameParser.executable_name))
    for c in candidates:
        c = os.path.normpath(c)
        if os.path.isfile(c):
            return c
    return None


def _install_stamp(directory):
    """(newest mtime, count, bytes). if this changes riot touched something"""
    newest = count = total = 0
    for root, _, files in os.walk(directory):
        for f in files:
            try:
                st = os.stat(os.path.join(root, f))
            except OSError:
                continue
            newest = max(newest, st.st_mtime)
            count += 1
            total += st.st_size
    return newest, count, total


def client_settled(directory):
    """false while the riot client is still writing files"""
    return time.time() - _install_stamp(directory)[0] > SETTLE_SECONDS


class InstallChanged(UserInputException):
    pass


def add(directory):
    game_path = os.path.join(directory, GameParser.executable_name)
    version, is_new = can_update(game_path)
    if not is_new:
        raise ValueError(f'Already have patch {version}.')
    before = _install_stamp(directory)
    if time.time() - before[0] <= SETTLE_SECONDS:
        raise UserInputException(f'Patch {version} was updated in the last few minutes. '
                                 'Waiting for the update to finish before storing it.')

    print(f'Adding patch {version} to repository.')
    print(f'This may take a while.')
    repo.add(directory, version)

    # the riot client can start patching while we're reading. if anything changed,
    # throw the copy away, a patch made of two versions won't play
    if _install_stamp(directory) != before or GameParser(game_path).version != version:
        repo.drop(version)
        raise InstallChanged(f'The game updated while patch {version} was being stored, so that copy was '
                             'discarded. It will be stored again once the update finishes.')


def top_up(directory):
    """add files riot dropped in after we stored this version"""
    game_path = os.path.join(directory, GameParser.executable_name)
    version = GameParser(game_path).version
    before = _install_stamp(directory)
    if version not in repo.list() or time.time() - before[0] <= SETTLE_SECONDS:
        return 0
    old_patch, old_dups = repo.get_patch(version), repo.get_dups(version)
    added = repo.top_up(directory, version)
    if added and (_install_stamp(directory) != before or GameParser(game_path).version != version):
        with repo.lock:
            repo._save_dups(version, old_dups)
            repo._save_patch(version, old_patch)
            if repo.current() == version:
                repo.clean()
            repo.gc()
        raise InstallChanged(f'The game updated during the top-up of {version}; the change was rolled back.')
    return added


def needs_vanguard(version):
    # vanguard showed up in 14.9
    try:
        major, minor = (int(x) for x in version.split('.')[:2])
    except ValueError:
        return True
    return (major, minor) >= (14, 9)


def vanguard_preflight(version, warn_old=True):
    """(level, message), or (None, None) if we're good.

    14.9+ won't even start without vanguard. older ones don't need it, and running them
    with vanguard on is untested, so we can warn about that. every fix we suggest is riot's
    own way of doing it, we never touch vanguard ourselves"""
    from league_vcs import vanguard
    st = vanguard.status()
    new = needs_vanguard(version)
    if not st['installed']:
        if new:
            return 'block', (f'Patch {version} needs Riot Vanguard, which isn\'t installed. '
                             'Install it from the Riot Client, restart, then try again.')
        return None, None
    if new and not st['running']:
        if st['mode'] == 'on_demand':
            # pre-check: it starts with a riot game, so not running yet is normal
            return 'warn', (f'Patch {version} needs Riot Vanguard, which isn\'t running right now. '
                            'If the replay doesn\'t open, start League from the Riot Client first, then try again.')
        if st['mode'] == 'disabled':
            return 'block', (f'Patch {version} needs Riot Vanguard, but it\'s disabled. '
                             'Repair or reinstall it from the Riot Client, restart, then try again.')
        return 'block', (f'Patch {version} needs Riot Vanguard, which isn\'t running. '
                         'If you exited it from the tray, it comes back when you restart your PC.')
    if not new and st['running'] and warn_old:
        return 'warn', (f'Patch {version} is from before Vanguard. It should still play with Vanguard on, '
                        'but this combination is untested and may be unstable. If the replay crashes, '
                        'exit Vanguard from its tray icon (it comes back when you restart) and try again.')
    return None, None


def watch(replay):
    rofl = ROFLParser(replay)
    game_version = rofl.version
    if game_version not in repo.list():
        raise UserInputException(f'No game client found for patch {game_version}.')
    if winproc.game_running():
        raise UserInputException('A game is already running. Close it before watching a replay.')
    level, message = vanguard_preflight(game_version)
    if message:
        print('Note: ' + message)

    print(f'Preparing patch {game_version}...')
    repo.restore(game_version)

    game_path = repo.current_path(GameParser.executable_name)
    problem = signing.check_game_folder(repo.current_path(), GameParser.executable_name)
    if problem:
        raise UserInputException(f'Not launching patch {game_version}: {problem} '
                                 'The stored copy might be damaged, or it didn\'t come from Riot.')
    print('Launching replay...')
    # launch it the same way the riot client does. never touch the game process
    p = subprocess.Popen([game_path, replay], cwd=os.path.dirname(game_path))
    p.wait()


def list_replays(folders):
    return scan_replays(folders)
