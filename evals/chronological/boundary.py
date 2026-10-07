"""Strict chronological supplements to evals.audit.fixture, without a new runner."""
from pathlib import Path
import subprocess

from evals.audit.fixture import git, inventory, timestamp, verify_fixture


def select_cutoff(repo, cutoff):
    """Return the first main-line ancestor strictly before the given UTC instant.

    HEAD must already identify the intended main branch. Scan in topological
    first-parent order rather than using Git's inclusive --before predicate.
    """
    ceiling = timestamp(cutoff)
    for row in git(repo, 'log', '--first-parent', '--format=%H %cI', 'HEAD').splitlines():
        oid, committed = row.split(' ', 1)
        if timestamp(committed) < ceiling:
            return oid
    raise ValueError('No first-parent commit strictly before cutoff')


def verify_history(repo, cutoff, expected_commit):
    """Independently check exact closure and strict temporal/path boundaries.

    Run on the trusted evaluator's freshly frozen export, before the host can
    edit its Git configuration. This does not enforce UID/network isolation.
    """
    root = Path(repo).resolve()
    boundary = timestamp(cutoff)
    # NUL separation avoids Git's quotePath representation for tabs, newlines
    # and non-ASCII filenames. Check paths before the general inventory helper.
    tracked = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z'])
    for raw in tracked.split(b'\0'):
        if not raw:
            continue
        name = raw.decode('utf-8', errors='surrogateescape')
        path = root / name
        if path.is_symlink() and not path.resolve().is_relative_to(root):
            raise ValueError('Historical export contains an escaping symlink')
    result = inventory(root)
    if result['baseline'] != expected_commit:
        raise ValueError('Unexpected historical baseline')
    for row in git(root, 'log', '--format=%H %cI %aI', expected_commit).splitlines():
        _, committed, authored = row.split()
        if timestamp(committed) >= boundary or timestamp(authored) >= boundary:
            raise ValueError('Reachable author or committer time is not strictly before cutoff')
    for row in git(root, 'ls-tree', '-r', expected_commit).splitlines():
        if row.startswith('160000 '):
            raise ValueError('Submodule needs a separate admitted historical export')
    git_dir = root / '.git'
    if not git_dir.is_dir() or git_dir.is_symlink():
        raise ValueError('Historical export must use its own Git directory')
    if list(git_dir.rglob('*.promisor')):
        raise ValueError('Promisor object route is not permitted')
    refs = git(root, 'for-each-ref', '--format=%(refname) %(objectname)').splitlines()
    # The canonical freezer produces a detached checkout without branch refs.
    if refs:
        raise ValueError('Unexpected branch, tag or other ref in frozen export')
    if git(root, 'status', '--porcelain'):
        raise ValueError('Historical export is not clean')
    return {**result, 'cutoff_exclusive_utc': cutoff,
            'strict_author_and_committer_time': True,
            'submodules': False, 'promisor': False, 'refs': []}


def verify_chronological_fixture(destination):
    """Compose the existing fixture/hash check with the stricter cutoff policy."""
    manifest = verify_fixture(destination)
    boundary = verify_history(Path(destination) / 'checkout', manifest['cutoff'], manifest['baseline'])
    return {'fixture': manifest, 'chronological_boundary': boundary}
