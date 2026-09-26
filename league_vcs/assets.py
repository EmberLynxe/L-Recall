"""data dragon icon cache. per patch because old items look different"""
import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from large_vcs.progress import step, track

CDN = 'https://ddragon.leagueoflegends.com'
_BASE = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
ROOT = os.path.join(_BASE, 'L-Recall', 'ddragon')
_OLD_ROOT = os.path.join(_BASE, 'LeagueVCS', 'ddragon')

if os.path.isdir(_OLD_ROOT) and not os.path.exists(ROOT):
    try:
        os.makedirs(os.path.dirname(ROOT), exist_ok=True)
        shutil.move(_OLD_ROOT, ROOT)
    except OSError:
        pass

_lock = threading.Lock()
_versions = None
_failed_at = 0
RETRY_AFTER = 5 * 60


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={'User-Agent': 'l-recall'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.{threading.get_ident()}.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, path)


def versions():
    """patch list. the saved copy right away if there is one, the fresh one gets fetched in the
    background. waiting on riot's cdn here held up the whole replay list on every launch"""
    global _versions
    with _lock:
        if _versions is not None:
            return _versions
        if time.time() - _failed_at < RETRY_AFTER:
            return []
        try:
            with open(os.path.join(ROOT, 'versions.json')) as f:
                saved = json.load(f)
        except (OSError, ValueError):
            saved = None
        if saved:
            _versions = saved
            threading.Thread(target=_fetch_versions, daemon=True).start()
            return saved
    return _fetch_versions()


def _fetch_versions():
    global _versions, _failed_at
    try:
        found = [v for v in json.loads(_get(CDN + '/api/versions.json')) if v[:1].isdigit()]
        _save(os.path.join(ROOT, 'versions.json'), json.dumps(found).encode())
    except Exception:
        with _lock:
            # offline. without this every icon lookup sat through the timeout again
            _failed_at = time.time()
            return _versions or []
    with _lock:
        _versions = found
    return found


def version_for(patch):
    """'15.14' -> '15.14.1'. newest if we can't find it"""
    vs = versions()
    for v in vs:
        if v == patch or v.startswith(patch + '.'):
            return v
    return vs[0] if vs else None


# champion names come out of replay files, so they don't get anywhere near a path unchecked
_SAFE_VERSION = re.compile(r'\d+\.\d+[\w.]*')
_SAFE_NAME = re.compile(r"[A-Za-z0-9_' .&-]{1,64}")


def _safe(version, name=None):
    if not isinstance(version, str) or not _SAFE_VERSION.fullmatch(version) or '..' in version:
        raise ValueError(f'bad version {version!r}')
    if name is not None and (not _SAFE_NAME.fullmatch(name) or '..' in name):
        raise ValueError(f'bad name {name!r}')


def _ok(version, _kind, name):
    try:
        _safe(version, name)
        return True
    except ValueError:
        return False


def local_path(version, kind, name):
    _safe(version, name)
    return os.path.join(ROOT, version, kind, f'{name}.png')


# wukong is 'MonkeyKing' internally
CHAMP_FIX = {'Wukong': 'MonkeyKing', 'FiddleSticks': 'Fiddlesticks'}


def _fetch(version, kind, name):
    dst = local_path(version, kind, name)
    if os.path.exists(dst):
        return False
    try:
        data = _get(f'{CDN}/cdn/{version}/img/{kind}/{name}.png')
    except Exception:
        return False
    same = _same_icon_elsewhere(version, kind, name, data)
    if same:
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.link(same, dst)
            return True
        except OSError:
            pass
    _save(dst, data)
    return True


def _same_icon_elsewhere(version, kind, name, data):
    """most icons don't change between patches. if another patch's folder already has this exact
    picture, share that file instead of keeping one more copy"""
    try:
        others = [v for v in os.listdir(ROOT) if v != version and v[:1].isdigit()]
    except OSError:
        return None
    for v in others:
        path = os.path.join(ROOT, v, kind, f'{name}.png')
        try:
            if os.path.getsize(path) == len(data):
                with open(path, 'rb') as f:
                    if f.read() == data:
                        return path
        except OSError:
            continue
    return None


def share_duplicates():
    """one pass over the cache: identical icons in different patch folders become one file.
    returns how many copies got merged"""
    groups = {}
    for v in (d for d in os.listdir(ROOT) if d[:1].isdigit()) if os.path.isdir(ROOT) else ():
        for kind in ('champion', 'item', 'spell'):
            folder = os.path.join(ROOT, v, kind)
            if os.path.isdir(folder):
                for f in os.listdir(folder):
                    groups.setdefault((kind, f), []).append(os.path.join(folder, f))
    merged = 0
    for paths in groups.values():
        if len(paths) < 2:
            continue
        seen = {}
        for path in paths:
            try:
                st = os.stat(path)
                with open(path, 'rb') as f:
                    key = hashlib.sha256(f.read()).digest()
            except OSError:
                continue
            first = seen.setdefault(key, (path, st))
            if first[0] == path or os.path.samestat(first[1], st):
                continue
            tmp = path + '.link.tmp'
            try:
                os.link(first[0], tmp)
                os.replace(tmp, path)
                merged += 1
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    return merged


def ensure(version, champions=(), items=(), spells=()):
    """download whatever's missing. returns how many"""
    jobs = [(version, 'champion', c) for c in set(champions) if c]
    jobs += [(version, 'item', str(i)) for i in set(items) if i]
    jobs += [(version, 'spell', s) for s in set(spells) if s]
    jobs = [j for j in jobs if _ok(*j) and not os.path.exists(local_path(*j))]
    if not jobs:
        return 0
    got = 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        futures = [ex.submit(_fetch, *j) for j in jobs]
        for done, f in enumerate(as_completed(futures), 1):
            got += f.result()
            step(done, len(jobs), f'Downloading icons for {version}')
    return got


def _catalog(version, name):
    _safe(version)
    path = os.path.join(ROOT, version, f'{name}.json')
    try:
        with open(path, 'rb') as f:
            return json.loads(f.read())
    except (OSError, ValueError):
        data = _get(f'{CDN}/cdn/{version}/data/en_US/{name}.json')
        _save(path, data)
        return json.loads(data)


# about a thousand small pngs, only the first time
def prefetch_latest():
    """every champ/item/spell icon for the newest patch"""
    vs = _fetch_versions()  # background anyway, so ask for the real newest
    if not vs:
        return 0
    v = vs[0]
    try:
        champs = list(_catalog(v, 'champion')['data'])
        items = list(_catalog(v, 'item')['data'])
        spells = list(_catalog(v, 'summoner')['data'])
    except Exception:
        return 0
    table = runes(v)
    ensure_rune_icons(table, list(table))
    return ensure(v, champs, items, spells)


def download_for_replays(replays, log=print):
    """newest patch in full + exactly what each replay's own patch needs"""
    vs = versions()
    if not vs:
        log("Couldn't reach Data Dragon.")
        return
    log(f'Patch {vs[0]}: all champions, items and spells...')
    log(f'  {prefetch_latest()} new files')

    groups = {}
    for r in replays:
        v = version_for(r.patch_short)
        if not v or v == vs[0]:
            continue
        champs, items, spells, perks = groups.setdefault(v, (set(), set(), set(), set()))
        for p in r.players:
            champs.add(CHAMP_FIX.get(p['champion'], p['champion']))
            items.update(i for i in p['items'] if i)
            spells.update(s for s in p['summoner_spells'] if s)
            rn = p.get('runes') or {}
            perks.update(i for i in rn.get('perks', []) + [rn.get('primary'), rn.get('secondary')] if i)

    def one(v):
        champs, items, spell_ids, perks = groups[v]
        ensure_rune_icons(runes(v), perks)
        try:
            by_key = {d['key']: name for name, d in _catalog(v, 'summoner')['data'].items()}
        except Exception:
            by_key = {}
        names = [by_key[str(s)] for s in spell_ids if str(s) in by_key]
        return v, len(champs), len(items), ensure(v, champs, items, names)

    order = sorted(groups, key=_vkey, reverse=True)
    log(f'{len(order)} older patches used by your replays...')
    # one patch at a time, each one already downloads 16 at once. and it keeps progress on this thread
    for v in order:
        v, nc, ni, got = one(v)
        log(f'  {v}: {nc} champions, {ni} items, {got} new files')
    log('Done.')


def champion_names(version):
    """data dragon ids, which is also what the champion archives are called (MonkeyKing, not Wukong)"""
    return list(_catalog(version, 'champion')['data'])


def champion_display_names(version):
    """id -> the name people actually know (MonkeyKing -> Wukong). only what's already on disk,
    the page has its own list for when this is empty"""
    try:
        _safe(version)
        with open(os.path.join(ROOT, version, 'champion.json'), 'rb') as f:
            data = json.loads(f.read())['data']
        return {k.lower(): v['name'] for k, v in data.items() if v.get('name') and v['name'] != k}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def _vkey(v):
    return [int(x) if x.isdigit() else 0 for x in v.split('.')]


def runes(version):
    """rune id -> name/desc/icon"""
    try:
        data = _catalog(version, 'runesReforged')
    except Exception:
        return {}
    out = {}
    for style in data:
        out[style['id']] = {'name': style['name'], 'icon': style['icon'], 'desc': ''}
        for slot in style['slots']:
            for r in slot['runes']:
                desc = re.sub(r'<[^>]+>', '', r.get('shortDesc', ''))
                out[r['id']] = {'name': r['name'], 'icon': r['icon'], 'desc': desc}
    return out


def ensure_rune_icons(table, ids):
    def fetch(icon):
        parts = icon.split('/')
        if not all(_SAFE_NAME.fullmatch(p) and p.strip('.') for p in parts):
            return
        dst = os.path.join(ROOT, 'img', *parts)
        if os.path.exists(dst):
            return
        try:
            _save(dst, _get(f'{CDN}/cdn/img/{icon}'))
        except Exception:
            pass
    icons = {table[i]['icon'] for i in ids if i in table}
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(fetch, icons))


def refresh():
    return _fetch_versions()


def cache_info():
    files = size = 0
    patches = []
    if os.path.isdir(ROOT):
        for entry in os.scandir(ROOT):
            if entry.is_dir() and entry.name[:1].isdigit():
                patches.append(entry.name)
        for root, _, names in os.walk(ROOT):
            for n in names:
                files += 1
                try:
                    size += os.path.getsize(os.path.join(root, n))
                except OSError:
                    pass
    patches.sort(key=_vkey, reverse=True)
    return {'files': files, 'bytes': size, 'patches': patches, 'path': ROOT}


def clear():
    global _versions
    files = [os.path.join(r, f) for r, _, names in os.walk(ROOT) for f in names]
    print(f'Deleting {len(files):,} cached icons...')
    for path in track(files, label='Deleting icons'):
        try:
            os.unlink(path)
        except OSError:
            pass
    shutil.rmtree(ROOT, ignore_errors=True)
    print('Done.')
    with _lock:
        _versions = None
