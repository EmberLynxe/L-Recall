"""data dragon icon cache. per patch because old items look different"""
import json
import os
import re
import shutil
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from large_vcs.progress import track

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
    global _versions
    with _lock:
        if _versions is not None:
            return _versions
        cache = os.path.join(ROOT, 'versions.json')
        try:
            data = _get(CDN + '/api/versions.json')
            found = [v for v in json.loads(data) if v[:1].isdigit()]
            _save(cache, json.dumps(found).encode())
        except Exception:
            try:
                with open(cache) as f:
                    found = json.load(f)
            except (OSError, ValueError):
                found = []
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
        _save(dst, _get(f'{CDN}/cdn/{version}/img/{kind}/{name}.png'))
        return True
    except Exception:
        return False


def ensure(version, champions=(), items=(), spells=()):
    """download whatever's missing. returns how many"""
    jobs = [(version, 'champion', c) for c in set(champions) if c]
    jobs += [(version, 'item', str(i)) for i in set(items) if i]
    jobs += [(version, 'spell', s) for s in set(spells) if s]
    jobs = [j for j in jobs if _ok(*j) and not os.path.exists(local_path(*j))]
    if not jobs:
        return 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        return sum(ex.map(lambda j: _fetch(*j), jobs))


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
    vs = versions()
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
    with ThreadPoolExecutor(max_workers=4) as ex:
        for v, nc, ni, got in ex.map(one, order):
            log(f'  {v}: {nc} champions, {ni} items, {got} new files')
    log('Done.')


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
    global _versions
    with _lock:
        _versions = None
    return versions()


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
