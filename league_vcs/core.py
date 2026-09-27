import ctypes
import json
import os
import re
import shutil
import stat
import subprocess
import time
from typing import Optional

from large_vcs import STILL_CLOSING, LargeVCS, _version_key, _write_json_atomic, progress, show

from league_vcs import signing, winproc
from league_vcs.exceptions import UserInputException
from league_vcs.parsers import ROFLParser, GameParser

repo: Optional[LargeVCS] = None
# settings the gui keeps in sync with the config
keep_prepared = 1
quick_start = True
use_my_settings = True
prepare_newest = True
drive = None  # what the storage's on. None works it out
game_exes = []  # from the config, checked before auto detect


def set_repo_path(path):
    global repo
    new = LargeVCS.load_or_create(path)
    new.keep_prepared = keep_prepared  # before anyone else can see it, or a restore could drop kept copies
    new.drive = drive
    repo = new


def replay_map(players):
    """which map a replay's on. the file doesn't say, so it's a guess from who played. None if no idea"""
    if len(players) > 10 or any(p.get('subteam') for p in players):
        return 'map30'  # arena
    if any(s in (30, 31, 32) for p in players for s in p.get('summoner_spells', ())):
        return 'map12'  # mark/snowball, or poro king's two. only ever on the abyss
    if len(players) == 10:
        return 'map11'
    return None


def quick_start_skip(version, players, everyone=False):
    """archives this replay won't touch: other champions, tft (it doesn't make replays) and maps the game
    isn't on. they go in as riot's empty placeholder. everyone=True skips every champion and guesses the
    rift, for getting a patch ready before there's a replay to go on. empty set if we can't tell anything,
    then everything gets built like normal"""
    playing = {p['champion'].lower() for p in players if p.get('champion')}
    if not playing and not everyone:
        return set()
    from league_vcs import assets
    try:
        champions = {c.lower() for c in assets.champion_names(assets.version_for('.'.join(version.split('.')[:2])))}
    except Exception:
        champions = set()
    if not playing <= champions:
        champions = set()  # someone we've never heard of, so all the champions get built
    game_map = 'map11' if everyone else replay_map(players)
    skip = set()
    for _, rel in repo.pairs(version):
        parts = rel.replace('\\', '/').lower().split('/')
        if len(parts) < 3 or not parts[-1].endswith('.wad.client'):
            continue
        name = parts[-1].split('.')[0]
        if parts[-2] == 'champions':
            if name in champions and name not in playing:
                skip.add(rel)
        elif parts[-2] == 'final':
            if name == 'companions' or name.startswith('tftset'):
                skip.add(rel)
        elif parts[-2] == 'shipping' and game_map and re.fullmatch(r'map\d+', name) and name != game_map:
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


class DiskFull(UserInputException):
    pass


def disk_full(e):
    """windows says it one of three ways"""
    return isinstance(e, OSError) and (e.errno == 28 or getattr(e, 'winerror', None) in (39, 112))


def _gb(n):
    return f'{n / 1024 ** 3:.1f} GB'


def _free():
    try:
        return shutil.disk_usage(repo.root).free
    except OSError:
        return 0


def restore(tag, **kw):
    """repo.restore, but with the reasons it can fail said in plain words: what's holding the prepared
    files, or the drive filling up"""
    try:
        return repo.restore(tag, **kw)
    except OSError as e:
        if not disk_full(e):
            raise
        drive = os.path.splitdrive(os.path.abspath(repo.root))[0] or repo.root
        raise DiskFull(f'Ran out of space on {drive} while getting patch {show(tag)} ready ({_gb(_free())} free). '
                       'A patch ready to play takes up to about 29 GB. Keep fewer patches ready in Settings, '
                       'delete patches you don\'t need on the Patches tab, or free up some space.') from None
    except ValueError as e:
        if str(e) != STILL_CLOSING or winproc.game_running():
            raise
        apps = [a for a in winproc.apps_using(repo.current_path()) if 'league of legends' not in a.lower()]
        if not apps:
            raise
        who = ', '.join(apps)
        raise UserInputException(f'{who} has some of the prepared game files open (usually the game\'s log '
                                 f'files), so the patch can\'t be switched. Close {who} or restart it, then try '
                                 'again. This only happens once, new patches keep their logs somewhere else.') from None


def clear_ready():
    """throw away every patch that's built to play, to get the space back. saved patches stay, and the next
    replay builds what it needs again. returns about how much it freed"""
    if winproc.game_running():
        raise UserInputException('League is running. Close the game (and any replay) first.')
    before = repo.space_used()['ready']
    try:
        repo.clear_prepared()
    except ValueError as e:
        if str(e) != STILL_CLOSING:
            raise
        raise UserInputException('Some of the ready files are still open, most likely because League only just '
                                 'closed. Give it a few seconds and try again.') from None
    return max(0, before - repo.space_used()['ready'])


def prepare_newest_patch():
    """most replays people open are from the last few days, so get the newest stored patch built before
    anyone clicks watch. only into a free keep ready slot, never pushing out a patch someone prepared
    themselves. true if it built something"""
    if not prepare_newest or winproc.game_running():
        return False
    # everything checked again once we've got the lock, a watch could have changed it all meanwhile
    with repo.lock:
        tags = repo.list()
        if not tags or winproc.game_running():
            return False
        newest = max(tags, key=_version_key)
        current, kept = repo.current(), repo.kept()
        if newest == current or newest in kept:
            return False
        if current is not None and len(kept) >= repo.keep_prepared - 1:
            return False
        print(f'Getting patch {show(newest)} ready ahead of time...')
        # no champions yet and only the rift, the replay adds its own in seconds. keeps this one small
        skip = quick_start_skip(newest, [], everyone=True) if quick_start else ()
        built = restore(newest, make_room=False, skip=skip)
        if not built:
            print('Not enough room to get it ready without clearing something, so it was left for now.')
        return bool(built)


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


class InstallChanged(UserInputException):
    pass


def add(directory, workers=None):
    game_path = os.path.join(directory, GameParser.executable_name)
    version, is_new = can_update(game_path)
    if not is_new:
        raise ValueError(f'Already have patch {show(version)}.')
    before = _install_stamp(directory)
    if time.time() - before[0] <= SETTLE_SECONDS:
        raise UserInputException(f'Patch {show(version)} was updated in the last few minutes. '
                                 'Waiting for the update to finish before storing it.')

    print(f'Storing patch {show(version)}. This may take a while.')
    try:
        repo.add(directory, version, workers=workers)
    except OSError as e:
        if not disk_full(e):
            raise
        drive = os.path.splitdrive(os.path.abspath(repo.root))[0] or repo.root
        raise DiskFull(f'Ran out of space on {drive} while storing patch {show(version)} ({_gb(_free())} free). '
                       'The first patch takes about 21 to 24 GB, after that usually well under 1 GB each. '
                       'Free up some space or move storage to a bigger drive in Settings.') from None

    # the riot client can start patching while we're reading. if anything changed,
    # throw the copy away, a patch made of two versions won't play
    if _install_stamp(directory) != before or GameParser(game_path).version != version:
        repo.drop(version)
        raise InstallChanged(f'The game updated while patch {show(version)} was being stored, so that copy was '
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
        raise InstallChanged(f'The game updated while patch {show(version)} was being topped up; the change was rolled back.')
    return added


def vanguard_preflight(version):
    """(level, message), or (None, None) if we're good.

    replays don't need vanguard. what stops them is vanguard being loaded when the game starts, it refuses
    game copies the riot client didn't launch (replaybook ran into the same thing, exiting vanguard is what
    fixed it for their users, 14.9+ replays included). pre-check keeps it unloaded until a game starts, so
    that just works. anything else, vanguard has to be exited first. we only ever tell people, never touch it"""
    from league_vcs import vanguard
    st = vanguard.status()
    if not st['running']:
        return None, None
    if st['mode'] == 'boot':
        return 'warn', PRECHECK_OFF + TWO_WAYS + _other_options(version)
    return 'warn', ('Riot Vanguard is running right now, probably from a game or the League client, and while it\'s '
                    'running it blocks replays. If this one doesn\'t start, exit Vanguard from its tray icon and try '
                    'again. With Pre-Check on it starts again by itself next time you play.' + _other_options(version))


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


def _needs_everything():
    try:
        with open(repo.repo_path('needs-everything.json'), encoding='utf-8') as f:
            names = json.load(f)
        return [n for n in names if isinstance(n, str)]
    except (OSError, ValueError, TypeError):
        return []


def needs_everything(replay):
    """replays that only started once the whole patch was there. a mode the map guess gets wrong, or
    ultimate spellbook pulling in everyone's ults. they get the whole patch from then on"""
    return os.path.basename(replay).lower() in _needs_everything()


def _remember_needs_everything(replay):
    name = os.path.basename(replay).lower()
    names = [n for n in _needs_everything() if n != name][-199:] + [name]
    try:
        _write_json_atomic(repo.repo_path('needs-everything.json'), names)
    except OSError:
        pass


def watch(replay, before_launch=None, on_retry=None, ask_retry=None):
    rofl = ROFLParser(replay)
    game_version = client_for(rofl.version)
    if game_version is None:
        raise UserInputException(f'Patch {show(rofl.version)} isn\'t stored, so this replay can\'t be played.')
    if winproc.game_running():
        raise UserInputException('A game is already running. Close it before watching a replay.')
    level, message = vanguard_preflight(game_version)
    if message:
        print('Note: ' + message)

    print(f'Preparing patch {show(game_version)}...')
    # only this replay's champions and map are built, the last one's go back to placeholders. keeps the
    # prepared patch around 6 GB instead of 29
    lean = quick_start and not needs_everything(replay)
    skip = quick_start_skip(game_version, rofl.info.players) if lean else ()
    ran, repair, placeholders = _launch(game_version, replay, before_launch,
                                        lambda: restore(game_version, skip=skip, trim=True))
    if placeholders and (ran < QUICK_START_GRACE or repair):
        if winproc.game_running():
            # closed it to go play, most likely. not the time for a rebuild
            print('The replay closed straight away, but a game is running now, so it was left there.')
            return
        # quick start left something out the game wanted, or someone closed it. a repair note from the game
        # means it was missing something for sure. otherwise ask, a rebuild nobody wanted is a long wait
        if not repair and ask_retry and not ask_retry():
            print('The replay closed straight away. Left it there.')
            return
        print('The replay closed straight away, most likely because it needed something that was left out. '
              'Building the rest of the patch and trying again...')
        if on_retry:
            on_retry()
        ran, repair, _ = _launch(game_version, replay, before_launch, lambda: restore(game_version))
        if ran >= QUICK_START_GRACE and not repair:
            _remember_needs_everything(replay)


# a replay that closes this fast after a quick start didn't really start
QUICK_START_GRACE = 15


def _repair_notes():
    """where the game leaves SOFT_REPAIR when it's missing something (seen next to the game folder)"""
    return [os.path.join(repo.root, 'SOFT_REPAIR'), repo.current_path('SOFT_REPAIR')]


def _launch(game_version, replay, before_launch, prepare):
    """build with prepare(), then start the game, all under the storage lock so nothing (like getting the
    newest patch ready in the background) can swap the folder out in between. the lock goes once the game
    is running. returns (seconds the game ran, whether it left a repair note, whether placeholders were used)"""
    with repo.lock:
        # again now we've got the lock. a second watch could have started one while this one waited
        if winproc.game_running():
            raise UserInputException('A game is already running. Close it before watching a replay.')
        prepare()
        placeholders = bool(repo.stubs())
        p, started = _start(game_version, replay, before_launch)
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
    return ran, repair, placeholders


def _start(game_version, replay, before_launch):
    game_path = repo.current_path(GameParser.executable_name)
    problem = signing.check_game_folder(repo.current_path(), GameParser.executable_name)
    if problem:
        raise UserInputException(f'Not launching patch {show(game_version)}: {problem} '
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
    # the game's logs go outside the prepared folder, so overlay apps reading them can't lock it
    repo.redirect_logs()
    repo.detach_game_writable()
    repo.tidy_game_logs()
    for note in _repair_notes():
        try:
            os.unlink(note)  # an old one from last time would look like this launch failed
        except OSError:
            pass
    print(f'Launching replay on patch {show(game_version)}...')
    # launch it the same way the riot client does. never touch the game process
    started = time.monotonic()
    try:
        # minus our webview settings, those are for our window, not the game
        env = {k: v for k, v in os.environ.items() if not k.startswith('WEBVIEW2_')}
        p = subprocess.Popen([game_path, replay], cwd=os.path.dirname(game_path), env=env)
    except PermissionError:
        raise UserInputException(launch_refused(game_version)) from None
    return p, started


PRECHECK_OFF = ('Vanguard Pre-Check is off on this PC, so Vanguard is always on, and that blocks replays '
                'L-Recall opens.')
TWO_WAYS = (' Either turn on Pre-Check, or exit Vanguard from its tray icon before watching. It comes back when you '
            'restart your PC, and you\'ll need that restart before playing League again.')


def _other_options(version):
    if installed_version() == version:
        return ' This one\'s on the patch you have installed, so you can also watch it from the League client.'
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
        return (f'Windows wouldn\'t start patch {show(version)} (access denied). Antivirus is the usual '
                'suspect, check whether it blocked League of Legends.exe in the storage folder.')
    msg = (f'Windows wouldn\'t start patch {show(version)}. Riot Vanguard was running and blocked it, and L-Recall '
           'won\'t try to get around that.')
    if st['mode'] == 'boot':
        return msg + ' ' + PRECHECK_OFF + TWO_WAYS + _other_options(version)
    return msg + ' Exit Vanguard from its tray icon and try again.' + _other_options(version)


