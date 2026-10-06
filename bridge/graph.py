"""The org graph and decision memory behind the resolution ladder.

Additive storage over the same SQLite file the inbox uses. The inbox's
tables keep their shape (owners, runs, decisions, events); this module
adds the graph the old brain needed (engineers, artifacts, intents,
ownership with validity windows, connector sources), the decision columns
the ladder writes (kind, category, source, evidence, owner_evidence,
superseded_by, supersedes, options, repo, embedding), and full-text
indexes (SQLite FTS5 with the porter tokenizer, when the build has it).

Graph opens one connection per thread in autocommit mode so reads always
see the latest commit from the inbox's own connections and nothing here
ever holds a write transaction across a model call.

Ledger events land in the inbox's events table (kind=event, detail=JSON).
"""

from __future__ import annotations

import json
import math
import sqlite3
import struct
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .llm import clip_marked

MATERIAL_WEIGHT_DELTA = 0.15
MEMO_ENTRIES = 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS engineers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL DEFAULT '',
    github_username TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(name, email));
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY, repo TEXT NOT NULL, path TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'file', touch_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(repo, path));
CREATE TABLE IF NOT EXISTS intents (
    id TEXT PRIMARY KEY, repo TEXT NOT NULL, kind TEXT NOT NULL,
    ref TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '', resolved INTEGER NOT NULL DEFAULT 1,
    UNIQUE(repo, kind, ref));
CREATE TABLE IF NOT EXISTS ownership (
    repo TEXT NOT NULL, path_prefix TEXT NOT NULL, engineer TEXT NOT NULL,
    source TEXT NOT NULL, weight REAL NOT NULL DEFAULT 0,
    evidence TEXT NOT NULL DEFAULT '', valid_from REAL, valid_to REAL);
CREATE TABLE IF NOT EXISTS connector_sources (
    repo TEXT NOT NULL, kind TEXT NOT NULL, locator TEXT NOT NULL,
    PRIMARY KEY (repo, kind));
CREATE INDEX IF NOT EXISTS intents_repo ON intents(repo, created_at);
CREATE INDEX IF NOT EXISTS ownership_repo ON ownership(repo, valid_to);
CREATE TABLE IF NOT EXISTS changes (
    repo TEXT NOT NULL, sha TEXT NOT NULL, ts TEXT NOT NULL DEFAULT '',
    nfiles INTEGER NOT NULL DEFAULT 0, subject TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (repo, sha));
CREATE TABLE IF NOT EXISTS change_paths (
    repo TEXT NOT NULL, sha TEXT NOT NULL, path TEXT NOT NULL,
    PRIMARY KEY (repo, sha, path));
CREATE INDEX IF NOT EXISTS change_paths_path ON change_paths(repo, path);
CREATE TABLE IF NOT EXISTS change_people (
    repo TEXT NOT NULL, sha TEXT NOT NULL, engineer TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '', role TEXT NOT NULL,
    PRIMARY KEY (repo, sha, engineer, role));
CREATE INDEX IF NOT EXISTS change_people_eng ON change_people(repo, engineer);
CREATE TABLE IF NOT EXISTS listings (
    repo TEXT NOT NULL, kind TEXT NOT NULL, pattern TEXT NOT NULL, person TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT 'owner', section TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (repo, kind, pattern, person, role));
CREATE TABLE IF NOT EXISTS intent_paths (
    repo TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL, path TEXT NOT NULL,
    PRIMARY KEY (repo, kind, ref, path));
CREATE INDEX IF NOT EXISTS intent_paths_path ON intent_paths(repo, path);
CREATE TABLE IF NOT EXISTS fetched (
    repo TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, fetched_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (repo, kind, key));
CREATE TABLE IF NOT EXISTS model_cache (
    repo TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (repo, kind, key));
CREATE TABLE IF NOT EXISTS blame_lines (
    repo TEXT NOT NULL, rev TEXT NOT NULL, path TEXT NOT NULL, engineer TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '', lines INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, rev, path, engineer));
CREATE TABLE IF NOT EXISTS decision_links (
    decision_id TEXT NOT NULL, related_id TEXT NOT NULL, kind TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (decision_id, related_id, kind));
CREATE INDEX IF NOT EXISTS decision_links_related ON decision_links(related_id);
CREATE TABLE IF NOT EXISTS people (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL DEFAULT '',
    github_login TEXT NOT NULL DEFAULT '', slack_id TEXT NOT NULL DEFAULT '',
    aliases TEXT NOT NULL DEFAULT '[]', team TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT 'member',
    active INTEGER NOT NULL DEFAULT 1, source TEXT NOT NULL DEFAULT 'config',
    created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '');
CREATE UNIQUE INDEX IF NOT EXISTS people_email ON people(email) WHERE email != '';
CREATE UNIQUE INDEX IF NOT EXISTS people_github ON people(github_login) WHERE github_login != '';
CREATE UNIQUE INDEX IF NOT EXISTS people_slack ON people(slack_id) WHERE slack_id != '';
CREATE TABLE IF NOT EXISTS teams (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, handle TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'config', created_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS account_passwords (
    person_id TEXT PRIMARY KEY, password_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS account_invites (
    id TEXT PRIMARY KEY, email TEXT NOT NULL, role TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE, expires_at INTEGER NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0, created_by TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS teams_handle ON teams(handle) WHERE handle != '';
CREATE TABLE IF NOT EXISTS team_members (
    team_id TEXT NOT NULL, person_id TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'config',
    PRIMARY KEY (team_id, person_id));
CREATE TABLE IF NOT EXISTS authority (
    id TEXT PRIMARY KEY, repo TEXT NOT NULL DEFAULT '', person_id TEXT NOT NULL DEFAULT '',
    team_id TEXT NOT NULL DEFAULT '', scope_kind TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'config', asserted_by TEXT NOT NULL DEFAULT '',
    accepted INTEGER NOT NULL DEFAULT 1, note TEXT NOT NULL DEFAULT '',
    effective_from TEXT NOT NULL DEFAULT '', effective_to TEXT NOT NULL DEFAULT '',
    ended_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS authority_repo ON authority(repo, ended_at);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '');
-- A client_ref is an agent's own name for one node, and its promise is
-- that writing it twice writes one node. The claim is taken before the
-- ladder runs and carries the decision id once there is one, so a retry
-- that overlaps the first write waits for it instead of making a second.
CREATE TABLE IF NOT EXISTS node_claims (
    run_id TEXT NOT NULL, client_ref TEXT NOT NULL, decision_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, client_ref));
CREATE TABLE IF NOT EXISTS api_tokens (
    id TEXT PRIMARY KEY, person_id TEXT NOT NULL, label TEXT NOT NULL DEFAULT '',
    token_hash TEXT NOT NULL UNIQUE, created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '', last_used_at TEXT NOT NULL DEFAULT '',
    revoked_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS gh_pulls (
    repo TEXT NOT NULL, number INTEGER NOT NULL, merge_sha TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '',
    merged_by TEXT NOT NULL DEFAULT '', merged_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
    files TEXT NOT NULL DEFAULT '[]', reviews TEXT NOT NULL DEFAULT '[]', truncated INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (repo, number));
CREATE TABLE IF NOT EXISTS gh_users (
    login TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS sync_state (
    repo TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'github', cursor TEXT NOT NULL DEFAULT '',
    last_attempt_at TEXT NOT NULL DEFAULT '', last_success_at TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '', stats TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (repo, source));
"""

AUTHORITY_ROLES = ("knows", "decides", "approves")
AUTHORITY_SCOPES = ("path", "category", "repo")

_DECISION_COLUMNS = [
    ("model_pending", "INTEGER NOT NULL DEFAULT 0"),
    ("category", "TEXT NOT NULL DEFAULT ''"),
    ("source", "TEXT NOT NULL DEFAULT ''"),
    ("evidence", "TEXT NOT NULL DEFAULT ''"),
    ("owner_evidence", "TEXT NOT NULL DEFAULT ''"),
    ("superseded_by", "TEXT NOT NULL DEFAULT ''"),
    ("supersedes", "TEXT NOT NULL DEFAULT ''"),
    ("options", "TEXT NOT NULL DEFAULT ''"),
    ("repo", "TEXT NOT NULL DEFAULT ''"),
    ("kind", "TEXT NOT NULL DEFAULT ''"),
    ("embedding", "BLOB"),
    # The canvas: a decision is a node of its task's tree.
    ("parent_id", "TEXT NOT NULL DEFAULT ''"),
    ("client_ref", "TEXT NOT NULL DEFAULT ''"),
    ("depth", "INTEGER NOT NULL DEFAULT 0"),
    ("origin", "TEXT NOT NULL DEFAULT ''"),
    ("signoff", "TEXT NOT NULL DEFAULT ''"),
    ("signed_by", "TEXT NOT NULL DEFAULT ''"),
    ("brief", "TEXT NOT NULL DEFAULT ''"),
    # The decision contract: where a derived answer came from and at which
    # revision, whether a correction upstream has put it in doubt, the
    # exact revision a signer signed, and the scope that identifies the
    # decision beyond its question text.
    ("source_revision", "TEXT NOT NULL DEFAULT ''"),
    ("needs_review", "INTEGER NOT NULL DEFAULT 0"),
    ("review_reason", "TEXT NOT NULL DEFAULT ''"),
    ("signed_revision", "TEXT NOT NULL DEFAULT ''"),
    # A reusable rule: a signed answer its owner declared applicable to
    # matching questions without a fresh signature, with conditions
    # (phrases the question or context must contain) and an expiry.
    ("reusable", "INTEGER NOT NULL DEFAULT 0"),
    ("rule_conditions", "TEXT NOT NULL DEFAULT ''"),
    ("rule_expires", "TEXT NOT NULL DEFAULT ''"),
    ("rule_by", "TEXT NOT NULL DEFAULT ''"),
    ("rule_at", "TEXT NOT NULL DEFAULT ''"),
    # A rule applies in the scope it was decided in ('same') unless its
    # owner said it applies anywhere ('any'); and the moment it ended.
    ("rule_scope", "TEXT NOT NULL DEFAULT ''"),
    ("rule_ended_at", "TEXT NOT NULL DEFAULT ''"),
    # Several required approvers on one decision: who must sign (from the
    # authority map's 'approves' rows) and who has, so far.
    ("required_signers", "TEXT NOT NULL DEFAULT ''"),
    ("signatures", "TEXT NOT NULL DEFAULT ''"),
    # Who actually acted last (by stable id) and on what standing:
    # owner, required-signer, authority, coordinator, admin-override,
    # operator. The assigned owner stays in owner_id; the actor is not
    # assumed to be them.
    ("actor_id", "TEXT NOT NULL DEFAULT ''"),
    ("actor_name", "TEXT NOT NULL DEFAULT ''"),
    ("actor_basis", "TEXT NOT NULL DEFAULT ''"),
    # The facts the agent stated about this decision (key=value), the
    # only thing a structured rule condition is checked against.
    ("facts", "TEXT NOT NULL DEFAULT ''"),
    # Human-declared applicability of this answer. Unlike prose in the
    # rationale, these conditions can be checked before memory reuse.
    ("applicability", "TEXT NOT NULL DEFAULT ''"),
    # A node still being built: created, not yet routed and not yet given
    # its place on the tree. No reader sees one, so nobody reads a
    # decision that has no owner yet only because the write is in flight.
    ("draft", "INTEGER NOT NULL DEFAULT 0"),
    ("signed_hash", "TEXT NOT NULL DEFAULT ''"),
    ("scope_key", "TEXT NOT NULL DEFAULT ''"),
    # Every path the decision is about, as JSON: its own, else its
    # parent's, else its task's, plus files its question names. Required
    # approvers and approval standing are read from all of them.
    ("scope_paths", "TEXT NOT NULL DEFAULT ''"),
    # A follow-up question the person who added it marked required: the
    # task cannot finish until the agent adopts it and it is answered.
    ("followup_required", "INTEGER NOT NULL DEFAULT 0"),
]

_RUN_COLUMNS = [
    ("goal", "TEXT NOT NULL DEFAULT ''"),
    ("requester", "TEXT NOT NULL DEFAULT ''"),
    ("paths", "TEXT NOT NULL DEFAULT ''"),
    ("verdict", "TEXT NOT NULL DEFAULT ''"),
    ("verdict_why", "TEXT NOT NULL DEFAULT ''"),
    ("discovery", "TEXT NOT NULL DEFAULT ''"),
    ("client_key", "TEXT NOT NULL DEFAULT ''"),
    ("needs_review", "INTEGER NOT NULL DEFAULT 0"),
    # When the agent last read the tree over MCP: what people did after
    # that is news to it, and the finish waits until it has read it.
    ("agent_read_at", "TEXT NOT NULL DEFAULT ''"),
    # Per-decision receipts for committed human events. Timestamps can tie and
    # PostgreSQL sequence IDs can commit out of order; retain both count and
    # greatest ID. Legacy/imported tasks require a fresh complete read.
    ("agent_read_events", "TEXT NOT NULL DEFAULT '{}'"),
    # What holds for the whole task (release, customer), as JSON: every
    # node inherits it unless it states its own value.
    ("facts", "TEXT NOT NULL DEFAULT ''"),
]

_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS decisions_fts USING fts5(
    id UNINDEXED, question, context, answer, rationale,
    tokenize='porter unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS intents_fts USING fts5(
    id UNINDEXED, title, body, tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS decisions_fts_ai AFTER INSERT ON decisions BEGIN
    INSERT INTO decisions_fts(id, question, context, answer, rationale)
    VALUES (new.id, new.question, new.context, coalesce(new.answer,''), coalesce(new.rationale,''));
END;
CREATE TRIGGER IF NOT EXISTS decisions_fts_au AFTER UPDATE ON decisions BEGIN
    DELETE FROM decisions_fts WHERE id = old.id;
    INSERT INTO decisions_fts(id, question, context, answer, rationale)
    VALUES (new.id, new.question, new.context, coalesce(new.answer,''), coalesce(new.rationale,''));
END;
CREATE TRIGGER IF NOT EXISTS decisions_fts_ad AFTER DELETE ON decisions BEGIN
    DELETE FROM decisions_fts WHERE id = old.id;
END;
CREATE TRIGGER IF NOT EXISTS intents_fts_ai AFTER INSERT ON intents BEGIN
    INSERT INTO intents_fts(id, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS intents_fts_au AFTER UPDATE ON intents BEGIN
    DELETE FROM intents_fts WHERE id = old.id;
    INSERT INTO intents_fts(id, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS intents_fts_ad AFTER DELETE ON intents BEGIN
    DELETE FROM intents_fts WHERE id = old.id;
END;
"""


_FTS_SUPPORTED: bool | None = None


def fts_available(db: sqlite3.Connection | None = None) -> bool:
    """Does this SQLite build have FTS5 with the porter tokenizer? Probed
    once, in a throwaway in-memory database, never by writing to the
    store: a write here would queue behind the inbox's own transaction."""
    if getattr(db, "dialect", "") == "postgres":
        return False  # PostgreSQL uses its own generated tsvector indexes.
    global _FTS_SUPPORTED
    if _FTS_SUPPORTED is None:
        try:
            probe = sqlite3.connect(":memory:")
            probe.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='porter unicode61')")
            probe.close()
            _FTS_SUPPORTED = True
        except sqlite3.OperationalError:
            _FTS_SUPPORTED = False
    return _FTS_SUPPORTED


def migrate(db: sqlite3.Connection) -> None:
    """The additive schema, safe to run on every open. executescript
    commits whatever transaction is open, so Store runs this before it
    opens the migration transaction that backfill() runs inside."""
    db.executescript(_SCHEMA)
    from .routing_memory import migrate as migrate_routes
    migrate_routes(db)
    if fts_available(db):
        db.executescript(_FTS)


def backfill(db: sqlite3.Connection) -> None:
    """The columns and rows older databases lack. Runs inside Store's
    migration transaction so two processes opening at once (the inbox
    and an MCP server) migrate serially."""
    cols = {r["name"] for r in db.execute("PRAGMA table_info(decisions)")}
    for name, decl in _DECISION_COLUMNS:
        if name not in cols:
            db.execute(f"ALTER TABLE decisions ADD COLUMN {name} {decl}")
    run_cols = {r["name"] for r in db.execute("PRAGMA table_info(runs)")}
    for name, decl in _RUN_COLUMNS:
        if name not in run_cols:
            db.execute(f"ALTER TABLE runs ADD COLUMN {name} {decl}")
    # A listing's position in its file: CODEOWNERS is last-rule-wins.
    if "ord" not in {r["name"] for r in db.execute("PRAGMA table_info(listings)")}:
        db.execute("ALTER TABLE listings ADD COLUMN ord INTEGER NOT NULL DEFAULT 0")
    # An inbox owner is a person; the link lets the two tables agree.
    if "person_id" not in {r["name"] for r in db.execute("PRAGMA table_info(owners)")}:
        db.execute("ALTER TABLE owners ADD COLUMN person_id TEXT NOT NULL DEFAULT ''")
    # GitHub's immutable user id binds a sign-in to a person; a login can
    # be renamed or reused, an id cannot.
    if "github_id" not in {r["name"] for r in db.execute("PRAGMA table_info(people)")}:
        db.execute("ALTER TABLE people ADD COLUMN github_id TEXT NOT NULL DEFAULT ''")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS people_github_id ON people(github_id) WHERE github_id != ''")
    # A token is an agent credential unless it was minted for a person's
    # own scripts; agents write nodes, they never decide.
    if "kind" not in {r["name"] for r in db.execute("PRAGMA table_info(api_tokens)")}:
        db.execute("ALTER TABLE api_tokens ADD COLUMN kind TEXT NOT NULL DEFAULT 'agent'")
    db.execute("CREATE INDEX IF NOT EXISTS decisions_parent ON decisions(run_id, parent_id)")
    db.execute("CREATE INDEX IF NOT EXISTS decisions_parent_lookup ON decisions(parent_id)")
    db.execute("CREATE INDEX IF NOT EXISTS decisions_source ON decisions(source_id)")
    db.execute("CREATE INDEX IF NOT EXISTS decisions_status ON decisions(status, signoff, needs_review)")
    db.execute("CREATE INDEX IF NOT EXISTS decisions_rule_source ON decisions(signoff, source_id)")
    # No decision scan on ordinary tree reads in workspaces that never used
    # rules. Backfill once for existing databases; rule creation sets it too.
    marker = db.execute("SELECT value FROM settings WHERE key='rule_checks_needed'").fetchone()
    if marker is None or marker["value"] != "1":
        exists = db.execute("SELECT 1 FROM decisions WHERE reusable=1 OR signoff='rule' LIMIT 1").fetchone()
        if exists:
            db.execute("INSERT INTO settings(key,value,updated_at) VALUES('rule_checks_needed','1',?) "
                       "ON CONFLICT(key) DO UPDATE SET value='1',updated_at=excluded.updated_at", (now_iso(),))
        elif marker is not None:
            # An absent marker already means no rules. Do not populate an
            # empty import destination, or retain a pre-import negative cache
            # that could hide legacy rule rows copied in afterward.
            db.execute("DELETE FROM settings WHERE key='rule_checks_needed'")
    db.execute("CREATE INDEX IF NOT EXISTS decisions_draft ON decisions(draft) WHERE draft=1")
    # A kickoff retried with the same client key is one task, atomically.
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS runs_client_key ON runs(client_key) WHERE client_key != ''")
    # The inbox's own tables: events is read per decision and per task,
    # ownership is looked up by its full key on every upsert.
    db.execute("CREATE INDEX IF NOT EXISTS events_decision ON events(decision_id, id)")
    db.execute("CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id)")
    db.execute("CREATE INDEX IF NOT EXISTS ownership_key "
               "ON ownership(repo, path_prefix, engineer, source, valid_to)")
    if fts_available(db):
        # Backfill rows created before the index existed.
        missing = db.execute(
            "SELECT count(*) c FROM decisions WHERE id NOT IN (SELECT id FROM decisions_fts)").fetchone()["c"]
        if missing:
            db.execute("INSERT INTO decisions_fts(id, question, context, answer, rationale) "
                       "SELECT id, question, context, coalesce(answer,''), coalesce(rationale,'') "
                       "FROM decisions WHERE id NOT IN (SELECT id FROM decisions_fts)")
        missing = db.execute(
            "SELECT count(*) c FROM intents WHERE id NOT IN (SELECT id FROM intents_fts)").fetchone()["c"]
        if missing:
            db.execute("INSERT INTO intents_fts(id, title, body) SELECT id, title, body FROM intents "
                       "WHERE id NOT IN (SELECT id FROM intents_fts)")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_to_ts(value) -> float:
    """ISO timestamp (the inbox's format) to epoch seconds; empty or
    unparseable is 0 (oldest)."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).strip()).timestamp()
    except ValueError:
        return 0.0


def is_team_handle(name: str) -> bool:
    """Does this read as a team rather than a person: the org/team shape
    CODEOWNERS uses, with or without its at-sign."""
    text = (name or "").strip().lstrip("@")
    if "/" not in text or " " in text:
        return False
    org, _, team = text.partition("/")
    return bool(org and team and "." not in org)


def ts_to_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _f32blob(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unblob(b: bytes) -> list[float]:
    n = len(b) // 4
    return list(struct.unpack(f"{n}f", b))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# Statuses. The inbox's two ('pending', 'approved') plus the ladder's.
STATUS_OPEN = "pending"       # needs a human
STATUS_ANSWERED = "approved"  # a human signed it
MEMORY_STATUSES = ("approved", "resolved")
# A memory this size or smaller is scanned whole on every question; above
# it one pass scores a bounded candidate set (FTS5 matches plus the
# newest rows) so a question's cost stops growing with the memory.
FULL_SCAN_MAX = 2000
CANDIDATE_FTS = 600
CANDIDATE_RECENT = 500
RECENT_STATUSES = ("approved", "resolved", "partial")
# Statuses that carry an answer a person has not signed: evidence or a
# prediction, never authorization. A finish is refused while a task
# holds one, and an answer derived from one is a prediction.
UNSIGNED_STATUSES = ("resolved", "partial", "assumed", "proposed")
# SQL for "a person authorized this row": the inbox recorded a human
# answer, or a person signed the row (or a reusable rule covers it).
AUTHORIZED_SQL = "(d.status = 'approved' OR d.signoff IN ('signed', 'rule'))"
# What memory is made of: a person's own answer or signature on this
# very decision. A row a rule authorized is the rule's, not a fresh
# human answer, and never becomes memory in its own right: reuse would
# carry the rule's authorization into questions the rule's conditions
# never covered, under the owner's name.
PERSONALLY_SIGNED_SQL = "(d.status = 'approved' OR d.signoff = 'signed')"
# SQL for "this row waits on a person": an open question, an unsigned
# answer, or a row put in doubt by a correction upstream.
BLOCKING_SQL = ("(d.status = 'pending' OR d.needs_review = 1 OR "
                f"(d.status IN {UNSIGNED_STATUSES!r} AND NOT {AUTHORIZED_SQL}))")


def authorized(row) -> bool:
    """A person stands behind this row's answer: the inbox recorded a human
    answer, or someone signed it, or a reusable rule covers it."""
    keys = row.keys() if hasattr(row, "keys") else ()
    status = row["status"] if "status" in keys else ""
    signoff = (row["signoff"] if "signoff" in keys else "") or ""
    return status == "approved" or signoff in ("signed", "rule")


def still_to_sign(required_raw, signatures_raw, owner: str = "") -> list[str]:
    """Who must still sign: every required approver without a signature,
    and the owner when nobody has signed at all."""
    def names(raw, key=None):
        try:
            items = json.loads(raw or "[]")
        except (ValueError, TypeError):
            return []
        return [str(x.get(key) if key and isinstance(x, dict) else x) for x in items if x]
    required = names(required_raw)
    signed = {n.lower() for n in names(signatures_raw, "by")}
    missing = [n for n in required if n.lower() not in signed]
    if owner and not signed and owner.lower() not in {m.lower() for m in missing}:
        missing.insert(0, owner)
    return missing


def blocks_finish(row) -> bool:
    """This row waits on a person, so its task cannot finish."""
    keys = row.keys() if hasattr(row, "keys") else ()
    status = row["status"] if "status" in keys else ""
    if status == "pending" or ("model_pending" in keys and row["model_pending"]):
        return True
    if "needs_review" in keys and row["needs_review"]:
        return True
    if status == "suggested":
        # A follow-up a person marked required waits on the agent to take
        # it up; an optional one never holds the task.
        return bool("followup_required" in keys and row["followup_required"])
    return status in UNSIGNED_STATUSES and not authorized(row)


@dataclass
class Decision:
    id: str
    task_id: str
    question: str
    category: str
    status: str
    source: str
    answer: str
    answered_by: str
    evidence: str
    owner: str
    owner_evidence: str
    created_at: float
    updated_at: float
    superseded_by: str = ""
    options: list[str] = field(default_factory=list)
    repo: str = ""
    rationale: str = ""
    context: str = ""
    path: str = ""
    kind: str = ""
    owner_id: str = ""
    prediction: str = ""
    supersedes: str = ""
    parent_id: str = ""
    client_ref: str = ""
    depth: int = 0
    origin: str = ""
    signoff: str = ""
    signed_by: str = ""
    source_id: str = ""
    source_revision: str = ""
    needs_review: bool = False
    review_reason: str = ""
    signed_revision: str = ""
    scope_key: str = ""
    reusable: bool = False
    rule_conditions: str = ""
    rule_expires: str = ""
    rule_by: str = ""
    rule_scope: str = ""
    applicability: dict = field(default_factory=dict)

    @property
    def authorized(self) -> bool:
        """A person stands behind this answer: a human answer recorded by
        the inbox, a signature, or a reusable rule."""
        return self.status == "approved" or self.signoff in ("signed", "rule")

    @property
    def blocking(self) -> bool:
        """This decision waits on a person."""
        if self.status == "pending" or self.needs_review:
            return True
        return self.status in UNSIGNED_STATUSES and not self.authorized


def _load_options(raw) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    return [str(o) for o in parsed] if isinstance(parsed, list) else []


def _row_to_decision(row: sqlite3.Row) -> Decision:
    keys = row.keys()
    superseded = row["superseded_by"] or ""
    return Decision(
        id=row["id"], task_id=row["run_id"], question=row["question"],
        category=row["category"] or "", status=row["status"], source=row["source"] or "",
        answer=row["answer"] or "", answered_by=row["answered_by"] or "",
        evidence=row["evidence"] or "",
        owner=(row["owner_name"] if "owner_name" in keys else "") or "",
        owner_evidence=row["owner_evidence"] or "",
        created_at=iso_to_ts(row["created_at"]), updated_at=iso_to_ts(row["updated_at"]),
        superseded_by="" if superseded == "pending" else superseded,
        options=_load_options(row["options"]), repo=row["repo"] or "",
        rationale=row["rationale"] or "", context=row["context"] or "",
        path=row["path"] or "", kind=row["kind"] or "",
        owner_id=row["owner_id"] or "", prediction=row["prediction"] or "",
        supersedes=row["supersedes"] or "",
        parent_id=(row["parent_id"] if "parent_id" in keys else "") or "",
        client_ref=(row["client_ref"] if "client_ref" in keys else "") or "",
        depth=int((row["depth"] if "depth" in keys else 0) or 0),
        origin=(row["origin"] if "origin" in keys else "") or "",
        signoff=(row["signoff"] if "signoff" in keys else "") or "",
        signed_by=(row["signed_by"] if "signed_by" in keys else "") or "",
        source_id=(row["source_id"] if "source_id" in keys else "") or "",
        source_revision=(row["source_revision"] if "source_revision" in keys else "") or "",
        needs_review=bool((row["needs_review"] if "needs_review" in keys else 0) or 0),
        review_reason=(row["review_reason"] if "review_reason" in keys else "") or "",
        signed_revision=(row["signed_revision"] if "signed_revision" in keys else "") or "",
        scope_key=(row["scope_key"] if "scope_key" in keys else "") or "",
        reusable=bool((row["reusable"] if "reusable" in keys else 0) or 0),
        rule_conditions=(row["rule_conditions"] if "rule_conditions" in keys else "") or "",
        rule_expires=(row["rule_expires"] if "rule_expires" in keys else "") or "",
        rule_by=(row["rule_by"] if "rule_by" in keys else "") or "",
        rule_scope=(row["rule_scope"] if "rule_scope" in keys else "") or "",
        applicability=parse_applicability((row["applicability"] if "applicability" in keys else "") or ""),
    )


def parse_applicability(raw) -> dict:
    """Normalize a human's reusable-answer boundaries; reject malformed input.

    Facts are exact, case-insensitive key/value assertions supplied by the
    agent on the later question. Missing facts never satisfy a condition.
    Paths are repository-relative file or directory prefixes.
    """
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise ValueError("applicability must be a JSON object") from exc
    if not isinstance(raw, dict) or set(raw) - {"requires", "excludes", "paths", "valid_until"}:
        raise ValueError("applicability supports requires, excludes, paths, and valid_until")
    out: dict = {}
    for key in ("requires", "excludes"):
        values = raw.get(key) or {}
        if not isinstance(values, dict) or len(values) > 20:
            raise ValueError(f"applicability.{key} must be an object of at most 20 facts")
        normalized = {}
        for name, value in values.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(value, str) or not value.strip():
                raise ValueError(f"applicability.{key} needs nonempty string keys and values")
            if len(name) > 60 or len(value) > 200:
                raise ValueError(f"applicability.{key} fact is too long")
            normalized[name.strip().lower()] = " ".join(value.lower().split())
        if normalized:
            out[key] = normalized
    paths = raw.get("paths") or []
    if not isinstance(paths, list) or len(paths) > 20:
        raise ValueError("applicability.paths must be a list of at most 20 paths")
    clean_paths = []
    for path in paths:
        if not isinstance(path, str) or not path.strip() or len(path) > 500 or path.startswith("/") or ".." in path.split("/"):
            raise ValueError("applicability paths must be repository-relative")
        clean_paths.append(path.strip().removeprefix("./"))
    if clean_paths:
        out["paths"] = clean_paths
    until = raw.get("valid_until") or ""
    if until:
        if not isinstance(until, str):
            raise ValueError("applicability.valid_until must be an ISO date")
        try:
            datetime.fromisoformat(until.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("applicability.valid_until must be an ISO date") from exc
        out["valid_until"] = until
    return out


def applicability_status(d: "Decision", path: str = "", facts: dict | None = None,
                         now: float | None = None) -> tuple[bool, str]:
    """A declared boundary must be provably satisfied before answer reuse."""
    spec = d.applicability
    if not spec:
        return True, "no structured applicability declared; fresh sign-off remains required"
    until = spec.get("valid_until")
    if until:
        expiry = datetime.fromisoformat(until.replace("Z", "+00:00"))
        if len(until) == 10:
            # A date entered as "valid until" includes that calendar day.
            expiry += timedelta(days=1)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if (now if now is not None else time.time()) >= expiry.timestamp():
            return False, f"decision {d.id} expired on {until[:10]}"
    declared_paths = spec.get("paths") or []
    if declared_paths:
        current = (path or "").strip().lstrip("/")
        if not current:
            return False, f"decision {d.id} requires a path; none was stated"
        if not any(current == p.rstrip("/") or current.startswith(p.rstrip("/") + "/") for p in declared_paths):
            return False, f"decision {d.id} applies to {', '.join(declared_paths)}, not {current}"
    stated = {str(k).strip().lower(): " ".join(str(v).lower().split()) for k, v in (facts or {}).items()}
    for key, value in (spec.get("requires") or {}).items():
        if key not in stated:
            return False, f"decision {d.id} needs {key}={value}; the agent did not state {key}"
        if stated[key] != value:
            return False, f"decision {d.id} needs {key}={value}; this request states {key}={stated[key]}"
    for key, value in (spec.get("excludes") or {}).items():
        if stated.get(key) == value:
            return False, f"decision {d.id} excludes {key}={value}"
    return True, f"decision {d.id} matches its declared applicability"


def rule_conditions(text: str) -> list[str]:
    """The conditions a rule requires, one per line or semicolon: a
    `key=value` fact the agent must have stated, or a phrase the
    question or its context must carry (and not deny)."""
    import re as _re
    return [c.strip() for c in _re.split(r"[;\n]+", text or "") if c.strip()]


def parse_facts(raw) -> dict[str, str]:
    """`key=value` pairs, comma or newline separated (or a JSON object),
    keys lower-cased: what the agent states about a decision."""
    import re as _re
    if isinstance(raw, dict):
        items = raw.items()
    else:
        text = str(raw or "").strip()
        if text.startswith("{"):
            try:
                loaded = json.loads(text)
                items = loaded.items() if isinstance(loaded, dict) else []
            except ValueError:
                items = []
        else:
            items = []
            for part in _re.split(r"[,\n;]+", text):
                if "=" in part:
                    k, _, v = part.partition("=")
                    items.append((k, v))
    out: dict[str, str] = {}
    for k, v in items:
        k = str(k).strip().lower()
        if k:
            out[k[:60]] = " ".join(str(v).split())[:200]
    return out


_NEGATION_RE = r"\b(?:not|no|never|neither|nor|without|isn'?t|aren'?t|wasn'?t|weren'?t|don'?t|doesn'?t|didn'?t|can'?t|cannot|except|unless)\b"


def phrase_denied(phrase: str, text: str) -> bool:
    """Whether any mention of the phrase in the text sits inside a
    negation ('NOT on the enterprise plan'). One denial is enough: a text
    that both mentions a condition and denies it does not establish it,
    and a rule that authorizes on its own needs it established. Measured
    live on 5e967e4: the question asked about "the local sandbox policy",
    the context said "We are not in a local sandbox", and the rule
    authorized because the question's mention was not negated."""
    import re as _re
    low = text.lower()
    p = phrase.lower()
    for m in _re.finditer(_re.escape(p), low):
        window = low[max(0, m.start() - 40):m.start()]
        clause = _re.split(r"[.;!?]\s|,\s(?:but|and)\s", window)[-1]
        if _re.search(_NEGATION_RE, clause):
            return True
        # A condition can be denied after its mention as well as before it.
        # Do not interpret an unrelated later negation as denying the phrase.
        after = low[m.end():m.end() + 90]
        if _re.match(r"[\s\"')]*(?:(?:does\s+not|doesn't|doesn’t)\s+(?:hold|apply)|"
                     r"(?:is|was|are)\s+(?:false|untrue|denied)|"
                     r"(?:is\s+not|isn't|isn’t|was\s+not)\s+(?:true|valid|met|satisfied|applicable|established)|"
                     r"(?:is|was)\s+no\s+longer\s+(?:true|valid|applicable))\b", after):
            return True
    return False


def rule_status(d: "Decision", question: str, context: str = "", facts: dict | None = None,
                now: float | None = None) -> tuple[bool, str]:
    """Whether a reusable rule covers this question here and now, and
    when not, why: ended, expired, a fact condition the agent did not
    state or stated otherwise, or a phrase the question and context do
    not carry or deny. A decision that is not a rule never applies.
    Free text can nominate a rule; only stated facts and undenied
    phrases satisfy its conditions."""
    if not d.reusable or not d.authorized:
        return False, ""
    if d.rule_expires:
        try:
            expires = datetime.fromisoformat(d.rule_expires.replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if (now or time.time()) >= expires.timestamp():
                return False, f"the rule from decision {d.id} expired on {d.rule_expires[:10]}"
        except ValueError:
            return False, f"the rule from decision {d.id} has an unreadable expiry ({d.rule_expires})"
    text = f"{question}\n{context}"
    facts = {k.lower(): v for k, v in (facts or {}).items()}
    for cond in rule_conditions(d.rule_conditions):
        if phrase_denied(cond, text):
            return False, (f"the rule from decision {d.id} does not apply: the question or its context denies "
                           f"\"{cond}\" somewhere, and a condition both stated and denied is not established")
        if "=" in cond and " " not in cond.split("=", 1)[0].strip():
            key, _, value = cond.partition("=")
            key, value = key.strip().lower(), " ".join(value.split()).lower()
            if key not in facts:
                return False, (f"the rule from decision {d.id} needs the fact {key}={value}, which the agent did not "
                               "state; a person decides")
            if facts[key].lower() != value:
                return False, (f"the rule from decision {d.id} needs {key}={value}; the agent stated "
                               f"{key}={facts[key]}")
            continue
        if cond.lower() not in text.lower():
            return False, f"the rule from decision {d.id} does not apply: the question does not mention \"{cond}\""
    return True, ""


_DECISION_SELECT = ("SELECT d.*, o.name AS owner_name FROM decisions d "
                    "LEFT JOIN owners o ON o.id = d.owner_id")


def _pattern_covers(pattern: str, path: str) -> bool:
    """A CODEOWNERS-style pattern against one repository path."""
    from .signals import _pattern_re
    try:
        rx = _pattern_re(pattern, "codeowners")
    except Exception:
        return False
    probe = (path or "").lstrip("/").rstrip("/")
    return bool(rx.match(probe) or rx.match(probe + "/x"))


class _Probe(dict):
    """A dict that answers keys() the way sqlite3.Row does, for the row
    helpers above."""

    def keys(self):  # type: ignore[override]
        return list(super().keys())


class Graph:
    """The ladder's view of the store: per-thread autocommit connections."""

    def __init__(self, path: str):
        self.path = str(path)
        self._local = threading.local()
        # What routing reads is memoized per repo generation: a counter
        # every writer of those tables bumps, kept on this object.
        self._generation: dict[str, int] = {}
        self._memo: OrderedDict[tuple, Any] = OrderedDict()
        self._memo_lock = threading.Lock()
        self._sentinel: sqlite3.Connection | None = None
        # The schema is migrated by Store on open; opening the graph itself
        # never writes, so it is safe to touch inside an inbox transaction.
        from .database import is_postgres
        self.postgres = is_postgres(self.path)
        self.has_fts = self.postgres or (fts_available() and bool(self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='decisions_fts'").fetchone()))

    @property
    def db(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            from .database import connect
            db = connect(self.path, autocommit=True)
            try:
                db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA foreign_keys=ON")
            self._local.db = db
        return db

    def close(self) -> None:
        """Close this thread's connection and the freshness sentinel. The
        last connection to go takes the WAL and shm files with it; a
        connection left open would keep them beside a file another
        process then overwrites, and the next open reads a torn view."""
        db = getattr(self._local, "db", None)
        if db is not None:
            db.close()
            self._local.db = None
        with self._memo_lock:
            if self._sentinel is not None:
                self._sentinel.close()
                self._sentinel = None

    def close_thread(self) -> None:
        """Close this thread's connection only, for a worker thread that
        is done; the shared sentinel stays open for the others."""
        db = getattr(self._local, "db", None)
        if db is not None:
            db.close()
            self._local.db = None

    @contextmanager
    def transaction(self):
        """One write transaction on this thread's connection: BEGIN
        IMMEDIATE, COMMIT on success, ROLLBACK on error. A no-op when the
        connection is already in a transaction, so the outer one governs.
        Never use it inside Store.connect: that is another connection,
        and the two would deadlock on the write lock."""
        db = self.db
        if db.in_transaction:
            yield db
            return
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            # A full disk or an I/O error can end the transaction on its
            # own; the rollback then has nothing to undo, and the error
            # that matters is the original one.
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")

    # ---------------- route-time memo ----------------

    def generation(self, repo: str) -> int:
        """How many times this object has written what routing reads for
        the repo (the tree, the records and their paths, the listings,
        the history, ownership); repo '' counts every repo, for reads
        with no scope. Each memo entry below is keyed on it."""
        return self._generation.get(repo, 0)

    def _bump(self, repo: str) -> None:
        for key in {repo, ""}:
            self._generation[key] = self._generation.get(key, 0) + 1

    def data_version(self) -> int:
        """SQLite's own change mark, read on one sentinel connection this
        object keeps for the purpose: it moves whenever any other
        connection commits (a request thread of the inbox, the MCP server
        beside it), so every thread shares one notion of freshness."""
        with self._memo_lock:
            if self._sentinel is None:
                self._sentinel = sqlite3.connect(self.path, timeout=30.0, isolation_level=None,
                                                 check_same_thread=False)
                self._sentinel.execute("PRAGMA busy_timeout=30000")
            return self._sentinel.execute("PRAGMA data_version").fetchone()[0]

    def memo(self, repo: str, name: str, key_extra: Any, source: Callable[[], Any],
             derive: Callable[[Any], Any]) -> Any:
        """The value `derive` builds from the rows `source` reads, for
        (repo, name, key_extra) at the current generation, from a small
        LRU. An entry remembers the data version it was last checked at
        and a hash of its rows: after another connection commits, the
        rows are read again (cheap SQL) and the value is kept when they
        are the same, so a foreign write to these rows is never missed
        and the inbox's own writes to other tables never cost a rebuild."""
        if self.postgres:
            # SQLite data_version has no PG equivalent. Read current rows until
            # a commit-aware cache invalidation mechanism is introduced.
            return derive(source())
        key = (repo, name, self.generation(repo), key_extra)
        dv = self.data_version()
        with self._memo_lock:
            entry = self._memo.get(key)
            if entry is not None:
                self._memo.move_to_end(key)
                if entry["checked"] == dv:
                    return entry["value"]
        rows = source()
        mark = hash(tuple(rows))
        if entry is not None and entry["mark"] == mark:
            with self._memo_lock:
                entry["checked"] = dv
            return entry["value"]
        value = derive(rows)
        with self._memo_lock:
            self._memo[key] = {"value": value, "mark": mark, "checked": dv}
            self._memo.move_to_end(key)
            while len(self._memo) > MEMO_ENTRIES:
                self._memo.popitem(last=False)
        return value

    # ---------------- ledger (the inbox's events table) ----------------

    def append_event(self, event: str, payload: dict[str, Any]) -> str:
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        self.db.execute(
            "INSERT INTO events(decision_id, run_id, kind, detail, created_at) VALUES(?,?,?,?,?)",
            (payload.get("decision_id"), payload.get("task_id") or payload.get("run_id"),
             event, body, now_iso()))
        return event

    def count_events(self, event: str, task_id: str | None = None,
                     decision_id: str | None = None) -> int:
        sql, args = "SELECT count(*) c FROM events WHERE kind=?", [event]
        if decision_id:
            sql += " AND decision_id=?"
            args.append(decision_id)
        if task_id:
            sql += " AND run_id=?"
            args.append(task_id)
        return self.db.execute(sql, args).fetchone()["c"]

    # ---------------- engineers / artifacts / intents / ownership ----------------

    def upsert_engineer(self, name: str, email: str = "") -> None:
        self.db.execute("INSERT OR IGNORE INTO engineers(id, name, email) VALUES(?,?,?)",
                        (uuid.uuid4().hex, name, email))

    def upsert_artifact(self, repo: str, path: str, touch_count: int = 0) -> None:
        self.db.execute(
            """INSERT INTO artifacts(id, repo, path, touch_count) VALUES(?,?,?,?)
               ON CONFLICT(repo, path) DO UPDATE SET touch_count=excluded.touch_count""",
            (uuid.uuid4().hex, repo, path, touch_count))
        self._bump(repo)

    def upsert_intent(self, repo: str, kind: str, ref: str, title: str, body: str,
                      author: str, created_at: str, status: str = "", resolved: bool = True) -> None:
        self.db.execute(
            """INSERT INTO intents(id, repo, kind, ref, title, body, author, created_at, status, resolved)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(repo, kind, ref) DO UPDATE SET
                 title=excluded.title, body=excluded.body, status=excluded.status,
                 resolved=excluded.resolved""",
            (uuid.uuid4().hex, repo, kind, ref, title or "", body or "", author or "",
             created_at or "", status, 1 if resolved else 0))
        self._bump(repo)

    def set_ownership(self, repo: str, path_prefix: str, engineer: str, source: str,
                      weight: float, evidence: str) -> None:
        """Upsert an ownership signal with temporal validity. Small drifts
        update in place; a material change closes the old row and opens a
        fresh one so history stays auditable."""
        now = time.time()
        self._bump(repo)
        live = self.db.execute(
            """SELECT rowid, weight FROM ownership WHERE repo=? AND path_prefix=? AND engineer=?
               AND source=? AND valid_to IS NULL""", (repo, path_prefix, engineer, source)).fetchone()
        if live is None:
            self.db.execute(
                "INSERT INTO ownership(repo, path_prefix, engineer, source, weight, evidence, valid_from) "
                "VALUES(?,?,?,?,?,?,?)", (repo, path_prefix, engineer, source, weight, evidence, now))
        elif abs(live["weight"] - weight) > MATERIAL_WEIGHT_DELTA:
            self.db.execute("UPDATE ownership SET valid_to=? WHERE rowid=?", (now, live["rowid"]))
            self.db.execute(
                "INSERT INTO ownership(repo, path_prefix, engineer, source, weight, evidence, valid_from) "
                "VALUES(?,?,?,?,?,?,?)", (repo, path_prefix, engineer, source, weight, evidence, now))
            self.append_event("ownership_invalidated", {
                "repo": repo, "path_prefix": path_prefix, "engineer": engineer,
                "source": source, "old_weight": live["weight"], "new_weight": weight})
        else:
            self.db.execute("UPDATE ownership SET weight=?, evidence=? WHERE rowid=?",
                            (weight, evidence, live["rowid"]))

    # ---------------- the shared people-to-paths-to-changes structure ----------------

    def add_change(self, repo: str, sha: str, ts: str, paths: list[str],
                   people: list[tuple[str, str, str]]) -> None:
        """One change (a commit or a merge) with the paths it touched and
        the people on it with their roles (author, committer, reviewer
        trailers, merger). Routing sums these under any path prefix, so
        ownership exists at every depth without being materialized, and
        the same rows link records to paths for retrieval."""
        self.db.execute("INSERT OR REPLACE INTO changes(repo, sha, ts, nfiles) VALUES(?,?,?,?)",
                        (repo, sha, ts or "", len(paths)))
        self.db.executemany("INSERT OR IGNORE INTO change_paths(repo, sha, path) VALUES(?,?,?)",
                            [(repo, sha, p) for p in paths])
        self.db.executemany("INSERT OR REPLACE INTO change_people(repo, sha, engineer, email, role) VALUES(?,?,?,?,?)",
                            [(repo, sha, name, email or "", role) for name, email, role in people])
        self._bump(repo)

    def clear_changes(self, repo: str) -> None:
        for table in ("changes", "change_paths", "change_people"):
            self.db.execute(f"DELETE FROM {table} WHERE repo=?", (repo,))
        self._bump(repo)

    def clear_artifacts(self, repo: str) -> None:
        self.db.execute("DELETE FROM artifacts WHERE repo=?", (repo,))
        self._bump(repo)

    def clear_fetched(self, repo: str) -> None:
        """Forget what was fetched at ask time (changes, listings, tree,
        blame marks): a full rebuild re-derives all of it."""
        self.db.execute("DELETE FROM fetched WHERE repo=?", (repo,))

    def has_changes(self, repo: str) -> bool:
        return self.db.execute("SELECT 1 FROM changes WHERE repo=? LIMIT 1", (repo,)).fetchone() is not None

    def changes_under(self, repo: str, prefix: str) -> list[sqlite3.Row]:
        """(sha, ts, nfiles, engineer, email, role) for every change that
        touched a path under the prefix."""
        if prefix in ("", "*"):
            sql = ("SELECT c.sha, c.ts, c.nfiles, p.engineer, p.email, p.role FROM changes c "
                   "JOIN change_people p ON p.repo=c.repo AND p.sha=c.sha WHERE c.repo=?")
            return self.db.execute(sql, (repo,)).fetchall()
        sql = ("SELECT c.sha, c.ts, c.nfiles, p.engineer, p.email, p.role FROM changes c "
               "JOIN change_people p ON p.repo=c.repo AND p.sha=c.sha "
               "WHERE c.repo=? AND c.sha IN (SELECT DISTINCT sha FROM change_paths WHERE repo=? AND ")
        # A range on (repo, path) walks the index; substr would scan the
        # repo's partition. The upper bound bumps the last character, which
        # has no successor only at the top of the code space.
        if prefix[-1] == "\U0010ffff":
            sql += "substr(path,1,?)=?)"
            args = (repo, repo, len(prefix), prefix)
        else:
            path_column = 'path COLLATE "C"' if self.postgres else "path"
            sql += f"{path_column}>=? AND {path_column}<?)"
            args = (repo, repo, prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1))
        return self.db.execute(sql, args).fetchall()

    def last_activity(self, repo: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for r in self.db.execute(
                "SELECT p.engineer e, max(c.ts) t FROM change_people p JOIN changes c ON c.repo=p.repo AND c.sha=p.sha "
                "WHERE p.repo=? GROUP BY p.engineer", (repo,)):
            out[r["e"]] = r["t"] or ""
        return out

    def add_listing(self, repo: str, kind: str, pattern: str, person: str, email: str = "",
                    role: str = "owner", section: str = "", ord: int = 0) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO listings(repo, kind, pattern, person, email, role, section, ord) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (repo, kind, pattern, person, email or "", role, section or "", ord))
        self._bump(repo)

    def clear_listings(self, repo: str, kind: str = "") -> None:
        if kind:
            self.db.execute("DELETE FROM listings WHERE repo=? AND kind=?", (repo, kind))
        else:
            self.db.execute("DELETE FROM listings WHERE repo=?", (repo,))
        self._bump(repo)

    def listings(self, repo: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM listings WHERE repo=? ORDER BY kind, ord, pattern, role, person",
                               (repo,)).fetchall()

    def add_intent_paths(self, repo: str, kind: str, ref: str, paths: Iterable[str]) -> None:
        self.db.executemany("INSERT OR IGNORE INTO intent_paths(repo, kind, ref, path) VALUES(?,?,?,?)",
                            [(repo, kind, ref, p) for p in paths])
        self._bump(repo)

    def replace_intent_paths(self, repo: str, kind: str, ref: str, paths: Iterable[str]) -> None:
        """Replace an explicitly supplied import snapshot; [] clears it."""
        self.db.execute("DELETE FROM intent_paths WHERE repo=? AND kind=? AND ref=?", (repo, kind, ref))
        self.add_intent_paths(repo, kind, ref, paths)

    def paths_of_intents(self, repo: str, refs: Iterable[tuple[str, str]]) -> dict[tuple[str, str], list[str]]:
        out: dict[tuple[str, str], list[str]] = {}
        for kind, ref in refs:
            rows = self.db.execute("SELECT path FROM intent_paths WHERE repo=? AND kind=? AND ref=?",
                                   (repo, kind, ref)).fetchall()
            if rows:
                out[(kind, ref)] = [r["path"] for r in rows]
        return out

    def mark_fetched(self, repo: str, kind: str, key: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO fetched(repo, kind, key) VALUES(?,?,?)", (repo, kind, key))

    def was_fetched(self, repo: str, kind: str, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM fetched WHERE repo=? AND kind=? AND key=?",
                               (repo, kind, key)).fetchone() is not None

    def artifact_paths(self, repo: str) -> list[str]:
        return [r["path"] for r in self.db.execute("SELECT path FROM artifacts WHERE repo=?", (repo,))]

    def cache_get(self, repo: str, kind: str, key: str) -> str | None:
        """A model's answer for one input, so the same question never
        costs a second call and a replay is deterministic."""
        row = self.db.execute("SELECT value FROM model_cache WHERE repo=? AND kind=? AND key=?",
                              (repo, kind, key)).fetchone()
        return row["value"] if row else None

    def cache_set(self, repo: str, kind: str, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO model_cache(repo, kind, key, value) VALUES(?,?,?,?)",
                        (repo, kind, key, value))

    def add_blame(self, repo: str, rev: str, path: str, counts: dict[tuple[str, str], int]) -> None:
        """Line ownership of one file at one revision: (name, email) to
        the number of lines git blame attributes to them. Routing reads
        these rows straight from the table on every route, so this writer
        does not bump the generation: a bump here would only evict the
        tree, the listings and the history each time a file is first
        asked about."""
        self.db.execute("DELETE FROM blame_lines WHERE repo=? AND rev=? AND path=?", (repo, rev, path))
        self.db.executemany(
            "INSERT OR REPLACE INTO blame_lines(repo, rev, path, engineer, email, lines) VALUES(?,?,?,?,?,?)",
            [(repo, rev, path, name, email or "", n) for (name, email), n in counts.items() if name])

    def blame_for(self, repo: str, rev: str, path: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT engineer, email, lines FROM blame_lines WHERE repo=? AND rev=? AND path=? ORDER BY lines DESC",
            (repo, rev, path)).fetchall()

    def ownership_for(self, repo: str, path: str) -> list[sqlite3.Row]:
        rows = self.db.execute(
            "SELECT * FROM ownership WHERE repo=? AND valid_to IS NULL ORDER BY length(path_prefix) DESC",
            (repo,)).fetchall()
        return [r for r in rows if path.startswith(r["path_prefix"]) or r["path_prefix"] in ("", "*")]

    def counts(self) -> dict[str, int]:
        out = {}
        for table in ("engineers", "artifacts", "intents", "ownership", "decisions", "events"):
            out[table] = self.db.execute(f"SELECT count(*) c FROM {table}").fetchone()["c"]
        return out

    def set_source(self, repo: str, kind: str, locator: str) -> None:
        self.db.execute(
            "INSERT INTO connector_sources(repo, kind, locator) VALUES(?,?,?)"
            " ON CONFLICT(repo, kind) DO UPDATE SET locator=excluded.locator", (repo, kind, locator))

    def get_source(self, repo: str, kind: str) -> str:
        row = self.db.execute("SELECT locator FROM connector_sources WHERE repo=? AND kind=?",
                              (repo, kind)).fetchone()
        return row["locator"] if row else ""

    def _intent_corpus(self, repo: str) -> tuple[list[tuple[sqlite3.Row, str]], dict[str, int]]:
        """Every record in scope with its lowered text, read once per
        repo generation, and the document frequencies found in it so far."""
        sql, args = "SELECT * FROM intents", ()
        if repo:
            sql += " WHERE repo=?"
            args = (repo,)
        return self.memo(repo, "intents", (), lambda: self.db.execute(sql, args).fetchall(),
                         lambda rows: ([(row, (row["title"] + " " + row["body"]).lower()) for row in rows], {}))

    def term_dfs(self, terms: Iterable[str], repo: str = "") -> dict[str, int]:
        """Document frequency of each term over the records in scope."""
        texts, dfs = self._intent_corpus(repo)
        out = {}
        for t in terms:
            if t not in dfs:
                dfs[t] = sum(1 for _, x in texts if t in x)
            out[t] = dfs[t]
        return out

    def intents_matching(self, terms: Iterable[str], limit: int = 10,
                         repo: str = "") -> list[sqlite3.Row]:
        """Keyword search over intent titles and bodies, IDF-weighted with
        length normalization (the ported lexical rung). When FTS5 is
        present its porter-stemmed candidates join the scan so inflected
        forms are found too."""
        terms = [t.lower() for t in terms if len(t) > 2]
        if not terms:
            return []
        texts, _ = self._intent_corpus(repo)
        n_docs = len(texts)
        df = self.term_dfs(terms, repo=repo)
        fts_hits = self._fts_ids("intents_fts", terms, limit=max(limit * 3, 20))
        scored: list[tuple[float, sqlite3.Row]] = []
        for row, text in texts:
            score = sum(math.log(1.0 + n_docs / df[t]) for t in terms if t in text)
            if fts_hits and row["id"] in fts_hits:
                # A stemmed hit the substring scan missed still counts, at
                # the weight of a common term; a hit both saw is boosted.
                score += math.log(1.0 + n_docs / max(1, len(fts_hits)))
            if score > 0:
                score /= 1.0 + math.log1p(len(text) / 400.0)
                scored.append((score, row))
        scored.sort(key=lambda x: (-x[0], x[1]["created_at"] or ""))
        return [r for _, r in scored[:limit]]

    def _fts_ids(self, table: str, terms: Iterable[str], limit: int = 20, *,
                 repo: str = "", statuses: tuple | None = None) -> set[str]:
        """Rank eligible rows before taking the candidate budget.

        A global top-N followed by scope filtering can lose every local
        match. Decision retrieval uses the same eligibility predicate as
        its final row read, on both backends; irrelevant repositories or
        retired/unsigned-derived rows never consume that budget.
        """
        if not self.has_fts:
            return set()
        words = [t for t in terms if t.replace("_", "").isalnum() and len(t) > 2]
        if not words:
            return set()
        relation = {"decisions_fts": "decisions", "intents_fts": "intents"}[table]
        scope, args = "", []
        if relation == "decisions" and statuses is not None:
            scope, args = self._memory_filter(statuses, repo)
        elif repo:
            scope = "(d.repo=? OR d.repo='')" if relation == "decisions" else "d.repo=?"
            args = [repo]
        where = " AND " + scope if scope else ""
        if self.postgres:
            rows = self.db.execute(
                f"SELECT d.id FROM {relation} d, websearch_to_tsquery('english', ?) AS query "
                "WHERE d.search_vector @@ query" + where +
                " ORDER BY ts_rank(d.search_vector, query) DESC LIMIT ?",
                [" OR ".join(words[:24]), *args, limit]).fetchall()
            return {r["id"] for r in rows}
        query = " OR ".join('"' + w.replace('"', '') + '"' for w in words[:24])
        try:
            rows = self.db.execute(
                f"SELECT d.id FROM {table} JOIN {relation} d ON d.id={table}.id "
                f"WHERE {table} MATCH ?" + where + f" ORDER BY bm25({table}) LIMIT ?",
                [query, *args, limit]).fetchall()
        except sqlite3.OperationalError:
            return set()
        return {r["id"] for r in rows}

    def intents_by_ref(self, refs: Iterable[str], repo: str = "") -> list[sqlite3.Row]:
        refs = [str(r) for r in refs if str(r).strip()]
        if not refs:
            return []
        marks = ",".join("?" for _ in refs)
        sql, args = f"SELECT * FROM intents WHERE ref IN ({marks})", list(refs)
        if repo:
            sql += " AND repo=?"
            args.append(repo)
        return list(self.db.execute(sql + " ORDER BY created_at DESC", args))

    def record_routing_answer(self, repo: str, topic: str, owner: str) -> None:
        self.set_ownership(repo, topic, owner, "user", 1.0, "you told Raven this owner directly")

    # ---------------- tasks (runs) and decisions ----------------

    def create_task(self, title: str, agent: str = "Ladder", repo: str = "local") -> str:
        run_id = uuid.uuid4().hex[:12]
        self.db.execute("INSERT INTO runs(id, title, agent, repo, status, updated_at) VALUES(?,?,?,?,?,?)",
                        (run_id, (title or "task")[:300], agent, repo or "local", "working", now_iso()))
        return run_id

    def get_task(self, task_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM runs WHERE id=?", (task_id,)).fetchone()

    def resolve_engineer(self, name: str) -> str:
        """A handle, login, email, alias or bare first name resolved to the
        name the organization knows the person by: the people table
        first (verified identities), then a CODEOWNERS handle or a first
        name that exactly one engineer or owner the graph knows starts
        with (Tomas Lindqvist for @tomas); else the name as is."""
        raw = (name or "").strip()
        if not raw:
            return raw
        person = self.find_person(raw)
        if person is not None:
            return person["name"]
        raw = raw.lstrip("@")
        if " " in raw:
            return raw
        key = raw.lower()
        hits: list[str] = []
        for table in ("engineers", "owners"):
            for row in self.db.execute(f"SELECT name FROM {table}"):
                full = row["name"].strip()
                if " " in full and full.split()[0].lower() == key and full not in hits:
                    hits.append(full)
        return hits[0] if len(hits) == 1 else raw

    def owner_id_for_person(self, person_id: str) -> str | None:
        """The owner bound to this active person, without name fallback.

        Missing historical links are filled by creating a new owner row;
        an existing row bound to somebody else is never silently repaired.
        """
        person = self.get_person(person_id)
        if person is None or not person["active"]:
            return None
        row = self.db.execute("SELECT id FROM owners WHERE person_id=? ORDER BY created_at, id LIMIT 1",
                              (person_id,)).fetchone()
        if row:
            return row["id"]
        owner_id = uuid.uuid4().hex[:12]
        self.db.execute("INSERT INTO owners(id, name, team, patterns, created_at, person_id) VALUES(?,?,?,?,?,?)",
                        (owner_id, person["name"], person["team"], "", now_iso(), person_id))
        return owner_id

    def owner_id_for(self, name: str) -> str | None:
        """Resolve an identity before reducing it to an engineer name.

        Unresolved legacy names can still create unbound owner rows, but
        cannot select somebody's bound row through an ambiguous name.
        """
        person = self.find_person(name)
        if person is not None:
            return self.owner_id_for_person(person["id"])
        name = self.resolve_engineer(name)
        if not name:
            return None
        person = self.find_person(name)
        if person is not None:
            return self.owner_id_for_person(person["id"])
        row = self.db.execute("SELECT id FROM owners WHERE name=? AND person_id='' ORDER BY created_at, id LIMIT 1",
                              (name,)).fetchone()
        if row:
            return row["id"]
        owner_id = uuid.uuid4().hex[:12]
        self.db.execute("INSERT INTO owners(id, name, team, patterns, created_at, person_id) VALUES(?,?,?,?,?,?)",
                        (owner_id, name, "", "", now_iso(), ""))
        return owner_id

    # ---------------- people, teams, authority ----------------

    def people(self, active_only: bool = True) -> list[dict]:
        """Everyone the people table knows, memoized per generation."""
        sql = "SELECT * FROM people" + (" WHERE active=1" if active_only else "") + " ORDER BY name"

        def build(rows):
            out = []
            for r in rows:
                d = dict(r)
                try:
                    d["aliases"] = json.loads(d.get("aliases") or "[]")
                except ValueError:
                    d["aliases"] = []
                out.append(d)
            return out
        return self.memo("", "people" if active_only else "people_all", (), lambda: self.db.execute(sql).fetchall(), build)

    def _person_identity(self, row: dict):
        from .identity import Person
        return Person(name=row.get("name", ""), email=row.get("email", ""), handle=row.get("github_login", ""),
                      id=row.get("id", ""), slack_id=row.get("slack_id", ""), aliases=tuple(row.get("aliases") or ()))

    def find_person(self, text: str, repo: str = "") -> dict | None:
        """The person a name, email, login, Slack id, alias or unique first
        name refers to, or None. Never guesses between two candidates."""
        from .identity import first_name_match, norm_handle, parse_person, same_person
        text = (text or "").strip()
        if not text:
            return None
        people = self.people()
        if not people:
            return None
        low = text.lower()
        for p in people:
            if p["id"] == text or (p["slack_id"] and p["slack_id"].lower() == low):
                return p
        probe = parse_person(text)
        hits = [p for p in people if same_person(probe, self._person_identity(p))]
        if not hits and probe.handle:
            hits = [p for p in people if norm_handle(p["github_login"]) == norm_handle(probe.handle)]
        if len(hits) == 1:
            return hits[0]
        if hits:
            return None
        full = first_name_match(text, [p["name"] for p in people])
        if full != text:
            return next((p for p in people if p["name"] == full), None)
        return None

    def add_person(self, name: str, email: str = "", github_login: str = "", slack_id: str = "",
                   aliases: Iterable[str] = (), team: str = "", role: str = "member",
                   source: str = "config", merge: bool = True, github_id: str = "") -> str:
        """Create a person or, with merge, update the one these identities
        already name (by email, login, Slack id, name or alias); returns
        the id. An inbox owner row of the same name is linked or created
        so the person can be assigned decisions. merge=False always
        creates: a sign-in never merges into somebody by name."""
        from .identity import norm_handle
        name = " ".join((name or "").split())
        # A CODEOWNERS entry is a team as often as a person, and pasting
        # one in makes a "person" nobody can reach: messages go nowhere
        # and the decision sits. Teams have their own table, and their
        # members are the people a question can be put to.
        if is_team_handle(name) and not email and not slack_id and not github_login:
            raise ValueError(f"{name!r} is a team, not a person. Add it with add_team and name its members; "
                             "a team is not somebody a question can be put to.")
        email = (email or "").strip().lower()
        github_login = norm_handle(github_login)
        slack_id = (slack_id or "").strip()
        github_id = str(github_id or "").strip()
        alias_list = [a.strip() for a in aliases if a and a.strip()]
        existing = None
        if merge:
            for probe in (email, github_login and "@" + github_login, slack_id, name):
                if probe:
                    existing = self.find_person(probe)
                    if existing is not None:
                        break
        ts = now_iso()
        self._bump("")
        if existing is None:
            pid = uuid.uuid4().hex[:12]
            self.db.execute("INSERT INTO people(id, name, email, github_login, slack_id, aliases, team, role, active, "
                            "source, created_at, updated_at, github_id) VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?)",
                            (pid, name or email or github_login or slack_id, email, github_login, slack_id,
                             json.dumps(alias_list), team or "", role or "member", source, ts, ts, github_id))
        else:
            pid = existing["id"]
            merged = list(existing.get("aliases") or [])
            for a in alias_list + ([existing["name"]] if name and existing["name"] != name else []):
                if a and a not in merged and a != name:
                    merged.append(a)
            self.db.execute("UPDATE people SET name=?, email=?, github_login=?, slack_id=?, aliases=?, team=?, "
                            "role=CASE WHEN ?='' THEN role ELSE ? END, updated_at=?, "
                            "github_id=CASE WHEN ?='' THEN github_id ELSE ? END WHERE id=?",
                            (name or existing["name"], email or existing["email"],
                             github_login or existing["github_login"], slack_id or existing["slack_id"],
                             json.dumps(merged), team or existing["team"], role or "", role or "", ts,
                             github_id, github_id, pid))
        final = name or (existing or {}).get("name") or email or github_login
        # A namesake must never acquire an owner already bound to another
        # person. Adopt a legacy blank link only when the name is unique,
        # including inactive people whose historical decisions still exist.
        owner = self.db.execute("SELECT id, person_id, name FROM owners WHERE person_id=? OR "
                                "(person_id='' AND name=? AND NOT EXISTS "
                                "(SELECT 1 FROM people WHERE id<>? AND lower(trim(name))=lower(trim(?)))) "
                                "ORDER BY CASE WHEN person_id=? THEN 0 ELSE 1 END, created_at, id LIMIT 1",
                                (pid, final, pid, final, pid)).fetchone()
        if owner is None:
            self.db.execute("INSERT INTO owners(id, name, team, patterns, created_at, person_id) VALUES(?,?,?,?,?,?)",
                            (uuid.uuid4().hex[:12], final, team or "", "", ts, pid))
        else:
            # The owner row follows the person: the link, and the name the
            # organization now uses for them.
            self.db.execute("UPDATE owners SET person_id=?, name=?, team=CASE WHEN ?='' THEN team ELSE ? END WHERE id=?",
                            (pid, final, team or "", team or "", owner["id"]))
        return pid

    def get_person(self, person_id: str) -> dict | None:
        return next((p for p in self.people(active_only=False) if p["id"] == person_id), None)

    def person_by_github(self, github_id: str = "", login: str = "") -> dict | None:
        """The person a GitHub identity is bound to: by the immutable user
        id first, else by the exact login (case-insensitive). Never by
        name, alias or a concatenation of either."""
        from .identity import norm_handle
        people = self.people(active_only=False)
        gid = str(github_id or "").strip()
        if gid:
            hit = next((p for p in people if str(p.get("github_id") or "") == gid), None)
            if hit is not None:
                return hit
        handle = norm_handle(login)
        if handle:
            hits = [p for p in people if norm_handle(p.get("github_login") or "") == handle]
            if len(hits) == 1:
                return hits[0]
        return None

    def person_by_email(self, email: str) -> dict | None:
        """The one person with exactly this email, or None."""
        email = (email or "").strip().lower()
        if not email:
            return None
        hits = [p for p in self.people(active_only=False) if (p.get("email") or "").lower() == email]
        return hits[0] if len(hits) == 1 else None

    def add_team(self, name: str, handle: str = "", source: str = "config") -> str:
        from .identity import norm_handle
        handle = norm_handle(handle)
        row = None
        if handle:
            row = self.db.execute("SELECT id FROM teams WHERE handle=?", (handle,)).fetchone()
        if row is None and name:
            row = self.db.execute("SELECT id FROM teams WHERE lower(name)=lower(?)", (name,)).fetchone()
        self._bump("")
        if row is not None:
            self.db.execute("UPDATE teams SET name=?, handle=CASE WHEN ?='' THEN handle ELSE ? END WHERE id=?",
                            (name or handle, handle, handle, row["id"]))
            return row["id"]
        tid = uuid.uuid4().hex[:12]
        self.db.execute("INSERT INTO teams(id, name, handle, source, created_at) VALUES(?,?,?,?,?)",
                        (tid, name or handle, handle, source, now_iso()))
        return tid

    def set_team_members(self, team_id: str, person_ids: Iterable[str], source: str = "config",
                         replace: bool = False) -> None:
        self._bump("")
        if replace:
            self.db.execute("DELETE FROM team_members WHERE team_id=?", (team_id,))
        self.db.executemany("INSERT OR IGNORE INTO team_members(team_id, person_id, source) VALUES(?,?,?)",
                            [(team_id, pid, source) for pid in person_ids if pid])

    def teams(self) -> list[dict]:
        def build(rows):
            out = {}
            for r in rows:
                d = dict(r)
                member = d.pop("member_id")
                team = out.setdefault(d["id"], {**d, "members": []})
                if member:
                    team["members"].append(member)
            return list(out.values())
        # The cache fingerprint must include membership, not just team
        # metadata: another worker can remove a member without changing the
        # team row, and routing must immediately stop granting that standing.
        return self.memo("", "teams", (), lambda: self.db.execute(
            "SELECT t.*, m.person_id AS member_id FROM teams t LEFT JOIN team_members m ON m.team_id=t.id "
            "ORDER BY t.name,t.id,m.person_id").fetchall(), build)

    def team_members_by_handle(self, handle: str) -> list[dict]:
        """The people of a team CODEOWNERS names (@acme/payments or
        acme/payments), or none when the team is unknown."""
        from .identity import norm_handle
        h = norm_handle(handle)
        people = {p["id"]: p for p in self.people()}
        for t in self.teams():
            th = norm_handle(t["handle"])
            if h and (th == h or th.rsplit("/", 1)[-1] == h.rsplit("/", 1)[-1] and ("/" not in h or "/" not in th)):
                return [people[m] for m in t["members"] if m in people]
        return []

    def add_authority(self, scope_kind: str, scope: str, role: str, person_id: str = "", team_id: str = "",
                      repo: str = "", source: str = "config", asserted_by: str = "", accepted: bool = True,
                      note: str = "", effective_to: str = "") -> str:
        """A verified authority: who knows, decides or approves for a path
        pattern, a decision category, or a whole repository. A live row
        for the same subject and scope is ended first, so the newest
        assertion stands and history stays."""
        if scope_kind not in AUTHORITY_SCOPES:
            raise ValueError(f"scope_kind must be one of {', '.join(AUTHORITY_SCOPES)}")
        if role not in AUTHORITY_ROLES:
            raise ValueError(f"role must be one of {', '.join(AUTHORITY_ROLES)}")
        if not person_id and not team_id:
            raise ValueError("an authority names a person or a team")
        scope = (scope or "").strip().lstrip("/") if scope_kind == "path" else (scope or "").strip().lower()
        ts = now_iso()
        self._bump(repo or "")
        self._bump("")
        self.db.execute("UPDATE authority SET ended_at=? WHERE ended_at='' AND repo=? AND person_id=? AND team_id=? "
                        "AND scope_kind=? AND scope=? AND role=?",
                        (ts, repo or "", person_id or "", team_id or "", scope_kind, scope, role))
        aid = uuid.uuid4().hex[:12]
        self.db.execute("INSERT INTO authority(id, repo, person_id, team_id, scope_kind, scope, role, source, "
                        "asserted_by, accepted, note, effective_from, effective_to, ended_at, created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'',?)",
                        (aid, repo or "", person_id or "", team_id or "", scope_kind, scope, role, source,
                         asserted_by or "", 1 if accepted else 0, (note or "")[:500], ts, effective_to or "", ts))
        self.append_event("authority_recorded", {"authority_id": aid, "repo": repo or "", "person_id": person_id,
                                                 "team_id": team_id, "scope_kind": scope_kind, "scope": scope,
                                                 "role": role, "source": source, "asserted_by": asserted_by})
        return aid

    def end_authority(self, authority_id: str) -> None:
        self._bump("")
        row = self.db.execute("SELECT repo FROM authority WHERE id=?", (authority_id,)).fetchone()
        if row is not None:
            self._bump(row["repo"] or "")
        self.db.execute("UPDATE authority SET ended_at=? WHERE id=? AND ended_at=''", (now_iso(), authority_id))

    def accept_authority(self, authority_id: str) -> None:
        self._bump("")
        self.db.execute("UPDATE authority SET accepted=1 WHERE id=?", (authority_id,))

    def authority_rows(self, repo: str = "") -> list[dict]:
        """The live authority rows that apply to a repository: its own and
        the organization-wide ones, unexpired, with the person or team
        they name resolved."""
        def build(rows):
            people = {p["id"]: p for p in self.people(active_only=False)}
            teams = {t["id"]: t for t in self.teams()}
            out = []
            for r in rows:
                d = dict(r)
                d["person"] = people.get(d["person_id"])
                d["team"] = teams.get(d["team_id"])
                if d["person_id"] and (d["person"] is None or not d["person"].get("active", 1)):
                    continue
                out.append(d)
            return out
        rows = self.memo(repo or "", "authority", (), lambda: self.db.execute(
            "SELECT * FROM authority WHERE ended_at='' AND (repo=? OR repo='') "
            "ORDER BY created_at", (repo or "",)).fetchall(), lambda rows: [dict(r) for r in rows])
        # Expiry advances without a database write. People and teams also
        # change independently of these authority rows, so resolve them live
        # instead of retaining derived identities in the authority memo.
        stamp = now_iso()
        live = [r for r in rows if not r["effective_to"] or r["effective_to"] > stamp]
        return build(live) if live else []

    def deciders(self, repo: str = "") -> list[dict]:
        """The authority rows that would in fact put a question to
        somebody about this repository: accepted, still in force today,
        saying the person decides or approves rather than merely knows,
        and naming an active person or a team. A row recorded for
        another repository, or one whose `effective_to` has passed, is
        not authority here however many rows the table holds."""
        return [r for r in self.authority_rows(repo)
                if r["accepted"] and r["role"] in ("decides", "approves") and (r["person"] or r["team"])]

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                        (key, value or "", now_iso()))
        self._bump("")

    def learn_from_answer(self, decision_id: str, owner_name: str, owner_evidence: str) -> list[str]:
        """What one human answer teaches the authority map: a referral to
        the answering person is accepted, and an answer given for a
        decision that reached them on inference alone (no verified line)
        records that they know its decision categories, a weak row that
        makes them a candidate next time. Returns what was learned."""
        from .scopes import primary_scopes
        row = self.db.execute("SELECT question, context, category, repo, path FROM decisions WHERE id=?",
                              (decision_id,)).fetchone()
        person = self.find_person(owner_name) if owner_name else None
        if row is None or person is None:
            return []
        if self.db.execute("SELECT 1 FROM events WHERE decision_id=? AND kind='route_learning_optout'", (decision_id,)).fetchone():
            return []
        learned: list[str] = []
        from .routing_memory import record
        actual = self.db.execute("SELECT actor_id,actor_basis FROM decisions WHERE id=?", (decision_id,)).fetchone()
        if actual['actor_basis'] != 'admin-override' and (not actual['actor_id'] or actual['actor_id'] == person['id']):
            record(self, decision_id, person['id'], 'answered')
        # What the decision is about, not every topic its words touch: a
        # docs question that mentions "security fix" is not a security
        # decision, and answering it teaches nothing about security.
        scopes = primary_scopes(row["question"], row["category"] or "")
        # The route this very decision's hand-on recorded is accepted by
        # answering it, whatever scope the person handing it on picked.
        handed: set[str] = set()
        for e in self.db.execute("SELECT detail FROM events WHERE decision_id=? AND kind='route_learned'", (decision_id,)):
            try:
                handed.update(json.loads(e["detail"]).get("authority_ids") or [])
            except (ValueError, TypeError):
                pass
        for a in self.db.execute("SELECT id, scope_kind, scope, role FROM authority WHERE person_id=? AND source='referral' "
                                 "AND accepted=0 AND ended_at=''", (person["id"],)).fetchall():
            covers = a["id"] in handed
            if covers:
                self.accept_authority(a["id"])
                learned.append(f"accepted: {a['role']} {a['scope'] or 'everything'}")
                self.append_event("referral_accepted", {"decision_id": decision_id, "authority_id": a["id"],
                                                        "person": person["name"]})
        if len(scopes) == 1 and not any(ln.startswith("verified:") for ln in (owner_evidence or "").split("; ")):
            live = {(a["scope_kind"], a["scope"]) for a in self.authority_rows(row["repo"] or "")
                    if a["person_id"] == person["id"]}
            for scope in scopes:
                if ("category", scope) in live:
                    continue
                self.add_authority("category", scope, "knows", person_id=person["id"], repo=row["repo"] or "",
                                   source="answer", asserted_by=person["name"],
                                   note=f"answered decision {decision_id}, which reached them on inference")
                learned.append(f"knows {scope}")
        return learned

    def coordinator(self, repo: str = "") -> dict | None:
        """The person who receives what nobody else is verified to own: a
        per-repository coordinator, else the organization's."""
        for key in ((f"coordinator:{repo}",) if repo else ()) + ("coordinator",):
            pid = self.get_setting(key)
            if pid:
                person = self.get_person(pid)
                if person is not None and person.get("active", 1):
                    return person
        return None

    def add_decision(self, task_id: str, question: str, category: str, status: str,
                     source: str = "", answer: str = "", answered_by: str = "",
                     evidence: str = "", owner: str = "", owner_evidence: str = "",
                     embedding: list[float] | None = None, options: list[str] | None = None,
                     repo: str = "", context: str = "", path: str = "unknown",
                     rationale: str = "", routing_reason: str = "resolution ladder",
                     kind: str = "", draft: bool = False) -> str:
        did = uuid.uuid4().hex[:12]
        ts = now_iso()
        owner_id = self.owner_id_for(owner) if owner else None
        self.db.execute(
            """INSERT INTO decisions(id, run_id, question, context, path, owner_id, routing_reason,
                 status, prediction, source_id, answer, rationale, answered_by, created_at, updated_at,
                 category, source, evidence, owner_evidence, superseded_by, supersedes, options, repo,
                 kind, embedding, draft)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (did, task_id, question, context or "", path or "unknown", owner_id, routing_reason,
             status, None, None, answer or None, rationale or None, answered_by or None, ts, ts,
             category or "", source or "", evidence or "", owner_evidence or "", "", "",
             json.dumps(options) if options else "", repo or "", kind or "",
             _f32blob(embedding) if embedding else None, 1 if draft else 0))
        if draft:
            self.set_setting("drafts_pending", "1")
        return did

    def publish_draft(self, decision_id: str) -> None:
        """The node is built: routed, placed on the tree, its scope and
        sign-off written. From here it is a decision everyone can see.
        When it was the last one outstanding, the marker readers check
        is cleared, so a read pays nothing for the drafts mechanism."""
        self.db.execute("UPDATE decisions SET draft=0 WHERE id=? AND draft=1", (decision_id,))
        if self.db.execute("SELECT 1 FROM decisions WHERE draft=1 LIMIT 1").fetchone() is None:
            self.set_setting("drafts_pending", "")
        self._bump("")

    def claim_node_ref(self, run_id: str, client_ref: str, stale: int = 120) -> tuple[bool, str]:
        """Take the client_ref for this task, atomically. Returns whether
        it is ours and, when it is not, the decision the write that holds
        it produced (empty while that write is still running). A claim
        left behind by a write that died is taken over after `stale`
        seconds, the same window an abandoned draft is recovered in."""
        stamp = ts_to_iso(time.time())
        try:
            with self.transaction():
                self.db.execute("INSERT INTO node_claims(run_id, client_ref, decision_id, created_at) "
                                "VALUES(?,?,'',?)", (run_id, client_ref, stamp))
            return True, ""
        except sqlite3.IntegrityError:
            pass
        row = self.db.execute("SELECT decision_id, created_at FROM node_claims WHERE run_id=? AND client_ref=?",
                              (run_id, client_ref)).fetchone()
        if row is None:
            return self.claim_node_ref(run_id, client_ref, stale)
        if row["decision_id"]:
            return False, row["decision_id"]
        if row["created_at"] and row["created_at"] < ts_to_iso(time.time() - stale):
            with self.transaction():
                taken = self.db.execute(
                    "UPDATE node_claims SET created_at=? WHERE run_id=? AND client_ref=? AND created_at=? "
                    "AND decision_id=''", (stamp, run_id, client_ref, row["created_at"]))
            if taken.rowcount:
                return True, ""
        return False, ""

    def claimed_node_ref(self, run_id: str, client_ref: str) -> str:
        row = self.db.execute("SELECT decision_id FROM node_claims WHERE run_id=? AND client_ref=?",
                              (run_id, client_ref)).fetchone()
        return row["decision_id"] if row else ""

    def settle_node_ref(self, run_id: str, client_ref: str, decision_id: str) -> None:
        """The write that held the claim produced this node: anyone
        waiting on the claim gets it from here."""
        with self.transaction():
            self.db.execute("UPDATE node_claims SET decision_id=? WHERE run_id=? AND client_ref=?",
                            (decision_id, run_id, client_ref))

    def release_node_ref(self, run_id: str, client_ref: str) -> None:
        """The write that held the claim produced nothing. Give the
        client_ref back, so the agent's retry writes the node."""
        with self.transaction():
            self.db.execute("DELETE FROM node_claims WHERE run_id=? AND client_ref=? AND decision_id=''",
                            (run_id, client_ref))

    def expire_drafts(self, seconds: int = 120) -> list[str]:
        """A node whose creation died (the process was killed between the
        insert and the publish) is not hidden forever: after a couple of
        minutes it becomes visible, incomplete and routed to nobody,
        where a person can see it, rather than waiting invisibly. Costs
        one settings read while nothing is being built."""
        if self.get_setting("drafts_pending") != "1":
            return []
        stale = self.db.execute("SELECT id FROM decisions WHERE draft=1 AND client_ref='__model_pass__' AND created_at < ?",
                                (ts_to_iso(time.time() - 3600),)).fetchall()
        if stale:
            with self.transaction():
                for row in stale:
                    self.db.execute('DELETE FROM decision_links WHERE decision_id=? OR related_id=?', (row['id'], row['id']))
                    self.db.execute('DELETE FROM events WHERE decision_id=?', (row['id'],))
                    self.db.execute('DELETE FROM decisions WHERE id=?', (row['id'],))
        cutoff = ts_to_iso(time.time() - seconds)
        rows = self.db.execute("SELECT id, run_id FROM decisions WHERE draft=1 AND client_ref != '__model_pass__' AND created_at < ?",
                               (cutoff,)).fetchall()
        if not rows:
            return []
        with self.transaction():
            for r in rows:
                self.db.execute("UPDATE decisions SET draft=0, routing_reason=? WHERE id=?",
                                ("the write that created this node did not finish; it was never routed", r["id"]))
                self.append_event("draft_recovered", {"task_id": r["run_id"], "decision_id": r["id"]})
            if self.db.execute("SELECT 1 FROM decisions WHERE draft=1 LIMIT 1").fetchone() is None:
                self.set_setting("drafts_pending", "")
        return [r["id"] for r in rows]

    def get_decision(self, decision_id: str, exact: bool = False) -> Decision | None:
        """The decision with this id, or with exact=False the one decision
        whose id starts with it (a prefix matching several raises)."""
        row = self.db.execute(_DECISION_SELECT + " WHERE d.id=?", (decision_id,)).fetchone()
        if row or exact:
            return _row_to_decision(row) if row else None
        # Ids are hex, so '~' sorts after every id sharing the prefix and
        # the range walks the primary key instead of scanning for LIKE.
        rows = self.db.execute(_DECISION_SELECT + " WHERE d.id >= ? AND d.id < ? || '~' ORDER BY d.id LIMIT 6",
                               (decision_id, decision_id)).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise ValueError(f"'{decision_id}' matches {len(rows)} decisions; use the full id")
        return _row_to_decision(rows[0])

    def decisions_for_task(self, task_id: str) -> list[Decision]:
        rows = self.db.execute(_DECISION_SELECT + " WHERE d.run_id=? ORDER BY d.created_at, d.rowid",
                               (task_id,)).fetchall()
        return [_row_to_decision(r) for r in rows]

    def update_decision(self, decision_id: str, **fields: Any) -> None:
        allowed = {"status", "source", "answer", "answered_by", "evidence", "owner",
                   "owner_evidence", "superseded_by", "supersedes", "category", "kind",
                   "prediction", "source_id", "routing_reason", "rationale",
                   "parent_id", "client_ref", "depth", "origin", "signoff", "signed_by", "options", "brief",
                   "source_revision", "needs_review", "review_reason", "signed_revision", "signed_hash",
                   "scope_key", "model_pending"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"cannot update field {k}")
            if k == "owner":
                sets.append("owner_id=?")
                vals.append(self.owner_id_for(v) if v else None)
                continue
            sets.append(f"{k}=?")
            vals.append(v)
        sets.append("updated_at=?")
        vals.append(now_iso())
        vals.append(decision_id)
        self.db.execute(f"UPDATE decisions SET {', '.join(sets)} WHERE id=?", vals)

    def recent_answered(self, repo: str = "", limit: int = 4) -> list[Decision]:
        sql = (_DECISION_SELECT + " WHERE d.status IN ('approved','resolved','partial') "
               "AND d.superseded_by='' AND d.draft=0 AND coalesce(d.answer,'') != ''"
               f" AND ({PERSONALLY_SIGNED_SQL} OR d.source NOT IN ('memory', 'record', 'human'))")
        args: tuple = ()
        if repo:
            sql += " AND (d.repo=? OR d.repo='')"
            args = (repo,)
        sql += " ORDER BY d.updated_at DESC LIMIT ?"
        return [_row_to_decision(r) for r in self.db.execute(sql, args + (limit,)).fetchall()]

    def _memory_filter(self, statuses: tuple, repo: str) -> tuple[str, list]:
        """One eligibility predicate for counting, ranking and fetching memory."""
        marks = ",".join("?" for _ in statuses)
        sql = f"d.status IN ({marks}) AND d.superseded_by='' AND d.draft=0"
        args: list = list(statuses)
        excluded = getattr(self._local, 'model_exclude', '')
        if excluded:
            sql += ' AND d.id != ?'
            args.append(excluded)
        if "pending" not in statuses:
            sql += (" AND coalesce(d.answer,'') != ''"
                    f" AND ({PERSONALLY_SIGNED_SQL} OR d.source NOT IN ('memory', 'record', 'human'))")
        if repo:
            sql += " AND (d.repo=? OR d.repo='')"
            args.append(repo)
        return sql, args

    def _memory_rows(self, statuses: tuple, repo: str, ids: set[str] | None = None) -> list[sqlite3.Row]:
        """The rows memory is made of. A row that was itself derived from
        memory or a record, and that no person signed here, is left out:
        it adds nothing its source does not, and it would let one reuse
        breed another (a rule's authorization included, under a name
        that never reviewed this question). An unsigned answer the agent
        settled stays in, as a prediction the ladder labels as such.
        With `ids`, only those rows."""
        scope, args = self._memory_filter(statuses, repo)
        sql = _DECISION_SELECT + " WHERE " + scope
        if ids is not None:
            out: list[sqlite3.Row] = []
            wanted = list(ids)
            for chunk in range(0, len(wanted), 400):
                part = wanted[chunk:chunk + 400]
                out.extend(self.db.execute(sql + f" AND d.id IN ({','.join('?' for _ in part)})", args + part).fetchall())
            return out
        return self.db.execute(sql, args).fetchall()

    def _memory_count(self, statuses: tuple, repo: str) -> int:
        scope, args = self._memory_filter(statuses, repo)
        return int(self.db.execute("SELECT count(*) c FROM decisions d WHERE " + scope, args).fetchone()["c"])

    def _bounded_rows(self, statuses: tuple, repo: str, query: str = "") -> list[sqlite3.Row]:
        """The candidate rows one similarity pass scores. A memory under
        FULL_SCAN_MAX rows is scanned whole, exactly as before. Above it,
        the candidates are bounded: the FTS5 matches of the query's
        terms (BM25 order, CANDIDATE_FTS of them), plus the newest
        CANDIDATE_RECENT rows, so a question is still matched against
        what it shares words with and against what was decided lately,
        with bounded scoring work. Both budgets apply after repository
        and memory-eligibility filtering. Without FTS5 or a query the
        newest eligible rows alone bound the pass."""
        if self._memory_count(statuses, repo) <= FULL_SCAN_MAX:
            return self._memory_rows(statuses, repo)
        ids: set[str] = set()
        if query:
            import re as _re
            from .llm import stem
            terms = sorted({stem(t) for t in _re.findall(r"[a-z0-9]{3,}", query.lower())} - _STOP)
            ids |= self._fts_ids("decisions_fts", terms, limit=CANDIDATE_FTS,
                                 repo=repo, statuses=statuses)
        scope, args = self._memory_filter(statuses, repo)
        sql = "SELECT d.id FROM decisions d WHERE " + scope
        ids |= {r["id"] for r in self.db.execute(sql + " ORDER BY updated_at DESC LIMIT ?", args + [CANDIDATE_RECENT])}
        return self._memory_rows(statuses, repo, ids=ids)

    # ---------------- the decision contract ----------------

    def known_repos(self) -> list[str]:
        """Every repository scope the graph holds anything for."""
        sql = ("SELECT repo FROM connector_sources UNION SELECT repo FROM changes UNION SELECT repo FROM artifacts "
               "UNION SELECT repo FROM listings UNION SELECT repo FROM intents UNION SELECT repo FROM ownership")
        return self.memo("", "repos", (), lambda: sorted(r["repo"] for r in self.db.execute(sql) if r["repo"]),
                         lambda rows: list(rows))

    def resolve_repo(self, key: str) -> str:
        """The graph scope for a run's repository identity. An exact match
        wins. A checkout ingested without a remote is known by its
        directory name alone, so 'acme/platform' finds 'platform' when
        that is the only graph of that name, and a bare 'platform' finds
        'acme/platform' when that is the only one. Two different owners
        with the same name never share a graph or a memory."""
        key = (key or "").strip()
        if not key:
            return key
        known = self.known_repos()
        if key in known:
            return key
        tail = key.rsplit("/", 1)[-1]
        same_tail = [r for r in known if r.rsplit("/", 1)[-1] == tail]
        if "/" in key:
            # A bare graph of that name stands for this repository only
            # when no other owner's graph of the same name is known: with
            # other-company/platform ingested, a bare 'platform' could be
            # either, and nothing is guessed.
            bare = [r for r in same_tail if r == tail]
            others = [r for r in same_tail if "/" in r and r != key]
            return bare[0] if len(bare) == 1 and not others else key
        qualified = [r for r in same_tail if "/" in r]
        return qualified[0] if len(qualified) == 1 else key

    def add_link(self, decision_id: str, related_id: str, kind: str, note: str = "") -> None:
        """A relationship between two decisions: related (the same question
        asked in a different scope), derived (an answer taken from
        another), depends (this one cannot proceed before that one)."""
        if not decision_id or not related_id or decision_id == related_id:
            return
        self.db.execute("INSERT OR IGNORE INTO decision_links(decision_id, related_id, kind, note, created_at) "
                        "VALUES(?,?,?,?,?)", (decision_id, related_id, kind,
                                              clip_marked(note, 300, "the linked decision has the rest"), now_iso()))

    def links_for(self, decision_ids: Iterable[str]) -> dict[str, list[dict]]:
        ids = [i for i in decision_ids if i]
        out: dict[str, list[dict]] = {}
        if not ids:
            return out
        for chunk in range(0, len(ids), 400):
            part = ids[chunk:chunk + 400]
            marks = ",".join("?" for _ in part)
            for r in self.db.execute(
                    f"SELECT decision_id, related_id, kind, note FROM decision_links WHERE decision_id IN ({marks}) "
                    f"OR related_id IN ({marks})", part + part):
                for a, b in ((r["decision_id"], r["related_id"]), (r["related_id"], r["decision_id"])):
                    if a in part or a in ids:
                        out.setdefault(a, []).append({"id": b, "kind": r["kind"], "note": r["note"]})
        return out

    def rule_dependents(self, rule_id: str) -> list[sqlite3.Row]:
        """The outstanding nodes a rule authorized: on tasks not yet
        finished, still standing on the rule."""
        return self.db.execute(
            "SELECT d.id, d.run_id FROM decisions d JOIN runs r ON r.id=d.run_id WHERE d.source_id=? "
            "AND d.signoff='rule' AND r.status != 'completed'", (rule_id,)).fetchall()

    def invalidate_rule_dependents(self, rule_id: str, reason: str,
                                   decision_ids: Iterable[str] | None = None) -> list[str]:
        """Withdraw outstanding authorization, including its dependent chain.

        Finished tasks keep their history: the rule applied when they acted.
        ``decision_ids`` restricts a live applicability check to the nodes that
        no longer satisfy the rule; other covered work remains authorized.
        """
        wanted = set(decision_ids) if decision_ids is not None else None
        flagged: list[str] = []
        for row in self.rule_dependents(rule_id):
            if wanted is not None and row["id"] not in wanted:
                continue
            for decision_id in self.flag_dependents(row["id"], reason, include_root=True,
                                                   outstanding_only=True, source_id=rule_id):
                if decision_id not in flagged:
                    flagged.append(decision_id)
        return flagged

    def _refresh_rule_dependents(self, now: float | None = None) -> None:
        """Revalidate standing permission before returning live authorization.

        A source's structured expiry can precede its rule expiry. Conditions,
        scope and supersession may also change after an unfinished task reused
        it; creation-time applicability is not continuing permission.
        """
        if self.get_setting("rule_checks_needed") != "1":
            return
        from .ladder import _scope_difference
        rows = self.db.execute(
            "SELECT d.id, d.source_id, d.question, d.context, d.path, d.facts, d.repo "
            "FROM decisions d JOIN runs r ON r.id=d.run_id "
            "WHERE d.signoff='rule' AND r.status != 'completed'").fetchall()
        for row in rows:
            source = self.get_decision(row["source_id"], exact=True)
            reason = ""
            facts = parse_facts(row["facts"])
            if source is None or source.superseded_by or source.needs_review:
                reason = "the source rule was superseded or needs review"
            else:
                applies, why = rule_status(source, row["question"], row["context"], facts, now=now)
                if not applies:
                    reason = why or "the source is no longer an authorized reusable rule"
                if not reason:
                    applies, why = applicability_status(source, row["path"], facts, now=now)
                    if not applies:
                        reason = why
                if not reason and source.rule_scope != "any":
                    source_row = self.db.execute("SELECT facts FROM decisions WHERE id=?", (source.id,)).fetchone()
                    difference, _ = _scope_difference(row["question"], row["context"], row["path"], source,
                        facts, parse_facts(source_row["facts"]), row["repo"])
                    if difference:
                        reason = "the source rule no longer covers this scope: " + difference
            if reason:
                with self.transaction():
                    self.invalidate_rule_dependents(row["source_id"], reason, [row["id"]])

    def note_rule_expiry(self, expires: str) -> None:
        """Remember the earliest live rule expiry, so the sweep before a
        read costs one settings lookup until a rule is actually due."""
        if self.get_setting("rule_checks_needed") != "1":
            self.set_setting("rule_checks_needed", "1")
        current = self.get_setting("rule_next_expiry")
        if expires and (not current or expires < current):
            self.set_setting("rule_next_expiry", expires)

    def expire_rules(self, now: str = "") -> list[str]:
        """Rules past their expiry stop being rules, and what they still
        authorized wants a person again. Run before anything that reads
        authorization (a node, the tree, the wait, the finish). The expiry
        sweep retains its deadline fast path; live derived nodes also check
        their source's applicability. Both use the same clock."""
        stamp = now or ts_to_iso(time.time())
        due_at = self.get_setting("rule_next_expiry")
        due = (self.db.execute("SELECT id, rule_expires FROM decisions WHERE reusable=1 AND rule_expires != '' "
                               "AND rule_expires <= ?", (stamp,)).fetchall()
               if due_at and due_at <= stamp else [])
        ended: list[str] = []
        if due_at and due_at <= stamp:
            with self.transaction():
                for r in due:
                    self.db.execute("UPDATE decisions SET reusable=0, rule_ended_at=? WHERE id=?", (stamp, r["id"]))
                    self.append_event("rule_ended", {"task_id": "", "decision_id": r["id"], "by": "expiry",
                                                     "expired": r["rule_expires"]})
                    self.invalidate_rule_dependents(r["id"], f"the rule from decision {r['id']} expired on {r['rule_expires'][:10]}")
                    ended.append(r["id"])
                nxt = self.db.execute("SELECT min(rule_expires) m FROM decisions WHERE reusable=1 AND rule_expires != ''").fetchone()
                self.set_setting("rule_next_expiry", (nxt["m"] if nxt and nxt["m"] else "") or "")
        self._refresh_rule_dependents(now=iso_to_ts(stamp))
        return ended

    def blocking_nodes(self, task_id: str, sweep: bool = True) -> list[dict]:
        """The decisions of a task that still wait on a person, each with
        the reason: an open question, an unsigned answer, a correction
        upstream, or a duplicate whose canonical decision is in one of
        those states. Suggested follow-ups wait on the agent, not on a
        person, and do not count.

        The sweeps write, so a caller already inside a write transaction
        on another connection runs them itself, before opening it, and
        passes sweep=False."""
        if sweep:
            self.expire_rules()
            self.expire_drafts()
        rows = self.db.execute(
            "SELECT d.id, d.question, d.status, d.signoff, d.needs_review, d.review_reason, d.superseded_by, "
            "d.required_signers, d.signatures, d.followup_required, d.model_pending, "
            "o.name AS owner_name, c.id AS c_id, c.status AS c_status, c.signoff AS c_signoff, "
            "c.needs_review AS c_needs_review, c.run_id AS c_run, oc.name AS c_owner, "
            "c.required_signers AS c_required, c.signatures AS c_signatures "
            "FROM decisions d LEFT JOIN owners o ON o.id = d.owner_id "
            "LEFT JOIN decisions c ON d.status = 'duplicate' AND c.id = d.superseded_by "
            "LEFT JOIN owners oc ON oc.id = c.owner_id WHERE d.run_id = ? AND d.draft = 0", (task_id,)).fetchall()
        out: list[dict] = []
        for r in rows:
            probe = r
            if r["status"] == "duplicate":
                if not r["c_id"]:
                    continue
                probe = {"status": r["c_status"], "signoff": r["c_signoff"], "needs_review": r["c_needs_review"]}
                probe = _Probe(probe)
            if not blocks_finish(probe):
                continue
            status = probe["status"]
            owner = (r["c_owner"] if r["status"] == "duplicate" else r["owner_name"]) or ""
            if probe["needs_review"]:
                why = "needs review: " + ((r["review_reason"] if r["status"] != "duplicate" else "") or
                                          "an answer it was derived from was corrected")
            elif status == "pending":
                why = f"waiting on {owner}" if owner else "no owner yet (assign one in the inbox)"
            elif status == "suggested":
                why = (f"a follow-up a person marked required; adopt it with bridge_add_node(adopt={r['id']}) and "
                       "get it answered")
            else:
                # Who has not signed yet: every required approver still
                # missing. Measured live: the refusal named the owner who
                # had already answered, not the reviewer it waited on.
                dup = r["status"] == "duplicate"
                missing = still_to_sign(r["c_required"] if dup else r["required_signers"],
                                        r["c_signatures"] if dup else r["signatures"], owner)
                why = (f"sign-off wanted from {', '.join(missing or [owner])}" if missing or owner
                       else "sign-off wanted (no owner named yet)")
            if r["status"] == "duplicate":
                why = f"same decision as node {r['c_id']}, {why}"
            out.append({"id": r["id"], "question": r["question"], "owner": owner, "why": why,
                        "canonical": r["c_id"] or ""})
        return out

    def flag_dependents(self, decision_id: str, reason: str, exclude: Iterable[str] = (),
                        *, include_root: bool = False, outstanding_only: bool = False,
                        source_id: str = "") -> list[str]:
        """Put an invalidated premise and its complete dependent chain in doubt.

        Source reuse, parent/child questions and explicit dependencies can mix.
        Pending suggestions are withdrawn; signed answers keep their historical
        text but lose live signatures. Rule lifecycle changes preserve completed
        history, while corrections and supersession also flag completed proofs.
        """
        source_id = source_id or decision_id
        seen: set[str] = set(exclude)
        queue = [decision_id]
        flagged: list[str] = []
        ts = now_iso()
        if not include_root:
            seen.add(decision_id)
        while queue:
            src = queue.pop()
            if include_root and src == decision_id and src not in seen:
                rows = self.db.execute("SELECT * FROM decisions WHERE id=?", (src,)).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT * FROM decisions WHERE source_id=? OR parent_id=? OR id IN "
                    "(SELECT decision_id FROM decision_links WHERE related_id=? AND kind='depends')",
                    (src, src, src)).fetchall()
            for r in rows:
                if r["id"] in seen:
                    continue
                seen.add(r["id"])
                queue.append(r["id"])
                if r["status"] in ("withdrawn", "adopted"):
                    continue
                if outstanding_only:
                    run = self.db.execute("SELECT status FROM runs WHERE id=?", (r["run_id"],)).fetchone()
                    if run is not None and run["status"] == "completed":
                        continue
                why = f"{reason} (source decision {source_id})"
                if r["status"] == "pending":
                    self.db.execute("UPDATE decisions SET prediction=NULL, source_id=NULL, source_revision='', "
                                    "updated_at=? WHERE id=?", (ts, r["id"]))
                    self.append_event("prediction_withdrawn", {"task_id": r["run_id"], "decision_id": r["id"],
                                                               "reason": why})
                    continue
                self.db.execute("UPDATE decisions SET needs_review=1, review_reason=?, "
                                "status=CASE WHEN status='approved' THEN 'resolved' ELSE status END, "
                                "signoff=CASE WHEN signoff IN ('signed','rule') THEN 'required' ELSE signoff END, "
                                "signatures='[]', signed_by='', signed_hash='', signed_revision='', "
                                "updated_at=? WHERE id=?", (clip_marked(why, 500, "the source decision has the rest"),
                                                           ts, r["id"]))
                self.db.execute("UPDATE runs SET needs_review=1, updated_at=? WHERE id=?", (ts, r["run_id"]))
                self.append_event("dependent_flagged", {"task_id": r["run_id"], "decision_id": r["id"],
                                                        "source": source_id,
                                                        "previous_authorization": {key: r[key] for key in (
                                                            "status", "signoff", "signatures", "signed_by",
                                                            "signed_hash", "signed_revision")},
                                                        "reason": clip_marked(reason, 300,
                                                                              "the source decision has the rest")})
                flagged.append(r["id"])
        return flagged

    def _row_embedding(self, row: sqlite3.Row) -> list[float]:
        if row["embedding"]:
            return _unblob(row["embedding"])
        from .llm import embed
        return embed(row["question"])

    def similar_answered(self, embedding: list[float], top_k: int = 3, min_score: float = 0.60,
                         repo: str = "", query: str = "") -> list[tuple[float, Decision]]:
        """The signed and evidence-resolved decisions closest to an
        embedding; `query` (the question's text) bounds the candidates
        on a large memory."""
        out: list[tuple[float, Decision]] = []
        for row in self._bounded_rows(MEMORY_STATUSES, repo, query):
            if row["id"] == getattr(self._local, "model_exclude", ""):
                continue
            score = _cosine(embedding, self._row_embedding(row))
            if score >= min_score:
                out.append((score, _row_to_decision(row)))
        out.sort(key=lambda x: (-x[0], -x[1].updated_at))
        return out[:top_k]

    def similar_open(self, embedding: list[float], top_k: int = 3, min_score: float = 0.60,
                     repo: str = "", query: str = "") -> list[tuple[float, Decision]]:
        out: list[tuple[float, Decision]] = []
        for row in self._bounded_rows((STATUS_OPEN,), repo, query):
            if row["id"] == getattr(self._local, "model_exclude", ""):
                continue
            score = _cosine(embedding, self._row_embedding(row))
            if score >= min_score:
                out.append((score, _row_to_decision(row)))
        out.sort(key=lambda x: -x[0])
        return out[:top_k]

    # ---------------- hybrid memory search (the inbox's suggestion engine) ----------------

    def memory_search(self, query: str, limit: int = 5, repo: str = "",
                      owner_id: str | None = None, min_score: float = 0.35) -> list[dict]:
        """Approved and evidence-resolved decisions that answer a query.

        Score = max(cosine of hashed embeddings, stemmed term overlap on the
        question, a discounted overlap over answer, rationale and context),
        lifted by an FTS5 BM25 hit when the build has FTS5, then multiplied
        by a recency factor (full weight for 90 days, half-life 180 days,
        floor 0.7) so recency breaks more than exact ties. Rows superseded
        by a correction, an overturn, or an explicit supersedes link are
        never returned, and when two candidates are the same decision
        (question overlap at least 0.7 and compatible recorded scope)
        only the newest survives. Distinct or uncertain historical scopes
        stay visible; retrieval does not establish facts about this query.
        """
        from .llm import embed, stem
        import re
        qterms = [stem(t) for t in re.findall(r"[a-z0-9]{3,}", query.lower())]
        qset = set(qterms) - _STOP
        if not qset:
            return []
        qemb = embed(query)
        fts_hits = self._fts_ids("decisions_fts", sorted(qset), limit=limit * 4 + 10,
                                 repo=repo, statuses=MEMORY_STATUSES)
        rows = self._bounded_rows(MEMORY_STATUSES, repo, query)
        superseded_ids = {r["supersedes"] for r in rows if r["supersedes"]}
        scored: list[tuple[float, float, sqlite3.Row, float]] = []
        for row in rows:
            if row["id"] in superseded_ids:
                continue
            if owner_id and row["owner_id"] != owner_id:
                continue
            q_terms = {stem(t) for t in re.findall(r"[a-z0-9]{3,}", row["question"].lower())}
            rest = " ".join(x for x in (row["answer"], row["rationale"], row["context"]) if x)
            r_terms = {stem(t) for t in re.findall(r"[a-z0-9]{3,}", rest.lower())}
            ov_q = len(qset & q_terms) / len(qset)
            ov_all = len(qset & (q_terms | r_terms)) / len(qset)
            cos = _cosine(qemb, self._row_embedding(row))
            sim = max(cos, ov_q, 0.8 * ov_all)
            if fts_hits and row["id"] in fts_hits:
                sim = min(1.0, sim + 0.1)
            if sim < min_score:
                continue
            recency = _recency(iso_to_ts(row["updated_at"]))
            scored.append((sim * (0.7 + 0.3 * recency), sim, row, iso_to_ts(row["updated_at"])))
        scored.sort(key=lambda x: (-x[0], -x[3]))
        # Newest link governs: when two candidates share the subject (at
        # least half the question's terms) within the same recorded scope
        # and the newer one is a real match, the older one never outranks
        # it, whatever its overlap. Another customer is not a correction.
        subj = [({stem(t) for t in re.findall(r"[a-z0-9]{3,}", row["question"].lower())} - _STOP, ts)
                for _, _, row, ts in scored]
        scopes = [_memory_scope_signature(row) for _, _, row, _ in scored]
        eff = [x[0] for x in scored]
        demoted: dict[int, str] = {}
        for i in range(len(scored)):
            for j in range(len(scored)):
                if i == j or subj[j][1] <= subj[i][1] or scopes[i] != scopes[j]:
                    continue
                a, b = subj[i][0], subj[j][0]
                if a and b and len(a & b) / min(len(a), len(b)) >= 0.5 and scored[j][1] >= 0.5:
                    eff[i] = min(eff[i], eff[j] - 0.01)
                    demoted[i] = scored[j][2]["id"]
        scored = [(eff[k], scored[k][1], scored[k][2], scored[k][3], demoted.get(k, ""))
                  for k in range(len(scored))]
        scored.sort(key=lambda x: (-x[0], -x[3]))
        out: list[dict] = []
        kept_scopes: list[tuple[set[str], tuple]] = []
        for eff, sim, row, ts, demoted_by in scored:
            terms = {stem(t) for t in re.findall(r"[a-z0-9]{3,}", row["question"].lower())} - _STOP
            scope = _memory_scope_signature(row)
            same = False
            for kt, kept_scope in kept_scopes:
                if scope == kept_scope and kt and terms and len(kt & terms) / min(len(kt), len(terms)) >= 0.7:
                    same = True
                    break
            if same:
                continue
            kept_scopes.append((terms, scope))
            item = dict(row)
            item.pop("embedding", None)
            item["similarity"] = round(sim, 2)
            item["score"] = round(eff, 3)
            item["demoted_by"] = demoted_by
            # A person stands behind a signed row; anything else is a
            # prediction for the reader, never an approval to reuse.
            item["signed"] = authorized(row)
            out.append(item)
            if len(out) >= limit:
                break
        return out


def _memory_scope_signature(row) -> tuple:
    """Conservative equivalence for suppressing historical candidates.

    Similar questions alone do not identify the same decision. Compare
    recorded scope, including missing versus stated facts, without mining
    an answer or context for facts about the current task. Context-only
    legacy rows remain distinct unless their recorded context agrees.
    Explicit supersession is handled separately by the memory predicate.
    """
    def text(value):
        return " ".join((value or "").lower().split())

    def structured(value):
        try:
            return json.dumps(json.loads(value or "null"), sort_keys=True, separators=(",", ":"))
        except (ValueError, TypeError):
            return str(value or "")

    facts = parse_facts(row["facts"])
    fact_key = tuple(sorted((key, text(value)) for key, value in facts.items()))
    # Malformed legacy scope is uncertainty, not proof of an empty scope.
    if not facts and row["facts"] and structured(row["facts"]) != "{}":
        fact_key = (("", str(row["facts"])),)
    return (row["repo"], row["scope_key"], fact_key, text(row["context"]),
            (row["path"] or "").strip().lstrip("/"), structured(row["scope_paths"]),
            structured(row["applicability"]))


_STOP = {"the", "and", "for", "this", "that", "does", "with", "from", "are", "what",
         "which", "when", "how", "who", "into", "get", "one", "should", "can", "our",
         "there", "their", "they", "have", "has", "was", "were", "will", "would", "could",
         "about", "than", "then", "them", "you", "your", "not", "but", "any", "all"}

MEMORY_HALF_LIFE_DAYS = 180.0
MEMORY_FRESH_DAYS = 90.0


def _recency(updated_at: float) -> float:
    age_days = max(0.0, (time.time() - updated_at) / 86400.0)
    decayed = max(0.0, age_days - MEMORY_FRESH_DAYS)
    return math.exp(-decayed * math.log(2) / MEMORY_HALF_LIFE_DAYS)
