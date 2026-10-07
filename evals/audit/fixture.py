"""Create a history-preserving checkout that contains no post-baseline objects."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from .ledger import digest, encoded


def git(repo, *args, env=None, data=None):
    return subprocess.check_output(['git', '-C', str(repo), *args], input=data,
                                   env=env, stderr=subprocess.PIPE).decode().strip()


def timestamp(value):
    d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('Cutoffs and source timestamps require a timezone')
    return d.astimezone(timezone.utc)


def validate_records(records, cutoff):
    """Validate supplied historical snapshots, never slice today's edited body."""
    boundary = timestamp(cutoff)
    ids = set()
    for record in records:
        required = ('id', 'provider', 'namespace', 'body', 'version', 'created_at',
                    'updated_at', 'snapshot_at', 'provenance', 'acl')
        if any(k not in record for k in required):
            raise ValueError('Each source needs identity, version, body, historical timestamps, provenance and ACL')
        key = (record['provider'], record['namespace'], record['id'])
        if key in ids:
            raise ValueError('Duplicate source identity in frozen fixture')
        ids.add(key)
        created, updated, captured = (timestamp(record[k]) for k in ('created_at', 'updated_at', 'snapshot_at'))
        if not created <= updated <= captured <= boundary:
            raise ValueError(f'Source {record["id"]} is not a proven pre-cutoff snapshot')
        if not isinstance(record['acl'], list) or not record['acl'] or not record['provenance']:
            raise ValueError('A source needs an explicit readable audience and snapshot provenance')
    return records


def inventory(repo):
    head = git(repo, 'rev-parse', 'HEAD')
    objects = sorted(git(repo, 'cat-file', '--batch-all-objects', '--batch-check=%(objectname)').splitlines())
    reachable = sorted(line.split(' ', 1)[0] for line in git(repo, 'rev-list', '--objects', head).splitlines())
    if objects != reachable:
        raise ValueError('Checkout contains objects outside its baseline history')
    if git(repo, 'remote') or (Path(repo) / '.git/objects/info/alternates').exists():
        raise ValueError('Frozen checkout must not contain remotes or alternate object stores')
    root = Path(repo).resolve()
    for name in git(repo, 'ls-files').splitlines():
        path = root / name
        if path.is_symlink() and not path.resolve().is_relative_to(root):
            raise ValueError('Frozen checkout contains an escaping symlink')
    return {'baseline': head, 'tree': git(repo, 'rev-parse', 'HEAD^{tree}'),
            'objects_sha256': digest(objects), 'object_count': len(objects),
            'history_count': int(git(repo, 'rev-list', '--count', head))}


def freeze(repo, baseline, cutoff, destination, brief, *, records=(), brief_provenance=None):
    repo, destination = Path(repo).resolve(), Path(destination).resolve()
    if destination == repo or repo in destination.parents:
        raise ValueError('Fixture must be outside the source checkout')
    if destination.exists():
        raise ValueError('Refusing to overwrite an existing fixture')
    cutoff_time = timestamp(cutoff)
    baseline = git(repo, 'rev-parse', '--verify', baseline + '^{commit}')
    # Reject skewed/merged future commits, even when the selected tip is old.
    for line in git(repo, 'log', baseline, '--format=%H %cI').splitlines():
        if timestamp(line.split(' ', 1)[1]) > cutoff_time:
            raise ValueError('A reachable commit is newer than the cutoff')
    records = validate_records(list(records), cutoff)
    provenance = brief_provenance or {}
    if provenance.get('kind') not in ('preexisting_request', 'blinded_reconstruction') or not provenance.get('reviewer'):
        raise ValueError('Brief needs provenance kind and a named leakage reviewer')
    if not isinstance(brief, str) or not brief.strip():
        raise ValueError('A task brief is required')
    if provenance.get('reviewed_brief_sha256') != hashlib.sha256(brief.encode()).hexdigest():
        raise ValueError('Leakage review must bind to these exact brief bytes')
    if any(provenance.get(k) is not True for k in ('no_solution', 'no_expected_people', 'no_future_references')):
        raise ValueError('Reviewer must explicitly assess solution, identity and future-reference leakage')
    destination.mkdir(parents=True)
    checkout = destination / 'checkout'
    subprocess.run(['git', 'init', '-q', str(checkout)], check=True)
    # Fetch the exact closure into a new object store. A detached checkout in
    # the original clone would still expose future refs, reflogs and objects.
    git(checkout, 'fetch', '--no-tags', str(repo), baseline)
    git(checkout, 'checkout', '-q', '--detach', baseline)
    (checkout / '.git/FETCH_HEAD').unlink(missing_ok=True)
    (checkout / '.git/logs/HEAD').unlink(missing_ok=True)
    info = inventory(checkout)
    manifest = {'schema': 'raven-replay-fixture-v1', 'cutoff': cutoff, **info,
                'brief_sha256': hashlib.sha256(brief.encode()).hexdigest(),
                'records_sha256': digest(records), 'brief_provenance': provenance,
                'boundary': 'Only mount checkout and records into the host; filesystem/network isolation is external.'}
    manifest['sha256'] = digest(manifest)
    (destination / 'manifest.json').write_text(encoded(manifest) + '\n')
    (destination / 'brief.txt').write_text(brief)
    (destination / 'records.json').write_text(encoded(records) + '\n')
    return manifest


def verify_fixture(destination):
    root = Path(destination)
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest['sha256'] != digest({k: v for k, v in manifest.items() if k != 'sha256'}):
        raise ValueError('Fixture manifest changed')
    actual = inventory(root / 'checkout')
    if any(actual[k] != manifest[k] for k in actual):
        raise ValueError('Frozen history differs from the manifest')
    if git(root / 'checkout', 'status', '--porcelain'):
        raise ValueError('Frozen working tree is dirty')
    if hashlib.sha256((root / 'brief.txt').read_bytes()).hexdigest() != manifest['brief_sha256']:
        raise ValueError('Task brief changed')
    records = json.loads((root / 'records.json').read_text())
    if digest(records) != manifest['records_sha256']:
        raise ValueError('Source snapshots changed')
    validate_records(records, manifest['cutoff'])
    return manifest


def attest_patch(repo, baseline, submitted):
    """Capture actual bytes and test applicability, independently of host claims."""
    repo = Path(repo).resolve()
    baseline = git(repo, 'rev-parse', '--verify', baseline + '^{commit}')
    actual = subprocess.check_output(['git', '-C', str(repo), 'diff', '--binary', '--no-ext-diff', baseline, '--'])
    submitted = submitted.encode() if isinstance(submitted, str) else submitted
    untracked = git(repo, 'ls-files', '--others', '--exclude-standard').splitlines()
    with tempfile.TemporaryDirectory(prefix='raven-patch-check-') as tmp:
        env = {**os.environ, 'GIT_INDEX_FILE': str(Path(tmp) / 'index')}
        git(repo, 'read-tree', baseline, env=env)
        result = subprocess.run(['git', '-C', str(repo), 'apply', '--cached', '--check', '-'],
                                input=submitted, env=env, capture_output=True)
    return {'actual_sha256': hashlib.sha256(actual).hexdigest(),
            'submitted_sha256': hashlib.sha256(submitted).hexdigest(),
            'exact_match': actual == submitted, 'applies': result.returncode == 0,
            'untracked_files': untracked, 'diagnostics': result.stderr.decode(errors='replace'),
            'pass': actual == submitted and result.returncode == 0 and not untracked}
