import os
import shutil
import zipfile

import wx
from cx_Freeze import setup, Executable
from cx_Freeze.winversioninfo import VersionInfo

from league_vcs import __version__

DESCRIPTION = 'Watch any League of Legends replay on the patch it was recorded on.'
COPYRIGHT = 'Copyright 2020 Pranav Nutalapati, 2026 EmberLynxe. MIT license.'

dist = setup(
    name="L-Recall",
    version=__version__,
    author='EmberLynxe',
    description=DESCRIPTION,
    options={
        'build_exe': {
            'packages': ['league_vcs', 'large_vcs', 'zstandard', 'xxhash'],
            # only the bits of wx we use. the whole package is 55 mb, mostly editors and grids nobody opens
            'includes': ['wx', 'wx.adv', 'wx.html2'],
            # things that get dragged in by something importing something, never used
            'excludes': ['tkinter', 'PIL', 'wx.lib', 'wx.py', 'wx.tools', 'pythonwin', 'win32ui', 'win32uiole',
                         'dde', 'unittest', 'pydoc', 'pydoc_data', 'lib2to3', 'test', 'distutils', 'setuptools',
                         'IPython', 'numpy'],
            'include_files': [
                ('league_vcs/gui/web', 'lib/league_vcs/gui/web'),
                ('league_vcs/gui/icons', 'lib/league_vcs/gui/icons'),
                # wx looks for this next to the exe, not in lib/. without it the window's blank
                (os.path.join(os.path.dirname(wx.__file__), 'WebView2Loader.dll'), 'WebView2Loader.dll'),
            ],
        },
    },
    executables=[
        Executable("entrypoints/main.py", target_name='L-Recall',
                   icon='raster/icon.ico', base='gui', copyright=COPYRIGHT),
    ])

# redo the exe properties. cx_freeze pulls them from pyproject and you get "l-recall" plus the whole readme
build = dist.get_command_obj('build_exe')
if getattr(build, 'build_exe', None):
    # task manager shows the file description as the process name, so it's just the name
    VersionInfo(__version__, description='L-Recall', company='EmberLynxe', product='L-Recall',
                copyright=COPYRIGHT, internal_name='L-Recall',
                original_filename='L-Recall.exe').stamp(os.path.join(build.build_exe, 'L-Recall.exe'))

    # cx_freeze's wx hook copies every wx binary whatever you ask for. core, adv and html2 are all we
    # load (siplib comes with core), the rest is just download size. same for pythonwin, pywin32 drags it in
    lib = os.path.join(build.build_exe, 'lib')
    wx_dir = os.path.join(lib, 'wx')
    keep = {'_core', '_adv', '_html2', 'siplib', 'wxbase333u', 'wxbase333u_net', 'wxmsw333u_core',
            'wxmsw333u_webview'}
    for name in os.listdir(wx_dir):
        stem = name.split('.')[0].replace('_vc140_x64', '')
        if name.endswith(('.pyd', '.dll')) and stem not in keep and name != 'WebView2Loader.dll':
            os.remove(os.path.join(wx_dir, name))
        elif name in ('html.pyc', 'msw.pyc', 'xml.pyc', 'xrc.pyc'):
            os.remove(os.path.join(wx_dir, name))
    for folder in ('locale', 'include', 'svg'):  # translations (the app's english only), c headers, wx.svg
        shutil.rmtree(os.path.join(wx_dir, folder), ignore_errors=True)
    shutil.rmtree(os.path.join(lib, 'pythonwin'), ignore_errors=True)
    for name in ('scintilla.dll', 'win32ui.pyd', 'win32uiole.pyd', 'dde.pyd'):
        if os.path.exists(os.path.join(lib, name)):
            os.remove(os.path.join(lib, name))

    # each library's whole readme rides along in its METADATA. only the header lines at the top get read
    libzip = os.path.join(lib, 'library.zip')
    with zipfile.ZipFile(libzip) as src, zipfile.ZipFile(libzip + '.tmp', 'w') as out:
        for info in src.infolist():
            data = src.read(info)
            if info.filename.endswith('.dist-info/METADATA'):
                data = data.replace(b'\r\n', b'\n').split(b'\n\n', 1)[0] + b'\n'
            out.writestr(info, data)
    os.replace(libzip + '.tmp', libzip)
