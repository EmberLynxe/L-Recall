import os

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
            'packages': ['league_vcs', 'large_vcs', 'wx', 'zstandard', 'xxhash'],
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

# what shows up in the exe's properties, and code signing wants a proper product name and version in
# there. cx_freeze takes them from pyproject, which gives "l-recall" and the entire readme as a comment
build = dist.get_command_obj('build_exe')
if getattr(build, 'build_exe', None):
    VersionInfo(__version__, description=DESCRIPTION, company='EmberLynxe', product='L-Recall',
                copyright=COPYRIGHT, internal_name='L-Recall',
                original_filename='L-Recall.exe').stamp(os.path.join(build.build_exe, 'L-Recall.exe'))
