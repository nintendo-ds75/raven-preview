"""Additive execution storage; shared with the inbox's answer transaction."""

import json
import uuid

from .store import now


def migrate(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS executions (
            run_id TEXT PRIMARY KEY REFERENCES runs(id), submission_key TEXT NOT NULL UNIQUE,
            task TEXT NOT NULL, config TEXT NOT NULL, session_id TEXT UNIQUE,
            launch_state TEXT NOT NULL, status TEXT NOT NULL, snapshot TEXT NOT NULL DEFAULT '{}',
            last_error TEXT, review_required INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS decision_revisions (
            id TEXT PRIMARY KEY, decision_id TEXT NOT NULL REFERENCES decisions(id),
            answer TEXT NOT NULL, rationale TEXT NOT NULL, responder TEXT NOT NULL,
            provenance TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS provider_calls (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES executions(run_id),
            session_id TEXT NOT NULL, turn_id TEXT NOT NULL, call_id TEXT NOT NULL,
            tool TEXT NOT NULL, arguments TEXT NOT NULL, decision_id TEXT REFERENCES decisions(id),
            UNIQUE(session_id,turn_id,call_id));
        CREATE TABLE IF NOT EXISTS deliveries (
            id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES provider_calls(id),
            revision_id TEXT REFERENCES decision_revisions(id), kind TEXT NOT NULL,
            payload TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT, acknowledgement TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS provider_events (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, payload TEXT NOT NULL,
            state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS revisions_decision ON decision_revisions(decision_id,created_at);
        CREATE INDEX IF NOT EXISTS deliveries_state ON deliveries(state,next_attempt);
    """)


def backfill(db):
    """Preserve the current reviewed answer for databases created before
    this migration. Runs inside Store's migration transaction."""
    for row in db.execute("SELECT * FROM decisions WHERE status='approved' AND NOT EXISTS "
                          "(SELECT 1 FROM decision_revisions WHERE decision_id=decisions.id)").fetchall():
        db.execute("INSERT INTO decision_revisions(id,decision_id,answer,rationale,responder,provenance,created_at) VALUES(?,?,?,?,?,?,?)", (
            uuid.uuid4().hex, row["id"], row["answer"], row["rationale"] or "",
            row["answered_by"] or "Unknown", "recorded by local operator; migrated current answer", row["updated_at"]))


def revision(db, decision_id):
    """The current signed revision of a decision; a duplicate stub reads
    through to the decision it points at, whose answer settles both."""
    seen = set()
    while decision_id and decision_id not in seen:
        seen.add(decision_id)
        stub = db.execute("SELECT status, superseded_by FROM decisions WHERE id=?", (decision_id,)).fetchone()
        if stub and stub["status"] == "duplicate" and stub["superseded_by"]:
            decision_id = stub["superseded_by"]
            continue
        break
    row = db.execute("SELECT * FROM decision_revisions WHERE decision_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                     (decision_id,)).fetchone()
    return dict(row) if row else None


def enqueue(db, call_id, payload, revision_id=None, kind="result"):
    timestamp = now()
    key = uuid.uuid4().hex
    db.execute("INSERT INTO deliveries(id,call_id,revision_id,kind,payload,state,created_at,updated_at) "
               "VALUES(?,?,?,?,?,'queued',?,?)", (key, call_id, revision_id, kind, json.dumps(payload), timestamp, timestamp))
    return key


def record_answer(db, decision, answer, rationale, provenance="recorded by local operator"):
    """The signed revision of a decision, and its delivery to every
    managed agent waiting on it: the calls that asked this decision, and
    the calls whose node is a duplicate of it (one answer settles both)."""
    from .agents_api import result_event
    revision_id = uuid.uuid4().hex
    db.execute("INSERT INTO decision_revisions(id,decision_id,answer,rationale,responder,provenance,created_at) VALUES(?,?,?,?,?,?,?)", (
        revision_id, decision["id"], answer, rationale, decision["owner_name"], provenance, now()))
    reviewed = revision(db, decision["id"])
    calls = db.execute("SELECT c.* FROM provider_calls c LEFT JOIN decisions d ON d.id=c.decision_id "
                       "WHERE c.decision_id=? OR (d.status='duplicate' AND d.superseded_by=?)",
                       (decision["id"], decision["id"])).fetchall()
    for call in calls:
        sent = db.execute("SELECT 1 FROM deliveries WHERE call_id=? AND state IN ('sending','uncertain','delivered')",
                          (call["id"],)).fetchone()
        db.execute("UPDATE deliveries SET state='superseded',updated_at=? WHERE call_id=? AND state='queued'",
                   (now(), call["id"]))
        if sent:
            # Delivery might race this correction: preserve exactly what was sent.
            db.execute("UPDATE executions SET review_required=1,updated_at=? WHERE run_id=?", (now(), call["run_id"]))
            text = "Bridge correction: recheck affected work using this reviewed revision. " + json.dumps(reviewed, sort_keys=True)
            payload = {"type": "agent.session.input.message", "input": [{"role": "user",
                       "content": [{"type": "input_text", "text": text}]}]}
            enqueue(db, call["id"], payload, revision_id, "correction")
        else:
            payload = result_event({"type": "function_call", "turn_id": call["turn_id"], "call_id": call["call_id"]}, reviewed)
            enqueue(db, call["id"], payload, revision_id)
    return revision_id


def state(db, full_history=False):
    executions = []
    for row in db.execute("SELECT * FROM executions ORDER BY updated_at DESC"):
        item = dict(row)
        item["config"] = json.loads(item["config"])
        if not full_history:
            item["config"] = {k: v for k, v in item["config"].items() if k not in ("environment", "instructions")}
        item["snapshot"] = json.loads(item["snapshot"])
        executions.append(item)
    return {"executions": executions,
            "deliveries": [dict(row) for row in db.execute("SELECT d.*,c.run_id,c.decision_id FROM deliveries d "
                                                          "JOIN provider_calls c ON c.id=d.call_id ORDER BY d.created_at, d.id")],
            "revisions": [dict(row) for row in db.execute("SELECT * FROM decision_revisions")]}
