"""how windows knows L-Recall is there. a start menu shortcut, which is what windows search and all apps go
off, and an entry in settings > apps whose uninstall button opens ours. all per user, no admin needed.
nothing here is allowed to stop the app starting"""
import os
import winreg

from . import __version__

KEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\L-Recall'
SHORTCUT = 'L-Recall.lnk'


def start_menu_folder():
    return os.path.join(os.environ.get('APPDATA', ''), 'Microsoft', 'Windows', 'Start Menu', 'Programs')


def _size_kb(app_dir):
    """only what we ship. people unzip this into Downloads, walking the whole folder would count their stuff"""
    from .uninstall import shipped
    folders, files = shipped(app_dir)
    total = 0
    for name in files:
        try:
            total += os.path.getsize(os.path.join(app_dir, name))
        except OSError:
            pass
    for folder in folders:
        for root, _, names in os.walk(os.path.join(app_dir, folder)):
            for name in names:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    return total // 1024


def register(exe, shell):
    """runs every time the real exe starts, so a moved folder or an update just fixes itself"""
    app_dir = os.path.dirname(exe)
    os.makedirs(start_menu_folder(), exist_ok=True)
    link = shell.CreateShortCut(os.path.join(start_menu_folder(), SHORTCUT))
    link.Targetpath = exe
    link.WorkingDirectory = app_dir
    link.IconLocation = exe
    link.Description = 'Saves League patches so old replays still play'
    link.save()

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as key:
        for name, value in (('DisplayName', 'L-Recall'), ('DisplayVersion', __version__), ('Publisher', 'EmberLynxe'),
                            ('DisplayIcon', exe), ('InstallLocation', app_dir),
                            ('UninstallString', f'"{exe}" --uninstall'),
                            ('URLInfoAbout', 'https://github.com/EmberLynxe/L-Recall')):
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        for name, value in (('NoModify', 1), ('NoRepair', 1), ('EstimatedSize', _size_kb(app_dir))):
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)


def unregister(log=print):
    path = os.path.join(start_menu_folder(), SHORTCUT)
    if os.path.exists(path):
        os.unlink(path)
        log('Removed the Start menu shortcut')
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, KEY)
        log('Removed L-Recall from Installed apps')
    except OSError:
        pass
