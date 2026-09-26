from setuptools import setup, find_packages

setup(
    name='l-recall',
    version='1.1.3',
    packages=find_packages(),
    author='EmberLynxe',
    entry_points={
        'console_scripts': ['l-recall=league_vcs.cli:main']
    },
    install_requires=['click>=8.0', 'pywin32>=306', 'wxPython>=4.2', 'tqdm>=4.60', 'zstandard>=0.20', 'xxhash>=3.0'],
)
