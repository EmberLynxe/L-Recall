"""make sure the game files are riot's before we run anything out of storage"""
import ctypes
import os
import threading
from ctypes import wintypes

RIOT = 'Riot Games, Inc.'

_wintrust = ctypes.WinDLL('wintrust')
_crypt32 = ctypes.WinDLL('crypt32')


class _GUID(ctypes.Structure):
    _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD), ('Data3', wintypes.WORD),
                ('Data4', ctypes.c_ubyte * 8)]


# WINTRUST_ACTION_GENERIC_VERIFY_V2
_VERIFY_V2 = _GUID(0x00AAC56B, 0xCD44, 0x11D0, (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))


class _FileInfo(ctypes.Structure):
    _fields_ = [('cbStruct', wintypes.DWORD), ('pcwszFilePath', wintypes.LPCWSTR),
                ('hFile', wintypes.HANDLE), ('pgKnownSubject', ctypes.POINTER(_GUID))]


class _TrustData(ctypes.Structure):
    _fields_ = [('cbStruct', wintypes.DWORD), ('pPolicyCallbackData', ctypes.c_void_p),
                ('pSIPClientData', ctypes.c_void_p), ('dwUIChoice', wintypes.DWORD),
                ('fdwRevocationChecks', wintypes.DWORD), ('dwUnionChoice', wintypes.DWORD),
                ('pFile', ctypes.POINTER(_FileInfo)), ('dwStateAction', wintypes.DWORD),
                ('hWVTStateData', wintypes.HANDLE), ('pwszURLReference', wintypes.LPCWSTR),
                ('dwProvFlags', wintypes.DWORD), ('dwUIContext', wintypes.DWORD),
                ('pSignatureSettings', ctypes.c_void_p)]


class _ProvCert(ctypes.Structure):
    _fields_ = [('cbStruct', wintypes.DWORD), ('pCert', ctypes.c_void_p)]


_WTD_UI_NONE, _WTD_REVOKE_NONE, _WTD_CHOICE_FILE = 2, 0, 1
_WTD_STATEACTION_VERIFY, _WTD_STATEACTION_CLOSE = 1, 2
_WTD_CACHE_ONLY_URL_RETRIEVAL = 0x1000
_CERT_NAME_SIMPLE_DISPLAY_TYPE = 4

_wintrust.WinVerifyTrust.argtypes = [wintypes.HWND, ctypes.POINTER(_GUID), ctypes.c_void_p]
_wintrust.WinVerifyTrust.restype = wintypes.LONG
_wintrust.WTHelperProvDataFromStateData.argtypes = [wintypes.HANDLE]
_wintrust.WTHelperProvDataFromStateData.restype = ctypes.c_void_p
_wintrust.WTHelperGetProvSignerFromChain.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_wintrust.WTHelperGetProvSignerFromChain.restype = ctypes.c_void_p
_wintrust.WTHelperGetProvCertFromChain.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_wintrust.WTHelperGetProvCertFromChain.restype = ctypes.POINTER(_ProvCert)
_crypt32.CertGetNameStringW.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                        wintypes.LPWSTR, wintypes.DWORD]
_crypt32.CertGetNameStringW.restype = wintypes.DWORD


def signer(path):
    """name on a valid signature, else None. offline. old patches have expired certs but
    they're timestamped so they still pass"""
    info = _FileInfo(ctypes.sizeof(_FileInfo), os.path.abspath(path), None, None)
    data = _TrustData()
    data.cbStruct = ctypes.sizeof(_TrustData)
    data.dwUIChoice = _WTD_UI_NONE
    data.fdwRevocationChecks = _WTD_REVOKE_NONE
    data.dwUnionChoice = _WTD_CHOICE_FILE
    data.pFile = ctypes.pointer(info)
    data.dwStateAction = _WTD_STATEACTION_VERIFY
    data.dwProvFlags = _WTD_CACHE_ONLY_URL_RETRIEVAL
    guid = _GUID.from_buffer_copy(_VERIFY_V2)
    try:
        if _wintrust.WinVerifyTrust(None, ctypes.byref(guid), ctypes.byref(data)) != 0:
            return None
        prov = _wintrust.WTHelperProvDataFromStateData(data.hWVTStateData)
        sgnr = _wintrust.WTHelperGetProvSignerFromChain(prov, 0, False, 0) if prov else None
        cert = _wintrust.WTHelperGetProvCertFromChain(sgnr, 0) if sgnr else None
        if not cert or not cert.contents.pCert:
            return None
        buf = ctypes.create_unicode_buffer(256)
        _crypt32.CertGetNameStringW(cert.contents.pCert, _CERT_NAME_SIMPLE_DISPLAY_TYPE, 0, None, buf, 256)
        return buf.value or None
    finally:
        data.dwStateAction = _WTD_STATEACTION_CLOSE
        _wintrust.WinVerifyTrust(None, ctypes.byref(guid), ctypes.byref(data))


_seen = {}
_lock = threading.Lock()


def _cached_signer(path):
    # remembered per size+mtime, hashing 30 MB every time you hit watch is dumb
    st = os.stat(path)
    key = (os.path.normcase(os.path.abspath(path)), st.st_size, st.st_mtime_ns)
    with _lock:
        if key in _seen:
            return _seen[key]
    name = signer(path)
    with _lock:
        _seen[key] = name
    return name


def check_game_folder(folder, exe_name):
    """None if it's fine to launch, otherwise what's wrong. the exe has to be riot's, the other
    exes/dlls just need a valid signature (microsoft and unity ship a couple)"""
    exe = os.path.join(folder, exe_name)
    try:
        if _cached_signer(exe) != RIOT:
            return f"{exe_name} isn't signed by Riot Games."
        for root, _, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(('.exe', '.dll')) and not _cached_signer(os.path.join(root, f)):
                    return f"{os.path.relpath(os.path.join(root, f), folder)} isn't signed."
    except OSError as e:
        return f"Couldn't check the game files: {e}"
    return None
