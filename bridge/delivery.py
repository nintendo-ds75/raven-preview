"""Reaching the person: a durable outbox and the channels it speaks.

A decision that waits on someone is a notification row the moment it is
routed, signed-off-wanted, put in doubt or reassigned; a worker sends
it, retries with backoff, and records the message it became so a reply
in that thread comes back to the right decision at the right revision.
Nothing is sent twice for one state of one decision (the dedupe key is
decision, kind and revision), a failed send stays visible with its
reason and can be retried, and a person with no Slack identity is
reached through the fallback channel with a note saying who was meant.

Slack is the first channel: chat.postMessage over the standard library,
a DM to the owner or a message in the fallback channel, and the Events
API for replies in the thread (signed with the app's signing secret).
The transport is an object with post_message and open_dm, so tests use
a fake and another channel can plug in.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

from .llm import drop_absence
from .store import Invalid, answer_hash

KINDS = ("ask", "signoff", "review", "reassigned", "answered", "overdue")
BACKOFF = (30, 120, 600, 3600, 21600)
MAX_ATTEMPTS = 20


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------- the outbox ----------------

def migrate(db) -> None:
    from .slack_chat import migrate as chat_migrate
    chat_migrate(db)
    from .slack_events import migrate as inbox_migrate
    inbox_migrate(db)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS notifications (
            id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, run_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL, channel TEXT NOT NULL, destination TEXT NOT NULL DEFAULT '',
            person_id TEXT NOT NULL DEFAULT '', person_name TEXT NOT NULL DEFAULT '',
            revision TEXT NOT NULL DEFAULT '', dedupe_key TEXT NOT NULL UNIQUE,
            payload TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '', external_ref TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '', sent_at TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS notifications_state ON notifications(state, next_attempt);
        CREATE INDEX IF NOT EXISTS notifications_ref ON notifications(external_ref);
        CREATE INDEX IF NOT EXISTS notifications_decision ON notifications(decision_id, created_at);
        CREATE TABLE IF NOT EXISTS webhook_receipts (
            id TEXT PRIMARY KEY, channel TEXT NOT NULL, received_at TEXT NOT NULL DEFAULT '');
        -- What Raven thinks a person's message meant, held until they
        -- confirm it. A reading is never applied on the model's word: a
        -- message wrongly read as an answer becomes a decision somebody
        -- is recorded as having made.
        CREATE TABLE IF NOT EXISTS reply_readings (
            channel TEXT NOT NULL, thread_ts TEXT NOT NULL, person_id TEXT NOT NULL,
            decision_id TEXT NOT NULL, revision TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL,
            answer TEXT NOT NULL DEFAULT '', rationale TEXT NOT NULL DEFAULT '',
            recipient TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (channel, thread_ts, person_id));
    """)
    # What the person was shown, so a reply is checked against it; and
    # the inbound state machine: received, applied, failed.
    if "content_hash" not in {r["name"] for r in db.execute("PRAGMA table_info(notifications)")}:
        db.execute("ALTER TABLE notifications ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''")
    have = {r["name"] for r in db.execute("PRAGMA table_info(webhook_receipts)")}
    for name, decl in (("state", "TEXT NOT NULL DEFAULT 'applied'"), ("attempts", "INTEGER NOT NULL DEFAULT 0"),
                       ("error", "TEXT NOT NULL DEFAULT ''"), ("reply", "TEXT NOT NULL DEFAULT ''"),
                       ("payload", "TEXT NOT NULL DEFAULT ''"), ("updated_at", "TEXT NOT NULL DEFAULT ''")):
        if name not in have:
            db.execute(f"ALTER TABLE webhook_receipts ADD COLUMN {name} {decl}")


def _take_ownership(graph, decision_id: str, name: str) -> str:
    """The person replying takes a decision nobody owns, and the revision
    that makes, which their answer names. Taking it is itself a write:
    an answer that named the revision from before was refused as stale,
    and the person was left owning a decision they could not answer."""
    with graph.transaction():
        graph.update_decision(decision_id, owner=name)
        return graph.db.execute("SELECT updated_at FROM decisions WHERE id=?", (decision_id,)).fetchone()["updated_at"]


class Delivery:
    """The outbox for one store: enqueue from the write paths, send from
    the worker (or deliver_now in tests), and resolve inbound replies."""

    def __init__(self, store, transport=None, fallback_channel: str = "", base_url: str = ""):
        self.store = store
        self.transport = transport
        self.fallback_channel = fallback_channel
        self.base_url = base_url.rstrip("/")
        self._thread = None
        self._stop = threading.Event()
        from .slack_events import Inbox
        self.inbox = Inbox(self)

    def sync_directory(self):
        if not hasattr(self.transport, "list_users"):
            return {}
        from .slack_directory import sync
        status = sync(self.store.graph, self.transport)
        # Recover notifications that had no address when they were created.
        for row in self.list(state="failed"):
            if not row.get("destination"):
                try:
                    self.retry(row["id"])
                except Invalid:
                    pass
        return status

    def slack_person(self, user_id):
        if hasattr(self.transport, "user_info"):
            from .slack_directory import contact
            return contact(self.store.graph, self.transport, user_id)
        return self.store.graph.find_person(user_id)

    @property
    def enabled(self) -> bool:
        # Stdio MCP can enqueue into the same database as the Slack worker
        # without possessing its bot token. Only that worker sends messages.
        return self.transport is not None or self.store.graph.get_setting("slack_connected") == "1"

    @property
    def channel(self) -> str:
        return getattr(self.transport, "name", "slack")

    # ---------------- enqueue ----------------

    def _destination(self, person_name: str) -> tuple[str, str, str, str]:
        """(destination, person_id, note, kind_of_destination) for a
        person: their DM when they have a Slack id, else the fallback
        channel with a note naming them, else nothing."""
        graph = self.store.graph
        from .routing import contact_for
        person = contact_for(graph, person_name) if person_name else None
        if person is not None:
            graph.db.execute("UPDATE owners SET person_id=?,name=? WHERE name=? AND person_id=''",
                             (person["id"], person["name"], person_name))
        if not getattr(self.transport, "supports_dm", True):
            # A channel-only transport (Teams incoming webhook): every
            # message goes to the one channel, addressed to the person.
            return "channel", (person or {}).get("id", ""), "", "channel"
        unavailable = json.loads(graph.get_setting("slack_unavailable") or "[]")
        if person is not None and person.get("slack_id") and person["slack_id"] not in unavailable:
            return person["slack_id"], person["id"], "", "dm"
        fallback = self.fallback_channel or graph.get_setting("slack_fallback_channel")
        if fallback:
            who = person_name or "the owner"
            why = "has no Slack id yet" if person is not None else "is not one of the people Raven knows"
            return fallback, (person or {}).get("id", ""), f"{who} {why}; posted here instead", "fallback"
        return "", (person or {}).get("id", ""), "", ""

    def _with_records(self, row) -> dict:
        """The row a message is rendered from, with the records its
        question and context name and their status."""
        from .ladder import named_records
        out = dict(row)
        try:
            out["named_records"] = named_records(self.store.graph, out.get("repo") or "", out.get("question") or "",
                                                 out.get("context") or "")
        except Exception:
            out["named_records"] = []
        if out.get('source_id'):
            source = self.store.graph.get_decision(out['source_id'])
            if source:
                out['source_signer'] = (source.answered_by or source.signed_by) if source.authorized else ''
                out['source_question'] = source.question
        return out

    def _task_link(self, person_id: str, run_id: str, decision_id: str, notification_id: str) -> str:
        """The person's own link to the task page, or "" when there is no
        public address or no person to issue it to. A message to a
        channel on someone's behalf links to the inbox instead: a link
        that signs in as the person must reach only them."""
        from . import briefing
        if not self.base_url or not person_id or briefing.mode(self.store.graph) == "off":
            return ""
        token = briefing.mint(self.store.graph, person_id, run_id, decision_id, notification_id)
        return briefing.url_for(self.base_url, token)

    def _has_login(self, person_id: str) -> bool:
        """The person can sign in to Raven: a password, or GitHub."""
        if not person_id:
            return False
        graph = self.store.graph
        person = graph.get_person(person_id)
        if person is None:
            return False
        return bool(person.get("github_id")) or graph.db.execute(
            "SELECT 1 FROM account_passwords WHERE person_id=?", (person_id,)).fetchone() is not None

    def _answered_in_slack(self, person_name: str) -> bool:
        """The person has answered a decision by replying in Slack before,
        so the reply syntax beyond `answer:` is worth a line. On first
        contact `rule if <words> until <date>` meant nothing and took more
        of a phone screen than the question."""
        if not person_name:
            return False
        source = f"slack: {person_name}"
        for r in self.store.graph.db.execute(
                "SELECT detail FROM events WHERE kind IN ('owner_approved', 'answer_corrected') AND detail LIKE ?",
                ('%"source": "slack: %',)):
            try:
                if json.loads(r["detail"]).get("source") == source:
                    return True
            except ValueError:
                continue
        return False

    def enqueue(self, decision_id: str, kind: str, to: str = "", note: str = "") -> dict | None:
        """One notification for one state of one decision. `to` names the
        person; by default the decision's owner (the requester for
        'answered'). Returns the row, or None when nothing is queued: the
        same state was already queued, delivery is off, or nobody can be
        reached (that last case is recorded as failed, so it shows)."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        graph = self.store.graph
        row = graph.db.execute(
            "SELECT d.*, o.name AS owner_name, r.title AS run_title, r.requester, r.agent AS run_agent, r.repo FROM decisions d "
            "LEFT JOIN owners o ON o.id = d.owner_id JOIN runs r ON r.id = d.run_id WHERE d.id=?",
            (decision_id,)).fetchone()
        if row is None:
            return None
        person_name = to or (row["requester"] if kind == "answered" else row["owner_name"]) or ""
        if not person_name and kind not in ("ask", "signoff", "review"):
            return None
        if not self.enabled:
            return None
        if kind == "answered":
            # The requester hears about an answer somebody else gave, and
            # only when the requester is a person Raven can reach. Off
            # unless a team turns it on: the requester is usually the
            # agent, which reads the answer off the tree, and a reviewer
            # counted these as three of eleven messages for three
            # decisions.
            if graph.get_setting("notify_requester") != "1":
                return None
            asker = graph.find_person(person_name)
            answerer = graph.find_person(row["answered_by"] or "") if row["answered_by"] else None
            if asker is None or (answerer is not None and answerer["id"] == asker["id"]):
                return None
            person_name = asker["name"]
        destination, person_id, dest_note, dest_kind = self._destination(person_name)
        note = " ".join(n for n in (note, dest_note) if n)
        # What this message shows the person: the answer on the table, or
        # the question asked. A reply is checked against it.
        content_hash = (answer_hash(row["answer"] or "") if kind in ("signoff", "review", "answered")
                        else answer_hash((row["question"] or "") + "\n" + (row["context"] or "")))
        # A reminder is one per day, whatever the revision. Everything
        # else is one per thing said: a decision touched twice without
        # changing what this person would read is not two messages. A
        # reviewer counted eleven outbound messages for three decisions,
        # two of them the same message twice.
        revision = now_iso()[:10] if kind == "overdue" else row["updated_at"]
        dedupe = (f"{decision_id}:{kind}:{revision}:{person_name}" if kind == "overdue"
                  else f"{decision_id}:{kind}:{person_name}:{content_hash}")
        if graph.db.execute("SELECT 1 FROM notifications WHERE dedupe_key=?", (dedupe,)).fetchone():
            return None
        nid = uuid.uuid4().hex[:12]
        payload = render(self._with_records(row), kind, person_name, self.base_url, note,
                         task_link=self._task_link(person_id if dest_kind == "dm" else "", row["run_id"],
                                                   decision_id, nid),
                         has_account=self._has_login(person_id), rule_hint=self._answered_in_slack(person_name))
        state = "queued" if destination else "failed"
        error = "" if destination else (f"{person_name} has no Slack id yet and no triage channel is configured")
        # Newer state of the same decision to the same person supersedes
        # what has not gone out yet: one message, the current one.
        graph.db.execute("UPDATE notifications SET state='superseded' WHERE decision_id=? AND person_name=? "
                         "AND state='queued'", (decision_id, person_name))
        graph.db.execute(
            "INSERT INTO notifications(id, decision_id, run_id, kind, channel, destination, person_id, person_name, "
            "revision, dedupe_key, payload, state, attempts, next_attempt, last_error, created_at, content_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,0,?,?,?)",
            (nid, decision_id, row["run_id"], kind, self.channel, destination, person_id, person_name, revision,
             dedupe, json.dumps(payload), state, error, now_iso(), content_hash))
        graph.append_event("notification_queued" if destination else "notification_failed",
                           {"task_id": row["run_id"], "decision_id": decision_id, "kind": kind, "to": person_name,
                            "destination": dest_kind, "error": error})
        return self.get(nid)

    def get(self, nid: str) -> dict | None:
        row = self.store.graph.db.execute("SELECT * FROM notifications WHERE id=?", (nid,)).fetchone()
        return dict(row) if row else None

    def remind_overdue(self, now: float | None = None) -> int:
        """Open questions older than the overdue window get a reminder to
        their owner once a day; past twice the window, the coordinator
        hears too. Returns how many reminders were queued."""
        if self.transport is None:
            return 0
        graph = self.store.graph
        hours = self.store.overdue_hours()
        current = now or time.time()
        cutoff = datetime.fromtimestamp(current - hours * 3600, timezone.utc).isoformat()
        rows = graph.db.execute(
            "SELECT d.id, d.created_at, d.repo, o.name AS owner_name FROM decisions d JOIN owners o ON o.id=d.owner_id "
            "WHERE d.status='pending' AND d.created_at < ? ORDER BY d.created_at LIMIT 200", (cutoff,)).fetchall()
        queued = 0
        for r in rows:
            try:
                age_h = (current - datetime.fromisoformat(r["created_at"]).timestamp()) / 3600.0
            except ValueError:
                age_h = float(hours)
            age = f"{int(age_h // 24)} day{'s' if int(age_h // 24) != 1 else ''}" if age_h >= 48 else f"{int(age_h)} hours"
            with graph.transaction():
                if self.enqueue(r["id"], "overdue", to=r["owner_name"], note=f"open for {age}") is not None:
                    queued += 1
                if age_h >= 2 * hours:
                    coordinator = graph.coordinator(r["repo"] or "")
                    if coordinator is not None and coordinator["name"] != r["owner_name"]:
                        if self.enqueue(r["id"], "overdue", to=coordinator["name"],
                                        note=f"open for {age}, waiting on {r['owner_name']}; you are the coordinator") is not None:
                            queued += 1
        return queued

    def list(self, state: str = "", limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM notifications"
        args: tuple = ()
        if state:
            sql += " WHERE state=?"
            args = (state,)
        rows = self.store.graph.db.execute(sql + " ORDER BY created_at DESC LIMIT ?", args + (limit,)).fetchall()
        return [{k: v for k, v in dict(r).items() if k != "payload"} for r in rows]

    def retry(self, nid: str) -> dict:
        """Queue a failed notification again. One that never had a
        destination is re-addressed now (the person may have a Slack id
        by now, or a fallback channel may exist) and re-rendered, so the
        note about where it landed is right."""
        graph = self.store.graph
        row = self.get(nid)
        if row is None:
            raise Invalid("Notification not found")
        destination = row["destination"]
        payload = row["payload"]
        if not destination:
            destination, _pid, note, dest_kind = self._destination(row["person_name"])
            if not destination:
                raise Invalid(f"{row['person_name']} still has no Slack id and there is no fallback channel")
            decision = graph.db.execute(
                "SELECT d.*, o.name AS owner_name, r.title AS run_title, r.requester, r.agent AS run_agent, r.repo FROM decisions d "
                "LEFT JOIN owners o ON o.id = d.owner_id JOIN runs r ON r.id = d.run_id WHERE d.id=?",
                (row["decision_id"],)).fetchone()
            if decision is not None:
                payload = json.dumps(render(self._with_records(decision), row["kind"], row["person_name"],
                                            self.base_url, note,
                                            task_link=self._task_link(_pid if dest_kind == "dm" else "",
                                                                      decision["run_id"], row["decision_id"], nid),
                                            has_account=self._has_login(_pid),
                                            rule_hint=self._answered_in_slack(row["person_name"])))
        with graph.transaction():
            graph.db.execute("UPDATE notifications SET state='queued', next_attempt=0, last_error='', destination=?, "
                             "payload=? WHERE id=?", (destination, payload, nid))
        return self.get(nid)

    # ---------------- send ----------------

    def deliver_now(self, limit: int = 50) -> int:
        """Send what is due. Returns how many went out."""
        if self.transport is None:
            return 0
        graph = self.store.graph
        due = graph.db.execute("SELECT * FROM notifications WHERE state='queued' AND next_attempt<=? "
                               "ORDER BY created_at LIMIT ?", (time.time(), limit)).fetchall()
        sent = 0
        for row in due:
            payload = json.loads(row["payload"])
            try:
                channel = row["destination"]
                if channel.startswith("U") or channel.startswith("W"):
                    channel = self.transport.open_dm(channel)
                # A later word about the same decision to the same person
                # goes under the first one. One thread per decision is how
                # a person follows it, and it is why three decisions do
                # not read as eleven separate asks.
                thread = self._thread_for(row, channel)
                ts = self.transport.post_message(channel, payload["text"], payload.get("blocks"),
                                                 thread_ts=thread)
                # Keyed to the thread, not to this message: a reply comes
                # back carrying the thread's own timestamp, and it has to
                # find the newest thing Raven said there.
                ts = thread or ts
                with graph.transaction():
                    graph.db.execute("UPDATE notifications SET state='sent', attempts=attempts+1, external_ref=?, "
                                     "sent_at=?, last_error='' WHERE id=?", (f"{channel}:{ts}", now_iso(), row["id"]))
                    graph.append_event("notification_sent", {"task_id": row["run_id"], "decision_id": row["decision_id"],
                                                             "kind": row["kind"], "to": row["person_name"]})
                sent += 1
            except Exception as error:
                attempts = row["attempts"] + 1
                delay = BACKOFF[min(attempts - 1, len(BACKOFF) - 1)]
                state = "failed" if attempts >= MAX_ATTEMPTS else "queued"
                with graph.transaction():
                    graph.db.execute("UPDATE notifications SET attempts=?, next_attempt=?, last_error=?, state=? WHERE id=?",
                                     (attempts, time.time() + delay, str(error)[:300], state, row["id"]))
                    if state == "failed":
                        graph.append_event("notification_failed", {"task_id": row["run_id"], "decision_id": row["decision_id"],
                                                                   "kind": row["kind"], "to": row["person_name"],
                                                                   "error": str(error)[:300]})
        return sent

    def start(self, interval: float = 2.0) -> None:
        if self._thread is not None or self.transport is None:
            return

        self.inbox.start()
        last_reminder = [0.0]
        last_directory = [0.0]

        def loop():
            while not self._stop.is_set():
                try:
                    if hasattr(self.transport, "list_users") and time.time() - last_directory[0] > 900:
                        last_directory[0] = time.time()
                        try:
                            self.sync_directory()
                        except Exception as error:
                            print(f"Raven Slack directory: {error}")
                    self.deliver_now()
                    if time.time() - last_reminder[0] > 1800:
                        last_reminder[0] = time.time()
                        self.remind_overdue()
                except Exception as error:  # the loop never dies on one bad row
                    print(f"Raven delivery: {type(error).__name__}: {error}")
                self._stop.wait(interval)
        self._thread = threading.Thread(target=loop, name="bridge-delivery", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self.inbox.thread is not None:
            self.inbox.thread.join(timeout=1)
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ---------------- replies ----------------

    def _thread_for(self, row, channel: str) -> str:
        """The thread this person already has for this decision, if any.
        The first message opens it; everything after replies in it, so a
        person reads one decision in one place and can answer the newest
        state where they were already looking."""
        earlier = self.store.graph.db.execute(
            "SELECT external_ref FROM notifications WHERE decision_id=? AND person_name=? AND state='sent' "
            "AND external_ref != '' ORDER BY sent_at LIMIT 1",
            (row["decision_id"], row["person_name"])).fetchone()
        if not earlier:
            return ""
        ref = earlier["external_ref"] or ""
        where, _, ts = ref.rpartition(":")
        return ts if where == channel else ""

    def notification_for_thread(self, channel: str, thread_ts: str) -> dict | None:
        row = self.store.graph.db.execute(
            "SELECT * FROM notifications WHERE external_ref=? ORDER BY sent_at DESC, created_at DESC LIMIT 1",
            (f"{channel}:{thread_ts}",)).fetchone()
        return dict(row) if row else None

    IN_FLIGHT_SECONDS = 120

    def receive(self, channel: str, thread_ts: str, user_id: str, text: str, event_id: str = "", action_token: str = "") -> str:
        """A reply in a thread Raven started. Returns the text Raven
        posts back. Every delivery is a durable receipt: received while
        it is being applied, applied with the reply it got (a repeat of
        the same event gets the same reply and changes nothing), or
        failed with the error, in which case the same event is applied
        again when it is retried (Slack retries on our error, and the
        operator can from Deliveries). A crash after the answer landed
        but before the receipt was marked is caught by the handler,
        which recognizes an answer already recorded by this person."""
        graph = self.store.graph
        if event_id:
            row = graph.db.execute("SELECT state, reply, updated_at, attempts FROM webhook_receipts WHERE id=?",
                                   (event_id,)).fetchone()
            if row is not None:
                state = row["state"] or "applied"
                if state == "applied":
                    # Applied already: nothing changes, and the thread
                    # got its acknowledgement the first time.
                    return ""
                if state == "received":
                    try:
                        age = time.time() - datetime.fromisoformat(row["updated_at"]).timestamp()
                    except ValueError:
                        age = self.IN_FLIGHT_SECONDS + 1
                    if age < self.IN_FLIGHT_SECONDS:
                        return ""
            with graph.transaction():
                graph.db.execute(
                    "INSERT INTO webhook_receipts(id, channel, received_at, state, attempts, payload, updated_at) "
                    "VALUES(?,?,?,'received',1,?,?) ON CONFLICT(id) DO UPDATE SET state='received', "
                    "attempts=webhook_receipts.attempts+1, updated_at=excluded.updated_at, payload=excluded.payload",
                    (event_id, self.channel, now_iso(),
                     json.dumps({"channel": channel, "thread_ts": thread_ts, "user": user_id, "text": text}), now_iso()))
        try:
            reply = self._apply_reply(channel, thread_ts, user_id, text, action_token=action_token)
            note = self.notification_for_thread(channel, thread_ts)
            person = graph.find_person(user_id)
            from .slack_chat import EphemeralReply
            if note and person and reply and not isinstance(reply, EphemeralReply):
                from .slack_chat import remember
                remember(graph, note["decision_id"], channel, thread_ts, person["id"], "assistant", reply)
        except Exception as error:
            if event_id:
                with graph.transaction():
                    graph.db.execute("UPDATE webhook_receipts SET state='failed', error=?, updated_at=? WHERE id=?",
                                     (f"{type(error).__name__}: {error}"[:500], now_iso(), event_id))
                    graph.append_event("inbound_failed", {"event_id": event_id, "channel": self.channel,
                                                          "error": str(error)[:300]})
            raise
        if event_id:
            from .slack_chat import EphemeralReply
            kept = 'Search response is not retained. Ask again for current Slack sources.' if isinstance(reply, EphemeralReply) else reply
            with graph.transaction():
                graph.db.execute("UPDATE webhook_receipts SET state='applied', reply=?, error='', updated_at=? WHERE id=?",
                                 (kept, now_iso(), event_id))
        return reply

    def inbound_failed(self) -> list[dict]:
        """Inbound events that were received and could not be applied."""
        rows = [dict(r) for r in self.store.graph.db.execute(
            "SELECT id, channel, state, attempts, error, received_at, updated_at FROM webhook_receipts "
            "WHERE state='failed' ORDER BY received_at DESC LIMIT 200")]
        known = {r['id'] for r in rows}
        for r in self.store.graph.db.execute("SELECT * FROM slack_ingress WHERE error!='' ORDER BY created_at DESC LIMIT 200"):
            if r['id'] not in known:
                rows.append({'id': r['id'], 'channel': 'slack', 'state': r['state'], 'attempts': r['attempts'],
                             'error': r['error'], 'received_at': r['created_at'], 'updated_at': r['created_at']})
        return rows

    def reply_failures(self) -> list[dict]:
        return [dict(r) for r in self.store.graph.db.execute(
            "SELECT id,channel,thread_ts,state,attempts,error FROM slack_replies WHERE error!='' LIMIT 200")]

    def retry_inbound(self, event_id: str) -> dict:
        """Apply a failed inbound event again from what it carried."""
        with self.store.graph.transaction():
            queued = self.store.graph.db.execute("UPDATE slack_ingress SET state='queued',next_attempt=0,attempts=0,error='' "
                                                "WHERE id=? AND state IN ('failed','queued') AND error!=''", (event_id,))
        if queued.rowcount:
            self.inbox.start()
            return {'id': event_id, 'state': 'queued'}
        row = self.store.graph.db.execute("SELECT * FROM webhook_receipts WHERE id=?", (event_id,)).fetchone()
        if row is None or (row["state"] or "applied") != "failed":
            raise Invalid("No failed inbound event with that id")
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            payload = {}
        if not payload.get("channel"):
            raise Invalid("This event kept no payload to retry from")
        reply = self.receive(payload["channel"], payload.get("thread_ts", ""), payload.get("user", ""),
                             payload.get("text", ""), event_id=event_id)
        return {"id": event_id, "state": "applied", "reply": reply}

    def _stale(self, note: dict, decision: dict, person: dict) -> str:
        """Why a reply to this message cannot stand: the decision moved
        on since the person was shown it, by somebody else's hand. What
        this person signed as it stands (their own answer, their own
        correction, an answer they agreed to) is theirs to build on in the
        same thread, whoever else signed it after them. Measured live on
        63eb671: Theo's co-signature made Mira's own answer read as
        "answered by Mira Runtime since this message"."""
        kind = note.get("kind") or ""
        shown = note.get("content_hash") or ""
        if not shown:
            return ""
        theirs = _signed_as_it_stands(decision, person)
        if kind in ("signoff", "review", "answered"):
            if shown != answer_hash(decision.get("answer") or "") and not theirs:
                return (f"the answer changed since this message; it now reads: "
                        f"{_clip(decision.get('answer') or '', 400, 'the inbox has the rest')}")
        elif kind in ("ask", "reassigned", "overdue"):
            # What this message showed is the question, so a reply is
            # stale when the question changed, or when somebody else
            # settled it. A node Raven resolved from the record and
            # handed to this person is not stale: putting it in front of
            # them is what the message was for, and refusing their reply
            # for want of a `pending` status sent a reviewer hunting for
            # another thread to answer in.
            if shown != answer_hash((decision.get("question") or "") + "\n" + (decision.get("context") or "")):
                return "the question changed since this message"
            settled = decision.get("status") == "approved" or decision.get("signoff") == "signed"
            if settled and not theirs:
                who = decision.get("answered_by") or decision.get("signed_by") or "someone"
                return (f"this decision was answered by {who} since this message; it now reads: "
                        f"{_clip(decision.get('answer') or '', 400, 'the inbox has the rest')}")
        return ""

    # ---------------- reading a reply that is not in Raven's words ----------------

    def _reading(self, channel: str, thread_ts: str, person_id: str) -> dict | None:
        row = self.store.graph.db.execute(
            "SELECT * FROM reply_readings WHERE channel=? AND thread_ts=? AND person_id=?",
            (channel, thread_ts, person_id)).fetchone()
        return dict(row) if row else None

    def _forget_reading(self, channel: str, thread_ts: str, person_id: str) -> None:
        with self.store.graph.transaction():
            self.store.graph.db.execute(
                "DELETE FROM reply_readings WHERE channel=? AND thread_ts=? AND person_id=?",
                (channel, thread_ts, person_id))

    def _offer_reading(self, channel: str, thread_ts: str, decision: dict, person: dict, text: str) -> str:
        """What Raven thinks the message meant, read back for the person
        to confirm. Empty when there is no model backend or it is not
        confident, which leaves the deterministic refusal in place.

        The reading is never applied here. The model is good at seeing
        that "three states, off by default" is an answer; it is not the
        thing that gets to say a person decided something."""
        if _ACK_ONLY_RE.match(text or ""):
            return ""
        from . import llm as llm_mod
        from .config import load as load_config
        try:
            read = llm_mod.read_reply(load_config(), decision.get("question") or "",
                                      decision.get("answer") or "", text)
        except Exception:
            return ""
        kind = read.get("kind", "")
        if kind in ("", "chat"):
            return ""
        if kind == "question":
            return ("Noted, and not recorded: that reads as a question rather than a decision. Ask it here and the "
                    "requester sees it, or answer with `answer: <the decision> because <why>` when you are ready.")
        if kind == "rule":
            return ("That reads as something you want to hold in future cases too. Reply `rule if <words> until "
                    "<date>` once the answer itself is signed, so the terms are recorded with it.")
        if kind == "handoff" and not read.get("to"):
            return ""
        graph = self.store.graph
        with graph.transaction():
            graph.db.execute(
                "INSERT INTO reply_readings(channel, thread_ts, person_id, decision_id, revision, kind, answer, "
                "rationale, recipient, created_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(channel, thread_ts, person_id) DO UPDATE SET decision_id=excluded.decision_id, "
                "revision=excluded.revision, kind=excluded.kind, answer=excluded.answer, "
                "rationale=excluded.rationale, recipient=excluded.recipient, created_at=excluded.created_at",
                (channel, thread_ts, person["id"], decision["id"],
                 _what_it_says(decision), kind, read.get("answer", ""), read.get("rationale", ""),
                 read.get("to", ""), now_iso()))
        if kind == "handoff":
            return (f"Not recorded yet. I read that as handing this to {read['to']}. Reply `yes` and I will pass it "
                    "on, or `not me @person` to name them yourself.")
        if kind == "signoff":
            return ("Not recorded yet. I read that as agreeing with the answer on the table. Reply `yes` and I will "
                    "sign it in your name, or `sign off` to say so outright.")
        line = read["answer"]
        return (f"Not recorded yet. I read your answer as: \"{line}\". Reply `yes` and I will record it in your "
                "name, or put it in your own words with `answer: <the decision> because <why>`.")

    def _take_reading(self, channel: str, thread_ts: str, decision: dict, person: dict, text: str,
                      actor) -> str | None:
        """Apply a reading the person has just confirmed, or None when
        this message is not a confirmation of one."""
        held = self._reading(channel, thread_ts, person["id"])
        if held is None:
            return None
        if _DECLINE_RE.match(text or ""):
            self._forget_reading(channel, thread_ts, person["id"])
            return ("Dropped, and nothing was recorded. Put it in your own words with `answer: <the decision> "
                    "because <why>`.")
        if not _CONFIRM_RE.match(text or ""):
            # They said something new; the old reading is not what they
            # meant any more, whatever this turns out to be.
            self._forget_reading(channel, thread_ts, person["id"])
            return None
        if decision.get('status') == 'withdrawn':
            self._forget_reading(channel, thread_ts, person['id'])
            return 'This question was withdrawn because its task was closed. Nothing was recorded.'
        said = held["revision"] or ""
        moved = said and (said != _what_it_says(decision) if said.startswith("c:") else said != decision["updated_at"])
        if held["decision_id"] != decision["id"] or moved:
            self._forget_reading(channel, thread_ts, person["id"])
            return ("Not recorded: the decision changed since I read your message back to you. Reply in the newer "
                    "thread, or answer again here.")
        self._forget_reading(channel, thread_ts, person["id"])
        from . import canvas
        if held["kind"] == "handoff":
            who = self.store.graph.find_person(held["recipient"].strip("@ "))
            if who is None:
                return f"I do not know {held['recipient']} in Raven; name them by Slack mention, email or full name."
            return self.store.refer(decision["id"], {"person": who["id"], "by": person["name"],
                                                     "expected_updated_at": decision["updated_at"],
                                                     "note": f"handed on in Slack: {held['recipient']}"},
                                    actor=actor)["notice"]
        if held["kind"] == "signoff":
            canvas.sign_off(self.store, decision["id"], {"by": person["name"],
                                                         "expected_updated_at": decision["updated_at"]},
                            actor=actor)
            return f"Signed off by {person['name']}. The agent sees it on the tree"
        rationale = held["rationale"] or "answered in Slack, in their own words"
        if decision["status"] == "pending":
            if not decision["owner_id"]:
                decision["updated_at"] = _take_ownership(self.store.graph, decision["id"], person["name"])
            self.store.answer(decision["id"], {"answer": held["answer"], "rationale": rationale,
                                               "expected_updated_at": decision["updated_at"],
                                               "signed_by": person["name"],
                                               "source": f"slack: {person['name']} (read back and confirmed)"},
                              actor=actor)
            return f"Recorded as {person['name']}'s answer. The agent sees it on the tree"
        canvas.sign_off(self.store, decision["id"], {"by": person["name"], "answer": held["answer"],
                                                     "rationale": rationale,
                                                     "expected_updated_at": decision["updated_at"]}, actor=actor)
        return f"Corrected and signed by {person['name']}. The agent sees it on the tree"

    def _apply_reply(self, channel: str, thread_ts: str, user_id: str, text: str, action_token: str = "") -> str:
        graph = self.store.graph
        note = self.notification_for_thread(channel, thread_ts)
        if note is None:
            return ""
        person = self.slack_person(user_id)
        if person is None:
            return ("I could not verify you as an active member of the connected Slack workspace. "
                    "The operator can check Slack permissions in Connections & setup; no Raven account is needed.")
        from .authz import Actor, Refused
        actor = Actor.person(person, kind="slack")
        decision = self.store.get_decision(note["decision_id"])
        if decision.get('status') == 'withdrawn':
            return 'This question was withdrawn because its task was closed. Nothing was recorded.'
        text = (text or "").strip()
        lowered = text.lower()
        reply = ""
        from .slack_chat import respond
        try:
            conversational = respond(self, note, decision, person, text, actor, action_token)
            if conversational is not None:
                return conversational
        except (Refused, Invalid) as error:
            return f"Nothing recorded: {error}"
        if not decision.get("owner_id"):
            # The configured channel is a routing conversation, not an
            # opportunity to sign an unassigned decision by replying 'ok'.
            fallback = self.fallback_channel or graph.get_setting("slack_fallback_channel")
            if channel != fallback or note.get("person_name"):
                return "This decision has no contact yet. Reply in Raven's triage channel to route it."
            claim = re.fullmatch(r"(?:i['’]ll take this|i can answer|take this|claim)", lowered)
            referral = re.fullmatch(r"(?:not me|ask|refer to)\s+<@([A-Z0-9]+)(?:\|[^>]*)?>[.!]?", text, re.I)
            target = person if claim else self.slack_person(referral[1]) if referral else None
            if target is None:
                return "No answer recorded. Reply `I'll take this` or `ask @person` (use a Slack mention) to route this question."
            self.store.claim_slack_question(decision["id"], target["id"], actor)
            return f"Sent to {target['name']} in Slack for this question. No answer or sign-off recorded yet."
        # An answer this person already gave in these words is this event
        # applied before its receipt was marked: the same reply again.
        answer_text, _rationale = _split_rationale(_explicit_answer(text) or text)
        if (decision.get("status") == "approved" and (decision.get("signed_by") or "") == person["name"]
                and (decision.get("answer") or "").strip() == answer_text.strip() and answer_text.strip()):
            return (f"Recorded as {person['name']}'s answer. The agent sees it on the tree"
                    + (f" (task {note['run_id']}; it reads the answer with bridge_get_tree)." if note.get("run_id") else "."))
        stale = self._stale(note, decision, person)
        if stale and not re.match(r"^(?:not me|refer(?: to)?|ask|hand(?: it)? to|reassign(?: to)?)\b", text, re.IGNORECASE):
            # A reply to what is no longer there: refused, and the current
            # state goes out as a fresh message to reply to (or it went
            # out already, when the decision changed). Never a request to
            # sign what this person has already signed as it stands.
            if decision.get("status") == "pending" or not _signed_as_it_stands(decision, person):
                with graph.transaction():
                    self.enqueue(decision["id"], "ask" if decision.get("status") == "pending" else "signoff",
                                 to=person["name"], note="the earlier message is out of date; this is the current state")
                return (f"Not recorded: {stale}. Reply in the newer thread for this decision (sent when it changed, "
                        "or on its way now), or act in the inbox.")
            return f"Not recorded: {stale}. You have signed the answer as it stands; act on it in the inbox."
        try:
            confirmed = self._take_reading(channel, thread_ts, decision, person, text, actor)
            handoff = re.match(r"^(?:not me|refer(?: to)?|ask|hand(?: it)? to|reassign(?: to)?)\s*:?\s*(.+)$", text, re.IGNORECASE)
            if confirmed is not None:
                reply = confirmed
            elif handoff:
                target = handoff.group(1).strip()
                mention = re.match(r"^<@([A-Z0-9]+)(?:\|[^>]*)?>", target)
                if mention:
                    who, rest = self.slack_person(mention.group(1)), target[mention.end():]
                else:
                    # A name, then maybe why: "Release Owner, they own what we
                    # tell users" or "Release Owner for docs".
                    name = re.split(r",|;|\s+(?:for|because|since|as|-)\s+|\.\s", target, maxsplit=1)[0]
                    who, rest = graph.find_person(name.strip("@ ")), target[len(name):]
                    if who is None:
                        # Or a name and then anything else, with no comma:
                        # "@Theo Release just this one". The longest run of
                        # leading words that names one person is the name.
                        # Measured live on eb9d22d: the whole tail was read
                        # as the person's name.
                        words = name.split()
                        for n in range(min(len(words) - 1, 4), 0, -1):
                            lead = " ".join(words[:n])
                            found = graph.find_person(lead.strip("@ "))
                            if found is not None:
                                who, rest = found, target[len(lead):]
                                break
                if who is None:
                    return f"I do not know {target} in Raven; name a person by their Slack mention, email or full name."
                refer = {"person": who["id"], "by": person["name"], "expected_updated_at": decision["updated_at"],
                         "note": _clip(_named_mentions(graph, text), 300, "the Slack reply has the rest")}
                scope = _handon_scope(rest)
                if scope:
                    refer.update(scope)
                reply = self.store.refer(decision["id"], refer, actor=actor)["notice"]
            elif re.match(r"^(?:make (?:this |it )?a )?rule\b", lowered):
                # `rule`, `rule if enterprise plan`, `rule until 2027-01-01`:
                # the signed answer becomes reusable on those terms.
                rest = re.sub(r"^(?:make (?:this |it )?a )?rule\b:?", "", text, flags=re.IGNORECASE)
                until = re.search(r"\buntil\s+(\d{4}-\d{2}-\d{2})", rest, re.IGNORECASE)
                anywhere = bool(re.search(r"\b(?:anywhere|any scope|for any customer|everywhere)\b", rest, re.IGNORECASE))
                trimmed = re.sub(r"\buntil\s+\d{4}-\d{2}-\d{2}|\b(?:anywhere|any scope|for any customer|everywhere)\b", "",
                                 rest, flags=re.IGNORECASE).strip()
                conds = re.search(r"\b(?:if|when)\s+(.+)$", trimmed, re.IGNORECASE)
                reply = self.store.make_rule(decision["id"], {"by": person["name"], "expected_updated_at": decision["updated_at"],
                                                              "conditions": conds.group(1).strip() if conds else "",
                                                              "expires": until.group(1) if until else "",
                                                              "scope": "any" if anywhere else "same"}, actor=actor)["notice"]
            elif re.match(r"^(sign(?:ed)?[- ]?off|approve[d]?|lgtm|confirm(?:ed)?|yes,? sign(?:ed)?(?: off)?|ok(?:ay)?,? sign(?:ed)?(?: off)?)\W*$", lowered):
                from . import canvas
                canvas.sign_off(self.store, decision["id"], {"by": person["name"], "expected_updated_at": decision["updated_at"]},
                                actor=actor)
                reply = f"Signed off by {person['name']}. The agent sees it on the tree"
            else:
                explicit = _explicit_answer(text)
                if not explicit and not re.search(r"\b(?:because|rationale:|reason:)\b", text, re.IGNORECASE):
                    # A conversation is not a decision: an answer is stated
                    # as one, or carries its because. Where a person wrote
                    # neither, ask the model what they meant and read it
                    # back to them; their `yes` applies it, nothing else.
                    offer = self._offer_reading(channel, thread_ts, decision, person, text)
                    if offer:
                        return offer
                    return ("Not recorded. To answer, reply `answer: <the decision> because <why>`; to confirm the "
                            "answer on the table, `sign off`; to hand it on, `not me @person`; to make a signed "
                            "answer reusable, `rule if <words> until <date>`.")
                answer, rationale = _split_rationale(explicit or text)
                if decision["status"] == "pending":
                    self.store.answer(decision["id"], {"answer": answer, "rationale": rationale or "answered in Slack",
                                                       "expected_updated_at": decision["updated_at"],
                                                       "signed_by": person["name"], "source": f"slack: {person['name']}"},
                                      actor=actor)
                    reply = f"Recorded as {person['name']}'s answer. The agent sees it on the tree"
                else:
                    from . import canvas
                    canvas.sign_off(self.store, decision["id"], {"by": person["name"], "answer": answer,
                                                                 "rationale": rationale or "corrected in Slack",
                                                                 "expected_updated_at": decision["updated_at"]},
                                    actor=actor)
                    reply = f"Corrected and signed by {person['name']}. The agent sees it on the tree"
        except Refused as error:
            return f"Not recorded: {error}"
        except Invalid as error:
            return f"Not recorded: {error}"
        with graph.transaction():
            graph.append_event("reply_received", {"task_id": note["run_id"], "decision_id": decision["id"],
                                                  "by": person["name"], "channel": self.channel})
        return reply + (f" (task {note['run_id']}; it reads the answer with bridge_get_tree)." if note.get("run_id") else ".")


_CONFIRM_RE = re.compile(r"^\s*(?:yes|yep|yeah|correct|right|confirm(?:ed)?|record (?:it|that)|that'?s it|"
                         r"do it|go ahead|exactly)\W*$", re.IGNORECASE)
_DECLINE_RE = re.compile(r"^\s*(?:no|nope|not quite|wrong|nah)\W*$", re.IGNORECASE)
# A person saying they have seen the message. The deterministic layer
# already declines to read these as agreement, and the model does not get
# to overrule that: measured on the reply panel, "ok" came back as a
# signoff, which would put somebody's name on a decision they only
# acknowledged.
_ACK_ONLY_RE = re.compile(r"^\s*(?:ok(?:ay)?|k|sure|thanks?|thx|ty|got it|gotcha|noted|seen|ack|"
                          r"understood|makes sense|fair enough|right(?:o)?|cool|nice|\W+)\W*$", re.IGNORECASE)


def _signed_as_it_stands(decision: dict, person: dict) -> bool:
    """Whether this person's signature covers the decision's answer as it
    reads now: signed after the last change to its text, whoever else
    signed since. A decision kept before signatures carried a hash counts
    their name among its signers."""
    name = (person.get("name") or "").strip().lower()
    if not name or not (decision.get("answer") or "").strip():
        return False
    raw = decision.get("signatures") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except ValueError:
            raw = []
    current = answer_hash(decision.get("answer") or "")
    hashed = [s for s in raw if isinstance(s, dict) and s.get("hash")]
    if hashed:
        return any((s.get("by") or "").strip().lower() == name and s.get("hash") == current for s in hashed)
    return name in [n.strip().lower() for n in (decision.get("signed_by") or "").split(",")]


def _what_it_says(decision: dict) -> str:
    """A reading's revision: the question, the answer, who has it and
    whether it is still open. A signature that changes none of them (a
    co-signer agreeing) does not make a reading read back to someone
    else out of date."""
    return "c:" + answer_hash("\n".join([decision.get("question") or "", decision.get("context") or "",
                                         decision.get("answer") or "", decision.get("owner_id") or "",
                                         "open" if decision.get("status") == "pending" else "settled"]))


def _explicit_answer(text: str) -> str:
    """The answer a reply states as one: `answer: …`, `decision: …`,
    `decide: …`; empty when the reply is not phrased that way."""
    m = re.match(r"^\s*(?:answer|decision|decide|decided)\s*[:\-]\s*(.+)$", text or "", re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else ""


def _split_rationale(text: str) -> tuple[str, str]:
    m = re.search(r"\b(?:because|rationale:|reason:)\s*", text, re.IGNORECASE)
    if m:
        return text[:m.start()].strip().rstrip(",;") or text, text[m.end():].strip()
    return text, ""


# ---------------- rendering ----------------

def _source_label(row: dict) -> str:
    """Where the answer on the table came from, in the words a person
    approving it needs. A guess is never described as a record: a
    reviewer watching this found an assumed default presented as coming
    "from the records", with an unrelated pull request beside it as
    precedent, which is the one mistake this line must not make."""
    who = (row.get("answered_by") or "").strip()
    if who:
        return f"Answer on the table (from {who})"
    kind = (row.get("kind") or "").strip()
    if kind == "agent":
        return "Answer on the table (the agent settled this itself, unconfirmed)"
    if kind == "evidence":
        return "Answer on the table (found in your records)"
    if kind == "prediction":
        if row.get('source_signer'):
            return f"Proposed from {row['source_signer']}'s signed answer (confirm its use here)"
        found = (row.get("evidence") or "").lower()
        if "another scope" in found or "other scope" in found:
            return "Raven's guess (an answer given for another scope, which may not carry over)"
        if "proposal" in found or "unratified" in found or "open ticket" in found:
            return "Raven's guess (an open proposal nobody has ratified)"
        if "low-stakes" in found or "assumption" in found or "default" in found:
            return "Raven's guess (a default it assumed, not a decision of yours)"
        return "Raven's guess (a prediction, not a record)"
    return "Answer on the table"


def split_evidence(text: str) -> list[str]:
    """The lines of a routing evidence string: split on "; " outside
    parentheses and brackets only. Measured live: a "Why you" line ended
    mid-parenthesis because the parenthetical itself held a "; "."""
    out, depth, start, i = [], 0, 0, 0
    while i < len(text):
        ch = text[i]
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        elif ch == ";" and depth == 0 and text[i + 1:i + 2] == " ":
            out.append(text[start:i])
            start = i + 2
            i += 1
        i += 1
    out.append(text[start:])
    return [ln.strip() for ln in out if ln.strip()]


def _found_for_owner(evidence: str, rest: str = "the inbox has the rest") -> str:
    """What Raven found that a person can use: the earlier decisions and
    the records it cites. How the search went (scores, floors, what did
    not match) belongs to the audit trail in the inbox, not the message.
    Measured live: an owner was sent "memory: best match scored 0.28,
    under the 0.6 floor". A conflict has its own line."""
    keep = []
    # "how they decide" starts a part of its own; without it the ladder's
    # "assumption: ..." before it was kept, glued to the cited decision.
    for part in re.split(r";\s+(?=[a-z]+:\s|how they decide\b)", evidence or ""):
        part = part.strip()
        if part.startswith("conflict:"):
            continue
        if re.search(r"\bdecision [0-9a-f]{12}\b|\b(?:retrieved|selected)\b", part) \
                and not re.search(r"\bscored\b|\bfloor\b", part):
            keep.append(part)
    return _clip("; ".join(keep), 400, rest)


def _conflicts(evidence: str) -> list[str]:
    """The conflicts the evidence records, each whole: two sources Raven
    holds that disagree, quoted, with what Raven did about it."""
    out = []
    for part in re.split(r";\s+(?=[a-z]+:\s)", evidence or ""):
        part = part.strip()
        if part.startswith("conflict:"):
            text = part[len("conflict:"):].strip()
            if text and text not in out:
                out.append(text[0].upper() + text[1:])
    return out


def _conversational() -> bool:
    from .config import load
    return load().semantic_retrieval


def _message_context(context: str, has_options: bool, has_page: bool) -> str:
    """The agent's context as a message shows it. The canvas writes a
    node's paths and options onto its context; the options have their
    own line, and with a task page the paths are on it. Measured on
    prometheus/prometheus: the options were in the message twice, once
    run into the context."""
    lines = (context or "").split("\n")
    while lines and ((has_options and lines[-1].startswith("Options: "))
                     or (has_page and lines[-1].startswith("Paths: "))):
        lines.pop()
    text = " ".join(" ".join(lines).split())
    return "" if text == "a node on the canvas" else text


def render(row: dict, kind: str, person_name: str, base_url: str, note: str = "", task_link: str = "",
           has_account: bool = True, rule_hint: bool = True) -> dict:
    """The message for one notification: what is being decided, the
    brief when one was written, the context, what Raven found, the
    options, and how to reply. Plain text with Slack mrkdwn.

    A `task_link` is the person's own link to the task page; it opens
    without an account. It comes right under the question: measured on
    prometheus/prometheus, it was the second-to-last line, two and a
    half phone screens down. With it, why the decision came to them is
    on the page, and the inbox line is left out for a person with no
    account (`has_account`), for whom it opened a sign-in page.
    `rule_hint` adds how to make an answer a rule, for a person who has
    answered in Slack before."""
    link = f"{base_url}/#inbox" if base_url and (has_account or not task_link) else ""
    question = row.get("question") or ""
    title = row.get("run_title") or ""
    heads = {
        "ask": f"*A decision needs you, {person_name}.*",
        "signoff": f"*Sign-off wanted from {person_name}.*",
        "review": f"*Needs your review, {person_name}: an answer this decision leaned on was corrected.*",
        "reassigned": f"*Handed to you, {person_name}.*",
        "answered": f"*Your question was answered, {person_name}.*",
        "overdue": f"*Still waiting on you, {person_name}: an agent's task is blocked on this decision.*",
    }
    lines = [heads.get(kind, heads["ask"])]
    if not person_name:
        lines = ["*Who can help with this decision?*",
                 "Raven has not identified a reachable contact. Reply `I'll take this` or `ask @person` "
                 "using a Slack mention. This routes the question; it does not approve anything."]
    if note:
        lines.append(f"_{note}_")
    lines.append(f"*{question}*")
    if title:
        # Who asked, and through which agent: the first thing a skeptical
        # maintainer asks of a message from a bot.
        requester = (row.get("requester") or "").strip()
        agent = (row.get("run_agent") or "").strip()
        asked = ""
        if requester and requester.lower() != (person_name or "").lower():
            asked = f", requested by {requester}" + (f" via {agent}" if agent else "")
        lines.append(f"Task: {title}" + (f" ({row.get('repo')})" if row.get("repo") else "") + asked)
    if task_link:
        # Short: on a phone the 20-word parenthetical took more room than
        # the question. The page itself says not to forward the link.
        lines.append(f"<{task_link}|Open the task> (your own link, no account needed)")
    rest = "the task page has the rest" if task_link else "the inbox has the rest"
    conflicts = _conflicts(row.get("evidence") or "")
    if kind in ("ask", "reassigned", "overdue") and conflicts:
        # Why this came to a person, before anything else: two sources
        # Raven holds disagree. Measured live on 63eb671: the brief said
        # no current policy was given, and the two policies were quoted
        # lower down, cut at 400 characters.
        lines.append("*Conflict:* " + _clip(" ".join(conflicts), 1500, rest))
    brief = drop_absence(row.get("brief") or "")
    if brief:
        lines.append(brief)
    from .ladder import records_line
    named = records_line(row.get("named_records") or [])
    if named:
        lines.append("Records it names: " + named + ".")
    options = []
    try:
        options = json.loads(row.get("options") or "[]")
    except ValueError:
        pass
    context = _message_context(row.get("context") or "", bool(options), bool(task_link))
    if context:
        # The agent's own words, the part a withheld brief leaves to be read:
        # whole up to a length a message holds, else cut at a word and said so.
        lines.append("Context: " + _clip(context, 1500, rest))
    if kind in ("signoff", "review", "answered") and row.get("answer"):
        # A signature covers the whole answer. Split it across Slack blocks
        # rather than requiring an inbox account to read the rest.
        lines.append(f"{_source_label(row)}: " + row["answer"])
        if (row.get("kind") or "") == "prediction":
            if row.get('source_signer'):
                lines.append(f"_Built from the answer {row['source_signer']} signed to a similar question "
                             f"(decision {row.get('source_id')}): {row.get('source_question', '')}. Confirm its use here._")
            elif row.get('source_id'):
                lines.append(f"_Reused from decision {row['source_id']}, which no person has signed. Confirm or correct it._")
            else:
                lines.append("_Bridge did not find this decided anywhere. It is a guess for you to confirm or "
                             "correct, not something your team settled._")
    if kind == "review" and row.get("review_reason"):
        lines.append(f"Why: {_clip(row['review_reason'], 600, 'the inbox has the rest')}")
    evidence = split_evidence(row.get("owner_evidence") or "")[:3]
    if kind == "reassigned" or (kind == "ask" and not task_link):
        # Who handed it on, and what answering teaches, belongs in the
        # message itself; the routing evidence behind an ask is on the
        # task page, said plainly, when there is one.
        if evidence:
            lines.append("Why you: " + "; ".join(evidence))
    if kind in ("ask", "reassigned", "overdue") and row.get("prediction"):
        lines.append(f"How you decided before: {_clip(row['prediction'], 800, 'the rest is in the inbox')} "
                     "(a prediction from your earlier answers; confirm or correct it)")
        # The scope the answer it leans on was given for, beside it rather
        # than only in the evidence. Measured live on eb9d22d: predictions
        # for another customer or an expired answer read as settled policy.
        scope = prediction_scope(row.get("evidence") or "")
        if scope:
            lines.append(f"Scope: {scope}.")
    found = _found_for_owner(row.get("evidence") or "", rest)
    if kind == "ask" and found:
        lines.append(f"What Raven found: {found}")
    if options:
        lines.append("Options: " + " | ".join(str(o) for o in options))
    if not _conversational() and kind in ('ask', 'reassigned', 'overdue', 'signoff', 'review') and person_name:
        lines.append("Reply in this thread: `answer: <the decision> because <why>` to answer or correct it, or `not me @person` to hand it on. "
                     "Reply `sign off` to confirm the complete answer. Natural-language conversation needs an inference backend."
                     + (" After answering, `rule if <words> until <date>` makes the answer reusable for matching questions." if rule_hint else ""))
    elif kind in ("ask", "reassigned", "overdue") and person_name:
        lines.append("Reply in this thread in your own words: ask for context, give your answer, or mention someone better to ask. Raven reads decisions back for confirmation. Shortcuts: `answer: <the decision> because <why>` or `not me @person`."
                     + (" After answering, `rule if <words> until <date>` makes the answer reusable for matching questions." if rule_hint else ""))
    elif kind == "signoff" and person_name:
        lines.append("Ask a question or explain what should change in your own words. Raven will read your decision back before signing. Reply `sign off` to confirm the complete answer.")
    elif kind == "review":
        lines.append("Tell me what changed or ask for context. I will read corrections back for confirmation. Reply `sign off` if the complete answer still stands.")
    if link:
        lines.append(f"Optional web inbox: {link} (decision {row.get('id')}). You can answer here without a Raven account.")
    text = "\n".join(lines)
    # Slack shows the blocks, not the text, and one section holds 3000
    # characters: a long message goes out as several sections rather than
    # one cut off at the limit.
    return {"text": text, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": part}}
                                     for part in _sections(text, 2900)]}


def prediction_scope(evidence: str) -> str:
    """The scope note a prediction carries when the answer it leans on was
    given for another scope, or ""."""
    m = re.search(r"(?:^|; )prediction scope: ([^;]+)", evidence or "")
    return m.group(1).strip() if m else ""


def _clip(text: str, limit: int, rest: str) -> str:
    """The text, whole when it fits; else cut at a word before `limit`,
    marked as cut, never mid-word and never silently."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:")
    return f"{cut} … _[cut here: {rest}]_"


def _sections(text: str, size: int) -> list[str]:
    """Split a message into Slack section texts at line breaks; a single
    line longer than a section is split at a space."""
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > size:
            head = line[:size].rsplit(" ", 1)[0] or line[:size]
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(head)
            line = line[len(head):].lstrip()
        if cur and len(cur) + 1 + len(line) > size:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts[:50] or [""]


# ---------------- Slack ----------------

class SlackTransport:
    """Slack's Web API over the standard library: a bot token, DMs opened
    on demand, messages posted with a text fallback."""

    name = "slack"

    def __init__(self, bot_token: str, api_base: str = ""):
        self.token = bot_token
        # A Slack-compatible Web API elsewhere: an egress proxy, or a test
        # double standing in for Slack in an end-to-end run.
        self.api_base = (api_base or os.environ.get("SLACK_API_BASE", "") or "https://slack.com/api").rstrip("/")
        self._dm: dict[str, str] = {}

    def _call(self, method: str, payload: dict) -> dict:
        req = urllib.request.Request(f"{self.api_base}/{method}", data=json.dumps(payload).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"Slack {method} failed: HTTP {error.code}") from error
        except (urllib.error.URLError, OSError) as error:
            raise RuntimeError(f"Slack {method} unreachable: {error}") from error
        if not body.get("ok"):
            raise RuntimeError(f"Slack {method} failed: {body.get('error', 'unknown error')}")
        return body

    def open_dm(self, user_id: str) -> str:
        if user_id in self._dm:
            return self._dm[user_id]
        body = self._call("conversations.open", {"users": user_id})
        channel = body["channel"]["id"]
        self._dm[user_id] = channel
        return channel

    def post_message(self, channel: str, text: str, blocks=None, thread_ts: str = "") -> str:
        payload = {"channel": channel, "text": text, "unfurl_links": False}
        if blocks:
            payload["blocks"] = blocks
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return self._call("chat.postMessage", payload)["ts"]

    def post_reply(self, channel, text, blocks, thread_ts, event_id):
        payload = {"channel": channel, "text": text, "blocks": blocks, "thread_ts": thread_ts,
                   "client_msg_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "raven-slack:" + event_id)), "unfurl_links": False}
        return self._call("chat.postMessage", payload)["ts"]

    def search_context(self, query, action_token):
        if not action_token:
            return []
        body = self._call("assistant.search.context", {"query": query, "action_token": action_token,
                         "channel_types": ["public_channel"], "content_types": ["messages"],
                         "include_bots": False, "include_context_messages": True})
        messages = (body.get('results') or {}).get('messages') or []
        return [{"author": m.get('author_user_id',''), "text": m.get('content',''),
                 "url": m.get('permalink','')} for m in messages[:5] if not m.get('is_author_bot')]

    def lookup_by_email(self, email: str) -> str:
        try:
            return self._call("users.lookupByEmail", {"email": email})["user"]["id"]
        except RuntimeError:
            return ""

    def workspace_id(self) -> str:
        return self._call("auth.test", {}).get("team_id", "")

    def list_users(self) -> list[dict]:
        users, cursor, seen = [], "", set()
        while True:
            payload = {"limit": 200}
            if cursor:
                payload["cursor"] = cursor
            page = self._call("users.list", payload)
            users.extend(page.get("members") or [])
            cursor = (page.get("response_metadata") or {}).get("next_cursor", "").strip()
            if not cursor:
                return users
            if cursor in seen or len(seen) >= 500:
                raise RuntimeError("Slack directory pagination did not complete")
            seen.add(cursor)

    def user_info(self, user_id: str) -> dict:
        return self._call("users.info", {"user": user_id})["user"]


def verify_slack_signature(signing_secret: str, timestamp: str, body: bytes, signature: str,
                           now: float | None = None) -> bool:
    """Slack's v0 request signature, with the five minute replay window."""
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((now or time.time()) - ts) > 300:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _handon_scope(rest: str) -> dict:
    """The scope a Slack hand-on names after the person: `for docs` (a
    topic, optionally "decisions"), or `just this one` for no route.
    Empty when it names none, and Raven learns its default."""
    from .scopes import CATEGORIES
    words = (rest or "").lower()
    if re.search(r"\b(?:just|only) (?:this|for this)\b|\bthis (?:one|question) only\b|\bfor this (?:one|question)\b",
                 words):
        return {"scope_kind": "none"}
    m = re.search(r"\bfor (?:all )?([a-z]+)(?: decisions?| questions?)?\b", words)
    if m and m.group(1) in CATEGORIES:
        return {"scope_kind": "category", "scope": m.group(1)}
    return {}


def _named_mentions(graph, text: str) -> str:
    """Slack writes a mention as <@U123ABC>; whoever reads the text later
    in Raven, a person on the People page or an agent citing a record,
    needs the name."""
    def name(m):
        who = graph.find_person(m.group(1))
        return "@" + (who["name"] if who else (m.group(2) or m.group(1)))
    return re.sub(r"<@([A-Z0-9]+)(?:\|([^>]*))?>", name, text or "")


_CAPTURE_RE = re.compile(r"^\s*(?:<@[A-Z0-9]+>\s*)?(?:(?:bridge|raven)[,:]?\s*)?record\s*[:\-]\s*(.+)$", re.IGNORECASE | re.DOTALL)


def capture_record(delivery: Delivery, channel: str, ts: str, user_id: str, text: str, event_id: str = "") -> str:
    """A decision written in a channel, `record: <what was decided>`,
    becomes a record: evidence the ladder can cite with its Slack
    permalink as locator, never sign-off. Returns the acknowledgement."""
    graph = delivery.store.graph
    m = _CAPTURE_RE.match(text or "")
    if not m:
        return "To capture a decision, write `record: <what was decided>`; it becomes evidence Raven can cite, not sign-off."
    if event_id:
        if graph.db.execute("SELECT 1 FROM webhook_receipts WHERE id=?", (event_id,)).fetchone():
            return ""
        with graph.transaction():
            graph.db.execute("INSERT INTO webhook_receipts(id, channel, received_at, state, updated_at) "
                             "VALUES(?,?,?,'applied',?)", (event_id, delivery.channel, now_iso(), now_iso()))
    person = graph.find_person(user_id)
    body = _named_mentions(graph, m.group(1).strip())
    ref = f"{channel}:{ts}"
    record = delivery.store.add_record({
        "repo": graph.get_setting("slack_capture_repo") or "", "kind": "slack", "ref": ref,
        "title": body[:120], "body": body, "author": person["name"] if person else user_id,
        "url": f"https://slack.com/archives/{channel}/p{ts.replace('.', '')}", "created_at": now_iso()})
    return (f"Recorded as decision record slack {record['ref']}"
            + (f" by {person['name']}" if person else "") + "; Raven cites it as evidence, not as sign-off.")


class TeamsTransport:
    """Microsoft Teams through an incoming webhook: outbound only, one
    channel, the person addressed by name; replies come through the
    inbox, which every message links to."""

    name = "teams"
    supports_dm = False

    def __init__(self, webhook_url: str, opener=None):
        self.webhook_url = webhook_url
        self.opener = opener or urllib.request.urlopen

    def open_dm(self, user_id: str) -> str:
        raise RuntimeError("Teams incoming webhooks cannot open direct messages")

    def post_message(self, channel: str, text: str, blocks=None, thread_ts: str = "") -> str:
        markdown = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"**\1**", text)
        body = json.dumps({"text": markdown}).encode()
        req = urllib.request.Request(self.webhook_url, data=body, headers={"Content-Type": "application/json"},
                                     method="POST")
        with self.opener(req, timeout=20) as resp:
            resp.read()
        return "teams:" + uuid.uuid4().hex[:12]


def handle_slack_event(delivery: Delivery, event: dict) -> dict:
    """The Events API payload after its signature was verified: the URL
    challenge, a message in a thread Raven started, or a decision
    written in a channel for Raven to record."""
    if event.get("type") == "url_verification":
        return {"challenge": event.get("challenge", "")}
    if event.get("type") != "event_callback":
        return {}
    inner = event.get("event") or {}
    if inner.get("bot_id") or inner.get("subtype"):
        return {}
    from .slack_events import accepts_workspace
    if not accepts_workspace(delivery, event):
        return {"ok": True, "ignored": "workspace_mismatch"}
    text = inner.get("text") or ""
    channel = inner.get("channel", "")
    thread_ts = inner.get("thread_ts", "")
    eid = event.get("event_id", "")
    if _CAPTURE_RE.match(text) and not thread_ts:
        ack = capture_record(delivery, channel, inner.get("ts", ""), inner.get("user", ""), text, event_id=eid)
        delivery.inbox.ack(eid, channel, inner.get("ts", ""), ack)
        return {"ok": True, "captured": bool(ack and ack.startswith("Recorded"))}
    if inner.get("type") not in ("message", "app_mention"):
        return {}
    if not thread_ts:
        # A person replies in the DM composer rather than the thread.
        # Select only when exactly one actionable question exists in this DM.
        rows = delivery.store.graph.db.execute("SELECT n.* FROM notifications n JOIN decisions d ON d.id=n.decision_id "
            "WHERE n.external_ref LIKE ? AND (d.status='pending' OR d.signoff='required') ORDER BY n.sent_at DESC",
            (channel + ':%',)).fetchall()
        choices = {r['decision_id']: dict(r) for r in rows}
        if len(choices) == 1:
            thread_ts = next(iter(choices.values()))['external_ref'].split(':',1)[1]
        else:
            lines = ["Reply in the thread of the question you want to discuss." if choices else
                     "I help your coding agent get decisions from the right people. Start a task in your agent; I will bring its questions here. To capture an existing decision, write `record: <what was decided>`."]
            for did,n in list(choices.items())[:5]:
                d=delivery.store.get_decision(did)
                ts=n['external_ref'].split(':',1)[1]
                lines.append(f"<https://slack.com/archives/{channel}/p{ts.replace('.', '')}|{d['question']}>")
            delivery.inbox.ack(eid,channel,inner.get('ts',''), '\n'.join(lines))
            return {"ok":True}
    reply = delivery.receive(channel, thread_ts, inner.get("user", ""), text,
                             event_id=eid, action_token=inner.get('action_token',''))
    if not reply and eid:
        receipt=delivery.store.graph.db.execute("SELECT reply FROM webhook_receipts WHERE id=? AND state='applied'",(eid,)).fetchone()
        reply=receipt['reply'] if receipt else ''
    delivery.inbox.ack(eid,channel,thread_ts,reply)
    return {"ok": True}
