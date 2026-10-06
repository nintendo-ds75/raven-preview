"""Exact, read-only handover from an external identity to stored source links.

This index does not retrieve providers, infer installations, or reinterpret
source lifecycle as adopted policy. Lookup responses contain metadata only.
"""
import json
from urllib.parse import urlsplit

MAX_ITEMS = 100
DEFAULT_LIMIT = 25


def _page(rows, limit, render=dict):
    """One lookahead row detects truncation without unbounded materialization."""
    rows = list(rows)
    return {'items': [render(row) for row in rows[:limit]],
            'limit': limit, 'truncated': len(rows) > limit}


def _metadata(values):
    """An imported identifier can itself be a URL. Never echo URL credentials.

    Selection uses the untouched exact key; output redaction is explicit and
    must never be interpreted as a new/normalized external identity.
    """
    redacted = []
    for key, value in values.items():
        if not isinstance(value, str) or not ('://' in value or value.startswith('//')):
            continue
        try:
            url = urlsplit(value)
            private = bool(url.username or url.password or url.query or url.fragment or '@' in value)
        except ValueError:
            private = True
        if private:
            values[key] = ''
            redacted.append(key)
    if redacted:
        values['redacted_fields'] = redacted
    return values


def _version(row, head_id):
    snapshot = json.loads(row['snapshot'])
    # Deliberate allowlist: no source body, locator, URL, audience metadata,
    # notification, transcript, author, or alleged approval enters this index.
    return _metadata({'source_version_id': row['id'], 'sequence': row['sequence'],
            'latest_observed': row['id'] == head_id,
            'provider': snapshot.get('provider', ''), 'namespace': snapshot.get('namespace', ''),
            'object_kind': snapshot.get('object_kind', snapshot.get('kind', '')),
            'external_id': snapshot.get('external_id', ''), 'ref': snapshot.get('display_ref', ''),
            'status': snapshot.get('status', ''), 'resolved': bool(snapshot.get('resolved', False)),
            'availability': snapshot.get('availability', 'available'),
            'source_created_at': snapshot.get('source_created_at', ''),
            'source_updated_at': row['source_updated_at'], 'source_sequence': row['source_sequence'],
            'source_version': row['source_version'], 'observed_at': row['observed_at'],
            'provenance': row['provenance'], 'fingerprint': row['fingerprint']})


def lookup(db, *, repo, external_id='', ref='', provider='', namespace='', object_kind='', limit=DEFAULT_LIMIT):
    """Caller owns a consistent read snapshot; all matching stays in SQL."""
    from .store import Invalid, repo_key
    values = {'repo': repo, 'external_id': external_id, 'ref': ref, 'provider': provider,
              'namespace': namespace, 'object_kind': object_kind}
    bounds = {'repo': 500, 'external_id': 500, 'ref': 200, 'provider': 30, 'namespace': 1000, 'object_kind': 30}
    for key, value in values.items():
        if not isinstance(value, str) or len(value) > bounds[key]:
            raise Invalid(f'{key} must be text of at most {bounds[key]} characters')
        if value != value.strip():
            raise Invalid(f'{key} must be an exact stored key without surrounding whitespace')
    if not repo or repo != repo_key(repo):
        raise Invalid('repo must be the exact stored repository key, not a path or URL')
    if bool(external_id) == bool(ref):
        raise Invalid('Supply exactly one of external_id or ref; no text search or URL inference is performed')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ITEMS:
        raise Invalid(f'limit must be an integer from 1 to {MAX_ITEMS}')

    identity_where, identity_args = [], []
    for key in ('provider', 'namespace', 'object_kind', 'external_id'):
        if values[key]:
            identity_where.append(f'i.{key}=?')
            identity_args.append(values[key])
    identity_filter = ''.join(' AND ' + clause for clause in identity_where)
    predicate = ('s.repo=? AND EXISTS (SELECT 1 FROM source_identities i '
                 'WHERE i.record_id=s.id AND i.repo=s.repo' + identity_filter + ')')
    args = [repo, *identity_args]
    if ref:
        predicate += ' AND s.display_ref=?'
        args.append(ref)
    # Do not filter availability: an inaccessible duplicate must not silently
    # select a different installation's record. Neither recency nor prose rank.
    rows = db.execute('SELECT s.* FROM source_records s WHERE ' + predicate +
                      ' ORDER BY s.provider,s.namespace,s.object_kind,s.external_id,s.id LIMIT ?',
                      [*args, limit + 1]).fetchall()
    result = {'status': 'unknown', 'repo': repo,
              'notice': 'No exact stored match. This does not establish provider absence or access; no provider was fetched.'}
    if not rows:
        return result

    def candidate(row):
        identities = db.execute('SELECT i.provider,i.namespace,i.object_kind,i.external_id FROM source_identities i '
                                'WHERE i.record_id=? AND i.repo=?' + identity_filter +
                                ' ORDER BY i.provider,i.namespace,i.object_kind,i.external_id LIMIT ?',
                                [row['id'], repo, *identity_args, limit + 1]).fetchall()
        return _metadata({'record_id': row['id'], 'repo': repo, 'ref': row['display_ref'],
                'availability': row['availability'],
                'matching_identities': _page(identities, limit, lambda identity: _metadata(dict(identity)))})

    if len(rows) > 1:
        result.update(status='ambiguous', candidates=_page(rows, limit, candidate),
                      disambiguation=['provider', 'namespace', 'object_kind', 'external_id'],
                      notice='Multiple exact stored records match. Select a candidate identity and repeat lookup; no record or policy was selected.')
        return result
    record = rows[0]
    result['source'] = candidate(record)
    if record['availability'] != 'available':
        result.update(status='unavailable', notice='The exact source is unavailable. Retained versions and links are withheld; no provider was fetched.')
        return result
    head = db.execute('SELECT * FROM source_versions WHERE record_id=? AND id=?',
                      (record['id'], record['head_id'])).fetchone()
    if head is None:
        result.update(status='unknown', notice='The exact stored source has no readable observed head; no content or policy is inferred.')
        return result
    result.update(status='matched', latest_observed=_version(head, record['head_id']),
                  observation_basis='stored observations only; local sequence orders observations, not adoption',
                  notice='Links are explicit stored associations, never approval. Latest observed source state is not currently adopted policy. Read the linked decision/task for its scope and current authority. Source bodies and URLs are omitted; exact-record reads remain separate.')
    versions = db.execute('SELECT * FROM source_versions WHERE record_id=? ORDER BY sequence DESC LIMIT ?',
                          (record['id'], limit + 1)).fetchall()
    from .context_memory import latest_observation
    result['latest_observation'] = _metadata(latest_observation(record))
    result['versions'] = _page(versions, limit, lambda row: _version(row, record['head_id']))
    result['versions']['order'] = 'newest observation first'

    def pin(row):
        return {'pinned_source_version_id': row['source_version_id'],
                'latest_observed_source_version_id': record['head_id'],
                'pin_matches_latest_observed': row['source_version_id'] == record['head_id']}

    anchors = db.execute('SELECT a.*,r.status AS task_status,r.needs_review AS task_needs_review '
                         'FROM task_source_anchors a JOIN runs r ON r.id=a.task_id '
                         'WHERE a.record_id=? AND r.repo=? '
                         'ORDER BY a.task_id,a.role,a.source_version_id LIMIT ?',
                         (record['id'], repo, limit + 1)).fetchall()
    result['task_anchors'] = _page(anchors, limit, lambda row: {
        'relation': 'task_anchor', 'task_id': row['task_id'], 'role': row['role'],
        'task_status': row['task_status'], 'task_needs_review': bool(row['task_needs_review']),
        'recorded_at': row['recorded_at'], **pin(row)})

    def decision(row):
        return {'decision_id': row['decision_id'], 'task_id': row['task_id'],
                'decision_status': row['decision_status'], 'decision_signoff': row['decision_signoff'],
                'decision_needs_review': bool(row['decision_needs_review']),
                'decision_updated_at': row['decision_updated_at'],
                'decision_superseded_by': row['decision_superseded_by'], 'task_status': row['task_status']}

    edges = db.execute('SELECT e.*,d.run_id AS task_id,d.status AS decision_status,d.signoff AS decision_signoff,'
                       'd.needs_review AS decision_needs_review,d.updated_at AS decision_updated_at,'
                       'd.superseded_by AS decision_superseded_by,r.status AS task_status '
                       'FROM decision_source_edges e JOIN decisions d ON d.id=e.decision_id JOIN runs r ON r.id=d.run_id '
                       'WHERE e.record_id=? AND d.repo=? AND r.repo=? AND d.draft=0 '
                       'ORDER BY e.active DESC,e.decision_id,e.recorded_at DESC,e.id LIMIT ?',
                       (record['id'], repo, repo, limit + 1)).fetchall()

    def source_edge(row):
        historical = (not row['active'] or row['task_status'] in ('completed', 'abandoned')
                      or row['decision_status'] in ('withdrawn', 'adopted', 'duplicate')
                      or bool(row['decision_superseded_by']))
        return {**decision(row), **pin(row), 'role': row['role'],
                'relation': 'decision_history' if historical else (
                    'decision_premise' if row['role'] in ('support', 'contradiction') else 'decision_association'),
                'edge_active': bool(row['active']), 'historical': historical,
                'stored_stale': bool(row['stale']), 'decision_version_id': row['decision_version_id'],
                'recorded_at': row['recorded_at'], 'retired_at': row['retired_at']}

    result['decision_sources'] = _page(edges, limit, source_edge)
    # A work-item anchors a task, not every decision's premises. Return those
    # decisions as a distinct association without inventing any evidence edges.
    linked = db.execute('SELECT a.*,d.id AS decision_id,d.run_id AS task_id,d.status AS decision_status,'
                        'd.signoff AS decision_signoff,d.needs_review AS decision_needs_review,'
                        'd.updated_at AS decision_updated_at,d.superseded_by AS decision_superseded_by,r.status AS task_status '
                        'FROM task_source_anchors a JOIN runs r ON r.id=a.task_id JOIN decisions d ON d.run_id=r.id '
                        'WHERE a.record_id=? AND r.repo=? AND d.repo=? AND d.draft=0 '
                        'ORDER BY d.id,a.role,a.source_version_id LIMIT ?',
                        (record['id'], repo, repo, limit + 1)).fetchall()
    result['task_decisions'] = _page(linked, limit, lambda row: {
        **decision(row), **pin(row), 'relation': 'task_anchor_association', 'anchor_role': row['role'],
        'notice': 'Task association only; this does not assert that the decision used this source as a premise.'})
    return result
