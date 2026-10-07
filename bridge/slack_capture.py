"""Follow mutations only for explicitly captured Slack message identities.

The signed callback's workspace is carried throughout. No Slack reads, search
results, old imports, or channel history can enroll a message here.
"""
import hashlib
import json
import re
import time

from .graph import now_iso

MAX_PENDING = 1000
MAX_TEXT = 20000
KEY_SQL = 'workspace=? AND channel=? AND message_ts=?'


class CaptureQueueFull(RuntimeError):
    pass


def migrate(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS slack_captures (
            workspace TEXT NOT NULL, channel TEXT NOT NULL, message_ts TEXT NOT NULL,
            repo TEXT NOT NULL, record_id TEXT NOT NULL DEFAULT '',
            latest_clock BIGINT NOT NULL DEFAULT 0, latest_digest TEXT NOT NULL DEFAULT '',
            deleted INTEGER NOT NULL DEFAULT 0, accepted_at TEXT NOT NULL,
            PRIMARY KEY(workspace,channel,message_ts));
        CREATE TABLE IF NOT EXISTS slack_capture_mutations (
            workspace TEXT NOT NULL, channel TEXT NOT NULL, message_ts TEXT NOT NULL,
            event_id TEXT NOT NULL, clock BIGINT NOT NULL, digest TEXT NOT NULL,
            payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
            accepted_at TEXT NOT NULL,
            PRIMARY KEY(workspace,channel,message_ts),
            FOREIGN KEY(workspace,channel,message_ts)
                REFERENCES slack_captures(workspace,channel,message_ts));
        CREATE INDEX IF NOT EXISTS slack_capture_due ON slack_capture_mutations(next_attempt,accepted_at);
        CREATE INDEX IF NOT EXISTS slack_capture_record ON slack_captures(record_id);
        CREATE INDEX IF NOT EXISTS slack_capture_repo ON slack_captures(repo);
    ''')


def pending_sql(alias='s'):
    """A committed newer callback makes the old snapshot uncertain immediately.

    The importer removes this transient gate atomically with its observation.
    Do not rewrite immutable evidence, signatures or grants while waiting.
    """
    return ("EXISTS (SELECT 1 FROM slack_captures sc JOIN slack_capture_mutations sm "
            "ON sm.workspace=sc.workspace AND sm.channel=sc.channel AND sm.message_ts=sc.message_ts "
            f"WHERE sc.record_id={alias}.id AND sm.clock>sc.latest_clock)")


def clock(value):
    """Slack timestamps are decimal strings, never IEEE-754 floats.

    Require the full microsecond field and a signed BIGINT-safe result. Reject
    malformed, missing and imprecise clocks rather than borrowing receipt time.
    """
    if not isinstance(value, str) or not re.fullmatch(r'(?:0|[1-9][0-9]{0,12})\.[0-9]{6}', value):
        return None
    seconds, fraction = value.split('.')
    result = int(seconds) * 1000000 + int(fraction)
    return result if 0 < result <= 9223372036854775807 else None


def identity(workspace, channel, ts):
    if (not isinstance(workspace, str) or not re.fullmatch(r'[A-Z0-9]{1,100}', workspace)
            or not isinstance(channel, str) or not re.fullmatch(r'[A-Z0-9]{1,100}', channel)
            or clock(ts) is None):
        return None
    return workspace, channel, ts


def original(event):
    from .delivery import _CAPTURE_RE
    inner = event.get('event') or {}
    text = inner.get('text')
    if (inner.get('type') not in ('message', 'app_mention') or inner.get('subtype')
            or inner.get('bot_id') or inner.get('thread_ts') or not isinstance(text, str)
            or len(text) > MAX_TEXT or not _CAPTURE_RE.match(text)):
        return None
    return identity(event.get('team_id'), inner.get('channel'), inner.get('ts'))


def enroll(graph, key):
    """Caller holds the transaction accepting the original, not a disposable job.

    Pending mutations belong to this exact identity even when multiple accepted
    original deliveries race or one exhausts its retry budget.
    """
    graph.db.execute('''INSERT INTO slack_captures(workspace,channel,message_ts,repo,accepted_at)
        VALUES(?,?,?,?,?) ON CONFLICT(workspace,channel,message_ts) DO NOTHING''',
        (*key, graph.get_setting('slack_capture_repo') or '', now_iso()))


def mutation(event):
    from .delivery import _CAPTURE_RE
    inner = event.get('event') or {}
    subtype = inner.get('subtype')
    if inner.get('type') != 'message' or subtype not in ('message_changed', 'message_deleted'):
        return None
    message = inner.get('message') or {}
    if not isinstance(message, dict):
        return None
    ts = message.get('ts') if subtype == 'message_changed' else inner.get('deleted_ts')
    key = identity(event.get('team_id'), inner.get('channel'), ts)
    # event_ts, when supplied, is Slack's event occurrence; ts is the documented
    # mutation envelope clock. Neither is the original message's identity.
    occurrence = inner.get('event_ts', inner.get('ts'))
    order = clock(occurrence)
    if key is None or order is None or order <= clock(ts):
        return None
    if 'ts' in inner and clock(inner['ts']) is None:
        return None
    deleted = subtype == 'message_deleted'
    text = '' if deleted else message.get('text')
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        return None
    match = _CAPTURE_RE.match(text)
    # Removing the explicit record marker withdraws the captured evidence.
    # Keep no unrelated new text, previous_message, blocks or action tokens.
    body = match.group(1).strip() if match and not deleted else ''
    payload = {'text': body, 'deleted': deleted, 'withdrawn': not bool(body), 'timestamp': occurrence}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    eid = event.get('event_id')
    if not isinstance(eid, str) or not eid or len(eid) > 200:
        eid = 'capture:' + hashlib.sha256(json.dumps([key, payload], sort_keys=True).encode()).hexdigest()
    return key, order, digest, payload, eid


def queue_mutation(graph, event):
    """Keep one newest bounded snapshot per enrolled identity, transactionally.

    Unknown identities are dropped before any content is stored. Queue pressure
    returns a retryable HTTP error; an accepted mutation is never evicted.
    """
    parsed = mutation(event)
    if parsed is None:
        return {'ok': True, 'ignored': 'invalid_capture_mutation'}
    key, order, digest, payload, eid = parsed
    with graph.transaction():
        capture = graph.db.execute('SELECT * FROM slack_captures WHERE ' + KEY_SQL, key).fetchone()
        if capture is None:
            return {'ok': True, 'ignored': 'uncaptured_message'}
        pending = graph.db.execute('SELECT * FROM slack_capture_mutations WHERE ' + KEY_SQL, key).fetchone()
        newest = pending['clock'] if pending else capture['latest_clock']
        if order <= newest or capture['deleted']:
            return {'ok': True, 'ignored': 'duplicate_or_stale_capture_mutation'}
        # Slack cannot edit a deleted message; retain a queued deletion even if
        # a later malformed delivery claims to edit it before capture commits.
        if pending and json.loads(pending['payload'])['deleted']:
            return {'ok': True, 'ignored': 'deleted_capture'}
        if pending is None and graph.db.execute('SELECT count(*) FROM slack_capture_mutations').fetchone()[0] >= MAX_PENDING:
            raise CaptureQueueFull('Captured Slack update queue is full; retry this callback later')
        graph.db.execute('''INSERT INTO slack_capture_mutations
            (workspace,channel,message_ts,event_id,clock,digest,payload,accepted_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(workspace,channel,message_ts) DO UPDATE SET
            event_id=excluded.event_id,clock=excluded.clock,digest=excluded.digest,payload=excluded.payload,
            attempts=0,next_attempt=0,error='',accepted_at=excluded.accepted_at''',
            (*key, eid, order, digest, json.dumps(payload), now_iso()))
    return {'ok': True, 'queued': True}


def _receipt(graph, eid, reply=''):
    if eid:
        graph.db.execute('''INSERT INTO webhook_receipts(id,channel,received_at,state,updated_at,reply)
            VALUES(?,'slack',?,'applied',?,?) ON CONFLICT(id) DO UPDATE SET
            state='applied',updated_at=excluded.updated_at,reply=excluded.reply,error='' ''',
            (eid, now_iso(), now_iso(), reply))


def capture(delivery, key, user_id, text, event_id=''):
    from .delivery import _CAPTURE_RE, _named_mentions
    graph = delivery.store.graph
    match = _CAPTURE_RE.match(text)
    with graph.transaction():
        if event_id and graph.db.execute("SELECT 1 FROM webhook_receipts WHERE id=? AND state='applied'", (event_id,)).fetchone():
            return ''
        enroll(graph, key)
        row = graph.db.execute('SELECT * FROM slack_captures WHERE ' + KEY_SQL, key).fetchone()
        if row['record_id']:
            _receipt(graph, event_id)
            return ''
        person = graph.find_person(user_id)
        body = _named_mentions(graph, match.group(1).strip())
        ref = key[1] + ':' + key[2]
        record = delivery.store.add_record({'repo': row['repo'], 'kind': 'slack', 'ref': ref,
            'provider': 'slack', 'namespace': key[0], 'external_id': ref,
            'title': body[:120], 'body': body, 'author': person['name'] if person else user_id,
            'url': f"https://slack.com/archives/{key[1]}/p{key[2].replace('.', '')}",
            'source_sequence': clock(key[2]), 'source_version': key[2], 'created_at': now_iso()})
        graph.db.execute('UPDATE slack_captures SET record_id=?,latest_clock=? WHERE ' + KEY_SQL,
                         (record['source']['record_id'], clock(key[2]), *key))
        reply = (f"Recorded as decision record slack {record['ref']}"
                 + (f" by {person['name']}" if person else '') + '; Raven cites it as evidence, not as sign-off.')
        _receipt(graph, event_id, reply)
    return reply


def process(delivery, limit=10):
    """Retry independently of original ingress jobs; each import/receipt is atomic."""
    from .delivery import _named_mentions, retry_delay
    graph = delivery.store.graph
    candidates = graph.db.execute('''SELECT m.* FROM slack_capture_mutations m JOIN slack_captures c
        ON c.workspace=m.workspace AND c.channel=m.channel AND c.message_ts=m.message_ts
        WHERE m.workspace=? AND c.record_id<>'' AND m.next_attempt<=?
        ORDER BY m.accepted_at,m.channel,m.message_ts LIMIT ?''',
        (graph.get_setting('slack_team_id'), time.time(), max(0, min(limit, 100)))).fetchall()
    applied = 0
    for candidate in candidates:
        key = tuple(candidate[k] for k in ('workspace', 'channel', 'message_ts'))
        attempted = candidate
        try:
            with graph.transaction():
                row = graph.db.execute('SELECT * FROM slack_capture_mutations WHERE ' + KEY_SQL, key).fetchone()
                capture = graph.db.execute('SELECT * FROM slack_captures WHERE ' + KEY_SQL, key).fetchone()
                if row is None or row['next_attempt'] > time.time():
                    continue
                # Recheck the installation during replay; do not reassign identity
                # when settings change while a callback is being accepted.
                if graph.get_setting('slack_team_id') != key[0]:
                    continue
                attempted = row
                value = json.loads(row['payload'])
                if row['clock'] > capture['latest_clock'] and not capture['deleted']:
                    ref = key[1] + ':' + key[2]
                    body = _named_mentions(graph, value['text'])
                    delivery.store.add_record({'repo': capture['repo'], 'kind': 'slack', 'ref': ref,
                        'provider': 'slack', 'namespace': key[0], 'external_id': ref,
                        'title': body[:120] if body else 'Captured Slack message removed', 'body': body,
                        'status': 'deleted' if value['deleted'] else 'capture_removed' if value['withdrawn'] else '',
                        'availability': 'deleted' if value['withdrawn'] else 'available',
                        'source_sequence': row['clock'], 'source_version': value['timestamp']})
                    graph.db.execute('UPDATE slack_captures SET latest_clock=?,latest_digest=?,deleted=? WHERE ' + KEY_SQL,
                        (row['clock'], row['digest'], int(value['deleted']), *key))
                _receipt(graph, row['event_id'])
                graph.db.execute('DELETE FROM slack_capture_mutations WHERE ' + KEY_SQL, key)
            applied += 1
        except Exception as error:
            with graph.transaction():
                # A newer accepted mutation must not inherit an older retry delay.
                graph.db.execute('''UPDATE slack_capture_mutations SET attempts=attempts+1,next_attempt=?,error=?
                    WHERE ''' + KEY_SQL + ' AND clock=?',
                    (time.time() + retry_delay(error, min(300, 2 ** min(attempted['attempts'] + 1, 8))),
                     type(error).__name__, *key, attempted['clock']))
    return applied
