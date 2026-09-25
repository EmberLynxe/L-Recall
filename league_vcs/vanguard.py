"""vanguard status. read only, we never start, stop or change it"""
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
    """installed / running / mode. mode is boot, on_demand (pre-check) or disabled"""
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
