"""Durable Slack ingress and replies; acknowledge webhooks before inference."""
import copy
import hashlib
import json
import threading
import time
from contextlib import contextmanager

from .graph import now_iso

LEASE_SECONDS = 120


def accepts_workspace(delivery, event):
    """A signed callback belongs to this instance's connected workspace.

    Slack signs all installations of an app with the same app secret.
    The signature proves the sender, not the workspace. Never learn the
    workspace from the incoming payload: pin it to auth.test instead.
    Check again during replay, before any capture, lookup, or reply.
    """
    graph = delivery.store.graph
    team = graph.get_setting("slack_team_id")
    if not team:
        if not hasattr(delivery.transport, "workspace_id"):
            return False
        from .slack_directory import workspace
        # A transient auth.test failure must remain retryable rather than
        # acknowledging and dropping the callback as an unknown workspace.
        team = workspace(graph, delivery.transport)
        with graph.transaction():
            pinned = graph.get_setting("slack_team_id")
            if pinned and pinned != team:
                return False
            graph.set_setting("slack_team_id", team)
    return event.get("team_id") == team


def migrate(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS slack_ingress (
        id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
        lease_until REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS slack_replies (
        id TEXT PRIMARY KEY, channel TEXT NOT NULL, thread_ts TEXT NOT NULL, text TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');''')
    if 'conversation_key' not in {r['name'] for r in db.execute('PRAGMA table_info(slack_ingress)')}:
        db.execute("ALTER TABLE slack_ingress ADD COLUMN conversation_key TEXT NOT NULL DEFAULT ''")
    db.execute('CREATE INDEX IF NOT EXISTS slack_ingress_pending ON slack_ingress(state,next_attempt)')
    db.execute('CREATE INDEX IF NOT EXISTS slack_ingress_thread ON slack_ingress(conversation_key,created_at)')


class Inbox:
    def __init__(self, delivery):
        self.delivery = delivery
        self.graph = delivery.store.graph
        self.thread = None
        self.lock = threading.Lock()
        self.tokens = {}

    def enqueue(self, event):
        if event.get('type') == 'url_verification':
            return {'challenge': event.get('challenge', '')}
        inner = event.get('event') or {}
        if event.get('type') != 'event_callback' or inner.get('bot_id') or inner.get('subtype'):
            return {'ok': True}
        if not accepts_workspace(self.delivery, event):
            return {'ok': True, 'ignored': 'workspace_mismatch'}
        clean = copy.deepcopy(event)
        token = clean.pop('action_token', '') or clean['event'].pop('action_token', '')
        clean['event'].pop('action_token', None)
        event_id = event.get('event_id') or hashlib.sha256(json.dumps(clean, sort_keys=True).encode()).hexdigest()
        clean['event_id'] = event_id
        # A whole DM is serialized, including messages sent outside its thread.
        conversation = inner.get('channel', '')
        with self.graph.transaction():
            self.graph.db.execute('''INSERT INTO slack_ingress(id,payload,created_at,conversation_key)
                VALUES(?,?,?,?) ON CONFLICT(id) DO NOTHING''',
                (event_id, json.dumps(clean), now_iso(), conversation))
        with self.lock:
            self.tokens = {k: v for k, v in self.tokens.items() if v[1] > time.time()}
            if token:
                self.tokens[event_id] = (token, time.time() + 180)
        self.start()
        return {'ok': True, 'queued': True}

    def ack(self, event_id, channel, thread, text):
        if not text:
            return
        from .slack_chat import EphemeralReply
        transient = isinstance(text, EphemeralReply)
        key = event_id or hashlib.sha256((channel + thread + text).encode()).hexdigest()
        kept = ('The live Slack search response was not retained. Please ask again for current sources.'
                if transient else text)
        with self.graph.transaction():
            self.graph.db.execute('''INSERT INTO slack_replies(id,channel,thread_ts,text)
                VALUES(?,?,?,?) ON CONFLICT(id) DO NOTHING''', (key, channel, thread, kept))
        if transient:
            row = self.graph.db.execute('SELECT * FROM slack_replies WHERE id=?', (key,)).fetchone()
            self._send(row, text)
        else:
            self.flush()

    def _send(self, row, transient_text=None):
        if self.delivery.transport is None:
            return
        with self.graph.transaction():
            claim = self.graph.db.execute('''UPDATE slack_replies SET state='sending', next_attempt=?
                WHERE id=? AND next_attempt<=? AND state IN ('queued','sending')''',
                (time.time() + LEASE_SECONDS, row['id'], time.time()))
        if claim.rowcount != 1:
            return
        try:
            from .delivery import _sections
            text = str(transient_text) if transient_text is not None else row['text']
            blocks = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': t}} for t in _sections(text, 2900)]
            if hasattr(self.delivery.transport, 'post_reply'):
                self.delivery.transport.post_reply(row['channel'], text, blocks, row['thread_ts'], row['id'])
            else:
                self.delivery.transport.post_message(row['channel'], text, blocks, thread_ts=row['thread_ts'])
            with self.graph.transaction():
                self.graph.db.execute("UPDATE slack_replies SET state='sent',error='' WHERE id=?", (row['id'],))
        except Exception as error:
            with self.graph.transaction():
                self.graph.db.execute('''UPDATE slack_replies SET state='queued',attempts=attempts+1,
                    next_attempt=?,error=? WHERE id=?''',
                    (time.time() + min(300, 2 ** min(row['attempts'] + 1, 8)), type(error).__name__, row['id']))

    def flush(self):
        rows = self.graph.db.execute('''SELECT * FROM slack_replies
            WHERE state IN ('queued','sending') AND next_attempt<=? ORDER BY id LIMIT 20''', (time.time(),)).fetchall()
        for row in rows:
            self._send(row)

    @contextmanager
    def _keep_lease(self, event_id):
        finished = threading.Event()

        def heartbeat():
            try:
                while not finished.wait(LEASE_SECONDS / 3):
                    with self.graph.transaction():
                        self.graph.db.execute("UPDATE slack_ingress SET lease_until=? WHERE id=? AND state='processing'",
                                              (time.time() + LEASE_SECONDS, event_id))
                        self.graph.db.execute("UPDATE webhook_receipts SET updated_at=? WHERE id=? AND state='received'",
                                              (now_iso(), event_id))
            finally:
                self.graph.close_thread()

        thread = threading.Thread(target=heartbeat, name='raven-slack-lease', daemon=True)
        thread.start()
        try:
            yield
        finally:
            finished.set()
            thread.join(timeout=1)

    def process(self, limit=10):
        from .delivery import handle_slack_event
        done = 0
        rows = self.graph.db.execute('''SELECT * FROM slack_ingress
            WHERE (state='queued' OR (state='processing' AND lease_until<?)) AND next_attempt<=?
            ORDER BY created_at,id LIMIT ?''', (time.time(), time.time(), limit)).fetchall()
        for row in rows:
            with self.graph.transaction():
                earlier = self.graph.db.execute('''SELECT 1 FROM slack_ingress WHERE conversation_key=?
                    AND state IN ('queued','processing') AND (created_at<? OR (created_at=? AND id<?)) LIMIT 1''',
                    (row['conversation_key'], row['created_at'], row['created_at'], row['id'])).fetchone()
                if earlier:
                    continue
                claimed = self.graph.db.execute('''UPDATE slack_ingress SET state='processing',lease_until=?,attempts=attempts+1
                    WHERE id=? AND (state='queued' OR (state='processing' AND lease_until<?))''',
                    (time.time() + LEASE_SECONDS, row['id'], time.time()))
                if claimed.rowcount != 1:
                    continue
                # This queue owns retries. A previous process may have died
                # between recording the receipt and finishing the action.
                self.graph.db.execute("UPDATE webhook_receipts SET state='failed' WHERE id=? AND state='received'", (row['id'],))
            try:
                event = json.loads(row['payload'])
                with self.lock:
                    token, until = self.tokens.pop(row['id'], ('', 0))
                if token and until > time.time():
                    event['event']['action_token'] = token
                with self._keep_lease(row['id']):
                    handle_slack_event(self.delivery, event)
                with self.graph.transaction():
                    self.graph.db.execute("UPDATE slack_ingress SET state='done',payload='{}',error='' WHERE id=?", (row['id'],))
                done += 1
            except Exception as error:
                with self.graph.transaction():
                    state = 'failed' if row['attempts'] >= 4 else 'queued'
                    self.graph.db.execute('''UPDATE slack_ingress SET state=?,lease_until=0,next_attempt=?,error=? WHERE id=?''',
                        (state, time.time() + min(300, 2 ** (row['attempts'] + 1)), type(error).__name__, row['id']))
        self.flush()
        return done

    def start(self):
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return

            def worker():
                try:
                    while not self.delivery._stop.is_set():
                        try:
                            self.process()
                        except Exception as error:
                            print('Raven Slack inbox:', type(error).__name__)
                        self.delivery._stop.wait(.25)
                finally:
                    self.graph.close_thread()

            self.thread = threading.Thread(target=worker, name='raven-slack-inbox', daemon=True)
            self.thread.start()
