"""report a problem. zips the last few logs and a short summary with nothing personal in it, to send to
whoever's helping"""
import datetime
import os
import re
import zipfile

from .config import DOWNLOADS, known_folder


def _home_forms():
    """every way the home folder and username turn up in a log"""
    home = os.path.expanduser('~')
    forms = {home, home.replace('\\', '/'), os.environ.get('USERNAME', '')}
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.kernel32.GetShortPathNameW(home, buf, 260):
            forms |= {buf.value, buf.value.replace('\\', '/'), os.path.basename(buf.value)}
    except (OSError, AttributeError):
        pass
    return sorted((f for f in forms if len(f) > 2), key=len, reverse=True)


def scrub(text, forms=None):
    """logs get cleaned as they're written. this catches anything that got past that"""
    for form in forms or _home_forms():
        text = re.sub(re.escape(form), '<you>', text, flags=re.IGNORECASE)
    return text


def where_to_save():
    for folder in (known_folder(DOWNLOADS), os.path.join(os.path.expanduser('~'), 'Downloads'),
                   os.path.expanduser('~')):
        if folder and os.path.isdir(folder):
            return folder
    return os.getcwd()


def make(logs_dir, summary, dest=None, keep=3):
    """the zip, with the newest few logs and the summary. returns its path"""
    try:
        logs = sorted((e for e in os.scandir(logs_dir) if e.is_file() and e.name.endswith('.log')),
                      key=lambda e: e.stat().st_mtime)[-keep:]
    except FileNotFoundError:
        logs = []
    name = f'L-Recall report {datetime.datetime.now():%Y-%m-%d %H-%M}.zip'
    path = os.path.join(dest or where_to_save(), name)
    forms = _home_forms()
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('summary.txt', scrub(summary, forms))
        for e in logs:
            with open(e.path, encoding='utf-8', errors='replace') as f:
                z.writestr('logs/' + e.name, scrub(f.read(), forms))
    return path

