"""vanguard status. read only.

l-recall never starts, stops or reconfigures vanguard. riot's faq says the only sanctioned ways to
turn it off are exit vanguard in its tray menu or uninstalling it, and anything else can stop you
playing. there used to be a switch here that flipped the service settings. it's gone, and with
pre-check (on demand vanguard) it would've quietly broken riot's setup anyway
"""
import win32service

SERVICES = ('vgk', 'vgc')
_START = {0: 'boot', 1: 'system', 2: 'automatic', 3: 'manual', 4: 'disabled'}
FAQ_URL = 'https://support.riotgames.com/en-us/league-of-legends/performance/riot-vanguard-faq-league-of-legends'


def _query(scm, name):
    try:
        svc = win32service.OpenService(scm, name, win32service.SERVICE_QUERY_STATUS | win32service.SERVICE_QUERY_CONFIG)
    except win32service.error:
        return None
    try:
        state = win32service.QueryServiceStatus(svc)[1]
        start = win32service.QueryServiceConfig(svc)[1]
        return {'running': state == win32service.SERVICE_RUNNING, 'start': _START.get(start, str(start))}
    finally:
        win32service.CloseServiceHandle(svc)


def status():
    """installed / running / mode. mode is boot (classic), on_demand (pre-check, starts with a
    riot game) or disabled. on_demand also covers setups where someone turned it off the old way,
    there's no telling those apart from the outside, so we don't pretend to"""
    try:
        scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    except win32service.error:
        return {'installed': None, 'running': None, 'mode': None}
    try:
        info = {name: _query(scm, name) for name in SERVICES}
    finally:
        win32service.CloseServiceHandle(scm)
    vgk = info.get('vgk')
    if vgk is None:
        mode = None
    elif vgk['start'] in ('boot', 'system', 'automatic'):
        mode = 'boot'
    elif vgk['start'] == 'disabled':
        mode = 'disabled'
    else:
        mode = 'on_demand'
    return {
        'installed': vgk is not None,
        'running': bool(vgk and vgk['running']),
        'mode': mode,
        **info,
    }
