"""asks github if there's a newer release. that's it, nothing gets sent"""
import json
import re
import threading
import time
import urllib.request

from league_vcs import __version__

REPO = 'EmberLynxe/L-Recall'
RELEASES_URL = f'https://github.com/{REPO}/releases'
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
    # this url gets opened with a click, so it has to be our releases page and nothing else
    url = data.get('html_url') or ''
    if not isinstance(url, str) or not url.startswith(RELEASES_URL + '/'):
        url = RELEASES_URL
    return {
        'version': '.'.join(map(str, version)),
        'url': url,
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
