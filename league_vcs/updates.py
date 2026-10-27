"""asks github if there's a newer release. that's it, nothing gets sent"""
import json
import re
import threading
import time
import urllib.request

from league_vcs import __version__

REPO = 'EmberLynxe/L-Recall'
RELEASES_URL = f'https://github.com/{REPO}/releases'
DOWNLOADS_URL = f'{RELEASES_URL}/download/'
_API = f'https://api.github.com/repos/{REPO}/releases/latest'
_CACHE_SECONDS = 60 * 60

_lock = threading.Lock()
_cache = {'at': 0, 'release': None}


def parse_version(text):
    """'v1.2.3' -> (1, 2, 3). None if it isn't a version"""
    m = re.match(r'v?(\d+)\.(\d+)(?:\.(\d+))?', (text or '').strip())
    return tuple(int(x or 0) for x in m.groups()) if m else None


def _fetch_latest(timeout=10):
    # /latest already skips drafts and prereleases. 404 until there's a public release, which is fine
    req = urllib.request.Request(_API, headers={'User-Agent': 'l-recall', 'Accept': 'application/vnd.github+json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except Exception:
        return None
    version = parse_version(data.get('tag_name'))
    if not version:
        return None
    # only ever open our own releases page
    url = data.get('html_url') or ''
    if not isinstance(url, str) or not url.startswith(RELEASES_URL + '/'):
        url = RELEASES_URL
    # the zip and its hash, for updating in place. same deal, has to come from our own releases
    zip_url = sha_url = None
    size = 0
    for asset in data.get('assets') or []:
        link = asset.get('browser_download_url') or ''
        if not isinstance(link, str) or not link.startswith(DOWNLOADS_URL):
            continue
        if link.endswith('-win64.zip'):
            zip_url, size = link, asset.get('size') or 0
        elif link.endswith('-win64.zip.sha256'):
            sha_url = link
    return {
        'version': '.'.join(map(str, version)),
        'url': url,
        'zip': zip_url,
        'sha256': sha_url,
        'size': size,
        'notes': (data.get('body') or '')[:2000],
        'published': data.get('published_at'),
    }


def latest(force=False):
    """newest release on github, or None if we can't tell. cached for an hour"""
    with _lock:
        if not force and time.time() - _cache['at'] < _CACHE_SECONDS:
            return _cache['release']
    release = _fetch_latest()
    with _lock:
        _cache.update(at=time.time(), release=release)
    return release


def newer_than_running(force=False):
    """the release if it beats the version we're running, else None"""
    release = latest(force)
    if release and parse_version(release['version']) > parse_version(__version__):
        return release
    return None


def notes_for(version, timeout=10):
    """release notes for one version, '' if github doesn't have any or we're offline"""
    url = f'https://api.github.com/repos/{REPO}/releases/tags/v{version}'
    req = urllib.request.Request(url, headers={'User-Agent': 'l-recall', 'Accept': 'application/vnd.github+json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (json.loads(r.read()).get('body') or '')[:2000]
    except Exception:
        return ''


def whats_new(config, fetch=True):
    """notes for the version we're running, once, on the first start after an update. None otherwise.
    the updater saves the notes before restarting, so normally nothing gets fetched. someone who unzipped
    the new version over the old one gets them from github instead"""
    seen = config.get('seen_version') or ''
    if seen == __version__:
        return None
    config['seen_version'] = __version__
    saved = config.get('whats_new') or {}
    config['whats_new'] = None
    config.save()
    # brand new install, or going back a version. nothing to tell them
    if not config.get('configured') or (seen and parse_version(seen) > parse_version(__version__)):
        return None
    notes = saved.get('notes') if saved.get('version') == __version__ else None
    if notes is None and fetch:
        notes = notes_for(__version__)
    if not (notes or '').strip():
        return None
    return {'version': __version__, 'notes': notes}
