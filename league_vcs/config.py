import json
import os
import threading


def _default_replay_folders():
    docs = os.path.join(os.path.expanduser('~'), 'Documents',
                        'League of Legends', 'Replays')
    if os.path.isdir(docs):
        return [docs]
    return []


class Config(dict):
    defaults = {
        'game_paths': [],
        'replay_folders': [],
        'repository': None,
        'configured': False,
        'player_names': [],
        'auto_download_assets': True,
        'vanguard_warn_old': True,
        'check_updates': True,
        'dismissed_update': '',
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
