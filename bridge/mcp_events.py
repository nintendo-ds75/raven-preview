"""MCP Events webhook subscriptions for Work cloud chats and dots.

Local harnesses keep using the existing leased resume mailbox. Both consume
the same committed event ledger. Notifications contain IDs and event kinds;
the authenticated host reads current evidence through Raven's normal tools.
"""
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone

from .context_memory import digest
from .host_sessions import WAKE_EVENTS
from .store import Invalid

VERSION = '2026-07-28'
NAME = 'raven.task.changed'
DEFINITION = {'name': NAME,
    'description': 'A human answer, correction, referral, source change, discovery review or completed diff reading on one Raven task. Read the current task through bridge_get_tree.',
    'delivery': ['webhook'],
    'inputSchema': {'type': 'object', 'properties': {'task_id': {'type': 'string'}},
                    'required': ['task_id'], 'additionalProperties': False},
    'payloadSchema': {'type': 'object', 'properties': {'task_id': {'type': 'string'},
        'event_kind': {'type': 'string'}, 'decision_id': {'type': 'string'}},
        'required': ['task_id', 'event_kind', 'decision_id'], 'additionalProperties': False}}


class CallbackError(Invalid):
    def __init__(self, reason):
        self.reason = reason
        super().__init__('Callback verification failed: ' + reason)


def migrate(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS mcp_event_subscriptions (
      id TEXT PRIMARY KEY, principal TEXT NOT NULL, credential_id TEXT NOT NULL,
      task_id TEXT NOT NULL, url TEXT NOT NULL, secret TEXT NOT NULL,
      previous_secret TEXT NOT NULL DEFAULT '', rotate_until REAL NOT NULL DEFAULT 0,
      expires REAL NOT NULL, cursor BIGINT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
      verified_at REAL NOT NULL, generation INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS mcp_event_deliveries (
      id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL, event_id BIGINT NOT NULL,
      payload TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
      next_attempt REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
      last_error TEXT NOT NULL DEFAULT '', UNIQUE(subscription_id,event_id));''')


def _secret(value):
    try:
        if not isinstance(value, str) or not value.startswith('whsec_'):
            raise ValueError()
        key = base64.b64decode(value[6:], validate=True)
        if not 24 <= len(key) <= 64:
            raise ValueError()
        return key
    except (ValueError, TypeError) as error:
        raise Invalid('A whsec_ signing secret encoding 24 to 64 bytes is required') from error


def destination(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or parsed.port not in (None, 443)):
            raise ValueError()
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        ips = [a[4][0] for a in addresses]
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise ValueError()
        return parsed, ips[0]
    except (ValueError, OSError) as error:
        raise CallbackError('invalid_destination') from error


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=10)
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def post(url, payload, headers):
    # Re-resolve, validate and pin each connection, including verification.
    parsed, address = destination(url)
    conn = _PinnedHTTPS(parsed.hostname, address)
    try:
        conn.request('POST', urllib.parse.urlunsplit(('', '', parsed.path or '/', parsed.query, '')),
                     body=payload, headers=headers)
        response = conn.getresponse()
        body = response.read(16385)
        if len(body) > 16384:
            raise CallbackError('response_too_large')
        return response.status, body
    finally:
        conn.close()


def signed_headers(sub, event_id, body):
    stamp = str(int(time.time()))
    signed = event_id.encode() + b'.' + stamp.encode() + b'.' + body
    keys = [sub['secret']]
    if sub.get('previous_secret') and sub.get('rotate_until', 0) > time.time():
        keys.append(sub['previous_secret'])
    signatures = ['v1,' + base64.b64encode(hmac.new(_secret(key), signed, hashlib.sha256).digest()).decode() for key in keys]
    return {'Content-Type': 'application/json', 'webhook-id': event_id, 'webhook-timestamp': stamp,
            'webhook-signature': ' '.join(signatures), 'X-MCP-Subscription-Id': sub['id']}


def _authorized(graph, principal, credential_id):
    row = graph.db.execute('SELECT t.person_id,p.active FROM api_tokens t JOIN people p ON p.id=t.person_id '
                           "WHERE t.id=? AND t.revoked_at=''", (credential_id,)).fetchone()
    return bool(row and row['person_id'] == principal and row['active'])


def subscription(store, params, principal, credential_id, *, remove=False, sender=None):
    g = store.graph
    if not principal or not credential_id or not _authorized(g, principal, credential_id):
        raise Invalid('MCP Events requires an active personal API credential')
    args, delivery = params.get('arguments'), params.get('delivery')
    if (params.get('name') != NAME or not isinstance(args, dict) or set(args) != {'task_id'}
            or not isinstance(args['task_id'], str) or not args['task_id']
            or not isinstance(delivery, dict) or delivery.get('mode') != 'webhook'
            or not isinstance(delivery.get('url'), str) or len(delivery['url']) > 4096):
        raise Invalid('Subscribe to raven.task.changed with one task_id and a webhook delivery')
    if not g.db.execute('SELECT 1 FROM runs WHERE id=?', (args['task_id'],)).fetchone():
        raise Invalid('Unknown task')
    sid = digest([principal, delivery['url'], NAME, args])
    if remove:
        with g.transaction():
            g.db.execute('UPDATE mcp_event_subscriptions SET active=0,generation=generation+1 WHERE id=? AND principal=?', (sid, principal))
        return {}
    if params.get('cursor') is not None:
        raise Invalid('This event has no protocol replay cursor; read the current tree after reconnecting')
    _secret(delivery.get('secret'))
    ttl = params.get('ttlMs', 86400000)
    if ttl is None:
        ttl = 86400000
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 0 < ttl <= 604800000:
        raise Invalid('ttlMs must be positive and at most seven days; null receives a one-day lease')
    ttl = max(60000, ttl)
    at = time.time()
    old = g.db.execute('SELECT * FROM mcp_event_subscriptions WHERE id=?', (sid,)).fetchone()
    head = g.db.execute('SELECT coalesce(max(id),0) AS n FROM events').fetchone()['n']
    sub = {'id': sid, 'secret': delivery['secret']}
    if not (old and old['active'] and old['expires'] > at and old['verified_at'] > at - 300
            and old['secret'] == sub['secret'] and old['credential_id'] == credential_id):
        challenge = secrets.token_urlsafe(32)
        body = json.dumps({'type': 'verification', 'challenge': challenge}, separators=(',', ':')).encode()
        event_id = 'verification_' + secrets.token_hex(16)
        try:
            status, response = (sender or post)(delivery['url'], body, signed_headers(sub, event_id, body))
            echoed = json.loads(response).get('challenge', '')
            if not 200 <= status < 300 or not isinstance(echoed, str) or not hmac.compare_digest(challenge, echoed):
                raise CallbackError('challenge_failed')
        except CallbackError:
            raise
        except (TimeoutError, OSError) as error:
            raise CallbackError('timeout') from error
        except (ValueError, TypeError, AttributeError) as error:
            raise CallbackError('challenge_failed') from error
    expires = at + ttl / 1000
    with g.transaction():
        if not _authorized(g, principal, credential_id):
            raise Invalid('Credential was revoked while verifying the callback')
        current = g.db.execute('SELECT * FROM mcp_event_subscriptions WHERE id=?', (sid,)).fetchone()
        if current:
            previous = current['secret'] if current['secret'] != sub['secret'] else current['previous_secret']
            until = at + 300 if current['secret'] != sub['secret'] else current['rotate_until']
            cursor = current['cursor'] if current['active'] and current['expires'] > at else head
            g.db.execute('UPDATE mcp_event_subscriptions SET secret=?,previous_secret=?,rotate_until=?,expires=?, '
                         'active=1,verified_at=?,credential_id=?,cursor=?,generation=generation+1 WHERE id=?',
                         (sub['secret'], previous, until, expires, at, credential_id, cursor, sid))
        else:
            g.db.execute('INSERT INTO mcp_event_subscriptions(id,principal,credential_id,task_id,url,secret,expires,cursor,verified_at) '
                         'VALUES(?,?,?,?,?,?,?,?,?)', (sid, principal, credential_id, args['task_id'], delivery['url'], sub['secret'], expires, head, at))
        if not current or not current['active'] or current['expires'] <= at:
            # Remember the actual visible baseline, not a sequence watermark:
            # on PostgreSQL a lower event ID may commit after a higher one.
            marks = ','.join('?' for _ in WAKE_EVENTS)
            for event in g.db.execute('SELECT id FROM events WHERE run_id=? AND kind IN (' + marks + ')',
                                      (args['task_id'], *WAKE_EVENTS)).fetchall():
                eid = digest([sid, event['id']])
                g.db.execute("INSERT OR IGNORE INTO mcp_event_deliveries(id,subscription_id,event_id,payload,state) VALUES(?,?,?,'','baseline')",
                             (eid, sid, event['id']))
            g.db.execute("UPDATE mcp_event_deliveries SET state='baseline',lease_until=0 WHERE subscription_id=? AND state IN ('pending','leased')", (sid,))
        g.append_event('event_subscription_created', {'task_id': args['task_id'], 'subscription_id': sid, 'principal': principal})
    return {'id': sid, 'refreshBefore': datetime.fromtimestamp(expires, timezone.utc).isoformat(), 'cursor': None, 'truncated': False}


def deliver(store, sender=None, stopped=None):
    from .investigation import expire_pending
    expire_pending(store)
    g = store.graph
    sent = 0
    kinds = WAKE_EVENTS
    for raw in g.db.execute('SELECT * FROM mcp_event_subscriptions WHERE active=1').fetchall():
        if stopped and stopped.is_set():
            break
        sub = dict(raw)
        if sub['expires'] <= time.time() or not _authorized(g, sub['principal'], sub['credential_id']):
            with g.transaction():
                g.db.execute('UPDATE mcp_event_subscriptions SET active=0 WHERE id=?', (sub['id'],))
            continue
        with g.transaction():
            fresh = g.db.execute('SELECT * FROM mcp_event_subscriptions WHERE id=?', (sub['id'],)).fetchone()
            if not fresh['active']:
                continue
            marks = ','.join('?' for _ in kinds)
            events = g.db.execute('SELECT e.id,e.kind,e.decision_id,e.created_at FROM events e '
                                  'LEFT JOIN mcp_event_deliveries d ON d.subscription_id=? AND d.event_id=e.id '
                                  'WHERE e.run_id=? AND d.event_id IS NULL AND e.kind IN (' + marks + ') '
                                  'ORDER BY e.id LIMIT 100', (sub['id'], sub['task_id'], *kinds)).fetchall()
            for event in events:
                if event['kind'] not in kinds:
                    continue
                eid = digest([sub['id'], event['id']])
                payload = {'eventId': eid, 'name': NAME, 'timestamp': event['created_at'], 'cursor': None,
                           'data': {'task_id': sub['task_id'], 'event_kind': event['kind'], 'decision_id': event['decision_id'] or ''}}
                g.db.execute("INSERT OR IGNORE INTO mcp_event_deliveries(id,subscription_id,event_id,payload,state) VALUES(?,?,?,?,'pending')",
                             (eid, sub['id'], event['id'], json.dumps(payload, separators=(',', ':'))))
        queue = g.db.execute("SELECT * FROM mcp_event_deliveries WHERE subscription_id=? AND state IN ('pending','leased') "
                             'AND next_attempt<=? AND lease_until<=? ORDER BY event_id LIMIT 20',
                             (sub['id'], time.time(), time.time())).fetchall()
        for item in queue:
            if stopped and stopped.is_set():
                break
            with g.transaction():
                claimed = g.db.execute("UPDATE mcp_event_deliveries SET state='leased',lease_until=? WHERE id=? "
                                       "AND state IN ('pending','leased') AND lease_until<=?", (time.time() + 60, item['id'], time.time()))
                if claimed.rowcount != 1:
                    continue
            # Recheck immediately before every attempt, including retries.
            sub = dict(g.db.execute('SELECT * FROM mcp_event_subscriptions WHERE id=?', (sub['id'],)).fetchone())
            if not sub['active'] or sub['expires'] <= time.time() or not _authorized(g, sub['principal'], sub['credential_id']):
                break
            body = item['payload'].encode()
            status, error = 0, ''
            try:
                status, _ = (sender or post)(sub['url'], body, signed_headers(sub, item['id'], body))
            except Exception as failure:
                error = type(failure).__name__
            attempts = item['attempts'] + 1
            accepted = 200 <= status < 300
            terminal = status in (410, 413) or (400 <= status < 500 and status not in (408, 429)) or attempts >= 8
            with g.transaction():
                g.db.execute('UPDATE mcp_event_deliveries SET state=?,attempts=?,next_attempt=?,lease_until=0,last_error=? WHERE id=?',
                    ('sent' if accepted else 'failed' if terminal else 'pending', attempts,
                     time.time() + min(3600, 2 ** attempts * 5), '' if accepted else error or f'HTTP {status}', item['id']))
                if status == 410:
                    g.db.execute('UPDATE mcp_event_subscriptions SET active=0 WHERE id=?', (sub['id'],))
                g.append_event('event_webhook_delivered' if accepted else 'event_webhook_failed', {
                    'task_id': sub['task_id'], 'subscription_id': sub['id'], 'event_id': item['id'],
                    'attempt': attempts, 'status': status, 'terminal': terminal})
            sent += int(accepted)
    return sent


class Worker:
    def __init__(self, store):
        self.store = store
        self.stop = threading.Event()
        self.thread = None

    def start(self):
        def loop():
            try:
                while not self.stop.is_set():
                    try:
                        deliver(self.store, stopped=self.stop)
                    except Exception as error:
                        print(f'Raven event worker: {type(error).__name__}; queued deliveries retained', file=sys.stderr)
                    self.stop.wait(2)
            finally:
                self.store.graph.close_thread()
        self.thread = threading.Thread(target=loop, name='raven-mcp-events', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=12)
