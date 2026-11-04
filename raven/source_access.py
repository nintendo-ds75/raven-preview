"""Fresh access observations for native connectors, separate from source history.

A transport failure means access is unverified, not that the document was deleted.
Both are excluded from new retrieval. A later authoritative read can restore
access without erasing the old versions or silently granting approval.
"""
import json
from datetime import datetime, timedelta, timezone

SCHEMA = '''CREATE TABLE IF NOT EXISTS native_source_access (
 record_id TEXT NOT NULL REFERENCES source_records(id),
 connection_id TEXT NOT NULL REFERENCES source_connections(id),
 state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', verified_until TEXT NOT NULL DEFAULT '',
 checked_at TEXT NOT NULL, PRIMARY KEY(record_id,connection_id));
CREATE INDEX IF NOT EXISTS native_source_connection ON native_source_access(connection_id,verified_until);
CREATE TABLE IF NOT EXISTS task_context_reads (
 task_id TEXT NOT NULL REFERENCES runs(id), decision_id TEXT NOT NULL REFERENCES decisions(id),
 revision TEXT NOT NULL, read_at TEXT NOT NULL, PRIMARY KEY(task_id,decision_id,revision));
CREATE TABLE IF NOT EXISTS task_source_reads (
 task_id TEXT NOT NULL REFERENCES runs(id), record_id TEXT NOT NULL REFERENCES source_records(id),
 source_version_id TEXT NOT NULL REFERENCES source_versions(id), read_at TEXT NOT NULL,
 PRIMARY KEY(task_id,record_id,source_version_id));
CREATE TABLE IF NOT EXISTS withheld_source_versions (
 source_version_id TEXT PRIMARY KEY REFERENCES source_versions(id),
 reason TEXT NOT NULL, observed_at TEXT NOT NULL);
'''


def migrate(db):
    db.executescript(SCHEMA)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def freshness_sql(alias='s'):
    return (f"NOT EXISTS (SELECT 1 FROM native_source_access na LEFT JOIN source_connections nc ON nc.id=na.connection_id "
            f"WHERE na.record_id={alias}.id AND (nc.id IS NULL OR nc.enabled=0 OR na.state<>'fresh' OR na.verified_until<=?))", [stamp()])


def observe(store, connection_id, record_id, *, state='fresh', reason=''):
    at = stamp()
    until = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat() if state == 'fresh' else ''
    store.graph.db.execute('INSERT INTO native_source_access VALUES(?,?,?,?,?,?) '
        'ON CONFLICT(record_id,connection_id) DO UPDATE SET state=excluded.state,reason=excluded.reason,'
        'verified_until=excluded.verified_until,checked_at=excluded.checked_at',
        (record_id, connection_id, state, reason, until, at))


def check_comment_access(store, record):
    """A readable issue does not establish access to comments it no longer shows.

    Keep old versions for private audit, but withhold their copied content. A
    partial provider comment page is not proof that omitted comments are public.
    """
    if record.get('provider') != 'jira':
        return
    scope = json.loads(record.get('access_scope') or '{}')
    current = set(scope.get('jira_comment_ids', []))
    db = store.graph.db
    for row in db.execute('SELECT v.id,v.snapshot FROM source_versions v JOIN source_records s ON s.id=v.record_id '
                          'WHERE s.repo=? AND s.provider=? AND s.namespace=? AND s.external_id=?',
                          (record['repo'], 'jira', record['namespace'], record['external_id'])).fetchall():
        prior = json.loads(row['snapshot'])
        old_scope = json.loads(prior.get('access_scope') or '{}')
        old_ids = old_scope.get('jira_comment_ids')
        missing = bool(set(old_ids) - current) if old_ids is not None else (
            bool(scope.get('jira_restricted_comments')) and 'Comment by ' in prior.get('body', ''))
        if missing:
            db.execute('INSERT INTO withheld_source_versions VALUES(?,?,?) ON CONFLICT(source_version_id) DO NOTHING',
                       (row['id'], 'A previously imported Jira comment is no longer confirmed workspace-readable', stamp()))


def known_db(db, connection):
    """Backfill connector bindings from explicit import scopes, never name guesses."""
    from .sources import _module
    provider, namespace = _module(connection['kind']).identity(connection)
    rows = db.execute('SELECT s.id,v.snapshot FROM source_records s JOIN source_versions v ON v.id=s.head_id '
                                 'WHERE s.repo=? AND s.provider=? AND s.namespace=? AND NOT EXISTS '
                                 '(SELECT 1 FROM native_source_access a WHERE a.record_id=s.id AND a.connection_id=?)',
                                 (connection['repo'], provider, namespace, connection['id'])).fetchall()
    for row in rows:
        snapshot = json.loads(row['snapshot'])
        try:
            scope = json.loads(snapshot.get('access_scope') or '{}')
        except (ValueError, TypeError):
            scope = {}
        matches = (connection['kind'] == 'docs' or
                   scope.get('jira_project') == connection['target'] or
                   scope.get('slack_channel') == connection['target'])
        if matches:
            db.execute('INSERT INTO native_source_access VALUES(?,?,?,?,?,?) ON CONFLICT(record_id,connection_id) DO NOTHING',
                       (row['id'], connection['id'], 'unverified', 'Imported before access leases', '', stamp()))
    return [dict(r) for r in db.execute('SELECT s.* FROM native_source_access a JOIN source_records s ON s.id=a.record_id WHERE a.connection_id=?',
                                                   (connection['id'],))]


def known(store, connection):
    return known_db(store.graph.db, connection)


def backfill(db):
    from .sources import _view
    for row in db.execute('SELECT * FROM source_connections').fetchall():
        known_db(db, _view(row))


def due(db, connection_id):
    """Bounded oldest-first exact reads, including items outside the sync cursor."""
    soon = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    return [dict(r) for r in db.execute('SELECT s.*,v.snapshot FROM native_source_access a '
        'JOIN source_records s ON s.id=a.record_id JOIN source_versions v ON v.id=s.head_id '
        'WHERE a.connection_id=? AND a.verified_until<=? ORDER BY a.checked_at,s.id LIMIT 50',
        (connection_id, soon))]


def suspend(store, connection, state, reason):
    rows = known(store, connection)
    for row in rows:
        observe(store, connection['id'], row['id'], state=state, reason=reason)
    store.graph._bump(connection['repo'])
    if rows:
        store.graph.append_event('source_access_changed', {'repo': connection['repo'], 'connection_id': connection['id'],
            'state': state, 'reason': reason, 'records': [r['id'] for r in rows]})


def blocked_records(db, repo):
    return [r['id'] for r in db.execute('SELECT DISTINCT s.id FROM native_source_access na JOIN source_records s ON s.id=na.record_id '
        'LEFT JOIN source_connections nc ON nc.id=na.connection_id WHERE s.repo=? '
        "AND (nc.id IS NULL OR nc.enabled=0 OR na.state<>'fresh' OR na.verified_until<=?) LIMIT 100", (repo, stamp()))]
