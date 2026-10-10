"""Opt-in native context sources: Slack public channels, Jira projects and
documentation sites, kept current like GitHub, and the report that says
what Raven can read.

A connector only reads and transforms: given a connection, its client and
its per-item state, it returns a Batch of records, item state and the next
cursor. This module owns the rest: registration (an administrator attests
that the source is shared with every reader of the workspace), one write
transaction per pass through Store.add_record, the cursor that advances
only after that write, the error that keeps it, and the scheduler.

See docs/context-sources.md.
"""
import json
import os
import threading
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timedelta, timezone

from . import context_memory as cm
from .store import Invalid, repo_key

KINDS = ('slack', 'jira', 'docs')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS source_connections (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, repo TEXT NOT NULL, target TEXT NOT NULL,
 label TEXT NOT NULL DEFAULT '', options TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1,
 cursor TEXT NOT NULL DEFAULT '', last_attempt_at TEXT NOT NULL DEFAULT '',
 last_success_at TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
 stats TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT '',
 UNIQUE(kind, repo, target));
CREATE TABLE IF NOT EXISTS source_items (
 connection_id TEXT NOT NULL REFERENCES source_connections(id), item TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
 PRIMARY KEY(connection_id, item));
'''
ERROR_LIMIT = 500


def migrate(db):
    db.executescript(SCHEMA)


@dataclass
class Batch:
    """What one pass read: records for Store.add_record, per-item state to
    keep (None removes it), the next cursor, refusals by reason, and the
    label to show for the source."""
    records: list = dc_field(default_factory=list)
    items: dict = dc_field(default_factory=dict)
    cursor: str = ''
    refused: dict = dc_field(default_factory=dict)
    label: str = ''

    def refuse(self, reason, count=1):
        self.refused[reason] = self.refused.get(reason, 0) + count


class SourceError(Exception):
    """A source could not be read. retry_after, when set, is the seconds
    the provider asked Raven to wait."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def _module(kind):
    if kind == 'slack':
        from . import source_slack as module
    elif kind == 'jira':
        from . import source_jira as module
    elif kind == 'docs':
        from . import source_docs as module
    else:
        raise Invalid('Source kind must be slack, jira or docs')
    return module


def client_for(kind):
    """The live client from this process's settings, or None when the
    credentials it needs are not set."""
    return _module(kind).client_from_env()


def _row(store, connection_id):
    row = store.graph.db.execute('SELECT * FROM source_connections WHERE id=?', (connection_id,)).fetchone()
    if row is None:
        raise Invalid(f'No context source {connection_id!r}')
    return _view(row)


def _view(row):
    out = dict(row)
    out['options'] = json.loads(out['options'] or '{}')
    out['stats'] = json.loads(out['stats'] or '{}')
    out['enabled'] = bool(out['enabled'])
    return out


def connections(store, enabled_only=False):
    sql = 'SELECT * FROM source_connections' + (' WHERE enabled=1' if enabled_only else '') + ' ORDER BY kind, repo, target'
    return [_view(r) for r in store.graph.db.execute(sql).fetchall()]


def register(store, kind, repo, target, *, shared, options=None, by='', client=None):
    """Add or re-enable a source. `shared` is the administrator's
    attestation that everything in it may be read by every reader of this
    workspace; without it nothing is registered. The connector verifies
    the target with the live client before anything is stored."""
    module = _module(kind)
    if shared is not True:
        raise Invalid('Attest that this source is shared with every reader of this workspace (workspace-shared); '
                      'Raven has one audience and does not narrow records after import')
    repo = repo_key(str(repo or ''))
    if not repo or repo == 'local':
        raise Invalid('Name the repository (owner/name) these records belong to')
    if not isinstance(target, str) or not target.strip() or len(target) > 1000:
        raise Invalid('A source target is required')
    options = dict(options or {})
    target, options = module.normalize(target.strip(), options)
    client = client if client is not None else module.client_from_env()
    if client is None:
        raise Invalid(module.MISSING_CREDENTIALS)
    label = module.probe(client, target, options)
    at = cm.stamp()
    connection_id = cm.digest([kind, repo, target])[:16]
    with store.graph.transaction():
        existing = store.graph.db.execute('SELECT id FROM source_connections WHERE id=?', (connection_id,)).fetchone()
        if existing:
            store.graph.db.execute('UPDATE source_connections SET enabled=1, options=?, label=?, last_error=? WHERE id=?',
                                   (json.dumps(options, sort_keys=True), label, '', connection_id))
        else:
            store.graph.db.execute(
                'INSERT INTO source_connections(id,kind,repo,target,label,options,created_at,created_by) '
                'VALUES(?,?,?,?,?,?,?,?)', (connection_id, kind, repo, target, label,
                                            json.dumps(options, sort_keys=True), at, by))
        store.graph.append_event('context_source_registered', {'connection_id': connection_id, 'kind': kind,
                                                               'repo': repo, 'target': target, 'by': by,
                                                               'workspace_shared': True})
    return _row(store, connection_id)


def disable(store, connection_id, by=''):
    """Stop synchronizing. Records already imported stay as history."""
    _row(store, connection_id)
    with store.graph.transaction():
        store.graph.db.execute('UPDATE source_connections SET enabled=0 WHERE id=?', (connection_id,))
        store.graph.append_event('context_source_disabled', {'connection_id': connection_id, 'by': by})
    return _row(store, connection_id)


def _items(store, connection_id):
    return {r['item']: json.loads(r['detail']) for r in store.graph.db.execute(
        'SELECT item, detail FROM source_items WHERE connection_id=?', (connection_id,)).fetchall()}


def sync(store, connection_id, client=None):
    """One pass over one source. Network reads finish before any write;
    the records, item state and cursor are written in one transaction;
    a failure records the error and leaves the cursor where it was."""
    connection = _row(store, connection_id)
    if not connection['enabled']:
        raise Invalid('This context source is disabled')
    module = _module(connection['kind'])
    client = client if client is not None else module.client_from_env()
    at = cm.stamp()
    if client is None:
        _failed(store, connection_id, at, module.MISSING_CREDENTIALS)
        raise SourceError(module.MISSING_CREDENTIALS)
    try:
        batch = module.collect(connection, client, _items(store, connection_id))
    except (SourceError, Invalid, OSError, ValueError) as error:
        _failed(store, connection_id, at, str(error))
        raise
    imported = changed = 0
    try:
        with store.graph.transaction():
            current = store.graph.db.execute('SELECT enabled FROM source_connections WHERE id=?',
                                             (connection_id,)).fetchone()
            if not current or not current['enabled']:
                raise Invalid('This context source was disabled during the pass; nothing was written')
            for record in batch.records:
                result = store.add_record({**record, 'repo': connection['repo']})
                imported += 1
                changed += bool(result.get('changed'))
            for item, detail in batch.items.items():
                if detail is None:
                    store.graph.db.execute('DELETE FROM source_items WHERE connection_id=? AND item=?',
                                           (connection_id, item))
                else:
                    store.graph.db.execute(
                        'INSERT INTO source_items(connection_id,item,detail,updated_at) VALUES(?,?,?,?) '
                        'ON CONFLICT(connection_id,item) DO UPDATE SET detail=excluded.detail,updated_at=excluded.updated_at',
                        (connection_id, item, json.dumps(detail, sort_keys=True), at))
            totals = connection['stats']
            refused = dict(totals.get('refused') or {})
            for reason, count in batch.refused.items():
                refused[reason] = refused.get(reason, 0) + count
            stats = {'last_pass': {'records': imported, 'changed': changed, 'refused': batch.refused},
                     'refused': refused}
            store.graph.db.execute(
                'UPDATE source_connections SET cursor=?, last_attempt_at=?, last_success_at=?, last_error=?, stats=?, '
                'label=CASE WHEN ?<>\'\' THEN ? ELSE label END WHERE id=?',
                (batch.cursor or connection['cursor'], at, at, '', json.dumps(stats, sort_keys=True),
                 batch.label, batch.label, connection_id))
            store.graph.append_event('context_source_synced', {'connection_id': connection_id,
                                                               'records': imported, 'changed': changed,
                                                               'refused': batch.refused})
            store.graph._bump(connection['repo'])
    except Invalid as error:
        _failed(store, connection_id, at, str(error))
        raise
    return {'connection_id': connection_id, 'records': imported, 'changed': changed, 'refused': batch.refused}


def _failed(store, connection_id, at, message):
    with store.graph.transaction():
        store.graph.db.execute('UPDATE source_connections SET last_attempt_at=?, last_error=? WHERE id=?',
                               (at, message[:ERROR_LIMIT], connection_id))


def record_count(store, connection):
    """Records this source holds: its provider and namespace in its repository."""
    module = _module(connection['kind'])
    provider, namespace = module.identity(connection)
    row = store.graph.db.execute(
        'SELECT count(*) AS n FROM source_records WHERE repo=? AND provider=? AND namespace=?',
        (connection['repo'], provider, namespace)).fetchone()
    return row['n'] if row else 0


def _minutes():
    try:
        return max(1.0, float(os.environ.get('BRIDGE_SOURCE_SYNC_MINUTES', '15') or 15))
    except ValueError:
        return 15.0


def _source_report(store, connection, interval):
    module = _module(connection['kind'])
    credentials = module.client_from_env() is not None
    gaps = list(module.LIMITS)
    if not connection['enabled']:
        state = 'disabled'
    elif connection['last_error']:
        state = 'needs_attention'
        gaps.insert(0, connection['last_error'])
    elif not connection['last_success_at']:
        state = 'not_yet_synced'
    else:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(connection['last_success_at'])
        state = 'stale' if age > timedelta(minutes=interval * 3) else 'current'
    if not credentials:
        gaps.insert(0, module.MISSING_CREDENTIALS)
    do = {'needs_attention': 'Fix the error above, then run the source again',
          'not_yet_synced': 'Run the source once, or wait for the next pass',
          'stale': 'Check that Raven is running and the credentials still work',
          'disabled': 'Register it again to resume'}.get(state, '')
    return {'id': connection['id'], 'kind': connection['kind'], 'repo': connection['repo'],
            'target': connection['target'], 'label': connection['label'], 'state': state,
            'credentials_present': credentials, 'last_success_at': connection['last_success_at'],
            'last_attempt_at': connection['last_attempt_at'], 'records': record_count(store, connection),
            'refused': connection['stats'].get('refused') or {}, 'audience': 'workspace-shared (administrator attested)',
            'limits': gaps, 'do': do}


def report(store):
    """What context Raven can read, per source, and what is missing.
    Configuration and observed results only: a polled source cannot show
    deletions, and refused items are absent by design."""
    interval = _minutes()
    sources = [_source_report(store, c, interval) for c in connections(store)]
    configured = {s['kind'] for s in sources if s['state'] != 'disabled'}
    available = []
    for kind in KINDS:
        if kind in configured:
            continue
        module = _module(kind)
        available.append({'kind': kind, 'credentials_present': module.client_from_env() is not None,
                          'enable': module.ENABLE})
    from . import github
    repos = [{'repo': s['repo'], 'last_success_at': s.get('last_success_at') or '', 'last_error': s.get('last_error') or ''}
             for s in github.sync_states(store.graph) if s]
    try:
        from .context_connectors import status as airweave_status
        airweave = airweave_status(store)
    except Exception as error:  # the report never fails on one provider
        airweave = {'state': 'unknown', 'error': type(error).__name__}
    return {'sources': sources, 'not_configured': available, 'github': repos, 'airweave': airweave,
            'sync_minutes': interval,
            'note': ('Records are evidence, never approval. Only sources an administrator attested as shared with '
                     'every reader are imported; polling cannot see deletions.')}


def readiness(store):
    """Readiness entries for sources that need an administrator."""
    out = []
    for source in report(store)['sources']:
        if source['state'] in ('needs_attention', 'stale'):
            out.append({'key': 'context_source_' + source['id'], 'level': 'warn',
                        'what': f"{source['kind']} source {source['label'] or source['target']} is {source['state'].replace('_', ' ')}",
                        'do': source['do']})
    return out


class SourceSyncer:
    """Keeps every enabled source current from a background thread. One
    failure is recorded on its source and the loop goes on."""

    def __init__(self, store, minutes=None, clients=None):
        self.store = store
        self.minutes = minutes if minutes is not None else _minutes()
        self.clients = clients or {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None

    def tick(self):
        out = []
        for connection in connections(self.store, enabled_only=True):
            client = self.clients.get(connection['kind']) or client_for(connection['kind'])
            if client is None:
                continue
            try:
                out.append(sync(self.store, connection['id'], client))
            except SourceError as error:
                print(f"Raven sources: {connection['kind']} {connection['target']} failed: {error}")
                if error.retry_after:
                    self._stop.wait(min(error.retry_after, 900))
            except Exception as error:  # never let one source stop the loop
                print(f"Raven sources: {connection['kind']} {connection['target']}: {type(error).__name__}: {error}")
        return out

    def start(self):
        if self._thread is not None:
            return

        def loop():
            while not self._stop.is_set():
                self.tick()
                self._wake.wait(self.minutes * 60)
                self._wake.clear()
        self._thread = threading.Thread(target=loop, name='raven-source-sync', daemon=True)
        self._thread.start()

    def wake(self):
        self._wake.set()

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def main():
    import argparse
    from .store import Store
    parser = argparse.ArgumentParser(description='Native context sources: add, run, disable, report')
    parser.add_argument('action', choices=['add', 'sync', 'disable', 'status'])
    parser.add_argument('kind', nargs='?', choices=KINDS)
    parser.add_argument('--db', default=os.environ.get('DATABASE_URL') or '.bridge/bridge.db')
    parser.add_argument('--repo', default='')
    parser.add_argument('--target', default='', help='Slack channel ID, Jira project key, or documentation URL')
    parser.add_argument('--id', default='', help='sync/disable: the source id from status')
    parser.add_argument('--workspace-shared', action='store_true',
                        help='Attest the source is shared with every reader of this workspace')
    parser.add_argument('--max-pages', type=int, default=0, help='docs: page limit (default 200)')
    args = parser.parse_args()
    store = Store(args.db)
    try:
        if args.action == 'add':
            if not args.kind:
                parser.error('add needs a kind: slack, jira or docs')
            options = {'max_pages': args.max_pages} if args.max_pages else {}
            result = register(store, args.kind, args.repo, args.target, shared=args.workspace_shared,
                              options=options, by='cli')
        elif args.action == 'sync':
            result = [sync(store, args.id)] if args.id else SourceSyncer(store).tick()
        elif args.action == 'disable':
            result = disable(store, args.id, by='cli')
        else:
            result = report(store)
    except (Invalid, SourceError) as error:
        parser.exit(1, f'{error}\n')
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
