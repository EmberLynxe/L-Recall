import json
import os
import threading


DOCUMENTS = (0xFDD39AD0, 0x238F, 0x46AF, (0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
DOWNLOADS = (0x374DE290, 0x123F, 0x4565, (0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))


def known_folder(folder_id):
    """a folder windows keeps track of, like Documents or Downloads, wherever it's been moved to. None if
    windows won't say"""
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [('a', wintypes.DWORD), ('b', wintypes.WORD), ('c', wintypes.WORD), ('d', ctypes.c_ubyte * 8)]
        a, b, c, d = folder_id
        guid = GUID(a, b, c, (ctypes.c_ubyte * 8)(*d))
        path = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path)) != 0:
            return None
        try:
            return path.value
        finally:
            ctypes.windll.ole32.CoTaskMemFree(path)
    except (OSError, AttributeError):
        return None


def documents_folders():
    """where Documents actually is. windows 11 moves it into OneDrive on a lot of PCs, and league saves
    replays wherever windows says Documents is, so asking windows beats guessing"""
    found = [known_folder(DOCUMENTS)] if known_folder(DOCUMENTS) else []
    found.append(os.path.join(os.path.expanduser('~'), 'Documents'))
    for var in ('OneDrive', 'OneDriveConsumer', 'OneDriveCommercial'):
        if os.environ.get(var):
            found.append(os.path.join(os.environ[var], 'Documents'))
    seen, out = set(), []
    for f in found:
        key = os.path.normcase(os.path.abspath(f))
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def _default_replay_folders():
    folders = [os.path.join(d, 'League of Legends', 'Replays') for d in documents_folders()]
    return [f for f in folders if os.path.isdir(f)][:1]


class Config(dict):
    defaults = {
        'game_paths': [],
        'replay_folders': [],
        'repository': None,
        'configured': False,
        'player_names': [],
        'auto_download_assets': True,
        'check_updates': True,
        'dismissed_update': '',
        'keep_prepared': 1,
        'use_my_settings': True,
        'prepare_newest': True,
        'quick_start': True,
        'space_warning': True,
        'clear_on_exit': False,
        'save_new_patches': True,  # save each patch league updates to. set at setup, off means only when asked
        'drive': '',  # empty = work it out
    }

    def __init__(self, path):
        self._path = path
        try:
            with open(path, encoding='utf-8') as config_file:
                config = json.load(config_file)
            if not isinstance(config, dict):
                raise ValueError('not an object')
        except FileNotFoundError:
            config = {}
        except ValueError:
            # config's fucked. move it aside and start with defaults
            os.replace(path, path + '.corrupt')
            config = {}

        merged = {**self.defaults, **config}
        if merged.get('settings_version', 1) < 2:
            # older settings files kept more patches ready with every champion built. moved over once
            merged['keep_prepared'] = 1
            merged['quick_start'] = True
        merged['settings_version'] = 2

        if not merged.get('replay_folders'):
            merged['replay_folders'] = _default_replay_folders()

        super(Config, self).__init__(merged)
        self.save()

    def save(self):
        tmp = f'{self._path}.{threading.get_ident()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as config_file:
            json.dump(self, config_file, indent=2)
            config_file.flush()
            os.fsync(config_file.fileno())
        os.replace(tmp, self._path)
