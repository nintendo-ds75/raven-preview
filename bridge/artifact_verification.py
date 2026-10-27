"""Operator-selected, sandbox-observed checks bound to actual candidate bytes.

Receipts attest an observation, not universal correctness or authority to ship.
Profile commands and optional held-out fixtures are never supplied by an agent.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from . import workbench_fs as fs
from .store import Invalid, now

LIMITATIONS = [
    'Passing checks cover only the configured commands and fixtures, not all intended behavior.',
    'Repository-owned tests may have been changed by the coding agent.',
    'Trusted fixtures are copied from operator storage; sandbox mutation is detected, not prevented by a read-only mount.',
    'Checks share a process sandbox with candidate code. Post-run hashing cannot detect mutate-and-restore attacks; this is not a hostile-code oracle.',
    'No receipt grants publication, deployment or other external-action authority.',
]


def migrate(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS artifact_verification_receipts (
      id TEXT PRIMARY KEY, task_id TEXT NOT NULL, profile_id TEXT NOT NULL,
      status TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);''')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _tree(root):
    result, total = {}, 0
    for path in Path(root).rglob('*'):
        rel = path.relative_to(root)
        if not any(p in fs.IGNORED for p in rel.parts) and path.is_symlink():
            raise Invalid('Verification input cannot contain symlinks')
    for rel in fs.files(root):
        path = fs.safe_path(root, rel)
        content = path.read_bytes()
        total += len(content)
        if len(content) > fs.MAX_FILE or total > fs.MAX_TOTAL:
            raise Invalid('Candidate exceeds independent verification limits')
        result[rel] = {'sha256': hashlib.sha256(content).hexdigest(),
                       'mode': 0o755 if path.stat().st_mode & 0o111 else 0o644, 'bytes': len(content)}
    return result


def _stamp(root):
    """Every file the tree hash covers with its size, modification time and
    executable bit: the git patch omits ignored files, so the patch alone
    cannot say whether the candidate's bytes are still the hashed ones."""
    stamp = []
    for rel in fs.files(root):
        status = fs.safe_path(root, rel).stat()
        stamp.append([rel, status.st_size, status.st_mtime_ns, bool(status.st_mode & 0o111)])
    return stamp


def _copy(root, target, listing):
    for rel, entry in listing.items():
        content = fs.safe_path(root, rel).read_bytes()
        if hashlib.sha256(content).hexdigest() != entry['sha256']:
            raise Invalid('Candidate changed while preparing verification')
        path = fs.safe_path(target, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(entry['mode'])


def _safe_external(path, workbench):
    raw = Path(path)
    if not raw.is_absolute() or raw.is_symlink():
        raise Invalid('Verification profiles and fixtures require absolute non-symlink operator paths')
    resolved = raw.resolve()
    if resolved.is_relative_to(workbench.root.resolve()):
        raise Invalid('Verification configuration cannot be stored in agent task copies')
    for ancestor in raw.parents:
        # A symlinked ancestor the operating system owns (macOS /tmp and /var
        # point into /private) is not a path an agent or operator can repoint.
        if ancestor.is_symlink() and ancestor.lstat().st_uid != 0:
            raise Invalid('Verification configuration cannot traverse symlinks')
    return resolved


def _decisions(workbench, task_id):
    from .canvas import get_tree, _flatten
    from .approval_scope import revision
    tree = get_tree(workbench.store, task_id)
    nodes = _flatten(tree['nodes'])
    if tree.get('needs_review') or tree['counts'].get('blocking') or any(n.get('blocking') or n.get('needs_review') for n in nodes):
        raise Invalid('Resolve current judgment and source-review gates before verification')
    vector = []
    for node in nodes:
        decision = workbench.store.get_decision(node['node_id'])
        vector.append({'id': node['node_id'], 'revision': revision(decision),
                       'source_pins_sha256': _digest(node.get('sources', [])),
                       'canonical_sha256': _digest(decision.get('canonical')),
                       'authorized': bool(node.get('authorized'))})
    return sorted(vector, key=lambda d: d['id'])


class Verifier:
    def __init__(self, workbench, profile_file=None):
        self.workbench = workbench
        self.profile_file = profile_file if profile_file is not None else os.environ.get('RAVEN_VERIFICATION_PROFILES', '')

    def _profiles(self):
        if not self.profile_file:
            return []
        path = _safe_external(self.profile_file, self.workbench)
        try:
            if path.stat().st_size > 65536:
                raise Invalid('Verification profile file exceeds 64 KiB')
            value = json.loads(path.read_text())
        except (OSError, ValueError) as error:
            raise Invalid('Operator verification profiles could not be read') from error
        if not isinstance(value, list) or len(value) > 50:
            raise Invalid('Verification profiles must be an array of at most 50 profiles')
        seen = set()
        for profile in value:
            if not isinstance(profile, dict) or set(profile) - {'id','label','repository','commands','overlay_dir','coverage'}:
                raise Invalid('Unsupported verification profile fields')
            for key in ('id','repository'):
                if not isinstance(profile.get(key), str) or not profile[key].strip() or len(profile[key]) > 100:
                    raise Invalid('Verification profile requires a bounded id and repository')
            if profile['id'] in seen:
                raise Invalid('Verification profile IDs must be unique')
            seen.add(profile['id'])
            commands = profile.get('commands')
            if not isinstance(commands, list) or not 1 <= len(commands) <= 10 or any(
                    not isinstance(c, str) or not c.strip() or len(c) > 8000 for c in commands):
                raise Invalid('Verification profile requires one to ten bounded commands')
            for key in ('label','coverage'):
                if key in profile and (not isinstance(profile[key], str) or len(profile[key]) > 2000):
                    raise Invalid('Verification profile labels and coverage must be bounded text')
            if 'overlay_dir' in profile and not isinstance(profile['overlay_dir'], str):
                raise Invalid('Trusted fixture directory must be an absolute operator path')
            if profile.get('overlay_dir'):
                directory = _safe_external(profile['overlay_dir'], self.workbench)
                if not directory.is_dir():
                    raise Invalid('Trusted fixture directory is unavailable')
        return value

    def config(self):
        try:
            profiles = self._profiles()
        except Invalid as error:
            return {'ready': False, 'profiles': [], 'blockers': [str(error)]}
        blockers = []
        if not profiles:
            blockers.append('Configure RAVEN_VERIFICATION_PROFILES with operator-owned checks')
        if self.workbench.sandbox is None:
            blockers.append('Configure the existing isolated test sandbox')
        return {'ready': not blockers, 'profiles': [{k: p[k] for k in ('id','label','repository','coverage') if k in p} |
            {'trusted_fixtures': bool(p.get('overlay_dir'))} for p in profiles], 'blockers': blockers,
            'assurance': 'independent_profile', 'limitations': LIMITATIONS}

    def _snapshot(self, task_id, *, reuse=False):
        """The current binding of a task's candidate. A receipt-producing
        call hashes the actual tree; a listing may reuse the last tree hash
        while the base revision, patch digest, decision vector and the
        files' sizes and times are the ones it was computed under, so
        polling does not reread every byte."""
        from .visibility import require_task
        require_task(self.workbench.store, task_id)
        row = self.workbench.row(task_id)
        if row['status'] != 'completed':
            raise Invalid('Independent verification requires a completed, idle coding task')
        root = self.workbench.root / task_id
        if not root.is_dir() or not row['revision']:
            raise Invalid('The actual candidate checkout and base revision are required')
        from .objectives import assert_objective_authority
        objectives = []
        for binding in self.workbench.store.graph.db.execute('SELECT a.objective_id,a.proposal_id,o.lifecycle,o.revision FROM objective_run_authority a JOIN objectives o ON o.id=a.objective_id WHERE a.task_id=?', (task_id,)):
            assert_objective_authority(self.workbench.store, binding['objective_id'], binding['proposal_id'])
            objectives.append(dict(binding))
        patch = fs.diff(root)
        binding = {'base_revision': row['revision'], 'repository': row['repository'],
                   'patch_sha256': hashlib.sha256(patch.encode()).hexdigest(), 'patch_bytes': len(patch.encode()),
                   'objectives': objectives, 'decisions': _decisions(self.workbench, task_id)}
        key = _digest({**{k: binding[k] for k in ('base_revision', 'patch_sha256', 'decisions')}, 'files': _stamp(root)})
        cache = self.workbench.__dict__.setdefault('_verification_trees', {})
        cached = cache.get(task_id)
        if reuse and cached and cached[0] == key:
            tree = cached[1]
        else:
            tree = _tree(root)
            if len(cache) >= 64:
                cache.pop(next(iter(cache)))
            cache[task_id] = (key, tree)
        return {**binding, 'candidate_tree_sha256': _digest(tree)}, tree

    def _profile(self, profile_id, repository):
        profile = next((p for p in self._profiles() if p['id'] == profile_id), None)
        if not profile or profile['repository'] != repository:
            raise Invalid('Choose an operator verification profile for this exact repository')
        overlay = _tree(Path(profile['overlay_dir'])) if profile.get('overlay_dir') else {}
        return profile, overlay, _digest({'profile': profile, 'trusted_fixtures': overlay})

    def verify(self, task_id, profile_id, actor):
        if actor is None or actor.kind == 'agent' or not actor.allows('member'):
            raise Invalid('A workspace member must request independent verification')
        if self.workbench.sandbox is None:
            return {'status': 'unavailable', 'independently_verified': False,
                    'reason': 'The isolated test sandbox is not configured'}
        before, listing = self._snapshot(task_id)
        profile, overlay, profile_hash = self._profile(profile_id, before['repository'])
        root = self.workbench.root / task_id
        results, status, reason = [], 'passed', ''
        # The coding harness cannot choose the commands, paths, profile or
        # expected results. The same no-network sandbox executes the checks on
        # a disposable copy; no returned files can modify the actual candidate.
        with tempfile.TemporaryDirectory(prefix='raven-verification-') as temporary:
            copy = Path(temporary)
            _copy(root, copy, listing)
            if overlay:
                _copy(Path(profile['overlay_dir']), copy, overlay)
            prepared = _tree(copy)
            for command in profile['commands']:
                try:
                    result = self.workbench.sandbox.run(copy, command)
                    if (not isinstance(result, dict) or type(result.get('exit_code')) is not int
                            or type(result.get('timed_out')) is not bool or not isinstance(result.get('output'), str)):
                        raise Invalid('Sandbox returned no valid observed command result')
                    result = {k: result[k] for k in ('exit_code','timed_out','output')}
                    result['output'] = result['output'][:64000]
                    results.append({'command': command, **result})
                    changed = _tree(copy)
                    # Generated files may be useful output, but the executed
                    # source/fixtures themselves must not change during checks.
                    if any(changed.get(rel) != entry for rel, entry in prepared.items()):
                        status, reason = 'invalidated', 'Verification commands changed candidate or trusted fixture inputs'
                        break
                    if result['exit_code'] != 0 or result['timed_out']:
                        status, reason = 'failed', 'An operator-selected command failed or timed out'
                        break
                except (Invalid, OSError, ValueError) as error:
                    status, reason = 'unavailable', 'Isolated verification could not produce a valid result: ' + type(error).__name__
                    break
        try:
            after, _ = self._snapshot(task_id)
            _, _, current_profile_hash = self._profile(profile_id, before['repository'])
            if after != before or current_profile_hash != profile_hash:
                status, reason = 'stale', 'Candidate, judgment scope or operator profile changed during verification'
        except Invalid:
            status, reason = 'stale', 'Candidate access, task state or judgment gate changed during verification'
        payload = {'schema': 'raven-artifact-verification-v1', 'task_id': task_id, 'profile_id': profile_id,
                   'profile_sha256': profile_hash, 'assurance': 'independent_profile',
                   'coverage_basis': 'operator-held-fixtures' if overlay else 'repository-owned-tests-or-commands',
                   'coverage': profile.get('coverage', 'Only the configured commands; no universal behavioral guarantee'),
                   'status': status, 'reason': reason, 'independently_verified': status == 'passed',
                   'binding': before, 'commands': results, 'trusted_fixture_paths': sorted(overlay),
                   'limitations': LIMITATIONS, 'requested_by': actor.id, 'created_at': now()}
        with self.workbench.store.graph.transaction() as db:
            # Fence DB judgment and objective amendments through the receipt
            # commit, not merely through an earlier post-command snapshot.
            try:
                committed_binding, _ = self._snapshot(task_id)
                _, _, committed_profile = self._profile(profile_id, before['repository'])
                if committed_binding != before or committed_profile != profile_hash:
                    status = 'stale'
                    payload.update(status=status, independently_verified=False, reason='Inputs changed before verification receipt commit')
            except Invalid:
                status = 'stale'
                payload.update(status=status, independently_verified=False, reason='Judgment or access changed before verification receipt commit')
            ident = _digest(payload)
            db.execute('INSERT INTO artifact_verification_receipts VALUES(?,?,?,?,?,?)',
                       (ident, task_id, profile_id, status, json.dumps(payload), payload['created_at']))
        from .visibility import require_task
        require_task(self.workbench.store, task_id)  # Do not return copied output after access revocation.
        return {'id': ident, **payload}

    def list(self, task_id):
        from .visibility import require_task
        require_task(self.workbench.store, task_id)
        self.workbench.row(task_id)
        receipts, binding, profiles = [], None, {}
        rows = list(self.workbench.store.graph.db.execute('SELECT * FROM artifact_verification_receipts WHERE task_id=? ORDER BY created_at DESC', (task_id,)))
        if rows:
            # One binding per listing, shared by every receipt.
            try:
                binding, _ = self._snapshot(task_id, reuse=True)
            except Invalid:
                binding = None
        for row in rows:
            payload = json.loads(row['payload'])
            current = False
            if binding is not None:
                try:
                    if row['profile_id'] not in profiles:
                        profiles[row['profile_id']] = self._profile(row['profile_id'], binding['repository'])[2]
                    current = binding == payload['binding'] and profiles[row['profile_id']] == payload['profile_sha256']
                except Invalid:
                    pass
            receipts.append({'id': row['id'], **payload, 'current': current,
                             'usable_pass': current and payload['status'] == 'passed'})
        return receipts
