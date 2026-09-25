"""update in place. download the release zip, check it against its hash, unzip it to temp,
then a batch file swaps the files once we've closed and starts the new version"""
import hashlib
import os
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

from large_vcs.progress import step

from . import updates


def can_update():
    """only the built exe. running from source there's nothing to swap"""
    return bool(getattr(sys, 'frozen', False))


def _open(url, timeout=30):
    if not url or not url.startswith(updates.DOWNLOADS_URL):
        raise ValueError("That release doesn't have a download L-Recall can use.")
    return urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'l-recall'}), timeout=timeout)


def download(release):
    """-> folder with the new version unpacked. raises if anything's off, nothing gets touched"""
    with _open(release.get('sha256')) as r:
        want = r.read().decode('ascii', 'replace').split()[0].strip().lower()
    work = tempfile.mkdtemp(prefix='l-recall-update-')
    zip_path = os.path.join(work, 'update.zip')
    digest, done = hashlib.sha256(), 0
    with _open(release.get('zip')) as r, open(zip_path, 'wb') as f:
        total = int(r.headers.get('Content-Length') or release.get('size') or 0)
        while chunk := r.read(1 << 20):
            f.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            step(done, max(total, done), 'Downloading')
    if digest.hexdigest() != want:
        raise ValueError("The download doesn't match its published hash, so it wasn't installed.")

    new = os.path.join(work, 'new')
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            parts = name.replace('\\', '/').split('/')
            if name.startswith(('/', '\\')) or ':' in name or '..' in parts:
                raise ValueError(f'The update has a file in a weird place ({name}), so it wasn\'t installed.')
        z.extractall(new)
    os.unlink(zip_path)
    if not os.path.isfile(os.path.join(new, 'L-Recall.exe')):
        raise ValueError("The update doesn't have L-Recall.exe in it, so it wasn't installed.")
    return new


# waits for our pid to go, moves the old exe and lib aside, copies the new ones in. if the copy
# fails the old ones go back. either way it starts l-recall again
_SCRIPT = r'''@echo off
rem left behind by L-Recall's updater. deletes itself when it's done
rem full paths on purpose, git for windows can put its own find/ping ahead of windows' on PATH
if not defined LRECALL_DIR goto end
set tries=0
:wait
"%SystemRoot%\System32\tasklist.exe" /fi "PID eq %LRECALL_PID%" /nh 2>nul | "%SystemRoot%\System32\find.exe" "%LRECALL_PID%" >nul || goto swap
set /a tries+=1
if %tries% geq 60 goto end
"%SystemRoot%\System32\ping.exe" -n 2 127.0.0.1 >nul
goto wait
:swap
if exist "%LRECALL_DIR%\lib.old" rmdir /s /q "%LRECALL_DIR%\lib.old"
del /f /q "%LRECALL_DIR%\L-Recall.exe.old" >nul 2>&1
move /y "%LRECALL_DIR%\L-Recall.exe" "%LRECALL_DIR%\L-Recall.exe.old" >nul || goto start
move /y "%LRECALL_DIR%\lib" "%LRECALL_DIR%\lib.old" >nul || goto undo_exe
"%SystemRoot%\System32\robocopy.exe" "%LRECALL_NEW%" "%LRECALL_DIR%" /e /r:2 /w:1 /njh /njs /nfl /ndl /np >nul
if errorlevel 8 goto undo
rmdir /s /q "%LRECALL_DIR%\lib.old"
del /f /q "%LRECALL_DIR%\L-Recall.exe.old" >nul 2>&1
goto start
:undo
rmdir /s /q "%LRECALL_DIR%\lib" >nul 2>&1
move /y "%LRECALL_DIR%\lib.old" "%LRECALL_DIR%\lib" >nul
:undo_exe
del /f /q "%LRECALL_DIR%\L-Recall.exe" >nul 2>&1
move /y "%LRECALL_DIR%\L-Recall.exe.old" "%LRECALL_DIR%\L-Recall.exe" >nul
:start
start "" "%LRECALL_DIR%\L-Recall.exe"
:end
rmdir /s /q "%LRECALL_WORK%" >nul 2>&1
(goto) 2>nul & del "%~f0"
'''


def apply(new, app_dir, pid=None):
    """start the swap script. it waits for this process to exit, so quit right after"""
    fd, script = tempfile.mkstemp(prefix='l-recall-update-', suffix='.cmd')
    with os.fdopen(fd, 'w', encoding='ascii') as f:
        f.write(_SCRIPT.replace('\n', '\r\n'))
    # paths go in through env vars, same reason as uninstall. % and & in folder names break cmd
    env = {**os.environ, 'LRECALL_DIR': os.path.abspath(app_dir), 'LRECALL_NEW': os.path.abspath(new),
           'LRECALL_WORK': os.path.dirname(os.path.abspath(new)), 'LRECALL_PID': str(pid or os.getpid())}
    subprocess.Popen(f'cmd.exe /d /s /c ""{script}""', env=env, close_fds=True, cwd=tempfile.gettempdir(),
                     creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
