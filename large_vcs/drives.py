"""what kind of drive a folder's on, so building and storing can suit it. nvme takes lots of reads at once,
a sata ssd a handful, and a hard drive wants one long straight read at a time. windows says which is which
without needing admin, same as task manager's "SSD (NVMe)" and "HDD" labels"""
import ctypes
import functools
import os
from ctypes import wintypes

NVME, SSD, HDD = 'nvme', 'ssd', 'hdd'
NAMES = {NVME: 'NVMe SSD', SSD: 'SATA SSD', HDD: 'Hard drive'}

_IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400
_DEVICE_PROPERTY, _SEEK_PENALTY_PROPERTY = 0, 7
_BUS_NVME = 17


def _query(k32, handle, prop, size):
    request = (ctypes.c_uint32 * 3)(prop, 0, 0)  # property, standard query, no extra
    out = ctypes.create_string_buffer(size)
    got = wintypes.DWORD()
    if not k32.DeviceIoControl(handle, _IOCTL_STORAGE_QUERY_PROPERTY, request, ctypes.sizeof(request),
                               out, size, ctypes.byref(got), None):
        return None
    return out.raw[:got.value]


@functools.cache
def _kind_of_volume(volume):
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    k32.DeviceIoControl.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
    # no access rights asked for, just enough to ask the drive about itself
    handle = k32.CreateFileW('\\\\.\\' + volume.rstrip('\\'), 0, 3, None, 3, 0, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        return None
    try:
        seek = _query(k32, handle, _SEEK_PENALTY_PROPERTY, 12)
        device = _query(k32, handle, _DEVICE_PROPERTY, 64)
    finally:
        k32.CloseHandle(handle)
    if seek and len(seek) >= 9 and seek[8]:
        return HDD
    if device and len(device) >= 32:
        return NVME if int.from_bytes(device[28:32], 'little') == _BUS_NVME else SSD
    return SSD if seek else None


def kind(path):
    """NVME, SSD or HDD for the drive path is on. None if windows won't say (network drives, some usb
    enclosures and raid setups), and then it's treated like nvme, which is how everything worked before"""
    if os.name != 'nt':
        return None
    try:
        buf = ctypes.create_unicode_buffer(260)
        if not ctypes.windll.kernel32.GetVolumePathNameW(os.path.abspath(path), buf, 260):
            return None
        volume = buf.value
        if not volume[:1].isalpha() or volume[1:3] != ':\\':
            return None  # a folder mounted as a drive, or a network share
        return _kind_of_volume(volume[:2].upper())
    except (OSError, ValueError, AttributeError):
        return None
