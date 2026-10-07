"""Airweave retrieval feeds the same versioned graph as human decisions.

Collections must be scoped by the operator to content shared with this Raven
workspace. Source ACLs are checked on each retrieval; cached use has a lease. Search observations are
excerpts, never whole-document snapshots, policy adoption, or authorization.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
import re
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

from . import context_memory as cm
from .store import Invalid


class AccessRejected(Invalid):
    pass


class SourceRejected(Invalid):
    """A bounded public reason code, without remote content or identifiers."""
    def __init__(self, reason, message):
        self.reason = reason
        super().__init__(message)


REJECTION_MESSAGES = {
    'missing_access_metadata': 'The connector did not provide source access metadata. This result is not compatible with Raven durable import; do not assume public access.',
    'missing_source_identity': 'The connector did not provide a stable source identity.',
    'transient_or_unsynced': 'Transient, federated or unsynced results are not stored as durable context.',
    'access_not_shared': 'Source access does not explicitly cover every configured workspace reader.',
    'invalid_source': 'The source observation did not meet the required identity, content or metadata contract.',
}

SCHEMA = '''
CREATE TABLE IF NOT EXISTS context_connections (
 repo TEXT PRIMARY KEY, collection TEXT NOT NULL, audience TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1, generation INTEGER NOT NULL DEFAULT 1,
 last_success TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS context_entities (
 record_id TEXT PRIMARY KEY REFERENCES source_records(id), repo TEXT NOT NULL,
 collection TEXT NOT NULL, entity_id TEXT NOT NULL, source_name TEXT NOT NULL,
 chunk_index INTEGER NOT NULL, generation INTEGER NOT NULL, verified_until TEXT NOT NULL,
 checked_at TEXT NOT NULL, state TEXT NOT NULL, query TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS context_refresh ON context_entities(repo,verified_until);
'''
MAX_RESULTS = 12
MAX_BYTES = 2_000_000
TTL_SECONDS = 300


def migrate(db):
    db.executescript(SCHEMA)


def now():
    return datetime.now(timezone.utc).isoformat()


def freshness_sql(alias='s'):
    from .slack_capture import pending_sql
    return (f"(NOT EXISTS (SELECT 1 FROM context_entities ce LEFT JOIN context_connections cc ON cc.repo=ce.repo "
            f"WHERE ce.record_id={alias}.id AND (ce.state<>'fresh' OR ce.verified_until<=? "
            "OR cc.repo IS NULL OR cc.enabled=0 OR cc.generation<>ce.generation)) "
            "AND NOT " + pending_sql(alias) + ")", [now()])


def fresh(db, record_id):
    predicate, args = freshness_sql()
    return bool(db.execute('SELECT 1 FROM source_records s WHERE s.id=? AND ' + predicate,
                           [record_id, *args]).fetchone())


def blocked_decisions(db, repo):
    if not db.execute('SELECT 1 FROM context_entities WHERE repo=? UNION ALL '
            'SELECT 1 FROM slack_captures sc JOIN slack_capture_mutations sm '
            'ON sm.workspace=sc.workspace AND sm.channel=sc.channel AND sm.message_ts=sc.message_ts '
            "WHERE sc.repo=? AND sc.record_id<>'' AND sm.clock>sc.latest_clock LIMIT 1", (repo, repo)).fetchone():
        return set()
    predicate, args = freshness_sql()
    rows = db.execute("WITH RECURSIVE blocked(id) AS (SELECT e.decision_id FROM decision_source_edges e "
        "JOIN source_records s ON s.id=e.record_id WHERE s.repo=? AND e.active=1 AND e.role IN ('support','contradiction') "
        "AND NOT " + predicate + " UNION SELECT links.decision_id FROM ("
        "SELECT id AS decision_id,source_id AS related_id FROM decisions WHERE coalesce(source_id,'')<>'' "
        "UNION SELECT id,parent_id FROM decisions WHERE parent_id<>'' AND independent_source_replacement=0 AND historical_parent_revalidated=0 "
        "UNION SELECT decision_id,related_id FROM decision_links WHERE kind IN ('derived','depends')"
        ") links JOIN blocked b ON b.id=links.related_id) SELECT id FROM blocked", [repo, *args])
    return {r['id'] for r in rows}


def configure(store, repo, collection, audience, *, shared=False, enabled=True):
    from .store import Invalid, repo_key
    repo = repo_key(repo)
    if not repo or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', collection):
        raise Invalid('An exact repository and an Airweave collection readable ID are required')
    if not shared:
        raise Invalid('The operator must confirm this collection contains only workspace-shared content')
    if not isinstance(audience, list) or not all(isinstance(p, str) and p and len(p) <= 300 for p in audience):
        raise Invalid('audience must list the source principal IDs of every workspace reader')
    audience = sorted(set(audience))
    with store.graph.transaction():
        db = store.graph.db
        previous = db.execute('SELECT * FROM context_connections WHERE repo=?', (repo,)).fetchone()
        serialized = cm.encoded(audience)
        if previous and (previous['collection'], previous['audience'], previous['enabled']) == (collection, serialized, int(enabled)):
            return status(store, repo)
        generation = previous['generation'] + 1 if previous else 1
        db.execute('INSERT INTO context_connections(repo,collection,audience,enabled,generation) VALUES(?,?,?,?,?) '
                   'ON CONFLICT(repo) DO UPDATE SET collection=excluded.collection,audience=excluded.audience,'
                   'enabled=excluded.enabled,generation=excluded.generation,last_success=\'\',last_error=\'\'',
                   (repo, collection, serialized, int(enabled), generation))
        for row in db.execute('SELECT record_id FROM context_entities WHERE repo=?', (repo,)).fetchall():
            _unavailable(store, row['record_id'], 'connection_changed')
        store.graph.append_event('context_connection_configured', {'repo': repo, 'collection': collection,
            'generation': generation, 'enabled': enabled, 'audience_count': len(audience)})
        store.graph._bump(repo)
    return status(store, repo)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Airweave:
    """Small REST adapter; no model calls, generated answers or credential logs."""
    def __init__(self, base=None, key=None):
        from .store import Invalid
        self.base = (base or os.environ.get('BRIDGE_AIRWEAVE_URL', '')).rstrip('/')
        self.key = key if key is not None else os.environ.get('AIRWEAVE_API_KEY', '')
        p = urlsplit(self.base)
        if (p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password or p.query or p.fragment
                or (p.scheme == 'http' and p.hostname not in ('localhost', '127.0.0.1', 'airweave'))):
            raise Invalid('Configure an HTTPS Airweave URL, or a local http://localhost or http://airweave service')
        if not self.key or '\n' in self.key or '\r' in self.key:
            raise Invalid('Configure AIRWEAVE_API_KEY in the server environment')

    def search(self, collection, query, *, filters=None, limit=MAX_RESULTS):
        from .store import Invalid
        body = {'query': query, 'retrieval_strategy': 'hybrid', 'limit': limit, 'offset': 0}
        if filters:
            body['filter'] = [{'conditions': filters}]
        request = Request(self.base + '/collections/' + quote(collection, safe='') + '/search/instant',
            data=json.dumps(body).encode(), headers={'x-api-key': self.key, 'Content-Type': 'application/json'})
        try:
            with build_opener(_NoRedirect).open(request, timeout=10) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise Invalid('Airweave response exceeds the import limit; nothing was truncated')
            value = json.loads(raw)
            if not isinstance(value, dict) or not isinstance(value.get('results'), list):
                raise Invalid('Airweave returned an unsupported search response')
            if len(value['results']) > limit:
                raise Invalid('Airweave exceeded the requested result limit')
            return value['results']
        except HTTPError as error:
            failure = AccessRejected if error.code in (401, 403) else Invalid
            raise failure(f'Airweave returned HTTP {error.code}; no response body or credentials were logged') from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise Invalid('Airweave could not be reached or returned invalid JSON') from None


def _connection(store, repo):
    row = store.graph.db.execute('SELECT * FROM context_connections WHERE repo=? AND enabled=1', (repo,)).fetchone()
    return dict(row) if row else None


def _entry(result, connection):
    """Validate documented SearchResult metadata before persisting any body."""
    from .store import Invalid
    if not isinstance(result, dict):
        raise Invalid('Airweave result is not an object')
    meta, access = result.get('airweave_system_metadata'), result.get('access')
    if not isinstance(meta, dict):
        raise SourceRejected('missing_source_identity', REJECTION_MESSAGES['missing_source_identity'])
    source = meta.get('source_name')
    # Federated Slack search is transient even if returned by another provider.
    if not isinstance(source, str) or source.lower() == 'slack' or not meta.get('sync_id'):
        raise SourceRejected('transient_or_unsynced', REJECTION_MESSAGES['transient_or_unsynced'])
    if not isinstance(access, dict):
        raise SourceRejected('missing_access_metadata', REJECTION_MESSAGES['missing_access_metadata'])
    audience, viewers = json.loads(connection['audience']), access.get('viewers')
    permitted = access.get('is_public') is True or (
        access.get('is_public') is False and bool(audience) and isinstance(viewers, list)
        and set(audience).issubset(set(v for v in viewers if isinstance(v, str))))
    if not permitted:
        raise AccessRejected('Source access is unknown or does not cover every configured workspace reader')
    entity, chunk = meta.get('original_entity_id'), meta.get('chunk_index')
    if not isinstance(entity, str) or not entity or len(entity) > 500 or type(chunk) is not int or chunk < 0:
        raise Invalid('Source identity or chunk index is invalid')
    name, body = result.get('name'), result.get('textual_representation')
    if not isinstance(name, str) or not isinstance(body, str) or not body.strip():
        raise Invalid('Source excerpt is empty or malformed')
    updated = result.get('updated_at') or ''
    if not isinstance(updated, str):
        raise Invalid('Source timestamp must be text')
    if updated:
        parsed = datetime.fromisoformat(updated.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise Invalid('Source timestamp needs a timezone')
    raw = result.get('raw_source_fields') or {}
    if not isinstance(raw, dict):
        raise Invalid('Source fields are malformed')
    # Status remains source data. Unknown adoption never becomes settled policy.
    fields = raw.get('fields') if isinstance(raw.get('fields'), dict) else raw
    state = fields.get('status', '')
    if isinstance(state, dict):
        state = state.get('name', '')
    if not isinstance(state, str):
        state = ''
    resolution = fields.get('resolution') or ''
    if isinstance(resolution, dict):
        resolution = resolution.get('name', '')
    if not isinstance(resolution, str):
        resolution = ''
    prefix = ('Source status: ' + state + '\n' if state else '') + ('Resolution: ' + resolution + '\n' if resolution else '')
    identity = cm.digest([source, entity, chunk])
    return {'repo': connection['repo'], 'kind': 'doc', 'ref': identity,
            'provider': 'generic', 'namespace': 'airweave:' + connection['collection'], 'external_id': identity,
            'title': name, 'body': 'Retrieved excerpt (not the complete source).\n' + prefix + body,
            'url': result.get('web_url') or '', 'created_at': result.get('created_at') or '',
            'updated_at': updated, 'source_version': cm.digest([body, state, resolution, access]),
            'access_scope': cm.encoded(access), 'status': state, 'resolved': False,
            'availability': 'available', 'paths': []}, (source, entity, chunk)


def _unavailable(store, record_id, reason):
    """Fail current reuse closed; immutable source history remains intact."""
    db = store.graph.db
    before = db.execute('SELECT state FROM context_entities WHERE record_id=?', (record_id,)).fetchone()
    db.execute('UPDATE context_entities SET state=?,verified_until=? WHERE record_id=?', (reason, now(), record_id))
    row = db.execute('SELECT head_id FROM source_records WHERE id=?', (record_id,)).fetchone()
    if row and before and before['state'] != reason and reason in ('connection_changed', 'access_revoked'):
        cm.invalidate(db, record_id, row['head_id'], include_current=True,
            reason_override='The external evidence could not be revalidated. Restore source access and review current evidence before using this answer.')


def _accept(store, connection, result, query, task_id=''):
    from .store import Invalid
    data, (source, entity, chunk) = _entry(result, connection)
    with store.graph.transaction():
        current = _connection(store, connection['repo'])
        if not current or current['generation'] != connection['generation']:
            raise Invalid('Context connection changed during retrieval; retry with the current configuration')
        imported = store.add_record(data)
        record_id = imported['source']['record_id']
        previous = store.graph.db.execute('SELECT state,verified_until FROM context_entities WHERE record_id=?', (record_id,)).fetchone()
        at = now()
        until = (datetime.now(timezone.utc) + timedelta(seconds=TTL_SECONDS)).isoformat()
        store.graph.db.execute('INSERT INTO context_entities(record_id,repo,collection,entity_id,source_name,chunk_index,'
            'generation,verified_until,checked_at,state,query) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(record_id) DO UPDATE SET '
            'generation=excluded.generation,verified_until=excluded.verified_until,checked_at=excluded.checked_at,'
            'state=excluded.state,query=excluded.query',
            (record_id, connection['repo'], connection['collection'], entity, source, chunk,
             connection['generation'], until, at, 'fresh', query))
        if task_id:
            cm.add_anchor(store.graph.db, task_id, record_id, imported['source']['source_version_id'], 'context')
        store.graph._bump(connection['repo'])
        if previous and (previous['state'] != 'fresh' or previous['verified_until'] <= at):
            for dependent in store.graph.db.execute('SELECT DISTINCT d.id,d.run_id FROM decisions d JOIN decision_source_edges e '
                    'ON e.decision_id=d.id WHERE e.record_id=? AND e.active=1', (record_id,)).fetchall():
                store.graph.append_event('context_evidence_restored', {'task_id': dependent['run_id'],
                    'decision_id': dependent['id'], 'record_id': record_id})
        imported['source'] = cm.citation(store.graph.db, record_id, imported['source']['source_version_id'])
        return imported


def refresh(store, repo, *, client=None, force=False, stopped=None):
    """Recheck exact known chunks. A missing search hit is unavailable, not deleted."""
    connection = _connection(store, repo)
    if not connection:
        return {'checked': 0, 'unavailable': 0}
    soon = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    rows = store.graph.db.execute('SELECT * FROM context_entities WHERE repo=? AND collection=?' +
        ('' if force else ' AND verified_until<=?') + ' ORDER BY checked_at LIMIT 40',
        [repo, connection['collection']] + ([] if force else [soon])).fetchall()
    count = unavailable = 0
    for row in rows:
        if stopped is not None and stopped.is_set():
            break
        try:
            client = client or Airweave()
            results = client.search(connection['collection'], row['query'], filters=[
                {'field': 'airweave_system_metadata.original_entity_id', 'operator': 'equals', 'value': row['entity_id']},
                {'field': 'airweave_system_metadata.source_name', 'operator': 'equals', 'value': row['source_name']},
                {'field': 'airweave_system_metadata.chunk_index', 'operator': 'equals', 'value': row['chunk_index']}], limit=1)
            if len(results) != 1:
                raise ValueError('Source excerpt was not returned by an exact refresh')
            data, identity = _entry(results[0], connection)
            if identity != (row['source_name'], row['entity_id'], row['chunk_index']):
                raise ValueError('Source refresh returned another identity')
            _accept(store, connection, results[0], row['query'])
        except Exception as error:
            # Never keep a failed refresh usable; don't log arbitrary remote text.
            with store.graph.transaction():
                _unavailable(store, row['record_id'], 'access_revoked' if isinstance(error, AccessRejected) else 'refresh_failed')
                store.graph.append_event('context_refresh_failed', {'repo': repo, 'record_id': row['record_id'],
                    'error_type': type(error).__name__})
                store.graph._bump(repo)
            unavailable += 1
        count += 1
    return {'checked': count, 'unavailable': unavailable}


def search(store, args, *, client=None):
    from .store import Invalid, repo_key
    repo, query, task_id = repo_key(args.get('repo', '')), str(args.get('query') or '').strip(), str(args.get('task_id') or '')
    if not repo or not query or len(query) > 2000:
        raise Invalid('Context search needs an exact repository and a query of 1 to 2000 characters')
    if task_id:
        task = store.graph.db.execute('SELECT repo FROM runs WHERE id=?', (task_id,)).fetchone()
        if not task or task['repo'] != repo:
            raise Invalid('Context search task is outside this repository')
    connection = _connection(store, repo)
    imported, rejected, error = [], 0, ''
    reasons = Counter()
    if connection:
        try:
            client = client or Airweave()
            results = client.search(connection['collection'], query)
            for result in results:
                try:
                    imported.append(_accept(store, connection, result, query, task_id))
                except (Invalid, TypeError, ValueError) as invalid:
                    reason = (invalid.reason if isinstance(invalid, SourceRejected) else
                              'access_not_shared' if isinstance(invalid, AccessRejected) else 'invalid_source')
                    reasons[reason] += 1
                    if isinstance(invalid, AccessRejected):
                        meta = result.get('airweave_system_metadata') or {}
                        identity = cm.digest([meta.get('source_name'), meta.get('original_entity_id'), meta.get('chunk_index')])
                        with store.graph.transaction():
                            for old in store.graph.db.execute('SELECT s.id FROM source_records s JOIN context_entities c ON c.record_id=s.id '
                                    'WHERE c.repo=? AND c.collection=? AND s.external_id=?', (repo, connection['collection'], identity)).fetchall():
                                _unavailable(store, old['id'], 'access_revoked')
                            store.graph._bump(repo)
                    rejected += 1
            if rejected and not imported:
                error = 'No external excerpts were imported. ' + ' '.join(
                    REJECTION_MESSAGES[reason] for reason in sorted(reasons))
            with store.graph.transaction():
                store.graph.db.execute('UPDATE context_connections SET last_success=?,last_error=? WHERE repo=?', (now(), error, repo))
        except (Invalid, ValueError, TypeError) as failure:
            error = 'External retrieval unavailable; local evidence is shown with its recorded freshness'
            with store.graph.transaction():
                if isinstance(failure, AccessRejected):
                    for old in store.graph.db.execute('SELECT record_id FROM context_entities WHERE repo=? AND collection=?',
                            (repo, connection['collection'])).fetchall():
                        _unavailable(store, old['record_id'], 'access_revoked')
                    store.graph._bump(repo)
                store.graph.db.execute('UPDATE context_connections SET last_error=? WHERE repo=?', (error, repo))
    with store.graph.source_scope(task_id):
        from .ladder import _meaningful_terms
        sources = [dict(r) for r in store.graph.intents_matching(_meaningful_terms(query), repo=repo, limit=MAX_RESULTS)]
        observed = {r['source']['record_id'] for r in imported}
        for row, _ in store.graph._intent_corpus(repo)[0]:
            if row['record_id'] in observed and row['record_id'] not in {r.get('record_id') for r in sources}:
                sources.append(dict(row))
        memories = store.graph.linked_answers([r['record_id'] for r in sources if r.get('record_id')], repo)
    diagnostics = [{'reason': reason, 'count': count, 'message': REJECTION_MESSAGES[reason]}
                   for reason, count in sorted(reasons.items())]
    with store.graph.transaction():
        store.graph.append_event('context_searched', {'task_id': task_id or None, 'repo': repo, 'query': query,
            'imported': len(imported), 'rejected': rejected, 'rejections': diagnostics, 'error': error,
            'sources': [{'record_id': r.get('record_id'), 'source_version_id': r.get('source_version_id')} for r in sources],
            'decisions': [r.id for r in memories]})
    return {'repo': repo, 'sources': sources, 'related_decisions': [
        {'decision_id': d.id, 'question': d.question, 'answer': d.answer,
         'relation': 'Shares exact source evidence; relevance and applicability still require review',
         'sources': cm.edges(store.graph.db, d.id)} for d in memories],
        'external': {'configured': bool(connection), 'imported': len(imported), 'rejected': rejected,
                     'rejections': diagnostics, 'error': error},
        'notice': 'Evidence and earlier answers share this graph. Neither search results nor prior signatures authorize a new task.'}


def status(store, repo=''):
    rows = store.graph.db.execute('SELECT repo,collection,enabled,generation,last_success,last_error FROM context_connections' +
                                 (' WHERE repo=?' if repo else ''), (repo,) if repo else ()).fetchall()
    connections = []
    for row in rows:
        connection = dict(row)
        connection['state'] = ('disabled' if not row['enabled'] else 'needs_attention' if row['last_error']
            else 'search_observed' if row['last_success'] else 'configured_unverified')
        connections.append(connection)
    return {'backend': 'airweave', 'configured': bool(rows), 'connections': connections,
            'freshness_seconds': TTL_SECONDS, 'credential_configured': bool(os.environ.get('AIRWEAVE_API_KEY')),
            'last_success_means': 'Last successful search transport, not proof that any source was admitted or that permissions were independently verified.',
            'notice': 'Only workspace-shared collections; unknown source ACLs and transient Slack results are excluded.'}


class Refresher:
    def __init__(self, store):
        self.store, self.stopped = store, threading.Event()
        self.thread = None

    def start(self):
        def run():
            while not self.stopped.wait(30):
                try:
                    for row in self.store.graph.db.execute('SELECT repo FROM context_connections WHERE enabled=1').fetchall():
                        if self.stopped.is_set():
                            break
                        refresh(self.store, row['repo'], stopped=self.stopped)
                except Exception:
                    # Expiry still rejects reuse even if the refresh worker fails.
                    pass
        self.thread = threading.Thread(target=run, daemon=True, name='raven-context-refresh')
        self.thread.start()

    def close(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=1)


def main():
    from .store import Store
    parser = argparse.ArgumentParser(description='Connect an operator-approved Airweave collection to Raven')
    parser.add_argument('--db', default=os.environ.get('DATABASE_URL') or '.bridge/bridge.db')
    parser.add_argument('--repo', required=True)
    parser.add_argument('--collection', required=True)
    parser.add_argument('--audience', default='', help='Comma-separated source principal IDs for every workspace reader; empty admits public sources only')
    parser.add_argument('--workspace-shared', action='store_true', help='Confirm the collection is intended for this whole Raven workspace')
    parser.add_argument('--disable', action='store_true')
    args = parser.parse_args()
    store = Store(args.db)
    print(json.dumps(configure(store, args.repo, args.collection, [p.strip() for p in args.audience.split(',') if p.strip()],
                               shared=args.workspace_shared, enabled=not args.disable), indent=2))


if __name__ == '__main__':
    main()
