"""Consent-bound meeting lifecycle; transcripts use the canonical source corpus.

No scheduler, credential persistence, automatic join or implied authorization.
All service methods must be exposed only through the workspace's authorization.
"""
import json
import re
import uuid

from . import context_memory as cm
from .meeting_providers import Vexa, OutcomeUnknown
from .store import Invalid, field, now, repo_key

SCHEMA = '''
CREATE TABLE IF NOT EXISTS meeting_occurrences (
 id TEXT PRIMARY KEY, repo TEXT NOT NULL, provider TEXT NOT NULL, namespace TEXT NOT NULL,
 platform TEXT NOT NULL, native_meeting_id TEXT NOT NULL, occurrence_id TEXT NOT NULL, title TEXT NOT NULL,
 state TEXT NOT NULL, remote_id TEXT NOT NULL DEFAULT '', remote_state TEXT NOT NULL DEFAULT '', consent TEXT NOT NULL DEFAULT '{}',
 record_id TEXT NOT NULL DEFAULT '', revoked INTEGER NOT NULL DEFAULT 0,
 last_error TEXT NOT NULL DEFAULT '', transcript_version TEXT NOT NULL DEFAULT '',
 transcript_complete INTEGER NOT NULL DEFAULT 0, transcript_chars INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(repo,provider,namespace,occurrence_id));
CREATE TABLE IF NOT EXISTS meeting_operations (
 id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL REFERENCES meeting_occurrences(id),
 action TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meeting_transcript_chunks (
 meeting_id TEXT NOT NULL REFERENCES meeting_occurrences(id), chunk_key TEXT NOT NULL,
 record_id TEXT NOT NULL REFERENCES source_records(id), active INTEGER NOT NULL DEFAULT 1, generation INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(meeting_id,chunk_key));
'''

MAX_TRANSCRIPT_CHARS = 1_000_000
CHUNK_CHARS = 16000
MAX_SEGMENTS = 20000

def migrate(db):
    db.executescript(SCHEMA)
    columns = {r['name'] for r in db.execute('PRAGMA table_info(meeting_transcript_chunks)')}
    if 'generation' not in columns:
        db.execute('ALTER TABLE meeting_transcript_chunks ADD COLUMN generation INTEGER NOT NULL DEFAULT 1')
    # Upgrade the initial metadata schema without dropping its audit rows/FKs.
    # New occurrence/operation tables avoid SQLite-only constraint rebuilds.
    def table_exists(db, name):
        return bool(db.execute('PRAGMA table_info(' + name + ')').fetchall())
    if table_exists(db, 'meeting_records'):
        db.execute("INSERT INTO meeting_occurrences(id,repo,provider,namespace,platform,native_meeting_id,occurrence_id,title,state,"
            "remote_id,remote_state,consent,record_id,revoked,last_error,created_at,updated_at) "
            "SELECT id,repo,provider,namespace,platform,native_meeting_id,id,title,state,remote_id,remote_state,consent,record_id,"
            "revoked,last_error,created_at,updated_at FROM meeting_records WHERE 1=1 ON CONFLICT(id) DO NOTHING")
        db.execute("INSERT INTO meeting_transcript_chunks(meeting_id,chunk_key,record_id) "
            "SELECT id,'legacy',record_id FROM meeting_records WHERE record_id<>'' ON CONFLICT(meeting_id,chunk_key) DO NOTHING")
    if table_exists(db, 'meeting_commands'):
        db.execute("INSERT INTO meeting_operations SELECT * FROM meeting_commands WHERE 1=1 ON CONFLICT(id) DO NOTHING")


def get(store, meeting_id, repo):
    row = store.graph.db.execute('SELECT * FROM meeting_occurrences WHERE id=? AND repo=?',
                                 (meeting_id, repo_key(repo))).fetchone()
    if not row:
        raise Invalid('Meeting not found in this workspace scope')
    result = dict(row)
    result['consent'] = json.loads(result['consent'])
    result['revoked'] = bool(result['revoked'])
    result['transcript_complete'] = bool(result['transcript_complete'])
    result['chunks'] = [dict(r) for r in store.graph.db.execute('SELECT chunk_key,record_id,active FROM meeting_transcript_chunks WHERE meeting_id=? ORDER BY chunk_key', (meeting_id,))]
    result['notice'] = 'Transcript evidence is untrusted, not authorization. All workspace readers can access shared imports.'
    return result


def list_meetings(store, repo):
    return {'meetings': [operational_view(store, get(store, r['id'], repo)) for r in store.graph.db.execute(
        'SELECT id FROM meeting_occurrences WHERE repo=? ORDER BY created_at DESC LIMIT 200', (repo_key(repo),))],
        'capabilities': {'meeting_participant': ['vexa'], 'transcript_import': True,
                         'browser_microphone_is_meeting_bot': False, 'automatic_join': False}}


def create(store, data):
    repo = repo_key(field(data, 'repo'))
    provider = data.get('provider', 'import')
    if provider not in ('vexa', 'import'):
        raise Invalid('Implemented meeting providers are vexa and import; hosted alternatives are not configured')
    namespace = field(data, 'namespace', limit=300)
    native = field(data, 'native_meeting_id', limit=200)
    platform = data.get('platform', 'import')
    if platform not in ('google_meet', 'teams', 'zoom', 'import') or (provider == 'vexa' and platform == 'import'):
        raise Invalid('Select Google Meet, Teams, Zoom or transcript import')
    if provider == 'vexa' and (not re.fullmatch(r'[A-Za-z0-9_:@.\-]+', native)):
        raise Invalid('Use the platform meeting identifier, not a URL')
    title = field(data, 'title', limit=300)
    mid, at = str(uuid.uuid4()), now()
    occurrence = field(data, 'occurrence_id', limit=200) if 'occurrence_id' in data else mid
    with store.graph.transaction():
        previous = store.graph.db.execute('SELECT id FROM meeting_occurrences WHERE repo=? AND provider=? AND namespace=? '
                'AND occurrence_id=?', (repo, provider, namespace, occurrence)).fetchone()
        if previous:
            raise Invalid('This meeting occurrence already exists; use its existing record')
        store.graph.db.execute('INSERT INTO meeting_occurrences(id,repo,provider,namespace,platform,native_meeting_id,occurrence_id,title,state,created_at,updated_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?)', (mid, repo, provider, namespace, platform, native, occurrence, title, 'planned', at, at))
        store.graph.append_event('meeting_created', {'meeting_id': mid, 'repo': repo, 'provider': provider})
    return get(store, mid, repo)


def set_consent(store, meeting_id, repo, data):
    by = field(data, 'by', limit=100)
    for flag in ('join', 'transcribe', 'record', 'notice_confirmed'):
        if type(data.get(flag)) is not bool:
            raise Invalid('Explicit boolean join, transcribe, record and notice_confirmed permissions are required')
    if not data['join'] or not data['notice_confirmed']:
        raise Invalid('Joining requires explicit authorization and participant notice confirmation; use revoke to withdraw')
    consent = {k: data[k] for k in ('join', 'transcribe', 'record', 'notice_confirmed')}
    consent.update({'by': by, 'at': now()})
    with store.graph.transaction():
        meeting = get(store, meeting_id, repo)
        if meeting['state'] not in ('planned', 'rejected') or meeting['revoked']:
            raise Invalid('Consent is set only before dispatch; stop or revoke an existing capture')
        store.graph.db.execute("UPDATE meeting_occurrences SET consent=?,state='planned',updated_at=? WHERE id=?", (cm.encoded(consent), now(), meeting_id))
        store.graph.append_event('meeting_consent', {'meeting_id': meeting_id, 'repo': repo, **consent})
    return get(store, meeting_id, repo)


def command(store, meeting_id, repo, action, *, client=None, options=None):
    if action not in ('join', 'stop', 'delete_remote'):
        raise Invalid('Unsupported meeting command')
    client = client or Vexa()
    cid, at = str(uuid.uuid4()), now()
    with store.graph.transaction():
        meeting = get(store, meeting_id, repo)
        if meeting['provider'] != 'vexa' or getattr(client, 'provider', '') != 'vexa':
            raise Invalid('Transcript imports cannot dispatch meeting participants')
        pending = store.graph.db.execute("SELECT 1 FROM meeting_operations WHERE meeting_id=? AND state='pending'", (meeting_id,)).fetchone()
        if pending:
            raise Invalid('A provider command is pending; do not duplicate it')
        if action in ('join', 'stop'):
            overlap = store.graph.db.execute("SELECT id FROM meeting_occurrences WHERE id<>? AND provider=? "
                "AND platform=? AND native_meeting_id=? AND state NOT IN ('planned','completed','failed','remote_deleted','rejected')",
                (meeting_id, meeting['provider'], meeting['platform'], meeting['native_meeting_id'])).fetchone()
            if overlap:
                raise Invalid('Another occurrence in this room has an unresolved participant; reconcile it before controlling this room')
        if action == 'stop' and meeting['state'] in ('planned', 'completed', 'failed', 'remote_deleted'):
            raise Invalid('This occurrence has no active dispatch to stop; do not affect another occurrence in its room')
        if action == 'join':
            consent = meeting['consent']
            if meeting['state'] != 'planned' or meeting['revoked'] or not consent.get('join') or not consent.get('notice_confirmed'):
                raise Invalid('Join requires an unrevoked planned meeting with explicit participant consent')
        if action == 'delete_remote' and meeting['state'] not in ('completed', 'failed'):
            raise Invalid('Stop and reconcile the meeting before deleting remote artifacts')
        # Durable intent commits before provider I/O. Credentials and passcodes never enter this ledger.
        store.graph.db.execute('INSERT INTO meeting_operations VALUES(?,?,?,?,?,?)', (cid, meeting_id, action, 'pending', at, at))
        store.graph.db.execute('UPDATE meeting_occurrences SET state=?,last_error=\'\',updated_at=? WHERE id=?',
                               (action + '_pending', at, meeting_id))
    if action == 'delete_remote':
        revoke(store, meeting_id, repo)
    try:
        if action == 'join':
            response = client.join(meeting, meeting['consent'], options or {})
        else:
            response = getattr(client, action)(meeting)
        returned_id = str(response.get('meeting_id') or response.get('id') or '')
        if action != 'join' and meeting['remote_id'] and returned_id and returned_id != meeting['remote_id']:
            raise OutcomeUnknown('Provider response occurrence does not match bound occurrence')
    except Exception as error:
        # Even unexpected failures must not strand a silent pending command or permit duplicate dispatch.
        unknown = isinstance(error, OutcomeUnknown) or not isinstance(error, Invalid)
        state = 'unknown' if unknown else 'rejected'
        with store.graph.transaction():
            store.graph.db.execute('UPDATE meeting_operations SET state=?,updated_at=? WHERE id=?', (state, now(), cid))
            store.graph.db.execute('UPDATE meeting_occurrences SET state=?,last_error=?,updated_at=? WHERE id=?',
                (state if action == 'join' or unknown else meeting['state'], 'Provider outcome unknown; reconcile, never blindly repeat join' if unknown else 'Provider rejected command; check configuration and meeting permissions', now(), meeting_id))
        return get(store, meeting_id, repo)
    state = {'join': 'requested', 'stop': 'stopping', 'delete_remote': 'remote_deleted'}[action]
    with store.graph.transaction():
        store.graph.db.execute("UPDATE meeting_operations SET state='accepted',updated_at=? WHERE id=?", (now(), cid))
        remote_id = str(response.get('meeting_id') or response.get('id') or meeting.get('remote_id') or '')
        store.graph.db.execute('UPDATE meeting_occurrences SET state=?,remote_id=?,updated_at=? WHERE id=?', (state, remote_id, now(), meeting_id))
        store.graph.append_event('meeting_command_accepted', {'meeting_id': meeting_id, 'repo': repo, 'action': action})
    if action == 'delete_remote':
        revoke(store, meeting_id, repo)
    return get(store, meeting_id, repo)


def reconcile(store, meeting_id, repo, *, client=None):
    meeting = get(store, meeting_id, repo)
    if meeting['provider'] != 'vexa':
        raise Invalid('Imported transcripts have no remote participant')
    result = (client or Vexa()).status(meeting)
    detail = result.get('meeting')
    if isinstance(detail, dict) and str(detail.get('id')) == meeting.get('remote_id'):
        remote = str(detail.get('status', 'unknown'))
    else:
        running = result.get('running_bots', result.get('running'))
        if not isinstance(running, list):
            raise Invalid('Unsupported Vexa status response; no lifecycle inference made')
        matches = [b for b in running if isinstance(b, dict) and b.get('platform') == meeting['platform']
                   and str(b.get('native_meeting_id')) == meeting['native_meeting_id']]
        remote = str(matches[0].get('status', 'running')) if len(matches) == 1 else 'not_observed'
    # Absence cannot prove completion or a failed join. Keep unknown blocked.
    state = remote if remote in ('completed', 'failed') and meeting.get('remote_id') else meeting['state']
    with store.graph.transaction():
        store.graph.db.execute('UPDATE meeting_occurrences SET remote_state=?,state=?,updated_at=? WHERE id=?', (remote, state, now(), meeting_id))
    return get(store, meeting_id, repo)


def _withhold(store, record_id, reason):
    db = store.graph.db
    row = db.execute('SELECT head_id FROM source_records WHERE id=?', (record_id,)).fetchone()
    db.execute("UPDATE source_records SET availability='inaccessible' WHERE id=?", (record_id,))
    for version in db.execute('SELECT id FROM source_versions WHERE record_id=?', (record_id,)).fetchall():
        db.execute('INSERT INTO withheld_source_versions VALUES(?,?,?) ON CONFLICT(source_version_id) DO NOTHING',
                   (version['id'], reason, now()))
    if row:
        cm.invalidate(db, record_id, row['head_id'], include_current=True, reason_override=reason)


def _transcript_parts(data):
    """Bound before writes; preserve every character and explicit segment provenance."""
    if 'segments' not in data:
        body = data.get('body')
        if not isinstance(body, str) or not body.strip() or len(body) > MAX_TRANSCRIPT_CHARS:
            raise Invalid('Transcript body must be nonempty text of at most 1000000 characters')
        return [body[n:n + CHUNK_CHARS] for n in range(0, len(body), CHUNK_CHARS)], len(body)
    segments = data['segments']
    if not isinstance(segments, list) or not segments or len(segments) > MAX_SEGMENTS:
        raise Invalid('Transcript requires 1–20000 segments; no partial import performed')
    lines, seen, size = [], set(), 0
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict) or not isinstance(segment.get('text'), str):
            raise Invalid('Invalid transcript segment; no partial import performed')
        sid = str(segment.get('segment_id') or 'position:' + str(index))
        if len(sid) > 300 or sid in seen:
            raise Invalid('Transcript segment IDs must be unique and at most 300 characters')
        seen.add(sid)
        speaker = str(segment.get('speaker') or 'Unknown speaker')
        if len(speaker) > 300:
            raise Invalid('Transcript speaker label exceeds limit')
        for key in ('start', 'end'):
            value = segment.get(key)
            if value is not None and (type(value) not in (int, float) or not 0 <= value <= 604800):
                raise Invalid('Segment timestamps must be finite nonnegative seconds within one week')
        if segment.get('start') is not None and segment.get('end') is not None and segment['end'] < segment['start']:
            raise Invalid('Segment end precedes start')
        timing = '[' + str(segment.get('start', '?')) + '–' + str(segment.get('end', '?')) + 's] '
        draft = '[draft] ' if segment.get('completed') is False else ''
        line = '[segment ' + sid + '] ' + timing + draft + speaker + ': ' + segment['text'] + '\n'
        size += len(line)
        if size > MAX_TRANSCRIPT_CHARS:
            raise Invalid('Transcript exceeds 1000000 character bound; no partial import performed')
        lines.append(line)
    body = ''.join(lines)
    return [body[n:n + CHUNK_CHARS] for n in range(0, len(body), CHUNK_CHARS)], len(body)


def import_transcript(store, meeting_id, repo, data):
    if data.get('workspace_shared') is not True:
        raise Invalid('Confirm this transcript may be disclosed to every workspace reader')
    by = field(data, 'by', limit=100)
    version = field(data, 'source_version', limit=500)
    complete = data.get('complete', False)
    if type(complete) is not bool:
        raise Invalid('complete must explicitly be a boolean, not inferred from transcript content')
    parts, size = _transcript_parts(data)
    with store.graph.transaction():
        meeting = get(store, meeting_id, repo)
        if meeting['revoked']:
            raise Invalid('This meeting was revoked; its transcript cannot be re-imported')
        results, active_keys = [], set()
        for index, body in enumerate(parts):
            key = 'chunk:' + str(index).zfill(5)
            active_keys.add(key)
            previous = store.graph.db.execute('SELECT active,generation FROM meeting_transcript_chunks WHERE meeting_id=? AND chunk_key=?', (meeting_id, key)).fetchone()
            generation = previous['generation'] + (not previous['active']) if previous else 1
            result = store.add_record({'repo': repo, 'kind': 'meeting', 'provider': 'generic',
                'namespace': 'meeting:' + meeting['provider'] + ':' + meeting['namespace'],
                'external_id': meeting['occurrence_id'] + ':' + key,
                'ref': 'meeting:' + meeting_id + ':' + key, 'title': meeting['title'], 'body': body,
                'source_version': 'chunk:' + cm.digest(body),
                'access_scope': cm.encoded({'workspace_shared': True, 'attested_by': by,
                    'occurrence_id': meeting['occurrence_id'], 'chunk_key': key, 'generation': generation,
                    'transcript_complete': complete}),
                'availability': 'available', 'status': 'transcript', 'resolved': False})
            results.append(result)
            store.graph.db.execute('INSERT INTO meeting_transcript_chunks(meeting_id,chunk_key,record_id,active,generation) VALUES(?,?,?,1,?) '
                'ON CONFLICT(meeting_id,chunk_key) DO UPDATE SET record_id=excluded.record_id,active=1,generation=excluded.generation',
                (meeting_id, key, result['source']['record_id'], generation))
        for old in store.graph.db.execute('SELECT chunk_key,record_id FROM meeting_transcript_chunks WHERE meeting_id=? AND active=1', (meeting_id,)).fetchall():
            if old['chunk_key'] not in active_keys:
                _withhold(store, old['record_id'], 'Transcript chunk removed by a newer full snapshot')
                store.graph.db.execute('UPDATE meeting_transcript_chunks SET active=0 WHERE meeting_id=? AND chunk_key=?', (meeting_id, old['chunk_key']))
        store.graph.db.execute('UPDATE meeting_occurrences SET record_id=?,transcript_version=?,transcript_complete=?,transcript_chars=?,updated_at=? WHERE id=?',
            (results[0]['source']['record_id'], version, int(complete), size, now(), meeting_id))
        store.graph.append_event('meeting_transcript_imported', {'meeting_id': meeting_id, 'repo': repo, 'by': by,
            'chunk_count': len(results), 'characters': size, 'complete': complete, 'source_version': version})
    return {'meeting': get(store, meeting_id, repo), 'source': results[0]['source'],
            'sources': [r['source'] for r in results], 'changed': any(r['changed'] for r in results),
            'manifest': {'complete': complete, 'characters': size, 'chunk_count': len(results), 'source_version': version}}


def revoke(store, meeting_id, repo):
    """Withdraw local disclosure/reuse, not a claim of stopping remote recording."""
    with store.graph.transaction():
        meeting = get(store, meeting_id, repo)
        db = store.graph.db
        records = {r['record_id'] for r in db.execute('SELECT record_id FROM meeting_transcript_chunks WHERE meeting_id=?', (meeting_id,))}
        if meeting['record_id']:
            records.add(meeting['record_id'])
        for record_id in records:
            _withhold(store, record_id, 'Meeting disclosure revoked; retained transcript is not reusable')
        db.execute("UPDATE meeting_occurrences SET revoked=1,consent='{}',updated_at=? WHERE id=?", (now(), meeting_id))
        db.execute("UPDATE meeting_observers SET enabled=0,status='authorization_required',revision=revision+1,lease_owner='',lease_until=0,updated_at=? WHERE meeting_id=?", (now(), meeting_id))
        store.graph.append_event('meeting_revoked', {'meeting_id': meeting_id, 'repo': repo,
            'remote_stop_required': meeting['state'] not in ('planned', 'stopped', 'completed', 'failed', 'remote_deleted')})
        store.graph._bump(repo_key(repo))
    return get(store, meeting_id, repo)


def sync_transcript(store, meeting_id, repo, data, *, client=None):
    """Explicitly fetch Vexa transcript; never fetch before disclosure attestation."""
    if data.get('workspace_shared') is not True:
        raise Invalid('Confirm workspace-wide disclosure before fetching a transcript')
    field(data, 'by', limit=100)
    meeting = get(store, meeting_id, repo)
    if meeting['provider'] != 'vexa' or meeting['revoked'] or not meeting['remote_id']:
        raise Invalid('Only an unrevoked Vexa occurrence bound to an exact remote meeting ID can sync transcripts')
    response = (client or Vexa()).transcript(meeting)
    if str(response.get('id')) != meeting['remote_id']:
        raise Invalid('Provider transcript occurrence does not match the requested meeting')
    segments = response.get('segments')
    payload = dict(data, segments=segments, source_version='vexa:' + cm.digest(segments),
                   complete=response.get('status') == 'completed')
    return import_transcript(store, meeting_id, repo, payload)


def operational_view(store, meeting):
    """Capture control metadata is not transcript evidence and survives withdrawal.

    Deliberately omit record_id/chunks, transcript source citations and raw errors;
    the existing source visibility sanitizer must still govern evidence objects.
    """
    current = get(store, meeting['id'], meeting['repo'])
    keys = ('id', 'repo', 'provider', 'namespace', 'platform', 'native_meeting_id', 'occurrence_id',
            'state', 'remote_state', 'revoked', 'consent', 'created_at', 'updated_at',
            'transcript_complete', 'transcript_chars')
    result = {key: current[key] for key in keys}
    result['title'] = 'Meeting capture (transcript withheld)' if current['revoked'] else current['title']
    result['notice'] = current['notice']
    result['remote_stop_required'] = current['state'] not in ('planned', 'completed', 'failed', 'remote_deleted')
    result['evidence_withheld'] = current['revoked']
    from .meeting_worker import status
    result['background_sync'] = status(store, current['id'])
    return result
