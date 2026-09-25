import os
import sys

import click

from league_vcs import core


@click.group()
@click.option('--repo', envvar='LEAGUE_VCS_REPO', default=None,
              type=click.Path(file_okay=False, resolve_path=True),
              help='Path to the repository. Defaults to ./repo or LEAGUE_VCS_REPO env var.')
def main(repo):
    """A version control system for League of Legends.

    Long gone are the days of manually copying your game files before every patch, only to forget where you kept them.
    This is the solution to all your first-world problems.

    In addition to being a quick and easy way to keep old versions of League of Legends, it only stores the changes
    between patches, taking less space on your computer."""
    if repo is None:
        repo = os.path.join(os.getcwd(), 'repo')
    core.set_repo_path(repo)


@main.command(name='clean')
def clean_command():
    """Clean the current patch directory."""
    core.repo.clean()
    print('Cleaned!')


@main.command(name='list')
def list_command():
    """List all the currently-stored LoL versions."""
    tags = core.repo.list()
    if tags:
        print('Available patches:')
    else:
        print('No patches found!')
    current_patch = core.repo.current()
    for tag in sorted(tags, reverse=True):
        print(' -', tag, '(current)' if current_patch == tag else '')


@main.command(name='drop')
@click.argument('patch')
def drop_command(patch):
    """Remove a patch from the repository and free up space."""
    if patch not in core.repo.list():
        return print(f'Patch {patch} does not exist!', file=sys.stderr)

    if not click.confirm(f'Are you sure you want to drop patch {patch}? THIS CANNOT BE UNDONE.'):
        return

    core.repo.drop(patch)


@main.command(name='add')
@click.argument('directory', type=click.Path(exists=True, resolve_path=True, file_okay=False, dir_okay=True))
def add_command(directory):
    """Add a game version to the repository, if it doesn't exist."""
    core.add(directory)


@main.command(name='restore')
@click.argument('patch')
def restore_command(patch):
    """Manually restore a specific patch."""
    try:
        core.repo.restore(patch)
    except AssertionError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


@main.command(name='current')
def current_command():
    """Show the current client version."""
    patch = core.repo.current()
    print(f'Currently on patch {patch}' if patch else 'None')


@main.command(name='watch')
@click.argument('replay', type=click.Path(exists=True, readable=True, resolve_path=True))
def watch_command(replay):
    """Load the required game version and launch the replay."""
    core.watch(replay)


@main.command(name='export')
@click.argument('patch')
@click.argument('destination', type=click.Path(resolve_path=True, file_okay=False, dir_okay=True))
def export_command(patch, destination):
    """Export a patch to a directory (for use with ReplayBook etc)."""
    try:
        core.repo.export(patch, destination)
    except AssertionError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


@main.command(name='status')
def status_command():
    """Show storage format and size for each patch."""
    for row in core.repo.storage_report():
        fmt = 'old (whole files)' if row['whole'] else 'optimized'
        print(f" - {row['tag']}: {fmt}")
    print(f'Total: {core.repo.total_size() / 1024 ** 3:.1f} GB')
    est = core.repo.estimate_optimized()
    if est:
        print(f"Optimizing would bring this to about {est['after'] / 1024 ** 3:.1f} GB.")


@main.command(name='optimize')
def optimize_command():
    """Convert old whole-file storage into the shared, byte-exact format."""
    est = core.repo.estimate_optimized()
    if not est:
        return print('Already optimized.')
    print(f"{est['current'] / 1024 ** 3:.1f} GB now, about {est['after'] / 1024 ** 3:.1f} GB after.")
    if click.confirm('Convert now? Each archive is verified before its old copy is removed.'):
        core.repo.optimize()


@main.command(name='path')
def path_command():
    """The path to the restored game folder."""
    print(core.repo.current_path() if core.repo.current() else 'No current patch!')


@main.command(name='wipe')
def wipe_command():
    """The nuclear button. Removes all patches."""
    if click.confirm('Are you sure you want to remove all patches? THIS CANNOT BE UNDONE.'):
        core.repo.wipe()


if __name__ == '__main__':
    main()
