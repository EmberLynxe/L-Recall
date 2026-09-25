"""tags + notes, keyed by game id so moving the file doesn't lose them"""
import json
import os
import re
import threading

_lock = threading.Lock()


def replay_key(filename):
    m = re.match(r'^([A-Za-z]+\d?)[-_](\d+)', filename)
    return f'{m.group(1).upper()}-{m.group(2)}' if m else filename.lower()


class Notes:
    def __init__(self, path):
        self.path = path
        try:
            with open(path, encoding='utf-8') as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def get(self, filename):
        return self.data.get(replay_key(filename), {})

    def set(self, filename, tags, note):
        tags = sorted({t.strip().lstrip('#')[:32] for t in tags if t.strip().lstrip('#')}, key=str.lower)
        note = note.strip()[:4000]
        key = replay_key(filename)
        with _lock:
            if tags or note:
                self.data[key] = {'tags': tags, 'note': note}
            else:
                self.data.pop(key, None)
            tmp = self.path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=1, ensure_ascii=False)
            os.replace(tmp, self.path)
        return self.data.get(key, {})

    def all_tags(self):
        counts = {}
        for entry in self.data.values():
            for t in entry.get('tags', []):
                counts[t] = counts.get(t, 0) + 1
        return sorted(counts, key=lambda t: (-counts[t], t.lower()))
