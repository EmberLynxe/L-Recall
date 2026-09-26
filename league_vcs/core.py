import ctypes
import json
import os
import re
import shutil
import stat
import subprocess
import time
from typing import Optional

from large_vcs import LargeVCS, _version_key, progress

from league_vcs import signing, winproc
from league_vcs.exceptions import UserInputException
from league_vcs.parsers import ROFLParser, GameParser
from league_vcs.parsers.rofl import scan_replays

repo: Optional[LargeVCS] = None
# settings the gui keeps in sync with the config
keep_prepared = 2
quick_start = False
use_my_settings = True
prepare_newest = True
game_exes = []  # from the config, checked before auto detect


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


def client_for(version, tags=None):
    """which stored build plays a replay from this version. the exact build if it's stored, otherwise the
    newest build of the same patch (16.19.820 on 16.19.821), which is what the league client does too.
    None if nothing from that patch is stored"""
    tags = repo.list() if tags is None else tags
    if version in tags:
        return version
    patch = version.split('.')[:2]
    same = [t for t in tags if t.split('.')[:2] == patch]
    return max(same, key=_version_key) if same else None


def prepare_newest_patch():
    """most replays people open are from the last few days, so get the newest stored patch built before
    anyone clicks watch. only into a free keep ready slot, never pushing out a patch someone prepared
    themselves. true if it built something"""
    tags = repo.list()
    if not prepare_newest or not tags:
        return False
    newest = max(tags, key=_version_key)
    current, kept = repo.current(), repo.kept()
    if newest == current or newest in kept:
        return False
    if current is not None and len(kept) >= repo.keep_prepared - 1:
        return False
    print(f'Getting patch {newest} ready ahead of time...')
    repo.restore(newest)
    return True


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
            repo._save_dups(version, old_dups, keep_file=True)
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

    14.9+ won't start without vanguard. with pre-check off it's always on and blocks replays.
    older patches don't need it, and running them with it on is untested"""
    from league_vcs import vanguard
    st = vanguard.status()
    new = needs_vanguard(version)
    if not st['installed']:
        if new:
            return 'block', (f'Patch {version} needs Riot Vanguard, which isn\'t installed. '
                             'Install it from the Riot Client, restart, then try again.')
        return None, None
    if st['running'] and st['mode'] == 'boot':
        return 'warn', PRECHECK_OFF + ' Turn on Pre-Check to watch replays.' + _other_options(version)
    if new and not st['running']:
        if st['mode'] == 'on_demand':
            # pre-check switches it on and off by itself. not running yet is how it's meant to be
            return None, None
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


# graphics, camera, hotkeys and everything else. the client keeps them synced to your account and
# writes them here, next to the live game
SETTINGS_FILES = ('game.cfg', 'input.ini', 'PersistedSettings.json')


def copy_settings(game_folder):
    """your league settings into the prepared game, so a replay doesn't open on defaults. only ever
    reads the live install. the game looks in Config inside its own folder when started directly.
    returns how many files it copied"""
    for exe in list(game_exes) + detect_game_exes():
        source = os.path.join(os.path.dirname(os.path.dirname(exe)), 'Config')
        if os.path.isfile(os.path.join(source, 'game.cfg')):
            break
    else:
        return 0
    target = os.path.join(game_folder, 'Config')
    os.makedirs(target, exist_ok=True)
    copied = 0
    for name in SETTINGS_FILES:
        src, dst = os.path.join(source, name), os.path.join(target, name)
        if not os.path.isfile(src):
            continue
        if os.path.exists(dst):
            os.chmod(dst, stat.S_IWRITE)  # people make PersistedSettings read only to stop it resetting
            os.unlink(dst)
        shutil.copyfile(src, dst)  # not copy2, that'd carry the read only flag over
        copied += 1
    return copied


def watch(replay, before_launch=None):
    rofl = ROFLParser(replay)
    game_version = client_for(rofl.version)
    if game_version is None:
        raise UserInputException(f'No game client found for patch {rofl.version}.')
    if game_version != rofl.version:
        print(f'Build {rofl.version} isn\'t stored, so this plays on {game_version} from the same patch, '
              'like the League client does.')
    if winproc.game_running():
        raise UserInputException('A game is already running. Close it before watching a replay.')
    level, message = vanguard_preflight(game_version)
    if message:
        print('Note: ' + message)

    print(f'Preparing patch {game_version}...')
    repo.restore(game_version, skip=quick_start_skip(game_version, rofl.info.players) if quick_start else ())
    placeholders = bool(repo.stubs())
    ran, repair = _launch(game_version, replay, before_launch)
    if placeholders and (ran < QUICK_START_GRACE or repair):
        # quick start left something out the game wanted. fill everything in and go again, once
        print('The replay closed straight away, most likely because Quick start left out something it '
              'needed. Building the rest of the patch and trying again...')
        repo.restore(game_version)
        _launch(game_version, replay, before_launch)


# a replay that closes this fast after a quick start didn't really start
QUICK_START_GRACE = 15


def _repair_notes():
    """where the game leaves SOFT_REPAIR when it's missing something (seen next to the game folder)"""
    return [os.path.join(repo.root, 'SOFT_REPAIR'), repo.current_path('SOFT_REPAIR')]


def _launch(game_version, replay, before_launch):
    """returns (seconds the game ran, whether it left a repair note)"""
    game_path = repo.current_path(GameParser.executable_name)
    problem = signing.check_game_folder(repo.current_path(), GameParser.executable_name)
    if problem:
        raise UserInputException(f'Not launching patch {game_version}: {problem} '
                                 'The stored copy might be damaged, or it didn\'t come from Riot.')
    if use_my_settings:
        try:
            if copy_settings(repo.current_path()):
                print('Using your League settings.')
        except OSError as e:
            print(f"Couldn't copy your League settings ({e}), so the replay starts with the defaults.")
    progress.check()
    if before_launch:
        before_launch()
    for note in _repair_notes():
        try:
            os.unlink(note)  # an old one from last time would look like this launch failed
        except OSError:
            pass
    print(f'Launching replay on patch {game_version}...')
    # launch it the same way the riot client does. never touch the game process
    started = time.monotonic()
    try:
        # minus our webview settings, those are for our window, not the game
        env = {k: v for k, v in os.environ.items() if not k.startswith('WEBVIEW2_')}
        p = subprocess.Popen([game_path, replay], cwd=os.path.dirname(game_path), env=env)
    except PermissionError:
        raise UserInputException(launch_refused(game_version)) from None
    p.wait()
    ran = time.monotonic() - started
    repair = False
    for note in _repair_notes():
        if os.path.exists(note):
            repair = True
            try:
                with open(note, encoding='utf-8', errors='replace') as f:
                    print(f'The game reported a problem: {f.read(300).strip()}')
                os.unlink(note)
            except OSError:
                pass
    return ran, repair


PRECHECK_OFF = ('Vanguard Pre-Check is off on this PC, so Vanguard is always on, and that blocks replays '
                'L-Recall opens.')


def _other_options(version):
    if installed_version() == version:
        return ' This one\'s on the patch you have installed, so you can watch it from the League client instead.'
    if not needs_vanguard(version):
        # nobody's checked this one properly yet, so don't promise anything
        return (f' Patch {version} is from before Vanguard, so exiting Vanguard from its tray icon might let it play. '
                'That\'s untested. You\'d need to restart your PC before playing League again.')
    return ''


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
    msg = (f'Windows wouldn\'t start patch {version}. Riot Vanguard blocked it, and L-Recall won\'t try to '
           'get around that.')
    if st['mode'] == 'boot':
        msg += ' ' + PRECHECK_OFF + ' Turn on Pre-Check to watch replays.'
    return msg + _other_options(version)


def list_replays(folders):
    return scan_replays(folders)
