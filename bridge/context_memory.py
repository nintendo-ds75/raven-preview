"""Versioned source evidence in Raven's existing relational database.

No connector, source author, lifecycle state, or citation text confers authority.
Writes share the application's SQLite/PG single-writer transaction boundary.
"""
import hashlib
import json
import uuid
from datetime import datetime, timezone

ROLES = frozenset({'support', 'contradiction', 'context', 'work_item'})
RELIANCE = frozenset({'support', 'contradiction'})
LOCAL_ORIGINS = frozenset({'', 'human', 'human-reframing', 'agent', 'assumption'})

SCHEMA = '''
CREATE TABLE IF NOT EXISTS source_records (
 id TEXT PRIMARY KEY, repo TEXT NOT NULL, provider TEXT NOT NULL,
 namespace TEXT NOT NULL, object_kind TEXT NOT NULL, external_id TEXT NOT NULL,
 intent_id TEXT NOT NULL UNIQUE REFERENCES intents(id) ON DELETE CASCADE,
 head_id TEXT NOT NULL DEFAULT '', head_sequence INTEGER NOT NULL DEFAULT 0,
 newest_sequence BIGINT, newest_updated_at TEXT NOT NULL DEFAULT '',
 last_observed_at TEXT NOT NULL DEFAULT '', newest_source_version TEXT NOT NULL DEFAULT '',
 availability TEXT NOT NULL DEFAULT 'available', display_ref TEXT NOT NULL DEFAULT '',
 UNIQUE(repo,provider,namespace,object_kind,external_id));
CREATE TABLE IF NOT EXISTS source_versions (
 id TEXT PRIMARY KEY, record_id TEXT NOT NULL REFERENCES source_records(id) ON DELETE CASCADE,
 sequence INTEGER NOT NULL, fingerprint TEXT NOT NULL, snapshot TEXT NOT NULL,
 source_updated_at TEXT NOT NULL DEFAULT '', source_version TEXT NOT NULL DEFAULT '',
 source_sequence BIGINT, observed_at TEXT NOT NULL, provenance TEXT NOT NULL,
 UNIQUE(record_id,sequence));
CREATE TABLE IF NOT EXISTS source_identities (
 repo TEXT NOT NULL, provider TEXT NOT NULL, namespace TEXT NOT NULL,
 object_kind TEXT NOT NULL, external_id TEXT NOT NULL,
 record_id TEXT NOT NULL REFERENCES source_records(id) ON DELETE CASCADE,
 PRIMARY KEY(repo,provider,namespace,object_kind,external_id));
CREATE TABLE IF NOT EXISTS decision_versions (
 id TEXT PRIMARY KEY, decision_id TEXT NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
 sequence INTEGER NOT NULL, snapshot TEXT NOT NULL, fingerprint TEXT NOT NULL,
 recorded_at TEXT NOT NULL, UNIQUE(decision_id,sequence));
CREATE TABLE IF NOT EXISTS decision_source_edges (
 id TEXT PRIMARY KEY, decision_id TEXT NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
 decision_version_id TEXT NOT NULL REFERENCES decision_versions(id) ON DELETE CASCADE,
 record_id TEXT NOT NULL REFERENCES source_records(id), source_version_id TEXT NOT NULL REFERENCES source_versions(id),
 role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, stale INTEGER NOT NULL DEFAULT 0,
 recorded_at TEXT NOT NULL, retired_at TEXT NOT NULL DEFAULT '',
 UNIQUE(decision_version_id,record_id,source_version_id,role));
CREATE INDEX IF NOT EXISTS source_edges_live ON decision_source_edges(record_id,active,role);
CREATE INDEX IF NOT EXISTS decision_edges_live ON decision_source_edges(decision_id,active);
CREATE TABLE IF NOT EXISTS task_source_anchors (
 task_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
 record_id TEXT NOT NULL REFERENCES source_records(id), source_version_id TEXT NOT NULL REFERENCES source_versions(id),
 role TEXT NOT NULL, recorded_at TEXT NOT NULL,
 PRIMARY KEY(task_id,record_id,source_version_id,role));
CREATE TABLE IF NOT EXISTS source_review_requests (
 decision_id TEXT PRIMARY KEY REFERENCES decisions(id) ON DELETE CASCADE,
 source_version_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', updated_at TEXT NOT NULL);
'''


# This is an applicability gate, not a statement that historical signatures
# were invalid. In particular, migration never rewrites completed decisions.
LEGACY_REUSE_NOTICE = (
    'External source revisions are unknown for this legacy derivation. '
    'Automatic reuse requires deliberate review of current source evidence '
    'or an explicit independent human replacement; a signature or matching import '
    'does not reconstruct the old source revisions.')


def reuse_uncertainty_sql(alias='d'):
    """One correlated expression, so tree reads retain their query budget.

    Follow only typed live relationships, never citation prose. UNION also
    terminates malformed legacy cycles. Missing memory targets fail closed.
    """
    return f"""EXISTS (WITH RECURSIVE lineage(id) AS (
        SELECT {alias}.id UNION
        SELECT links.related_id FROM (
            SELECT id AS decision_id,source_id AS related_id FROM decisions WHERE coalesce(source_id,'')<>''
            UNION SELECT id,parent_id FROM decisions WHERE parent_id<>'' AND independent_source_replacement=0 AND historical_parent_revalidated=0
            UNION SELECT decision_id,related_id FROM decision_links WHERE kind IN ('derived','depends')
        ) links JOIN lineage ON links.decision_id=lineage.id
    ) SELECT 1 FROM lineage LEFT JOIN decisions p ON p.id=lineage.id
      WHERE p.id IS NULL OR p.source_reuse_state NOT IN ('','tracked','human','independent','reviewed')
        OR (p.source_reuse_state='' AND p.source NOT IN ('','human','human-reframing','agent','assumption')))"""


def reuse_uncertain(db, decision_id):
    row = db.execute('SELECT ' + reuse_uncertainty_sql() + ' AS uncertain FROM decisions d WHERE d.id=?',
                     (decision_id,)).fetchone()
    return row is None or bool(row['uncertain'])


def _legacy_reuse_state(db, row):
    """Classify stored typed metadata; never reconstruct a historical pin.

    Human wording/attribution is not proof of independence. A retained typed
    resolution or an immutable earlier snapshot survives source='human'.
    Unreadable typed provenance cannot establish safe automatic reuse.
    """
    if row['independent_source_replacement'] == 1:
        return 'independent'
    external = row['source'] == 'record'
    memory = row['source'] == 'memory'
    ambiguous = (row['independent_source_replacement'] not in (0, 1)
                 or (row['source'] or '') not in LOCAL_ORIGINS | {'record', 'memory'})
    for event in db.execute("SELECT detail FROM events WHERE decision_id=? AND kind IN ('resolve','proposed')", (row['id'],)):
        try:
            detail = json.loads(event['detail'])
        except (TypeError, ValueError):
            ambiguous = True
            continue
        if not isinstance(detail, dict) or not isinstance(detail.get('source'), str):
            ambiguous = True
            continue
        source = detail['source']
        external |= source == 'record'
        memory |= source == 'memory'
        ambiguous |= source not in LOCAL_ORIGINS | {'record', 'memory'}
    for version in db.execute('SELECT snapshot FROM decision_versions WHERE decision_id=?', (row['id'],)):
        try:
            value = json.loads(version['snapshot'])
            previous = value['decision']
            if not isinstance(previous, dict) or not isinstance(value.get('sources', []), list):
                raise ValueError('Malformed decision provenance')
            if any(not isinstance(source, dict) or not all(isinstance(source.get(k), str) and source[k]
                    for k in ('record_id', 'source_version_id', 'role')) or source['role'] not in ROLES
                    for source in value.get('sources', [])):
                raise ValueError('Malformed historical source pins')
            if previous.get('source') == 'record' and not value.get('sources'):
                ambiguous = True
            ambiguous |= (previous.get('source') or '') not in LOCAL_ORIGINS | {'record', 'memory'}
            external |= previous.get('source') == 'record'
            memory |= previous.get('source') == 'memory'
        except (KeyError, TypeError, ValueError):
            ambiguous = True
    source_edges = db.execute('SELECT e.role,e.record_id,v.record_id AS version_record,s.repo FROM decision_source_edges e '
                              'LEFT JOIN source_versions v ON v.id=e.source_version_id '
                              'LEFT JOIN source_records s ON s.id=e.record_id WHERE e.decision_id=? AND e.active=1', (row['id'],)).fetchall()
    ambiguous |= any(e['role'] not in ROLES or e['record_id'] != e['version_record'] or e['repo'] != row['repo'] for e in source_edges)
    pinned = bool(source_edges)
    linked = bool(row['source_id']) or db.execute("SELECT 1 FROM decision_links WHERE decision_id=? AND kind IN ('derived','depends') LIMIT 1", (row['id'],)).fetchone()
    if ambiguous or (external and not pinned) or (memory and not linked):
        return 'unknown'
    return 'tracked' if external or memory or pinned or linked else 'human'


def initialize_reuse_state(db, decision_id):
    row = db.execute('SELECT * FROM decisions WHERE id=?', (decision_id,)).fetchone()
    if row is not None and not row['source_reuse_state']:
        db.execute('UPDATE decisions SET source_reuse_state=? WHERE id=?', (_legacy_reuse_state(db, row), decision_id))


def backfill_reuse(db):
    """Run after additive decision columns exist, including on post-import open.

    No global initialized marker: an empty import destination must not suppress
    classification of legacy rows copied into it later.
    """
    for row in db.execute("SELECT * FROM decisions WHERE source_reuse_state='' ").fetchall():
        db.execute('UPDATE decisions SET source_reuse_state=? WHERE id=?', (_legacy_reuse_state(db, row), row['id']))
    roots = db.execute("SELECT d.id FROM decisions d JOIN runs r ON r.id=d.run_id "
                       "WHERE d.signoff='rule' AND r.status<>'completed' AND " + reuse_uncertainty_sql()).fetchall()
    queue, seen = [r['id'] for r in roots], set()
    while queue:
        did = queue.pop()
        if did in seen:
            continue
        seen.add(did)
        queue.extend(r['id'] for r in db.execute("SELECT id FROM decisions WHERE source_id=? OR (parent_id=? AND independent_source_replacement=0 AND historical_parent_revalidated=0) OR id IN (SELECT decision_id FROM decision_links WHERE related_id=? AND kind IN ('derived','depends'))", (did, did, did)))
        row = db.execute('SELECT d.*,r.status AS run_status FROM decisions d JOIN runs r ON r.id=d.run_id WHERE d.id=?', (did,)).fetchone()
        if row is None or row['run_status'] == 'completed' or row['status'] in ('withdrawn', 'adopted'):
            continue
        at = stamp()
        # Preserve the exact former signatures in immutable history and audit.
        snapshot_decision(db, did, reason='before-legacy-reuse-review')
        db.execute("UPDATE decisions SET needs_review=1,review_reason=?,prediction=NULL,signoff=CASE WHEN signoff IN ('signed','rule') THEN 'required' ELSE signoff END,status=CASE WHEN status='approved' THEN 'resolved' ELSE status END,signatures='[]',signed_by='',signed_hash='',signed_revision='',updated_at=? WHERE id=?", (LEGACY_REUSE_NOTICE, at, did))
        db.execute('UPDATE runs SET needs_review=1,updated_at=? WHERE id=?', (at, row['run_id']))
        db.execute('INSERT INTO events(decision_id,run_id,kind,detail,created_at) VALUES(?,?,?,?,?)',
                   (did, row['run_id'], 'legacy_source_reuse_review', encoded({'reason': LEGACY_REUSE_NOTICE,
                    'previous_authorization': {key: row[key] for key in ('status','signoff','signatures','signed_by','signed_hash','signed_revision')}}), at))


def review_legacy_reuse(db, decision_id, pins, decision_pins, reviewed_revision):
    """Only called inside an authenticated human review transaction.

    Explicit current pins establish a new present derivation, never a revision
    for the old answer. Every displayed known premise and decision is retained.
    Context-only pins cannot clear an unknown supporting derivation.
    """
    if not pins or not reuse_uncertain(db, decision_id):
        return False
    from .store import Invalid
    current = db.execute('SELECT updated_at FROM decisions WHERE id=?', (decision_id,)).fetchone()
    if not reviewed_revision or current is None or current['updated_at'] != reviewed_revision:
        raise Invalid('A fresh expected_updated_at is required for explicit legacy source review')
    validate(db, decision_id, pins)
    if not any(p['role'] in RELIANCE for p in pins):
        raise Invalid('Current supporting source evidence is required to review unknown legacy derivation')
    proposal = source_revalidation(db, decision_id)
    required = {(p['record_id'], p['source_version_id'], p['role']) for p in proposal['pins'] if p['role'] in RELIANCE}
    supplied = {(p['record_id'], p['source_version_id'], p['role']) for p in pins}
    expected = {(p['decision_id'], p['updated_at'], p['historical']) for p in proposal['decision_pins']}
    given = {(p['decision_id'], p['updated_at'], p.get('historical', False)) for p in decision_pins}
    if not required <= supplied or expected != given:
        raise Invalid('Review every current premise and submit the exact source_decision_pins for legacy source review')
    # Namespace restrictions apply to manually selected current sources too.
    scopes = {(r['provider'], r['namespace']) for r in db.execute('SELECT s.provider,s.namespace FROM task_source_anchors a JOIN source_records s ON s.id=a.record_id WHERE a.task_id=(SELECT run_id FROM decisions WHERE id=?)', (decision_id,))}
    for p in pins:
        source = citation(db, p['record_id'], p['source_version_id'])
        shared = source['provider'] in ('legacy', 'git') or (source['provider'], source['namespace']) == ('github', 'github.com')
        if scopes and not shared and (source['provider'], source['namespace']) not in scopes:
            raise Invalid('A source is outside the explicitly selected task namespace')
    db.execute("UPDATE decisions SET source_reuse_state='reviewed' WHERE id=?", (decision_id,))
    return True


def stamp():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def migrate(db):
    db.executescript(SCHEMA)
    columns = {row['name'] for row in db.execute('PRAGMA table_info(source_records)')}
    for name in ('last_observed_at', 'newest_source_version'):
        if name not in columns:
            db.execute(f"ALTER TABLE source_records ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")


def latest_observation(row):
    """Live import receipt metadata, never part of an immutable source pin."""
    return {'observed_at': row['last_observed_at'],
            'source_updated_at': row['newest_updated_at'],
            'source_sequence': row['newest_sequence'],
            'source_version': row['newest_source_version'],
            'meaning': 'Latest accepted local import; not a provider fetch, policy adoption or approval'}


def _snapshot(db, intent, metadata=None):
    row = dict(intent)
    meta = metadata or {}
    paths = [r['path'] for r in db.execute('SELECT path FROM intent_paths WHERE repo=? AND kind=? AND ref=? ORDER BY path',
                                          (row['repo'], row['kind'], row['ref']))]
    url = db.execute('SELECT locator FROM connector_sources WHERE repo=? AND kind=?',
                     (row['repo'], f"url:{row['kind']}:{row['ref']}")).fetchone()
    return {**{k: row.get(k, '') for k in ('repo', 'kind', 'ref', 'title', 'body', 'author', 'created_at', 'status', 'resolved')},
            'paths': paths, 'url': meta.get('url', url['locator'] if url else ''),
            'display_ref': meta.get('display_ref', row['ref']),
            'source_created_at': meta.get('source_created_at', row['created_at']),
            'source_updated_at': meta.get('source_updated_at', ''),
            'source_version': meta.get('source_version', ''), 'source_sequence': meta.get('source_sequence'),
            'availability': meta.get('availability', 'available'), 'access_scope': meta.get('access_scope', ''),
            'provider': meta.get('provider', 'legacy'), 'namespace': meta.get('namespace', 'legacy'),
            'object_kind': meta.get('object_kind', row['kind']), 'external_id': meta.get('external_id', row['ref'])}


def observe(db, intent, metadata=None, provenance='observed'):
    """Record exactly one complete observed snapshot. Caller owns write lock.

    The legacy identity remains distinct. Explicit namespaces never adopt an
    existing legacy object by matching prose, URL, or human-readable key.
    """
    from .store import Invalid
    meta = metadata or {}
    intent = dict(intent)
    record = db.execute('SELECT * FROM source_records WHERE intent_id=?', (intent['id'],)).fetchone()
    if record is None:
        rid = uuid.uuid4().hex
        db.execute('INSERT INTO source_records(id,repo,provider,namespace,object_kind,external_id,intent_id) VALUES(?,?,?,?,?,?,?)',
                   (rid, intent['repo'], meta.get('provider', 'legacy'), meta.get('namespace', 'legacy'),
                    meta.get('object_kind', intent['kind']), meta.get('external_id', intent['ref']), intent['id']))
        record = db.execute('SELECT * FROM source_records WHERE id=?', (rid,)).fetchone()
    old = db.execute('SELECT * FROM source_versions WHERE id=?', (record['head_id'],)).fetchone() if record['head_id'] else None
    if old and metadata is None:
        # Path-only compatibility calls retain observed source metadata.
        previous = json.loads(old['snapshot'])
        meta = {k: previous[k] for k in ('display_ref', 'source_updated_at', 'source_version',
                                        'source_sequence', 'availability', 'access_scope', 'url',
                                        'provider', 'namespace', 'object_kind', 'external_id')}
    snapshot = _snapshot(db, intent, meta)
    identity = (intent['repo'], snapshot['provider'], snapshot['namespace'], snapshot['object_kind'], snapshot['external_id'])
    known = db.execute('SELECT record_id FROM source_identities WHERE repo=? AND provider=? AND namespace=? AND object_kind=? AND external_id=?', identity).fetchone()
    if known is not None and known['record_id'] != record['id']:
        raise Invalid('Source identity already belongs to another record; explicit reconciliation is required')
    db.execute('INSERT OR IGNORE INTO source_identities(repo,provider,namespace,object_kind,external_id,record_id) VALUES(?,?,?,?,?,?)', (*identity, record['id']))
    mark = digest({k: v for k, v in snapshot.items() if k not in ('source_updated_at', 'source_version', 'source_sequence')})
    if old:
        seq = snapshot['source_sequence']
        if seq is not None and record['newest_sequence'] is not None and seq < record['newest_sequence']:
            raise Invalid('Out-of-order source sequence; current head was not changed')
        new_ts, old_ts = snapshot['source_updated_at'], record['newest_updated_at']
        if new_ts and old_ts:
            try:
                if datetime.fromisoformat(new_ts.replace('Z', '+00:00')) < datetime.fromisoformat(old_ts.replace('Z', '+00:00')):
                    raise Invalid('Out-of-order source timestamp; current head was not changed')
            except (ValueError, TypeError) as error:
                if isinstance(error, Invalid):
                    raise
                raise Invalid('Source timestamps must be comparable ISO timestamps') from error
        if mark == old['fingerprint']:
            db.execute("UPDATE source_records SET last_observed_at=?,newest_source_version=CASE WHEN ?='' THEN newest_source_version ELSE ? END WHERE id=?",
                       (stamp(), snapshot['source_version'], snapshot['source_version'], record['id']))
            db.execute('UPDATE source_records SET newest_sequence=coalesce(?,newest_sequence),newest_updated_at=CASE WHEN ?=\'\' THEN newest_updated_at ELSE ? END WHERE id=?',
                       (seq, new_ts, new_ts, record['id']))
            return {**dict(record), 'version_id': old['id'], 'changed': False, 'affected_decisions': []}
        if seq is not None and seq == record['newest_sequence']:
            raise Invalid('Conflicting content for the same source sequence')
    observed_at = stamp()
    vid = uuid.uuid4().hex
    ordinal = record['head_sequence'] + 1
    db.execute('INSERT INTO source_versions(id,record_id,sequence,fingerprint,snapshot,source_updated_at,source_version,source_sequence,observed_at,provenance) VALUES(?,?,?,?,?,?,?,?,?,?)',
               (vid, record['id'], ordinal, mark, encoded(snapshot), snapshot['source_updated_at'], snapshot['source_version'],
                snapshot['source_sequence'], observed_at, provenance))
    db.execute('UPDATE source_records SET head_id=?,head_sequence=?,newest_sequence=coalesce(?,newest_sequence),newest_updated_at=CASE WHEN ?=\'\' THEN newest_updated_at ELSE ? END WHERE id=?',
               (vid, ordinal, snapshot['source_sequence'], snapshot['source_updated_at'], snapshot['source_updated_at'], record['id']))
    db.execute("UPDATE source_records SET availability=?,display_ref=?,last_observed_at=?,newest_source_version=CASE WHEN ?='' THEN newest_source_version ELSE ? END WHERE id=?",
               (snapshot['availability'], snapshot['display_ref'], observed_at, snapshot['source_version'], snapshot['source_version'], record['id']))
    affected = invalidate(db, record['id'], vid) if old else []
    db.execute('INSERT INTO events(decision_id,run_id,kind,detail,created_at) VALUES(NULL,NULL,?,?,?)',
               ('source_observed', encoded({'record_id': record['id'], 'source_version_id': vid, 'sequence': ordinal,
                                            'previous_version_id': old['id'] if old else '', 'provenance': provenance}), stamp()))
    return {**dict(record), 'head_id': vid, 'head_sequence': ordinal, 'version_id': vid, 'changed': True, 'affected_decisions': affected}


def backfill(db):
    """Observation today is not a reconstruction of historical provenance."""
    columns = {r['name'] for r in db.execute('PRAGMA table_info(decision_links)')}
    for column in ('source_version_id', 'decision_version_id'):
        if column not in columns:
            db.execute(f"ALTER TABLE decision_links ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    for row in db.execute('SELECT i.* FROM intents i LEFT JOIN source_records s ON s.intent_id=i.id WHERE s.id IS NULL').fetchall():
        observe(db, row, provenance='legacy-observation')


def citation(db, record_id, version_id):
    row = db.execute('SELECT s.*,v.sequence,v.snapshot,v.observed_at,v.provenance,v.fingerprint FROM source_records s '
                     'JOIN source_versions v ON v.record_id=s.id WHERE s.id=? AND v.id=?', (record_id, version_id)).fetchone()
    if not row:
        return None
    snapshot = json.loads(row['snapshot'])
    return {'record_id': record_id, 'source_version_id': version_id, 'sequence': row['sequence'],
            'provider': snapshot.get('provider', row['provider']), 'namespace': snapshot.get('namespace', row['namespace']),
            'external_id': snapshot.get('external_id', row['external_id']),
            'kind': snapshot.get('object_kind', row['object_kind']), 'repo': row['repo'], 'ref': snapshot['display_ref'],
            'url': snapshot['url'], 'title': snapshot['title'], 'author': snapshot['author'],
            'source_created_at': snapshot['source_created_at'], 'source_updated_at': snapshot['source_updated_at'],
            'source_version': snapshot['source_version'], 'observed_at': row['observed_at'],
            'provenance': row['provenance'], 'fingerprint': row['fingerprint'],
            'availability': snapshot['availability'], 'head_id': row['head_id'],
            'current': version_id == row['head_id']}


def edges(db, decision_id):
    out = []
    for row in db.execute('SELECT * FROM decision_source_edges WHERE decision_id=? AND active=1 ORDER BY record_id,role', (decision_id,)):
        cit = citation(db, row['record_id'], row['source_version_id'])
        out.append({**cit, 'role': row['role'], 'decision_version_id': row['decision_version_id'],
                    'stale': bool(row['stale']) or not cit['current']})
    return out


def history(db, decision_id):
    return [dict(r) for r in db.execute('SELECT id,sequence,snapshot,recorded_at FROM decision_versions WHERE decision_id=? ORDER BY sequence', (decision_id,))]


def provenance_notice(source, sources, uncertain=False):
    if uncertain:
        return LEGACY_REUSE_NOTICE
    if not sources and source in ('record', 'memory', 'assumption'):
        return ('External source provenance is unknown for this answer. Existing human authority is separate. '
                'A new sign-off approves the exact answer and scope; it does not verify or reconstruct legacy source citations.')
    return ''


def pin(row, role='support'):
    row = dict(row)
    if not row.get('record_id') or not row.get('source_version_id'):
        return None  # An old free-text citation is unknown provenance.
    return {'record_id': row['record_id'], 'source_version_id': row['source_version_id'], 'role': role}


def _binding_projection(value):
    """Exact readback data, independent of mutable bookkeeping pointers."""
    decision = {k: v for k, v in value['decision'].items()
                if k not in ('embedding', 'search_vector', 'rowid', 'source_reuse_state')}
    sources = sorted([p['record_id'], p['source_version_id'], p['role']] for p in value.get('sources', []))
    links = sorted([p['related_id'], p['kind'], p.get('source_version_id', '')] for p in value.get('derivations', []))
    return {'decision': decision, 'sources': sources, 'derivations': links}


def decision_binding(db, decision_id):
    """Read-only, content-addressed current observation. Prefer an existing
    immutable version when it exactly represents what is being displayed.
    An unsnapshotted current observation is frozen only after confirmed review.
    """
    row = db.execute('SELECT * FROM decisions WHERE id=?', (decision_id,)).fetchone()
    if row is None:
        return None
    value = {'decision': dict(row), 'sources': [dict(p) for p in db.execute(
        'SELECT record_id,source_version_id,role FROM decision_source_edges WHERE decision_id=? AND active=1', (decision_id,))],
        'derivations': [dict(p) for p in db.execute("SELECT related_id,kind,source_version_id FROM decision_links WHERE decision_id=? AND (kind IN ('derived','depends') OR (kind='related' AND source_version_id<>''))", (decision_id,))]}
    projection = _binding_projection(value)
    fingerprint = digest(projection)
    latest = db.execute('SELECT id,snapshot FROM decision_versions WHERE decision_id=? ORDER BY sequence DESC LIMIT 1', (decision_id,)).fetchone()
    version_id = ''
    if latest:
        try:
            if digest(_binding_projection(json.loads(latest['snapshot']))) == fingerprint:
                version_id = latest['id']
        except (KeyError, TypeError, ValueError):
            pass
    return {'source_version_id': version_id, 'source_snapshot_sha256': fingerprint,
            'reviewed_snapshot': projection}


def _same_source_revision(recorded, current):
    try:
        a = datetime.fromisoformat(recorded.replace('Z', '+00:00'))
        b = datetime.fromisoformat(current.replace('Z', '+00:00'))
        return a.tzinfo is not None and b.tzinfo is not None and a == b
    except (ValueError, TypeError, AttributeError):
        return False


def _relied_on_source(db, row):
    # Inbox routing may offer a prior answer before there is any candidate
    # answer. The owner writing the first answer has not adopted that hint.
    # Resolved/answered reuse and typed derivation links remain premises.
    unused_hint = (row['kind'] == 'prediction' and row['status'] == 'pending' and not row['answer']
                   and row['signoff'] not in ('signed', 'rule') and not row['signed_revision']
                   and not json.loads(row['signatures'] or '[]'))
    if unused_hint and row['source_id']:
        typed = db.execute("SELECT 1 FROM decision_links WHERE decision_id=? AND kind IN ('derived','depends') LIMIT 1", (row['id'],)).fetchone()
        relied = db.execute("SELECT 1 FROM decision_source_edges WHERE decision_id=? AND active=1 AND role IN ('support','contradiction') LIMIT 1", (row['id'],)).fetchone()
        if not typed and not relied:
            return None
    return row['source_id']


def human_source_reason(db, decision_id):
    """Read/check guard only. It never upgrades a legacy or stale pin."""
    node = db.execute('SELECT * FROM decisions WHERE id=?', (decision_id,)).fetchone()
    if node and _relied_on_source(db, node):
        source = db.execute('SELECT updated_at FROM decisions WHERE id=?', (node['source_id'],)).fetchone()
        if source is None or not _same_source_revision(node['source_revision'], source['updated_at']):
            return 'A source decision changed or lacks a recorded revision; review its current version before revalidation'
    for link in db.execute("SELECT related_id,source_version_id FROM decision_links WHERE decision_id=? AND kind IN ('derived','depends') AND source_version_id<>''", (decision_id,)):
        saved = db.execute('SELECT snapshot FROM decision_versions WHERE id=? AND decision_id=?', (link['source_version_id'], link['related_id'])).fetchone()
        current = decision_binding(db, link['related_id'])
        try:
            matching = saved is not None and current is not None and digest(_binding_projection(json.loads(saved['snapshot']))) == current['source_snapshot_sha256']
        except (KeyError, TypeError, ValueError):
            matching = False
        if not matching:
            return 'A pinned source decision changed; a complete current-human-source review is required for revalidation'
    return ''


def validate(db, decision_id, pins, *, current_roles=ROLES):
    from .store import Invalid
    if not isinstance(pins, list) or any(not isinstance(p, dict) or
            not all(isinstance(p.get(k), str) and p[k] for k in ('record_id', 'source_version_id', 'role')) for p in pins):
        raise Invalid('source_evidence must be an array of record_id, source_version_id, and role objects')
    decision = db.execute('SELECT repo FROM decisions WHERE id=?', (decision_id,)).fetchone()
    if decision is None:
        raise Invalid('Decision not found')
    for p in pins:
        if p['role'] not in ROLES:
            raise Invalid('Unknown evidence role')
        cit = citation(db, p['record_id'], p['source_version_id'])
        if cit is None or cit['repo'] != decision['repo']:
            raise Invalid('Evidence is outside this decision repository')
        if p['role'] in current_roles and (not cit['current'] or cit['availability'] != 'available'):
            raise Invalid(f"Source evidence changed or became unavailable for decision {decision_id}, source {cit['ref']}; re-read and compose against current source versions")


def snapshot_decision(db, decision_id, pins=None, reason='snapshot'):
    """Freeze row and exact active edge set, then replace only live pointers."""
    initialize_reuse_state(db, decision_id)
    row = db.execute('SELECT * FROM decisions WHERE id=?', (decision_id,)).fetchone()
    if not row:
        return ''
    if row['independent_source_replacement'] == 1:
        db.execute("UPDATE decisions SET source_reuse_state='independent' WHERE id=?", (decision_id,))
    row = {k: v for k, v in dict(row).items() if k not in ('embedding', 'search_vector', 'rowid', 'source_reuse_state')}
    pins = pins if pins is not None else [{'record_id': e['record_id'], 'source_version_id': e['source_version_id'], 'role': e['role'], 'stale': e['stale']} for e in edges(db, decision_id)]
    pins = sorted({(p['record_id'], p['source_version_id'], p['role']): p for p in pins}.values(), key=lambda p: (p['record_id'], p['role']))
    cites = [{**citation(db, p['record_id'], p['source_version_id']), 'role': p['role'], 'stale': bool(p.get('stale'))} for p in pins]
    # Head/current are live comparison fields, not the immutable source itself.
    for cit in cites:
        cit.pop('head_id', None)
        cit.pop('current', None)
    derivations = [dict(r) for r in db.execute("SELECT related_id,kind,note,created_at,source_version_id FROM decision_links WHERE decision_id=? AND (kind IN ('derived','depends') OR (kind='related' AND source_version_id<>'')) ORDER BY related_id,kind", (decision_id,))]
    value = {'decision': row, 'sources': cites, 'derivations': derivations, 'reason': reason,
             'provenance': 'versioned' if cites else 'unknown' if row.get('source') == 'record' else 'human-or-local'}
    # Recording why a snapshot was read does not change the decision itself.
    mark = digest({key: value for key, value in value.items() if key != 'reason'})
    latest = db.execute('SELECT id,sequence,fingerprint FROM decision_versions WHERE decision_id=? ORDER BY sequence DESC LIMIT 1', (decision_id,)).fetchone()
    if latest and latest['fingerprint'] == mark:
        return latest['id']
    vid, at = uuid.uuid4().hex, stamp()
    db.execute('INSERT INTO decision_versions(id,decision_id,sequence,snapshot,fingerprint,recorded_at) VALUES(?,?,?,?,?,?)',
               (vid, decision_id, latest['sequence'] + 1 if latest else 1, encoded(value), mark, at))
    db.execute("UPDATE decision_links SET decision_version_id=? WHERE decision_id=? AND (kind IN ('derived','depends') OR (kind='related' AND source_version_id<>''))", (vid, decision_id))
    db.execute('UPDATE decision_source_edges SET active=0,retired_at=? WHERE decision_id=? AND active=1', (at, decision_id))
    for p in pins:
        db.execute('INSERT INTO decision_source_edges(id,decision_id,decision_version_id,record_id,source_version_id,role,stale,recorded_at) VALUES(?,?,?,?,?,?,?,?)',
                   (uuid.uuid4().hex, decision_id, vid, p['record_id'], p['source_version_id'], p['role'], int(bool(p.get('stale'))), at))
    if reason in ('human-answer', 'human-signoff'):
        db.execute('UPDATE runs SET needs_review=CASE WHEN EXISTS (SELECT 1 FROM decisions WHERE run_id=? AND needs_review=1) THEN 1 ELSE 0 END WHERE id=?', (row['run_id'], row['run_id']))
    return vid


def attach(db, decision_id, pins, *, replace=False):
    pins = [p for p in pins if p]
    validate(db, decision_id, pins)
    old = [] if replace else [pin(e, e['role']) for e in edges(db, decision_id)]
    snapshot_decision(db, decision_id, old + pins, reason='evidence-attached')


def check_current(db, decision_id, seen=None, pins=None, reviewed_human_sources=False):
    from .store import Invalid
    seen = set() if seen is None else seen
    if decision_id in seen:
        return
    seen.add(decision_id)
    root = db.execute('SELECT * FROM decisions WHERE id=?', (decision_id,)).fetchone()
    human_links = root and (_relied_on_source(db, root) or (root['parent_id'] and not root['independent_source_replacement'] and not root['historical_parent_revalidated']) or db.execute("SELECT 1 FROM decision_links WHERE decision_id=? AND kind IN ('derived','depends') LIMIT 1", (decision_id,)).fetchone())
    if root and root['needs_review'] and human_links and not reviewed_human_sources:
        raise Invalid(f'Decision {decision_id} needs review: submit a complete current-human-source review with source_decision_pins for revalidation')
    validate(db, decision_id, pins if pins is not None else [pin(e, e['role']) for e in edges(db, decision_id) if e['role'] in RELIANCE], current_roles=RELIANCE)
    reason = human_source_reason(db, decision_id)
    if reason:
        raise Invalid(reason)
    for row in db.execute("SELECT id,needs_review FROM decisions WHERE id IN (SELECT related_id FROM decision_links WHERE decision_id=? AND kind IN ('derived','depends')) OR id=? OR id=(SELECT parent_id FROM decisions WHERE id=? AND independent_source_replacement=0 AND historical_parent_revalidated=0)", (decision_id, _relied_on_source(db, root) if root else None, decision_id)):
        if row['needs_review']:
            raise Invalid('An active source decision needs revalidation before this answer can be signed or used')
        check_current(db, row['id'], seen)


def validate_derivations(db, decision_id):
    from .store import Invalid
    automatic = db.execute("SELECT 1 FROM decisions WHERE id=? AND signoff='rule'", (decision_id,)).fetchone()
    if automatic and reuse_uncertain(db, decision_id):
        raise Invalid(LEGACY_REUSE_NOTICE)
    for row in db.execute("SELECT l.related_id,l.source_version_id,v.snapshot FROM decision_links l LEFT JOIN decision_versions v ON v.id=l.source_version_id WHERE l.decision_id=? AND l.kind='derived'", (decision_id,)):
        if not row['source_version_id']:
            continue  # Legacy unknown provenance is never reconstructed.
        old = json.loads(row['snapshot'])['decision'] if row['snapshot'] else {}
        current = db.execute('SELECT updated_at,needs_review FROM decisions WHERE id=?', (row['related_id'],)).fetchone()
        if current is None or current['needs_review'] or current['updated_at'] != old.get('updated_at'):
            raise Invalid('Source decision changed during composition; re-read its current revision')
        check_current(db, row['related_id'])


def invalidate(db, record_id, new_head):
    """Withdraw current reliance transitively while preserving signed history."""
    roots = [r['decision_id'] for r in db.execute("SELECT DISTINCT decision_id FROM decision_source_edges WHERE record_id=? AND active=1 AND role IN ('support','contradiction') AND source_version_id<>?", (record_id, new_head))]
    queue, seen = list(roots), set()
    at = stamp()
    current_source = citation(db, record_id, new_head)
    while queue:
        did = queue.pop()
        if did in seen:
            continue
        seen.add(did)
        row = db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone()
        if not row:
            continue
        snapshot_decision(db, did, reason='before-source-change')
        reason = f"Source {current_source['ref']} changed to version {current_source['sequence']}; review the current sources before confirming this answer"
        run = db.execute('SELECT status FROM runs WHERE id=?', (row['run_id'],)).fetchone()
        completed = run is not None and run['status'] == 'completed'
        if completed:
            # The signed historical fact remains intact. Only its current
            # applicability changes, and traversal continues to active users.
            db.execute('UPDATE decisions SET needs_review=1,review_reason=?,updated_at=? WHERE id=?', (reason, at, did))
        else:
            db.execute("UPDATE decisions SET needs_review=1,review_reason=?,prediction=NULL,signoff=CASE WHEN signoff IN ('signed','rule') THEN 'required' ELSE signoff END,status=CASE WHEN status='approved' THEN 'resolved' ELSE status END,signatures='[]',signed_by='',signed_hash='',signed_revision='',updated_at=? WHERE id=?", (reason, at, did))
        db.execute('UPDATE runs SET needs_review=1,updated_at=? WHERE id=?', (at, row['run_id']))
        db.execute('UPDATE decision_source_edges SET stale=1 WHERE decision_id=? AND record_id=? AND active=1 AND role IN (\'support\',\'contradiction\')', (did, record_id))
        snapshot_decision(db, did, reason='source-review-required')
        db.execute('INSERT INTO events(decision_id,run_id,kind,detail,created_at) VALUES(?,?,?,?,?)',
                   (did, row['run_id'], 'source_review_required', encoded({'record_id': record_id, 'head_id': new_head, 'reason': reason}), at))
        if not completed:
            db.execute("INSERT INTO source_review_requests(decision_id,source_version_id,state,updated_at) VALUES(?,?,'pending',?) ON CONFLICT(decision_id) DO UPDATE SET source_version_id=excluded.source_version_id,state='pending',updated_at=excluded.updated_at", (did, new_head, at))
        queue.extend(r['id'] for r in db.execute("SELECT id FROM decisions WHERE source_id=? OR (parent_id=? AND independent_source_replacement=0 AND historical_parent_revalidated=0) OR id IN (SELECT decision_id FROM decision_links WHERE related_id=? AND kind IN ('derived','depends'))", (did, did, did)))
    return sorted(seen)


def add_anchor(db, task_id, record_id, version_id, role='work_item'):
    from .store import Invalid
    if role not in ('work_item', 'context'):
        raise Invalid('Task anchors are work_item or context, not supporting premises')
    run = db.execute('SELECT repo FROM runs WHERE id=?', (task_id,)).fetchone()
    cit = citation(db, record_id, version_id)
    if run is None or cit is None or run['repo'] != cit['repo']:
        raise Invalid('Source anchor is outside this task repository')
    db.execute('INSERT OR IGNORE INTO task_source_anchors(task_id,record_id,source_version_id,role,recorded_at) VALUES(?,?,?,?,?)',
               (task_id, record_id, version_id, role, stamp()))


def anchors(db, task_id):
    return [{**citation(db, r['record_id'], r['source_version_id']), 'role': r['role']} for r in db.execute('SELECT * FROM task_source_anchors WHERE task_id=? ORDER BY recorded_at', (task_id,))]


def review_changes_reliance(db, decision_id, pins, decision_pins):
    """Predict the material pin change displayed by a human-source review.

    Read-only: never create an immutable version or move a historical pointer.
    The confirmation still validates every pin under its selected writer.
    """
    root = db.execute('SELECT * FROM decisions WHERE id=?', (decision_id,)).fetchone()
    if root is None:
        return False
    if root['source_reuse_state'] == 'unknown':
        return True
    previous = [pin(e, e['role']) for e in edges(db, decision_id)]
    if pins is not None and sorted(map(encoded, pins)) != sorted(map(encoded, previous)):
        return True
    direct = {r['related_id'] for r in db.execute("SELECT related_id FROM decision_links WHERE decision_id=? AND kind IN ('derived','depends')", (decision_id,))}
    if _relied_on_source(db, root):
        direct.add(root['source_id'])
    if root['parent_id'] and not root['independent_source_replacement'] and not root['historical_parent_revalidated']:
        direct.add(root['parent_id'])
    selected = {p['decision_id']: p for p in decision_pins}
    for source_id in direct:
        selected_pin = selected.get(source_id)
        if selected_pin is None:
            continue  # Incomplete proposals are refused separately, never repaired here.
        linked = db.execute("SELECT source_version_id FROM decision_links WHERE decision_id=? AND related_id=? AND kind IN ('derived','depends')", (decision_id, source_id)).fetchall()
        if (not linked or selected_pin.get('historical')
                or any(link['source_version_id'] != selected_pin['source_version_id'] for link in linked)
                or (root['source_id'] == source_id and not _same_source_revision(root['source_revision'], selected_pin['updated_at']))):
            return True
    return False


def source_revalidation(db, decision_id):
    """Read-only proposal containing every snapshot an explicit review will bind.

    Nothing here updates active edges. Dependent human decisions must be current
    first; the caller stores the returned pins with the actual human readback.
    """
    root = db.execute('SELECT * FROM decisions WHERE id=?', (decision_id,)).fetchone()
    if root is None:
        return {'available': False, 'sources': [], 'pins': [], 'decision_pins': [], 'dependencies': [], 'notice': 'Decision not found'}
    has_reliance = bool(root['source_reuse_state'] == 'unknown' or _relied_on_source(db, root)
        or (root['parent_id'] and not root['independent_source_replacement'] and not root['historical_parent_revalidated'])
        or db.execute("SELECT 1 FROM decision_links WHERE decision_id=? AND kind IN ('derived','depends') LIMIT 1", (decision_id,)).fetchone()
        or db.execute("SELECT 1 FROM decision_source_edges WHERE decision_id=? AND active=1 AND role IN ('support','contradiction') LIMIT 1", (decision_id,)).fetchone())
    queue, seen, dependencies, source_map, blockers = [decision_id], set(), [], {}, []
    while queue:
        current_id = queue.pop()
        if current_id in seen:
            continue
        seen.add(current_id)
        current = db.execute('SELECT * FROM decisions WHERE id=?', (current_id,)).fetchone()
        if current is None or current['repo'] != root['repo']:
            blockers.append('A source decision is missing or outside this repository')
            continue
        if current['source_reuse_state'] == 'unknown':
            blockers.append(LEGACY_REUSE_NOTICE)
        if current_id != decision_id:
            run = db.execute('SELECT status FROM runs WHERE id=?', (current['run_id'],)).fetchone()
            historical = run is not None and run['status'] == 'completed'
            binding = decision_binding(db, current_id)
            dependencies.append({'decision_id': current_id, 'updated_at': current['updated_at'],
                                 'question': current['question'], 'answer': current['answer'] or '',
                                 'context': current['context'], 'needs_review': bool(current['needs_review']), 'historical': historical,
                                 **{key: current[key] for key in ('rationale', 'repo', 'path', 'scope_paths', 'facts', 'category', 'options', 'applicability', 'required_signers', 'signed_by', 'rule_conditions', 'rule_scope', 'rule_expires')},
                                 **binding})
            if current['needs_review'] and not historical:
                blockers.append(f'Review source decision {current_id} first')
        for old in edges(db, current_id):
            # Historical context remains readable but is not silently refreshed.
            if old['role'] not in RELIANCE:
                if current_id == decision_id:
                    version_id = old['source_version_id']
                else:
                    continue
            else:
                version_id = old['head_id']
            version = db.execute('SELECT snapshot FROM source_versions WHERE id=? AND record_id=?',
                                 (version_id, old['record_id'])).fetchone()
            if version is None:
                blockers.append('An exact source snapshot is missing')
                continue
            snapshot = json.loads(version['snapshot'])
            head = db.execute('SELECT availability FROM source_records WHERE id=?', (old['record_id'],)).fetchone()
            if head is None or head['availability'] != 'available':
                blockers.append(f"Source {old['ref']} is unavailable; restore access before revalidation")
                # Retained context is referenced by ID only, without exposing lost content.
                snapshot = {'availability': 'inaccessible', 'body': '', 'title': ''}
            key = (old['record_id'], version_id, old['role'])
            source_map[key] = {'record_id': old['record_id'], 'source_version_id': version_id,
                               'role': old['role'], 'previous_version_id': old['source_version_id'],
                               'changed': version_id != old['source_version_id'], 'snapshot': snapshot}
        linked = {r['related_id'] for r in db.execute("SELECT related_id FROM decision_links WHERE decision_id=? AND kind IN ('derived','depends')", (current_id,))}
        if _relied_on_source(db, current):
            linked.add(current['source_id'])
        if current['parent_id'] and not current['independent_source_replacement'] and not current['historical_parent_revalidated']:
            linked.add(current['parent_id'])
        queue.extend(sorted(linked - seen))
    sources = list(source_map.values())
    # Explicit IDs cannot be guessed across selected source installations.
    scopes = {(r['provider'], r['namespace']) for r in db.execute('SELECT s.provider,s.namespace FROM task_source_anchors a JOIN source_records s ON s.id=a.record_id WHERE a.task_id=?', (root['run_id'],))}
    for source in sources:
        snapshot = source['snapshot']
        provider, namespace = snapshot.get('provider', 'legacy'), snapshot.get('namespace', 'legacy')
        shared = provider in ('legacy', 'git') or (provider == 'github' and namespace == 'github.com')
        if scopes and not shared and (provider, namespace) not in scopes:
            blockers.append('A source is outside the explicitly selected task namespace')
    notice = '; '.join(dict.fromkeys(blockers)) if blockers else (
        'Review the complete displayed source snapshots and source decisions before confirming these exact pins. '
        'A newer source change will refuse this review; nothing is refreshed by an ordinary yes.')
    proposed_pins = [pin(source, source['role']) for source in sources]
    decision_pins = [{key: d[key] for key in ('decision_id', 'updated_at', 'historical', 'source_version_id', 'source_snapshot_sha256')} for d in dependencies]
    return {'available': bool(sources or dependencies) and not blockers, 'has_reliance': has_reliance, 'sources': sources,
            'retires_rule': bool(root['reusable'] and review_changes_reliance(db, decision_id, proposed_pins, decision_pins)),
            'pins': proposed_pins, 'decision_pins': decision_pins,
            'dependencies': dependencies, 'notice': notice if sources or dependencies or blockers else 'No versioned source chain is available; legacy provenance remains unknown.',
            'blocked_on_unknown_provenance_only': bool(blockers) and all(b == LEGACY_REUSE_NOTICE for b in blockers),
            'review_path': '/#runs/' + root['run_id']}


def validate_decision_pins(db, pins):
    from .store import Invalid
    if not isinstance(pins, list):
        raise Invalid('source_decision_pins must be an array')
    for item in pins:
        if (not isinstance(item, dict) or not isinstance(item.get('decision_id'), str)
                or not isinstance(item.get('updated_at'), str) or not isinstance(item.get('source_version_id'), str)
                or not isinstance(item.get('source_snapshot_sha256'), str)):
            raise Invalid('source_decision_pins must include decision_id, updated_at, source_version_id and source_snapshot_sha256 from a complete current review')
        current = db.execute('SELECT d.updated_at,d.needs_review,r.status AS run_status FROM decisions d JOIN runs r ON r.id=d.run_id WHERE d.id=?', (item['decision_id'],)).fetchone()
        historical = item.get('historical') is True and current is not None and current['run_status'] == 'completed'
        if current is None or (current['needs_review'] and not historical) or current['updated_at'] != item['updated_at'] or (item.get('historical') and not historical):
            raise Invalid('A displayed source decision changed; reopen the current-source review')
        binding = decision_binding(db, item['decision_id'])
        if (binding is None or binding['source_snapshot_sha256'] != item['source_snapshot_sha256']
                or binding['source_version_id'] != item['source_version_id']):
            raise Invalid('The immutable source-decision version or displayed scope changed; reopen the complete review')


def rebind_reviewed_human_sources(db, decision_id, pins, decision_pins, reviewed_revision):
    """Only an explicitly displayed, complete human review can move live
    human-source links. Old target and source snapshots are never rewritten.
    Runs on the answer/signoff writer connection, before its final snapshot.
    Returns (complete_review, changed_live_binding) so identical review does
    not erase an already-collected co-signature.
    """
    from .store import Invalid
    if pins is None and not decision_pins:
        return False, False
    proposal = source_revalidation(db, decision_id)
    expected = proposal['decision_pins']
    if not expected and not decision_pins:
        return False, False
    current = db.execute('SELECT * FROM decisions WHERE id=?', (decision_id,)).fetchone()
    if current is None or not reviewed_revision or reviewed_revision != current['updated_at']:
        raise Invalid('A fresh expected_updated_at is required for human-source revalidation')
    reviewed_legacy = current['source_reuse_state'] == 'reviewed' and pins and proposal.get('blocked_on_unknown_provenance_only')
    if not proposal['available'] and not reviewed_legacy:
        raise Invalid(proposal['notice'])
    validate_decision_pins(db, decision_pins)
    if sorted(map(encoded, expected)) != sorted(map(encoded, decision_pins)):
        raise Invalid('Submit the complete, exact source_decision_pins from the displayed current review')
    required = {(p['record_id'], p['source_version_id'], p['role']) for p in proposal['pins'] if p['role'] in RELIANCE}
    provided = {(p['record_id'], p['source_version_id'], p['role']) for p in (pins or [])}
    if not required <= provided:
        raise Invalid('Review and submit every displayed current external premise with the human-source pins')
    validate(db, decision_id, pins or [], current_roles=RELIANCE)
    snapshot_decision(db, decision_id, reason='before-human-source-rebind')
    selected = {p['decision_id']: p for p in decision_pins}
    direct = {r['related_id'] for r in db.execute("SELECT related_id FROM decision_links WHERE decision_id=? AND kind IN ('derived','depends')", (decision_id,))}
    if _relied_on_source(db, current):
        direct.add(current['source_id'])
    if current['parent_id'] and not current['independent_source_replacement'] and not current['historical_parent_revalidated']:
        direct.add(current['parent_id'])
    changed = False
    for source_id in sorted(direct):
        selected_pin = selected[source_id]
        version_id = selected_pin['source_version_id']
        if not version_id:
            # The observed hash already bound the complete read-only preview.
            # Persist that exact observation now, never manufacture a past one.
            version_id = snapshot_decision(db, source_id, reason='reviewed-current-human-source')
            saved = db.execute('SELECT snapshot FROM decision_versions WHERE id=?', (version_id,)).fetchone()
            if digest(_binding_projection(json.loads(saved['snapshot']))) != selected_pin['source_snapshot_sha256']:
                raise Invalid('The reviewed human-source snapshot changed while binding it')
        linked = db.execute("SELECT source_version_id FROM decision_links WHERE decision_id=? AND related_id=? AND kind IN ('derived','depends')", (decision_id, source_id)).fetchall()
        changed = changed or not linked or any(link['source_version_id'] != version_id for link in linked) or selected_pin['historical']
        if not linked:
            db.execute("INSERT INTO decision_links(decision_id,related_id,kind,note,created_at,source_version_id) VALUES(?,?,'derived',?,?,?)", (decision_id, source_id, 'Explicitly reviewed current human-source revision', stamp(), version_id))
        else:
            db.execute("UPDATE decision_links SET source_version_id=? WHERE decision_id=? AND related_id=? AND kind IN ('derived','depends')", (version_id, decision_id, source_id))
        if current['source_id'] == source_id:
            changed = changed or not _same_source_revision(current['source_revision'], selected_pin['updated_at'])
            db.execute('UPDATE decisions SET source_revision=? WHERE id=?', (selected_pin['updated_at'], decision_id))
    return True, changed


def require_retained_premises(db, decision_id, pins):
    from .store import Invalid
    if pins is None:
        return
    validate(db, decision_id, pins, current_roles=RELIANCE)
    before = {(e['record_id'], e['role']) for e in edges(db, decision_id) if e['role'] in RELIANCE}
    after = {(p['record_id'], p['role']) for p in pins}
    if not before <= after:
        raise Invalid('Use an explicit independent replacement to retire a supporting or contradictory premise')


def revalidate_historical_context(db, decision_id, pins, decision_pins):
    """An explicit current-source reading can keep a completed precedent as
    historical context while establishing this task's own current premises.
    It never rewrites or resigns that completed decision.
    """
    from .store import Invalid
    historical = {p['decision_id'] for p in decision_pins if p.get('historical') is True}
    if not historical:
        return
    proposal = source_revalidation(db, decision_id)
    reviewed_legacy = db.execute("SELECT 1 FROM decisions WHERE id=? AND source_reuse_state='reviewed'", (decision_id,)).fetchone()
    if not proposal['available'] and not (reviewed_legacy and pins and proposal.get('blocked_on_unknown_provenance_only')):
        raise Invalid(proposal['notice'])
    required = {(p['record_id'], p['source_version_id'], p['role']) for p in proposal['pins'] if p['role'] in RELIANCE}
    supplied = {(p['record_id'], p['source_version_id'], p['role']) for p in (pins or [])}
    if not required <= supplied:
        raise Invalid('Review and submit every displayed current premise before revalidating historical context')
    validate(db, decision_id, pins or [], current_roles=RELIANCE)
    for source_id in historical:
        # Every formerly active link stays recorded in earlier immutable
        # snapshots; the current link is explicitly historical context.
        for link in db.execute("SELECT * FROM decision_links WHERE decision_id=? AND related_id=? AND kind='derived'", (decision_id, source_id)).fetchall():
            db.execute("INSERT OR IGNORE INTO decision_links(decision_id,related_id,kind,note,created_at,source_version_id,decision_version_id) VALUES(?,?,'related',?,?,?,?)", (decision_id, source_id, 'Historical signed precedent; current premises explicitly revalidated on this task', stamp(), link['source_version_id'], link['decision_version_id']))
            db.execute("DELETE FROM decision_links WHERE decision_id=? AND related_id=? AND kind='derived'", (decision_id, source_id))
        db.execute("UPDATE decisions SET source_id=NULL,source_revision='' WHERE id=? AND source_id=?", (decision_id, source_id))
        db.execute('UPDATE decisions SET historical_parent_revalidated=1 WHERE id=? AND parent_id=?', (decision_id, source_id))
