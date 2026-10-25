"""Consented read-only Vexa observation; never dispatches a participant.

Each network result is fenced by lease, consent, exact occurrence and current
administrator identity. Credentials are exclusively the server's Vexa config.
"""
import logging
import threading
import time
import uuid

from . import context_memory as cm, meeting_records as mr
from .meeting_providers import Vexa
from .store import Invalid, now


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS meeting_observers (
        meeting_id TEXT PRIMARY KEY REFERENCES meeting_occurrences(id),
        enabled INTEGER NOT NULL DEFAULT 0, workspace_shared INTEGER NOT NULL DEFAULT 0,
        actor_id TEXT NOT NULL, actor_name TEXT NOT NULL, actor_kind TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1, next_poll REAL NOT NULL DEFAULT 0,
        lease_owner TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'disabled', updated_at TEXT NOT NULL)""")


def _authority(store, actor_id, kind, *, allow_operator=True):
    if not actor_id and kind == 'operator' and allow_operator:
        return 'Local operator'
    if kind not in ('session', 'token', 'bootstrap'):
        raise Invalid('Background disclosure requires a human administrator')
    row = store.graph.db.execute('SELECT name,active,role FROM people WHERE id=?', (actor_id,)).fetchone()
    if not row or not row['active'] or row['role'] != 'admin':
        raise Invalid('Background disclosure administrator is no longer active')
    return row['name']


def _eligible(meeting):
    consent = meeting['consent']
    return (meeting['provider'] == 'vexa' and not meeting['revoked'] and
            all(consent.get(k) is True for k in ('join', 'transcribe', 'notice_confirmed')))


def status(store, meeting_id):
    row = store.graph.db.execute('SELECT enabled,workspace_shared,revision,status,next_poll FROM meeting_observers WHERE meeting_id=?', (meeting_id,)).fetchone()
    return dict(row) if row else {'enabled': False, 'workspace_shared': False, 'revision': 0, 'status': 'disabled'}


def configure_sync(store, meeting_id, repo, data, actor):
    if type(data.get('enabled')) is not bool:
        raise Invalid('Background sync enabled must be an explicit boolean')
    with store.graph.transaction():
        name = _authority(store, actor.id, actor.kind)
        meeting = mr.get(store, meeting_id, repo)
        enabled = data['enabled']
        if enabled and (data.get('workspace_shared') is not True or not _eligible(meeting)):
            raise Invalid('Background sync requires explicit workspace disclosure and participant/transcription/notice consent')
        store.graph.db.execute("""INSERT INTO meeting_observers
            (meeting_id,enabled,workspace_shared,actor_id,actor_name,actor_kind,updated_at,status)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(meeting_id) DO UPDATE SET
            enabled=excluded.enabled,workspace_shared=excluded.workspace_shared,
            actor_id=excluded.actor_id,actor_name=excluded.actor_name,actor_kind=excluded.actor_kind,
            revision=meeting_observers.revision+1,next_poll=0,lease_owner='',lease_until=0,
            updated_at=excluded.updated_at,status=excluded.status""",
            (meeting_id, int(enabled), int(enabled), actor.id, name, actor.kind, now(), 'waiting_for_join' if enabled else 'disabled'))
        store.graph.append_event('meeting_background_sync', {'meeting_id': meeting_id, 'repo': repo,
            'enabled': enabled, 'workspace_shared': enabled, 'by': name})
    return status(store, meeting_id)


class Worker:
    def __init__(self, store, *, client_factory=Vexa, clock=time.time, allow_operator=False):
        self.store, self.client_factory, self.clock = store, client_factory, clock
        self.allow_operator = allow_operator
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if not self.thread or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, name='meeting-observer', daemon=True)
            self.thread.start()
        return self

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=35)

    def _run(self):
        try:
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception:
                    # A transient database/configuration failure must not kill recovery.
                    logging.getLogger(__name__).error('Meeting observer tick failed; retrying without provider mutations')
                self.stop_event.wait(15)
        finally:
            self.store.graph.close_thread()

    def tick(self, limit=4):
        db = self.store.graph.db
        rows = db.execute('SELECT meeting_id FROM meeting_observers WHERE enabled=1 AND next_poll<=? AND lease_until<=? ORDER BY next_poll LIMIT ?',
                          (self.clock(), self.clock(), min(max(limit, 1), 16))).fetchall()
        for row in rows:
            self._observe(row['meeting_id'])
        return len(rows)

    def _observe(self, mid):
        store, db = self.store, self.store.graph.db
        token = str(uuid.uuid4())
        with store.graph.transaction():
            row = db.execute('SELECT * FROM meeting_observers WHERE meeting_id=?', (mid,)).fetchone()
            if not row or not row['enabled'] or row['lease_until'] > self.clock() or row['next_poll'] > self.clock():
                return
            row = dict(row)
            raw = db.execute('SELECT repo FROM meeting_occurrences WHERE id=?', (mid,)).fetchone()
            meeting = mr.get(store, mid, raw['repo'])
            try:
                _authority(store, row['actor_id'], row['actor_kind'], allow_operator=self.allow_operator)
                if not _eligible(meeting):
                    raise Invalid('Consent withdrawn')
            except Invalid:
                db.execute("UPDATE meeting_observers SET enabled=0,status='authorization_required',lease_owner='',lease_until=0 WHERE meeting_id=?", (mid,))
                return
            if not meeting['remote_id'] or meeting['state'] in ('planned', 'rejected', 'join_pending', 'stop_pending', 'delete_remote_pending', 'remote_deleted'):
                db.execute("UPDATE meeting_observers SET status='waiting_for_occurrence',next_poll=? WHERE meeting_id=?", (self.clock() + 60, mid))
                return
            db.execute("UPDATE meeting_observers SET lease_owner=?,lease_until=?,status='observing' WHERE meeting_id=?", (token, self.clock() + 90, mid))
        # Reads only. No provider mutation is retried or initiated here.
        remote, payload, error, empty_complete = '', None, False, False
        try:
            client = self.client_factory()
            detail = client.status(meeting).get('meeting')
            if not isinstance(detail, dict) or str(detail.get('id')) != meeting['remote_id']:
                raise Invalid('Status occurrence mismatch')
            remote = str(detail.get('status', 'unknown'))
            with store.graph.transaction():
                gate = db.execute('SELECT * FROM meeting_observers WHERE meeting_id=?', (mid,)).fetchone()
                live = mr.get(store, mid, meeting['repo'])
                if (not gate or not gate['enabled'] or not gate['workspace_shared'] or
                    gate['lease_owner'] != token or gate['revision'] != row['revision'] or
                    gate['lease_until'] <= self.clock() or not _eligible(live) or
                    live['updated_at'] != meeting['updated_at'] or live['remote_id'] != meeting['remote_id']):
                    raise Invalid('Observation authorization changed')
                _authority(store, gate['actor_id'], gate['actor_kind'], allow_operator=self.allow_operator)
            response = client.transcript(meeting)
            if str(response.get('id')) != meeting['remote_id']:
                raise Invalid('Transcript occurrence mismatch')
            segments = response.get('segments')
            empty_complete = segments == [] and remote == 'completed' and response.get('status') == 'completed'
            if segments:
                payload = {'segments': segments, 'source_version': 'vexa:' + cm.digest(segments),
                    'complete': remote == 'completed' and response.get('status') == 'completed',
                    'workspace_shared': True, 'by': row['actor_name']}
                mr._transcript_parts(payload)  # Validate before the publication transaction.
        except Exception:
            error = True  # Never persist provider messages/credentials/transcript excerpts.
        with store.graph.transaction():
            current = db.execute('SELECT * FROM meeting_observers WHERE meeting_id=?', (mid,)).fetchone()
            if not current or current['lease_owner'] != token or current['revision'] != row['revision'] or current['lease_until'] <= self.clock():
                return
            live = mr.get(store, mid, meeting['repo'])
            try:
                _authority(store, current['actor_id'], current['actor_kind'], allow_operator=self.allow_operator)
                if not current['enabled'] or not current['workspace_shared'] or not _eligible(live):
                    raise Invalid('Consent withdrawn')
            except Invalid:
                db.execute("UPDATE meeting_observers SET enabled=0,status='authorization_required',lease_owner='',lease_until=0 WHERE meeting_id=?", (mid,))
                return
            if live['updated_at'] != meeting['updated_at'] or live['remote_id'] != meeting['remote_id'] or live['state'] != meeting['state']:
                db.execute("UPDATE meeting_observers SET status='changed_retry',lease_owner='',lease_until=0,next_poll=? WHERE meeting_id=?", (self.clock() + 60, mid))
                return
            if not error and payload:
                mr.import_transcript(store, mid, meeting['repo'], payload)
            terminal = remote == 'failed' or (not error and (empty_complete or (payload and payload['complete'])))
            label = 'complete_empty' if empty_complete and not error else 'incomplete' if remote == 'failed' else 'transcript_retry_pending' if error and remote else 'retry_pending' if error else 'complete' if payload and payload['complete'] else 'waiting_for_transcript'
            if empty_complete and not error:
                # No canonical source fabricated for an explicitly empty final transcript.
                # Existing nonempty evidence is not silently replaced by an empty response.
                if not live['record_id']:
                    db.execute('UPDATE meeting_occurrences SET transcript_complete=1,transcript_chars=0,transcript_version=? WHERE id=?', ('vexa:' + cm.digest([]), mid))
                else:
                    label = 'empty_conflicts_with_evidence'
            if remote:
                db.execute('UPDATE meeting_occurrences SET remote_state=?,state=?,updated_at=? WHERE id=?',
                    (remote, remote if remote in ('completed', 'failed') else live['state'], now(), mid))
            db.execute("UPDATE meeting_observers SET status=?,enabled=?,lease_owner='',lease_until=0,next_poll=?,updated_at=? WHERE meeting_id=?",
                       (label, 0 if terminal else 1, self.clock() + (300 if error else 60), now(), mid))
