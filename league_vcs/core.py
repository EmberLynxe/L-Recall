import ctypes
import json
import os
import re
import subprocess
import time
from typing import Optional

from large_vcs import LargeVCS, progress

from league_vcs import signing, winproc
from league_vcs.exceptions import UserInputException
from league_vcs.parsers import ROFLParser, GameParser
from league_vcs.parsers.rofl import scan_replays

repo: Optional[LargeVCS] = None
# settings the gui keeps in sync with the config
keep_prepared = 2
quick_start = False


def set_repo_path(path):
    global repo
    repo = LargeVCS.load_or_create(path)
    repo.keep_prepared = keep_prepared


def quick_start_skip(version, players):
    """champion archives nobody in this replay uses. they go in as riot's empty placeholder.
    empty set if we can't be sure, then everything gets built like normal"""
    playing = {p['champion'].lower() for p in players if p.get('champion')}
    if not playing:
        return set()
    from league_vcs import assets
    try:
        champions = {c.lower() for c in assets.champion_names(assets.version_for('.'.join(version.split('.')[:2])))}
    except Exception:
        return set()
    if not champions or not playing <= champions:
        return set()
    skip = set()
    for _, rel in repo.pairs(version):
        parts = rel.replace('\\', '/').split('/')
        if len(parts) > 1 and parts[-2].lower() == 'champions' and rel.lower().endswith('.wad.client'):
            name = parts[-1].split('.')[0].lower()
            if name in champions and name not in playing:
                skip.add(rel)
    return skip


def can_update(game_path):
    game = GameParser(game_path)
    version = game.version
    return version, version not in repo.list()


SETTLE_SECONDS = 10 * 60


def _install_roots():
    """league folders according to riot, windows, and the usual spots"""
    riot = os.path.join(os.environ.get('PROGRAMDATA', r'C:\ProgramData'), 'Riot Games')
    try:
        with open(os.path.join(riot, 'Metadata', 'league_of_legends.live',
                               'league_of_legends.live.product_settings.yaml'), encoding='utf-8') as f:
            m = re.search(r'^product_install_full_path:\s*"?([^"\r\n]+)"?', f.read(), re.M)
        if m:
            yield m.group(1)
    except OSError:
        pass
    try:
        with open(os.path.join(riot, 'RiotClientInstalls.json'), encoding='utf-8') as f:
            yield from (p for p in json.load(f).get('associated_client', {}) if 'league of legends' in p.lower())
    except (OSError, ValueError, AttributeError):
        pass
    import winreg
    key = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\Riot Game league_of_legends.live'
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, key) as k:
                yield winreg.QueryValueEx(k, 'InstallLocation')[0]
        except OSError:
            pass
    # fixed drives only, a disconnected network drive would hang this for ages
    drives = ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        root = f'{chr(65 + i)}:\\'
        if drives >> i & 1 and ctypes.windll.kernel32.GetDriveTypeW(root) == 3:
            yield os.path.join(root, 'Riot Games', 'League of Legends')


def detect_game_exes():
    """every live league install on this pc, best guess first. pbe is skipped, nobody wants those patches"""
    found, seen = [], set()
    for root in _install_roots():
        root = os.path.normpath(root)
        if 'pbe' in os.path.basename(root).lower():
            continue
        exe = os.path.join(root, 'Game', GameParser.executable_name)
        if os.path.normcase(exe) not in seen and os.path.isfile(exe):
            seen.add(os.path.normcase(exe))
            found.append(exe)
    return found


def detect_game_exe():
    found = detect_game_exes()
    return found[0] if found else None


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


def add(directory, workers=None):
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
    repo.add(directory, version, workers=workers)

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

    14.9+ won't start without vanguard. older ones don't need it, and running them with
    vanguard on is untested, so we can warn about that"""
    from league_vcs import vanguard
    st = vanguard.status()
    new = needs_vanguard(version)
    if not st['installed']:
        if new:
            return 'block', (f'Patch {version} needs Riot Vanguard, which isn\'t installed. '
                             'Install it from the Riot Client, restart, then try again.')
        return None, None
    if st['running'] and st['mode'] == 'boot':
        # loaded before the game starts, so it refuses copies the riot client didn't launch
        if not new:
            return 'warn', ('Riot Vanguard is running and starts with Windows on this PC, so it will block this '
                            'replay. Exit Vanguard from its tray icon first, then watch. You\'ll need to restart '
                            'your PC before you can play League again.')
        msg = (f'Riot Vanguard starts with Windows on this PC, so it will block this replay. Patch {version} '
               'also needs Vanguard, so exiting it won\'t help either. Replays from 14.9 on only play in L-Recall '
               'with Vanguard\'s Pre-Check turned on.')
        if installed_version() == version:
            msg += ' This one\'s on the patch you have installed, so you can watch it from the League client.'
        return 'warn', msg
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


def watch(replay, before_launch=None):
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
    repo.restore(game_version, skip=quick_start_skip(game_version, rofl.info.players) if quick_start else ())

    game_path = repo.current_path(GameParser.executable_name)
    problem = signing.check_game_folder(repo.current_path(), GameParser.executable_name)
    if problem:
        raise UserInputException(f'Not launching patch {game_version}: {problem} '
                                 'The stored copy might be damaged, or it didn\'t come from Riot.')
    progress.check()
    if before_launch:
        before_launch()
    print(f'Launching replay on patch {game_version}...')
    # launch it the same way the riot client does. never touch the game process
    try:
        p = subprocess.Popen([game_path, replay], cwd=os.path.dirname(game_path))
    except PermissionError:
        raise UserInputException(launch_refused(game_version)) from None
    p.wait()


def installed_version():
    live = detect_game_exe()
    try:
        return GameParser(live).version if live else None
    except Exception:
        return None


def launch_refused(version):
    """windows said no to starting the game. with vanguard loaded from boot that's vanguard
    refusing a copy the riot client didn't start. we don't try to get past that, ever"""
    from league_vcs import vanguard
    st = vanguard.status()
    if not st['running']:
        return (f'Windows wouldn\'t start patch {version} (access denied). Antivirus is the usual '
                'suspect, check whether it blocked League of Legends.exe in the storage folder.')
    msg = (f'Windows wouldn\'t start patch {version}. Riot Vanguard is running and blocked it, '
           'which it does to game copies the Riot Client didn\'t start. L-Recall won\'t try to get around that.')
    if installed_version() == version:
        return msg + ' This replay is on the patch you have installed, so open it from the League client instead.'
    if not needs_vanguard(version):
        return msg + (' This patch is from before Vanguard, so you can exit Vanguard from its tray icon '
                      '(it comes back when you restart your PC) and try again.')
    if st['mode'] == 'boot':
        return msg + (' Vanguard is set to start with Windows on this PC. Replays on 14.9 and newer only play '
                      'here when Vanguard is in Pre-Check mode, where it starts with a Riot game instead.')
    return msg


def list_replays(folders):
    return scan_replays(folders)
