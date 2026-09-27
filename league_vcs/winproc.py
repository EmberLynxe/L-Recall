"""process checks that never open a handle to anything"""
import ctypes
from ctypes import wintypes

GAME_EXE = 'league of legends.exe'
TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD),
        ('cntUsage', wintypes.DWORD),
        ('th32ProcessID', wintypes.DWORD),
        ('th32DefaultHeapID', ctypes.c_size_t),
        ('th32ModuleID', wintypes.DWORD),
        ('cntThreads', wintypes.DWORD),
        ('th32ParentProcessID', wintypes.DWORD),
        ('pcPriClassBase', ctypes.c_long),
        ('dwFlags', wintypes.DWORD),
        ('szExeFile', ctypes.c_wchar * 260),
    ]


_k32 = ctypes.WinDLL('kernel32', use_last_error=True)
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


def apps_using(folder):
    """names of programs with files open under folder, from windows' restart manager. skips junctions.
    only for explaining why something's locked, never call it while the game's running"""
    import os
    rm = ctypes.WinDLL('rstrtmgr')

    class _Unique(ctypes.Structure):
        _fields_ = [('dwProcessId', wintypes.DWORD), ('ProcessStartTime', wintypes.FILETIME)]

    class _Info(ctypes.Structure):
        _fields_ = [('Process', _Unique), ('strAppName', wintypes.WCHAR * 256),
                    ('strServiceShortName', wintypes.WCHAR * 64), ('ApplicationType', wintypes.DWORD),
                    ('AppStatus', wintypes.ULONG), ('TSSessionId', wintypes.DWORD), ('bRestartable', wintypes.BOOL)]

    files = []
    for root, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if not os.path.isjunction(os.path.join(root, d))]
        files += [os.path.join(root, n) for n in names]
    if not files:
        return []
    session, key = wintypes.DWORD(), ctypes.create_unicode_buffer(64)
    if rm.RmStartSession(ctypes.byref(session), 0, key):
        return []
    try:
        arr = (wintypes.LPCWSTR * len(files))(*files)
        if rm.RmRegisterResources(session, len(files), arr, 0, None, 0, None):
            return []
        needed, count, reasons = wintypes.UINT(), wintypes.UINT(0), wintypes.DWORD()
        rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reasons))
        if not needed.value:
            return []
        infos = (_Info * needed.value)()
        count = wintypes.UINT(needed.value)
        if rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), infos, ctypes.byref(reasons)):
            return []
        me = os.getpid()
        return sorted({i.strAppName for i in infos[:count.value] if i.Process.dwProcessId != me and i.strAppName})
    except OSError:
        return []
    finally:
        rm.RmEndSession(session)


# read-only snapshot, no handles. don't open the game process, vanguard won't like it
def process_names():
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE:
        return []
    names = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            names.append(entry.szExeFile.lower())
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return names


def game_running():
    return GAME_EXE in process_names()


def trim_memory():
    """hand unused memory back to windows. called when the window closes and after big jobs, so
    sitting in the tray doesn't keep whatever the last job needed"""
    import gc
    gc.collect()
    try:
        _k32.GetCurrentProcess.restype = wintypes.HANDLE
        _k32.SetProcessWorkingSetSize.argtypes = [wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t]
        _k32.SetProcessWorkingSetSize(_k32.GetCurrentProcess(), ctypes.c_size_t(-1), ctypes.c_size_t(-1))
    except Exception:
        pass
