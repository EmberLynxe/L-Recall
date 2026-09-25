"""uninstall. there's no installer, so this is all of it.

startup shortcut, icon cache, optionally the stored patches, then the program folder. the
program can't delete itself while it's running, so a little batch file waits for it to
close and finishes the job. it only deletes files we shipped, never the whole folder,
in case someone unzipped this straight into Downloads"""
import os
import shutil
import subprocess
import sys
import tempfile

from large_vcs import LargeVCS
from large_vcs.progress import track

from . import assets

STARTUP_NAMES = ('L-Recall.lnk', 'League VCS.lnk')


def _startup_folder():
    return os.path.join(os.environ.get('APPDATA', ''), 'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Startup')


def _shipped():
    """(folders, files) cx_freeze puts next to the exe, plus what we write there ourselves"""
    py = f'python{sys.version_info.major}{sys.version_info.minor}.dll'
    folders = ['lib', 'logs']
    files = ['L-Recall.exe', 'python3.dll', py, 'WebView2Loader.dll', 'frozen_application_license.txt',
             'user_settings.json', 'user_settings.json.*', 'replay_notes.json', 'replay_notes.json.*']
    return folders, files


def stored_patch_bytes(repository):
    if not repository or not os.path.isdir(os.path.join(repository, 'lvcs')):
        return 0
    return LargeVCS(repository).total_size()


def remove_data(repository=None, delete_patches=False, log=print):
    """everything except the program files. safe to run from source"""
    for name in STARTUP_NAMES:
        path = os.path.join(_startup_folder(), name)
        if os.path.exists(path):
            os.unlink(path)
            log(f'Removed startup shortcut {name}')

    cache = os.path.dirname(assets.ROOT)
    if os.path.isdir(cache):
        files = [os.path.join(r, f) for r, _, names in os.walk(cache) for f in names]
        for path in track(files, label='Deleting icon cache'):
            try:
                os.unlink(path)
            except OSError:
                pass
        shutil.rmtree(cache, ignore_errors=True)
        log('Deleted the icon cache')

    if delete_patches and repository and os.path.isdir(os.path.join(repository, 'lvcs')):
        log(f'Deleting stored patches in {repository}...')
        LargeVCS(repository).wipe()
        log('Deleted stored patches')


_SCRIPT = r'''@echo off
rem left behind by L-Recall's uninstall. waits for L-Recall to close, deletes its files, then itself
if not defined LRECALL_DIR goto end
set tries=0
:wait
del /f /q "%LRECALL_DIR%\L-Recall.exe" >nul 2>&1
if not exist "%LRECALL_DIR%\L-Recall.exe" goto gone
set /a tries+=1
if %tries% geq 60 goto end
ping -n 2 127.0.0.1 >nul
goto wait
:gone
{folders}
{files}
rmdir "%LRECALL_DIR%" >nul 2>&1
:end
(goto) 2>nul & del "%~f0"
'''


def schedule_program_removal(app_dir):
    """start the cleanup script. returns False when running from source, there's nothing to remove"""
    if not getattr(sys, 'frozen', False):
        return False
    folders, files = _shipped()
    script = _SCRIPT.format(
        folders='\n'.join(f'rmdir /s /q "%LRECALL_DIR%\\{f}" >nul 2>&1' for f in folders),
        files='\n'.join(f'del /f /q "%LRECALL_DIR%\\{f}" >nul 2>&1' for f in files))
    fd, path = tempfile.mkstemp(prefix='l-recall-uninstall-', suffix='.cmd')
    with os.fdopen(fd, 'w', encoding='ascii') as f:
        f.write(script.replace('\n', '\r\n'))
    # the install path goes in through the environment. on cmd's command line a folder with % or &
    # in its name gets mangled, env vars expand once and that's it
    env = {**os.environ, 'LRECALL_DIR': os.path.abspath(app_dir)}
    subprocess.Popen(f'cmd.exe /d /s /c ""{path}""', env=env, close_fds=True, cwd=tempfile.gettempdir(),
                     creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    return True
