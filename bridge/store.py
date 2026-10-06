"""Persistent decisions, explicit ownership, and auditable human answers."""

import fnmatch
import json
import re
import sys
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .llm import clip_marked


class Invalid(ValueError):
    pass


BRIEF_MODES = ("off", "static")


def brief_mode(graph):
    from .briefing import mode
    return mode(graph)


def now():
    return datetime.now(timezone.utc).isoformat()


_REPO_SCHEME_RE = re.compile(r"^(?:https?|ssh|git|file)://")
_REPO_SCP_RE = re.compile(r"^[\w.-]+@([\w.-]+):")


def repo_key(repo):
    """A repository's identity as the inbox and the graph scope it:
    'owner/name' for a hosted repository however it was written
    (acme/platform, https://github.com/acme/platform.git,
    git@github.com:acme/platform), and the checkout's own name for a local
    path (/src/platform is 'platform'). Two owners with the same
    repository name never collapse into one; Graph.resolve_repo maps a
    hosted identity onto a graph ingested from a bare checkout name when
    that is unambiguous."""
    s = (repo or "").strip().lower().rstrip("/")
    if not s:
        return ""
    local = s.startswith(("/", "./", "../", "~")) or re.match(r"^[a-z]:[\\/]", s) is not None
    s = _REPO_SCHEME_RE.sub("", s)
    s = _REPO_SCP_RE.sub(lambda m: m.group(1) + "/", s)
    s = re.sub(r"\.git$", "", s)
    parts = [p for p in re.split(r"[\\/]+", s) if p and p != "."]
    if not parts:
        return ""
    if local:
        return parts[-1]
    if "." in parts[0] and len(parts) >= 3:
        # host/owner/name, possibly with more path after it
        return "/".join(parts[1:3])
    if len(parts) > 2:
        return parts[-1]
    return "/".join(parts)


def field(data, key, default=None, limit=12000):
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise Invalid(f"{key} must be nonempty text, at most {limit} characters")
    return value.strip()


def answer_hash(answer):
    """A short fingerprint of an answer's text: what a signature covers."""
    import hashlib
    return hashlib.sha256(" ".join((answer or "").split()).encode()).hexdigest()[:16]


def rule_expiry(raw) -> str:
    """A rule's expiry as an ISO instant: a date means the end of that
    day (UTC); empty means never; the past is refused."""
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        if len(text) == 10:
            expires = datetime.fromisoformat(text + "T23:59:59+00:00")
        else:
            expires = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
    except ValueError:
        raise Invalid("expires must be a date (YYYY-MM-DD) or an ISO timestamp")
    if expires <= datetime.now(timezone.utc):
        raise Invalid("expires must be in the future")
    return expires.isoformat()


def check_revision(data, row):
    """Optimistic concurrency for a human action on a decision: the
    caller names the revision it reviewed (the row's updated_at), and a
    row that moved on since is refused rather than signed blind."""
    expected = data.get("expected_updated_at")
    if expected is None or expected == "":
        raise Invalid("expected_updated_at is required: name the revision you reviewed (the decision's updated_at)")
    if not isinstance(expected, str) or expected != row["updated_at"]:
        raise Invalid("This decision changed while you were reviewing it. Reopen it before acting on it.")


def ownership_rows(db, repo="", limit=200):
    """The live ownership rows (no valid_to), strongest first within each
    path; one repository when named, and all rows when limit is 0."""
    sql = ("SELECT rowid AS id, repo, path_prefix, engineer, source, weight, evidence FROM ownership "
           "WHERE valid_to IS NULL")
    args = []
    if repo:
        sql += " AND repo=?"
        args.append(repo)
    sql += " ORDER BY repo, path_prefix, weight DESC"
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit))
    return [dict(row) for row in db.execute(sql, args)]


def inbox_counts(db, overdue_hours=72):
    """What waits on people, counted in the database: open questions,
    unsigned answers, decisions put in doubt by a correction, questions
    with no owner, and questions open longer than the overdue window."""
    from .graph import BLOCKING_SQL
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=overdue_hours)).isoformat()
    row = db.execute(f"""SELECT
        sum(CASE WHEN d.status='pending' THEN 1 ELSE 0 END) AS pending,
        sum(CASE WHEN d.owner_id IS NULL AND (d.status='pending' OR (d.signoff='required'
                 AND d.status IN ('resolved','partial','assumed','proposed'))) THEN 1 ELSE 0 END) AS unrouted,
        sum(CASE WHEN d.status IN ('resolved','partial','assumed','proposed') AND d.status != 'approved'
                 AND d.signoff NOT IN ('signed','rule') THEN 1 ELSE 0 END) AS signoff_required,
        sum(CASE WHEN d.needs_review=1 THEN 1 ELSE 0 END) AS needs_review,
        sum(CASE WHEN d.status='pending' AND d.created_at < ? THEN 1 ELSE 0 END) AS overdue,
        sum(CASE WHEN {BLOCKING_SQL} THEN 1 ELSE 0 END) AS needs_you
        FROM decisions d WHERE d.draft=0""", (cutoff,)).fetchone()
    return {k: int(row[k] or 0) for k in ("pending", "unrouted", "signoff_required", "needs_review", "overdue", "needs_you")}


def graph_summary(db):
    """The shape of the ingested graph: its repositories and how many
    records and engineers it holds."""
    return {"repos": [row["repo"] for row in db.execute("SELECT DISTINCT repo FROM ownership UNION SELECT DISTINCT repo FROM intents")],
            "intents": db.execute("SELECT count(*) c FROM intents").fetchone()["c"],
            "engineers": db.execute("SELECT count(*) c FROM engineers").fetchone()["c"]}


class Store:
    def __init__(self, path):
        from .database import location, is_postgres
        self.path = location(path)
        if not is_postgres(self.path):
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS owners (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, team TEXT NOT NULL,
                    patterns TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, agent TEXT NOT NULL,
                    repo TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS decisions (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    question TEXT NOT NULL, context TEXT NOT NULL, path TEXT NOT NULL,
                    owner_id TEXT REFERENCES owners(id), routing_reason TEXT NOT NULL,
                    status TEXT NOT NULL, prediction TEXT, source_id TEXT,
                    answer TEXT, rationale TEXT, answered_by TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT,
                    run_id TEXT, kind TEXT NOT NULL, detail TEXT NOT NULL,
                    created_at TEXT NOT NULL);
            """)
            from . import briefing, delivery, execution_store, graph, interview
            execution_store.migrate(db)
            graph.migrate(db)
            delivery.migrate(db)
            briefing.migrate(db)
            interview.migrate(db)
            # executescript commits as it goes; the statements that read
            # the schema and write rows run in one transaction so two
            # processes opening at once (the inbox and an MCP server)
            # migrate serially.
            db.execute("BEGIN IMMEDIATE")
            execution_store.backfill(db)
            graph.backfill(db)
            if is_postgres(self.path):
                from .database import migrate_postgres
                migrate_postgres(db)
        self._graph = None
        self._delivery = None

    @property
    def delivery(self):
        """The outbox (bridge/delivery.py). Disabled until a transport is
        attached (Store.connect_delivery); enqueue is then a no-op."""
        if self._delivery is None:
            from .delivery import Delivery
            self._delivery = Delivery(self)
        return self._delivery

    def connect_delivery(self, transport, fallback_channel="", base_url=""):
        from .delivery import Delivery
        self._delivery = Delivery(self, transport=transport, fallback_channel=fallback_channel, base_url=base_url)
        if getattr(transport, "name", "") == "slack":
            self.graph.set_setting("slack_connected", "1")
            if fallback_channel:
                self.graph.set_setting("slack_fallback_channel", fallback_channel)
        return self._delivery

    def notify(self, decision_id, kind, to=""):
        """Queue a notification for one state of one decision; nothing
        happens when no channel is connected."""
        delivery = self.delivery
        if not delivery.enabled:
            return None
        try:
            with self.graph.transaction():
                return delivery.enqueue(decision_id, kind, to=to)
        except Exception as error:
            print(f"Raven delivery: could not queue {kind} for {decision_id}: {error}")
            return None

    @property
    def graph(self):
        """The org graph and decision memory behind the resolution ladder
        (bridge/graph.py), over this same database file."""
        if self._graph is None:
            from .graph import Graph
            self._graph = Graph(self.path)
        return self._graph

    @contextmanager
    def connect(self):
        from .database import connect
        db = connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def event(self, db, kind, detail, decision_id=None, run_id=None):
        db.execute("INSERT INTO events(decision_id,run_id,kind,detail,created_at) VALUES(?,?,?,?,?)",
                   (decision_id, run_id, kind, detail, now()))

    def add_owner(self, data):
        """A configured owner: a person, with the path patterns they decide
        for recorded as verified authority (a catch-all pattern makes them
        a repository-wide contact, not the decider of everything). The
        patterns also stay on the owner row for the inbox's own fallback
        routing when no graph exists."""
        name, team = field(data, "name", limit=100), field(data, "team", limit=100)
        patterns = field(data, "patterns", "*", limit=2000)
        graph = self.graph
        with graph.transaction() as db:
            pid = graph.add_person(name, team=team, email=str(data.get("email") or ""),
                                   github_login=str(data.get("github_login") or ""),
                                   slack_id=str(data.get("slack_id") or ""))
            row = db.execute("SELECT id FROM owners WHERE person_id=? ORDER BY created_at LIMIT 1", (pid,)).fetchone()
            owner_id = row["id"]
            db.execute("UPDATE owners SET team=?, patterns=? WHERE id=?", (team, patterns, owner_id))
            for pattern in patterns.split(","):
                pattern = pattern.strip()
                if not pattern:
                    continue
                if pattern.strip("/").replace("*", "") == "":
                    graph.add_authority("repo", "", "knows", person_id=pid, source="config",
                                        note="catch-all owner pattern")
                else:
                    graph.add_authority("path", pattern, "decides", person_id=pid, source="config")
        return {"id": owner_id, "name": name, "team": team, "patterns": patterns, "person_id": pid}

    # ---------------- people, teams, authority, settings ----------------

    def add_person(self, data):
        name = field(data, "name", limit=100)
        aliases = data.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [a.strip() for a in re.split(r"[,\n;]+", aliases) if a.strip()]
        if not isinstance(aliases, list) or any(not isinstance(a, str) or len(a) > 200 for a in aliases):
            raise Invalid("aliases must be a list of short names, emails or handles")
        role = str(data.get("role") or "member")
        if role not in ("admin", "member", "viewer"):
            raise Invalid("role must be admin, member or viewer")
        graph = self.graph
        with graph.transaction():
            pid = graph.add_person(name, email=str(data.get("email") or "")[:200],
                                   github_login=str(data.get("github_login") or "")[:100],
                                   slack_id=str(data.get("slack_id") or "")[:100], aliases=aliases,
                                   team=str(data.get("team") or "")[:100], role=role,
                                   source=str(data.get("source") or "config")[:40])
            if data.get("active") in (False, 0, "0", "false"):
                graph.db.execute("UPDATE people SET active=0, updated_at=? WHERE id=?", (now(), pid))
                graph._bump("")
        return self.person(pid)

    def person(self, person_id):
        p = self.graph.get_person(person_id)
        if p is None:
            raise Invalid("Person not found")
        p = dict(p)
        p["authority"] = [a for a in self.authority() if a["person_id"] == person_id]
        p["teams"] = [t["name"] for t in self.graph.teams() if person_id in t["members"]]
        return p

    def people(self):
        graph = self.graph
        rows = self.authority()
        teams = graph.teams()
        out = []
        for p in graph.people(active_only=False):
            d = dict(p)
            d["authority"] = [a for a in rows if a["person_id"] == p["id"]]
            d["teams"] = [t["name"] for t in teams if p["id"] in t["members"]]
            out.append(d)
        return out

    def _person_ref(self, value):
        """A person by id, email, login, name or alias; refused when unknown
        or ambiguous, so no authority is recorded for a guessed identity."""
        text = str(value or "").strip()
        if not text:
            raise Invalid("name a person (id, email, GitHub login or full name)")
        person = self.graph.find_person(text)
        if person is None:
            raise Invalid(f"no known person matches {text!r}; add them under People first")
        return person

    def add_team(self, data):
        name = field(data, "name", limit=100)
        handle = str(data.get("handle") or "")[:100]
        members = data.get("members") or []
        if isinstance(members, str):
            members = [m.strip() for m in re.split(r"[,\n;]+", members) if m.strip()]
        graph = self.graph
        with graph.transaction():
            tid = graph.add_team(name, handle=handle, source=str(data.get("source") or "config")[:40])
            ids = [self._person_ref(m)["id"] for m in members]
            graph.set_team_members(tid, ids, replace=bool(data.get("replace")))
        return next(t for t in graph.teams() if t["id"] == tid)

    def add_authority(self, data):
        from .graph import AUTHORITY_ROLES, AUTHORITY_SCOPES
        scope_kind = str(data.get("scope_kind") or "path")
        role = str(data.get("role") or "decides")
        if scope_kind not in AUTHORITY_SCOPES:
            raise Invalid(f"scope_kind must be one of {', '.join(AUTHORITY_SCOPES)}")
        if role not in AUTHORITY_ROLES:
            raise Invalid(f"role must be one of {', '.join(AUTHORITY_ROLES)}")
        scope = str(data.get("scope") or "").strip()
        if scope_kind != "repo" and not scope:
            raise Invalid("scope names a path pattern or a decision category")
        if scope_kind == "category":
            from .scopes import CATEGORIES
            if scope.lower() not in CATEGORIES:
                raise Invalid(f"category must be one of {', '.join(sorted(CATEGORIES))}")
        graph = self.graph
        person_id = team_id = ""
        if data.get("team"):
            team = next((t for t in graph.teams() if t["id"] == data["team"] or t["handle"] == str(data["team"]).lstrip("@").lower()
                         or t["name"].lower() == str(data["team"]).lower()), None)
            if team is None:
                raise Invalid("no known team matches; add it first")
            team_id = team["id"]
        else:
            person_id = self._person_ref(data.get("person"))["id"]
        repo = repo_key(str(data.get("repo") or ""))
        effective_to = str(data.get("effective_to") or "")
        if effective_to:
            try:
                datetime.fromisoformat(effective_to.replace("Z", "+00:00"))
            except ValueError:
                raise Invalid("effective_to must be an ISO date or timestamp")
        with graph.transaction():
            aid = graph.add_authority(scope_kind, scope, role, person_id=person_id, team_id=team_id, repo=repo,
                                      source=str(data.get("source") or "config")[:40],
                                      asserted_by=str(data.get("asserted_by") or data.get("by") or "")[:100],
                                      accepted=data.get("accepted", True) not in (False, 0, "0", "false"),
                                      note=str(data.get("note") or ""), effective_to=effective_to)
        return next(a for a in self.authority() if a["id"] == aid)

    def end_authority(self, authority_id):
        graph = self.graph
        with graph.transaction():
            graph.end_authority(authority_id)
        return {"id": authority_id, "ended": True}

    def authority(self, repo=""):
        graph = self.graph
        people = {p["id"]: p for p in graph.people(active_only=False)}
        teams = {t["id"]: t for t in graph.teams()}
        sql = "SELECT * FROM authority WHERE ended_at=''" + (" AND (repo=? OR repo='')" if repo else "") + " ORDER BY created_at DESC"
        out = []
        for r in graph.db.execute(sql, (repo,) if repo else ()):
            d = dict(r)
            d["who"] = (people.get(d["person_id"]) or {}).get("name") or (teams.get(d["team_id"]) or {}).get("name") or ""
            d["is_team"] = bool(d["team_id"])
            out.append(d)
        return out

    def settings(self):
        graph = self.graph
        coordinator = graph.coordinator()
        return {"coordinator": coordinator, "require_verified_route": graph.get_setting("require_verified_route") == "1",
                "slack_fallback_channel": graph.get_setting("slack_fallback_channel"),
                "overdue_hours": self.overdue_hours(),
                "slack_capture_repo": graph.get_setting("slack_capture_repo"),
                "auto_rules": graph.get_setting("auto_rules") == "1",
                "notify_requester": graph.get_setting("notify_requester") == "1",
                "brief_mode": brief_mode(graph),
                "coordinators": {k[len("coordinator:"):]: graph.get_person(v)
                                 for k, v in graph.db.execute("SELECT key, value FROM settings WHERE key LIKE 'coordinator:%'")}}

    def update_settings(self, data):
        graph = self.graph
        with graph.transaction():
            if "coordinator" in data:
                repo = repo_key(str(data.get("repo") or ""))
                key = f"coordinator:{repo}" if repo else "coordinator"
                if data["coordinator"] in ("", None):
                    graph.set_setting(key, "")
                else:
                    graph.set_setting(key, self._person_ref(data["coordinator"])["id"])
            if "require_verified_route" in data:
                on = data["require_verified_route"] in (True, 1, "1", "true", "on")
                graph.set_setting("require_verified_route", "1" if on else "0")
            if "slack_fallback_channel" in data:
                graph.set_setting("slack_fallback_channel", str(data["slack_fallback_channel"] or "").strip()[:100])
            if "overdue_hours" in data:
                try:
                    hours = int(data["overdue_hours"])
                except (TypeError, ValueError):
                    raise Invalid("overdue_hours must be a whole number of hours")
                if not 1 <= hours <= 24 * 90:
                    raise Invalid("overdue_hours must be between 1 and 2160")
                graph.set_setting("overdue_hours", str(hours))
            if "slack_capture_repo" in data:
                graph.set_setting("slack_capture_repo", repo_key(str(data["slack_capture_repo"] or "")))
            if "brief_mode" in data:
                mode = str(data["brief_mode"] or "")
                if mode not in BRIEF_MODES:
                    raise Invalid(f"brief_mode must be one of {', '.join(BRIEF_MODES)}")
                graph.set_setting("brief_mode", mode)
                graph.append_event("setting_changed", {"key": "brief_mode", "value": mode})
            for key in ("auto_rules", "notify_requester"):
                if key in data:
                    on = data[key] in (True, 1, "1", "true", "on")
                    graph.set_setting(key, "1" if on else "0")
                    graph.append_event("setting_changed", {"key": key, "value": "1" if on else "0"})
        return self.settings()

    def add_run(self, data):
        values = (uuid.uuid4().hex[:12], field(data, "title", limit=300),
                  field(data, "agent", "Coding agent", 100), field(data, "repo", "local", 300), "working", now())
        with self.connect() as db:
            db.execute("INSERT INTO runs(id, title, agent, repo, status, updated_at) VALUES(?,?,?,?,?,?)", values)
            self.event(db, "run_started", values[1], run_id=values[0])
            return dict(db.execute("SELECT * FROM runs WHERE id=?", (values[0],)).fetchone())

    def update_run(self, run_id, data):
        """A run's status. Completing is refused while any node waits on a
        person: an open question, an answer nobody signed, a decision put
        in doubt by a correction upstream, or a duplicate whose canonical
        decision is in one of those states. Evidence is not authorization."""
        status = field(data, "status")
        if status not in {"working", "completed"}:
            raise Invalid("status must be working or completed")
        if status == "completed":
            # Both sweeps write, on the graph's own connection. They run
            # before the write transaction below opens: inside it they
            # would wait on a lock this thread is holding.
            self.graph.expire_rules()
            self.graph.expire_drafts()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                raise Invalid("Run not found")
            if db.execute("SELECT 1 FROM executions WHERE run_id=?", (run_id,)).fetchone():
                raise Invalid("Managed run status comes from the execution provider")
            if status == "completed":
                if db.execute("SELECT 1 FROM scope_clarifications WHERE task_id=?", (run_id,)).fetchone():
                    raise Invalid("Refused: missing scope facts must be clarified before this task can finish. "
                                  "Read scope_clarifications on bridge_get_tree, then retry bridge_add_node "
                                  "with the same client_ref and the actual facts.")
                # A node still being written is invisible to every reader,
                # but the gate counts it: finishing in that window would
                # be finishing over a decision about to appear.
                if db.execute("SELECT 1 FROM decisions WHERE run_id=? AND draft=1", (run_id,)).fetchone():
                    raise Invalid("Refused: a node on this task is being written right now. "
                                  "Finish once the write that started it has returned.")
                blocking = self.graph.blocking_nodes(run_id, sweep=False)
                if blocking:
                    shown = "; ".join(f"{b['id']} ({b['why']})" for b in blocking[:4])
                    more = f"; and {len(blocking) - 4} more" if len(blocking) > 4 else ""
                    # A required follow-up waits on the agent to adopt it,
                    # everything else on a person.
                    adopt = [b for b in blocking if b["why"].startswith("a follow-up a person marked required")]
                    people = len(blocking) - len(adopt)
                    lead = (f"{people} node{'s' if people > 1 else ''} still wait{'' if people > 1 else 's'} on a "
                            "person" if people else "")
                    if adopt:
                        lead += (" and " if lead else "") + (f"{len(adopt)} required follow-up"
                                                             f"{'s' if len(adopt) > 1 else ''} not adopted")
                    raise Invalid(f"Refused: {lead}: {shown}{more}. "
                                  + ("Each named person answers or signs in the inbox or Slack before the task can "
                                     "finish: every required approver signs, and an answer found in records or "
                                     "memory is evidence, not sign-off, until a person signs it." if people else
                                     "Adopt each with bridge_add_node(adopt=<node_id>); the task finishes once "
                                     "they are answered."))
            db.execute("UPDATE runs SET status=?,updated_at=? WHERE id=?", (status, now(), run_id))
            self.event(db, "run_updated", status, run_id=run_id)
        return {"id": run_id, "status": status}

    def candidates(self, db, question, owner_id=None, repo=""):
        """Prior decisions that answer a question: stemmed lexical overlap,
        hashed-embedding cosine, and FTS5 when available, over question,
        context, answer and rationale, recency-weighted, superseded rows
        excluded. See Graph.memory_search. The db argument is kept for the
        callers that pass one; retrieval runs on the graph's own
        connection and never inside a write transaction."""
        return self.graph.memory_search(question, limit=5, repo=repo, owner_id=owner_id)

    def search(self, query, repo=""):
        matches = self.candidates(None, query, repo=repo)
        return {"matches": matches, "notice": "Prior approvals are evidence for their original context. New requests still require review."}

    def request(self, data, *, _db=None):
        question = field(data, "question", limit=2000)
        context = field(data, "context")
        path = field(data, "path", "unknown", 1000)
        run_id = field(data, "run_id", limit=100)
        with (nullcontext(_db) if _db is not None else self.connect()) as db:
            if _db is None:
                db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run or run["status"] == "completed":
                raise Invalid("An active run is required")
            owner_id, reason = data.get("owner_id") or None, "No matching owner. Assign someone in the inbox."
            if owner_id:
                if not isinstance(owner_id, str) or not db.execute("SELECT 1 FROM owners WHERE id=?", (owner_id,)).fetchone():
                    raise Invalid("Owner not found")
                reason = "Explicitly selected for this request"
            else:
                # Last matching rule wins, following the useful ordering convention of CODEOWNERS.
                for owner in db.execute("SELECT * FROM owners ORDER BY created_at,id"):
                    for pattern in owner["patterns"].split(","):
                        pattern = pattern.strip().lstrip("/")
                        if pattern and fnmatch.fnmatchcase(path.lstrip("/"), pattern):
                            owner_id, reason = owner["id"], f"Path {path} matches {pattern}"
            scope = self.graph.resolve_repo(repo_key(run["repo"]))
            candidates = self.candidates(db, question, owner_id, repo=scope) if owner_id else []
            from .graph import applicability_status, parse_facts
            stated_facts = parse_facts(data.get("facts"))
            candidates = [candidate for candidate in candidates
                          if (source := self.graph.get_decision(candidate["id"], exact=True)) is not None
                          and applicability_status(source, path, stated_facts)[0]]
            prior = candidates[0] if candidates else None
            decision_id, timestamp = uuid.uuid4().hex[:12], now()
            db.execute("""INSERT INTO decisions(id, run_id, question, context, path, owner_id, routing_reason,
                status, prediction, source_id, answer, rationale, answered_by, created_at, updated_at,
                kind, category, repo) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                decision_id, run_id, question, context, path, owner_id, reason, "pending",
                prior["answer"] if prior else None, prior["id"] if prior else None,
                None, None, None, timestamp, timestamp,
                "prediction" if prior else "new", field(data, "category", "policy", 40),
                scope))
            db.execute("UPDATE runs SET status='needs_judgment',updated_at=? WHERE id=?", (timestamp, run_id))
            self.event(db, "judgment_requested", reason, decision_id, run_id)
            if prior:
                self.event(db, "prediction_suggested", f"Unapproved suggestion from decision {prior['id']}; keyword similarity only", decision_id, run_id)
            if _db is not None:
                return {"id": decision_id}
        if owner_id:
            self.notify(decision_id, "ask")
        return self.get_decision(decision_id)

    def get_decision(self, decision_id):
        with self.connect() as db:
            row = db.execute("""SELECT d.*,o.name AS owner_name,o.team AS owner_team,
                r.title AS run_title,r.agent,r.repo FROM decisions d
                LEFT JOIN owners o ON o.id=d.owner_id JOIN runs r ON r.id=d.run_id WHERE d.id=?""", (decision_id,)).fetchone()
            if not row:
                raise Invalid("Decision not found")
            result = dict(row)
            result.pop("embedding", None)
            result["events"] = [dict(e) for e in db.execute("SELECT * FROM events WHERE decision_id=? ORDER BY id", (decision_id,))]
            from .graph import authorized, blocks_finish
            probe = row
            if row["status"] == "duplicate" and row["superseded_by"]:
                canonical = db.execute("""SELECT d.*,o.name AS owner_name FROM decisions d
                    LEFT JOIN owners o ON o.id=d.owner_id WHERE d.id=?""", (row["superseded_by"],)).fetchone()
                if canonical is not None:
                    probe = canonical
                    result["canonical"] = {k: canonical[k] for k in ("id", "run_id", "status", "answer", "answered_by",
                                                                      "signoff", "signed_by", "owner_name",
                                                                      "needs_review", "updated_at")}
            # A person still has to act: an open question, an unsigned
            # answer, or a decision put in doubt upstream. Evidence found
            # in records or memory is not approval.
            result["approval_pending"] = blocks_finish(probe)
            result["authorized"] = authorized(probe)
            from .execution_store import revision
            result["revision"] = revision(db, decision_id)
            return result

    def assign(self, decision_id, data, actor=None):
        """Reassign this one request to another owner. Nothing is learned
        from it: a hand-off that should shape future routing is refer().
        An open question and a node waiting for sign-off can both be
        assigned: a node Raven could not route is exactly the one an
        operator has to put in front of somebody, whichever rung
        answered it. The actor must be the decision's owner, verified
        for its scope, the coordinator, or an admin overriding."""
        from . import authz
        owner_id = field(data, "owner_id", limit=100)
        basis = authz.check(self.graph, actor, self.get_decision(decision_id), "assign")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute("SELECT * FROM owners WHERE id=?", (owner_id,)).fetchone()
            decision = db.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
            assignable = decision is not None and (
                decision["status"] == "pending"
                or (decision["signoff"] == "required"
                    and decision["status"] in ("resolved", "partial", "assumed", "proposed")))
            if not owner or not assignable:
                raise Invalid("A valid owner and an open question or a node waiting for sign-off are required")
            open_question = decision["status"] == "pending"
            by = (actor.name if actor is not None and actor.id else str(data.get("by") or "the inbox"))[:100]
            what = "question" if open_question else "sign-off"
            db.execute("UPDATE decisions SET owner_id=?,routing_reason=?,updated_at=?,"
                       "actor_id=?,actor_name=?,actor_basis=? WHERE id=?",
                       (owner_id, f"Reassigned to {owner['name']} by {by} (this request only)", now(),
                        actor.id if actor is not None else "", by, basis, decision_id))
            if open_question:
                db.execute("UPDATE decisions SET prediction=NULL, source_id=NULL WHERE id=?", (decision_id,))
            self.event(db, "owner_changed",
                       f"{what.capitalize()} assigned to {owner['name']} by {by} ({basis})"
                       + ("; previous suggestion cleared" if open_question else ""),
                       decision_id, decision["run_id"])
        self.notify(decision_id, "reassigned")
        return self.get_decision(decision_id)

    def handon_scopes(self, decision, actor=None) -> dict:
        """What handing this decision on may teach Raven, and what it
        teaches unless the person picks: the decision's one topic; with
        no topic, the directory of its file, unless the person handing
        it on decides that file; else nothing. A question that speaks of
        two topics teaches nothing by itself, since one hand-on does not
        say which of them the other person decides. Measured live: a docs
        question asking whether the release notes should say "security
        fix" taught Raven that the person it went to decides security.
        `options` are what the person may pick instead; "this question
        only" is always one of them."""
        from .graph import _pattern_covers
        from .scopes import primary_scopes
        repo = decision.get("repo") or ""
        topics = primary_scopes(decision.get("question") or "", decision.get("category") or "")
        options = [{"scope_kind": "category", "scope": t, "label": f"{t} decisions"} for t in topics]
        path = (decision.get("path") or "").lstrip("/")
        place = ""
        if path and path != "unknown":
            place = (path.rsplit("/", 1)[0] + "/*") if "/" in path else path
            options.append({"scope_kind": "path", "scope": place, "label": f"decisions on {place}"})
        only = {"scope_kind": "none", "scope": "", "label": "this question only"}
        options.append(only)
        # Someone the map says decides this very file saying "not me" is
        # saying the topic is not theirs, not the place: learning the place
        # would hand their own file, and its neighbours, to whoever
        # answered this one question.
        held = bool(place) and actor is not None and bool(actor.id) and any(
            r["source"] != "referral" and r["scope_kind"] == "path" and _pattern_covers(r["scope"], path)
            and (r["person_id"] == actor.id or (r["team"] and actor.id in r["team"]["members"]))
            for r in self.graph.deciders(repo))
        if len(topics) == 1:
            default, why = options[0], ""
        elif topics:
            default, why = only, (f"the question speaks of {' and '.join(topics)}, and one hand-on does not say "
                                  "which of those they decide")
        elif place and not held:
            default, why = next(o for o in options if o["scope_kind"] == "path"), ""
        elif place:
            default, why = only, (f"the question names no topic, and {actor.name} decides the file it is about"
                                  if actor is not None else "the question names no topic")
        else:
            default, why = only, "the question names no topic and no file"
        return {"default": default, "options": options, "why_none": why}

    def claim_slack_question(self, decision_id, person_id, actor):
        """Assign an unplaced question from its Slack triage thread, once.

        No approval and no standing authority is created by this action.
        The conditional write prevents two channel replies stealing it.
        """
        from . import authz
        if actor.kind != "slack":
            raise Invalid("This action requires an authenticated Slack reply")
        graph = self.graph
        person = graph.get_person(person_id)
        if not person or not person["active"] or not person["slack_id"] or person["role"] == "viewer":
            raise Invalid("The contact must be an active Slack member")
        with graph.transaction():
            decision = self.get_decision(decision_id)
            authz.check(graph, actor, decision, "assign")
            owner = graph.owner_id_for(person["name"])
            result = graph.db.execute(
                "UPDATE decisions SET owner_id=?,routing_reason=?,owner_evidence=?,updated_at=?, "
                "actor_id=?,actor_name=?,actor_basis='slack-triage' WHERE id=? "
                "AND (owner_id IS NULL OR owner_id='') AND (status='pending' OR signoff='required')",
                (owner, f"Slack referral from {actor.name} for this question only",
                 f"Slack referral from {actor.name} for this question only", now(), actor.id, actor.name, decision_id))
            if result.rowcount != 1:
                raise Invalid("This question already has a contact or no longer needs an answer")
            graph.append_event("owner_changed", {"task_id": decision["run_id"], "decision_id": decision_id,
                                                "by": actor.name, "to": person["name"], "basis": "slack-triage"})
        self.notify(decision_id, "reassigned")

    def refer(self, decision_id, data, actor=None):
        """Hand a decision to the person who owns decisions like it, and
        learn from it: the decision is reassigned, and a referral is
        recorded in the authority map for the scope `handon_scopes`
        names (or the one the person picked: `scope_kind` category, path
        or repo with `scope`, or none), asserted by the referrer and
        accepted the moment the referred-to person answers. From then on
        routing sends decisions in that scope to them first. The actor
        must be the decision's owner, verified for its scope, the
        coordinator, or an admin overriding."""
        from . import authz
        from .graph import AUTHORITY_ROLES
        from .scopes import CATEGORIES
        person = self._person_ref(data.get("person"))
        basis = authz.check(self.graph, actor, self.get_decision(decision_id), "refer")
        by = (actor.name if actor is not None and actor.id else str(data.get("by") or "the inbox"))[:100]
        role = str(data.get("role") or "decides")
        if role not in AUTHORITY_ROLES:
            raise Invalid(f"role must be one of {', '.join(AUTHORITY_ROLES)}")
        graph = self.graph
        decision = self.get_decision(decision_id)
        if data.get("expected_updated_at"):
            check_revision(data, decision)
        if decision["status"] not in ("pending",) and not (decision["status"] in ("resolved", "partial", "assumed", "proposed")
                                                         and decision["signoff"] == "required"):
            raise Invalid("Only an open question or a node waiting for sign-off can be handed on")
        owner_id = graph.owner_id_for(person["name"])
        choice = str(data.get("scope_kind") or "").strip().lower()
        explicit = str(data.get("scope") or "").strip()
        if choice in ("none", "this", "contact"):
            targets, why_none = [], "it was handed on for this question only"
        elif choice == "category":
            if explicit.lower() not in CATEGORIES:
                raise Invalid(f"A category scope is one of {', '.join(sorted(CATEGORIES))}")
            targets, why_none = [("category", explicit.lower())], ""
        elif choice == "path":
            if not explicit.strip("/"):
                raise Invalid("A path scope needs the path, like src/billing/*")
            targets, why_none = [("path", explicit.lstrip("/"))], ""
        elif choice == "repo":
            targets, why_none = [("repo", "")], ""
        elif choice:
            raise Invalid("scope_kind must be category, path, repo, contact or none")
        else:
            plan = self.handon_scopes(decision, actor)
            default = plan["default"]
            targets = [] if default["scope_kind"] == "none" else [(default["scope_kind"], default["scope"])]
            why_none = plan["why_none"]
        repo = decision["repo"] or ""
        learned = [(f"{scope} decisions" if kind == "category" else f"decisions on {scope}" if kind == "path"
                    else "every decision") + (f" in {repo}" if repo else "") for kind, scope in targets]
        # Why it is theirs now: the hand-on, in the words of the person who
        # made it. The previous owner's routing evidence is not a reason to
        # give the next one. Measured live: "Why you" in the handed-on
        # message quoted the other person's repository authority.
        said = clip_marked(" ".join(str(data.get("note") or "").split()), 200, "the hand-on note has the rest")
        why_them = (f"handed on by {by}" + (f": \"{said}\"" if said else "")
                    + (f"; once you answer, Raven routes {', '.join(learned)} to you first" if learned else ""))
        with graph.transaction() as db:
            from .routing_memory import record
            previous = graph.find_person(decision.get("owner_name") or "")
            if previous and previous['id'] != person['id'] and choice not in ('none', 'this'):
                record(graph, decision_id, previous['id'], 'declined')
            if choice in ('none', 'this'):
                graph.append_event('route_learning_optout', {'decision_id': decision_id, 'by': by})
            # The prediction was for the previous owner and goes with them;
            # an open question left marked as a prediction kept its
            # "Prediction" pill in the inbox with nothing predicted.
            db.execute("UPDATE decisions SET owner_id=?, routing_reason=?, owner_evidence=?, prediction=NULL, "
                       "source_id=NULL, kind=CASE WHEN status='pending' AND kind='prediction' THEN 'new' ELSE kind END, "
                       "updated_at=?, actor_id=?, actor_name=?, actor_basis=? WHERE id=?",
                       (owner_id, f"Referred to {person['name']} by {by}", why_them, now(),
                        actor.id if actor is not None else "", by, basis, decision_id))
            graph.append_event("owner_changed", {"task_id": decision["run_id"], "decision_id": decision_id,
                                                 "to": person["name"], "by": by, "referral": True})
            ids = []
            for kind, scope in targets:
                ids.append(graph.add_authority(kind, scope, role, person_id=person["id"], repo=repo, source="referral",
                                               asserted_by=by, accepted=False,
                                               note=clip_marked(str(data.get("note") or f"referred decision {decision_id}"),
                                                                300, "the hand-on note has the rest"),
                                               effective_to=str(data.get("effective_to") or "")))
            graph.append_event("route_learned", {"task_id": decision["run_id"], "decision_id": decision_id,
                                                 "person": person["name"], "scopes": learned, "by": by,
                                                 "authority_ids": ids})
        self.notify(decision_id, "reassigned")
        row = self.get_decision(decision_id)
        if learned:
            row["notice"] = (f"Handed to {person['name']}; Raven will route {', '.join(learned)} to them "
                             f"first from now on (they accept by answering)")
        elif choice == "contact":
            row["notice"] = (f"Handed to {person['name']}. If they answer, Raven will remember them as a first contact "
                             "for similar questions, without creating a broad ownership rule.")
        elif choice in ("none", "this"):
            row["notice"] = f"Handed to {person['name']} for this question only; Raven learned no route from it"
        else:
            row["notice"] = (f"Handed to {person['name']}; Raven learned no route from it: {why_none}. To teach one, "
                             f"name the scope when you hand on (in Slack, `not me @{person['name']} for docs`) or add "
                             "it on the People page")
        row["learned"] = learned
        return row

    def make_rule(self, decision_id, data, actor=None):
        """A signed answer its owner declares reusable: from now on a
        question memory matches to it resolves as authorized (signoff
        'rule') without a fresh signature, while the conditions (phrases
        the question or its context must mention) hold and until it
        expires. Default is the opposite: every decision is request
        specific. With `end`, the rule stops and the answer is evidence
        to sign again. The actor must be the decision's owner, a
        required signer, verified for its scope, or an admin overriding."""
        from . import authz
        from .graph import rule_conditions
        decision = self.get_decision(decision_id)
        if data.get("expected_updated_at"):
            check_revision(data, decision)
        basis = authz.check(self.graph, actor, decision, "rule")
        by = (actor.name if actor is not None and actor.id else field(data, "by", limit=100))[:100]
        graph = self.graph
        stamp = now()
        if data.get("end") in (True, 1, "1", "true", "on", "yes"):
            if not decision.get("reusable"):
                raise Invalid("This decision is not a rule")
            with graph.transaction():
                graph.db.execute("UPDATE decisions SET reusable=0, rule_ended_at=?, updated_at=? WHERE id=?",
                                 (stamp, stamp, decision_id))
                graph.append_event("rule_ended", {"task_id": decision["run_id"], "decision_id": decision_id, "by": by,
                                                  "basis": basis})
                flagged = graph.invalidate_rule_dependents(decision_id, f"the rule from decision {decision_id} was ended by {by}")
            for nid in flagged:
                self.notify(nid, "review")
            row = self.get_decision(decision_id)
            row["notice"] = (f"No longer a rule ({by}); from now on this answer is evidence that wants sign-off"
                             + (f"; {len(flagged)} outstanding node{'s' if len(flagged) != 1 else ''} it authorized "
                                "now wait for a person again" if flagged else ""))
            row["invalidated"] = flagged
            return row
        if not (decision.get("signoff") in ("signed", "rule") and (decision.get("answer") or "").strip()):
            raise Invalid("Only a signed answer can be a rule: answer or sign the decision first")
        conditions = "; ".join(rule_conditions(str(data.get("conditions") or "")))
        if len(conditions) > 500:
            # Refused, not cut: a condition cut short ("customer=ac") is a
            # different condition, and the rule would apply where nobody said.
            raise Invalid("A rule's conditions must fit in 500 characters; name fewer or shorter ones")
        expires = rule_expiry(data.get("expires"))
        scope = "any" if str(data.get("scope") or "").strip().lower() in ("any", "anywhere", "all") else "same"
        with graph.transaction():
            graph.db.execute("UPDATE decisions SET reusable=1, rule_conditions=?, rule_expires=?, rule_by=?, rule_at=?, "
                             "rule_scope=?, rule_ended_at='', updated_at=? WHERE id=?",
                             (conditions, expires, by, stamp, scope, stamp, decision_id))
            graph.append_event("rule_made", {"task_id": decision["run_id"], "decision_id": decision_id, "by": by,
                                             "conditions": conditions, "expires": expires, "scope": scope,
                                             "basis": basis})
            graph.note_rule_expiry(expires)
        auto = graph.get_setting("auto_rules") == "1"
        row = self.get_decision(decision_id)
        row["notice"] = (f"Rule made by {by}: a question memory matches to this answer"
                         + (f" whose stated facts and words satisfy {conditions}" if conditions else "")
                         + (" in any scope" if scope == "any" else " in the same scope (other customers or files still ask)")
                         + (" resolves without a fresh signature" if auto else
                            " is shown as covered by this rule and still wants a signature, because automatic rule "
                            "authorization is off (settings.auto_rules)")
                         + (f" until {expires[:10]}" if expires else "") + ". End the rule to go back to sign-off.")
        return row

    def answer(self, decision_id, data, actor=None, transaction_hook=None):
        """A person's answer: the signed revision of the decision. The
        actor must be the decision's owner, a required signer, verified
        for its scope, or an admin overriding; who acted and on what
        standing is recorded apart from the assigned owner."""
        from . import authz
        from .graph import parse_applicability
        answer = field(data, "answer")
        rationale = field(data, "rationale", "No additional rationale supplied")
        try:
            applicability = parse_applicability(data.get("applicability"))
        except ValueError as exc:
            raise Invalid(str(exc)) from exc
        applicability_json = json.dumps(applicability, sort_keys=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            decision = db.execute("SELECT d.*,o.name AS owner_name FROM decisions d LEFT JOIN owners o ON o.id=d.owner_id WHERE d.id=?", (decision_id,)).fetchone()
            if not decision or not decision["owner_id"]:
                raise Invalid("Assign an owner before recording an answer")
            basis = authz.check(self.graph, actor, dict(decision),
                                "correct" if decision["status"] == "approved" or decision["answer"] else "answer")
            if db.execute("SELECT 1 FROM executions WHERE run_id=?", (decision["run_id"],)).fetchone() and not data.get("expected_updated_at"):
                raise Invalid("Managed decisions require expected_updated_at to prevent concurrent review conflicts")
            if data.get("expected_updated_at") and data["expected_updated_at"] != decision["updated_at"]:
                raise Invalid("This decision changed while you were reviewing it. Reopen it before answering.")
            # Internal-only hook: provenance is committed with the answer, after
            # authority and revision checks. Never supplied by a client payload.
            if transaction_hook is not None:
                transaction_hook(db, decision)
            kind = "answer_corrected" if decision["status"] == "approved" else "owner_approved"
            # A human answer that replaces a different answer nobody signed
            # (a record's, memory's, the agent's) is a correction for every
            # decision that leaned on the old text, even though it is the
            # first signed answer here.
            previous_applicability = parse_applicability(decision["applicability"] or "")
            corrects = kind == "answer_corrected" or previous_applicability != applicability or bool(
                decision["answer"] and decision["answer"].strip() != answer.strip()
                and decision["status"] in ("resolved", "partial", "assumed", "proposed"))
            if decision["answer"]:
                self.event(db, "previous_answer", json.dumps({"answer": decision["answer"], "rationale": decision["rationale"], "owner": decision["answered_by"], "applicability": previous_applicability}), decision_id, decision["run_id"])
            # A signed answer: the pre-approval suggestion is cleared so the
            # approved row never carries stale provenance, and an explicit
            # supersedes link retires the decision this one replaces.
            supersedes = data.get("supersedes") or ""
            if supersedes:
                if not isinstance(supersedes, str):
                    raise Invalid("supersedes must name an existing decision")
                prior = db.execute("SELECT d.*,o.name AS owner_name FROM decisions d "
                                   "LEFT JOIN owners o ON o.id=d.owner_id WHERE d.id=?", (supersedes,)).fetchone()
                if prior is None:
                    raise Invalid("supersedes must name an existing decision")
                if supersedes == decision_id:
                    raise Invalid("A decision cannot supersede itself")
                if repo_key(prior["repo"]) != repo_key(decision["repo"]):
                    raise Invalid("A decision can only supersede another decision in the same repository")
                # Retiring a decision changes its authority just as a
                # correction does. Owning the replacement is not standing
                # to withdraw somebody else's answer on another task.
                authz.check(self.graph, actor, dict(prior), "correct")
                db.execute("UPDATE decisions SET superseded_by=?,updated_at=? WHERE id=?", (decision_id, now(), supersedes))
                self.event(db, "superseded", f"Superseded by decision {decision_id}", supersedes, prior["run_id"])
            stamp = now()
            # With auth on, the signer is the person who is signed in; the
            # local operator records on behalf of the named owner, as
            # the single-operator workspace always did.
            signer = (actor.name if actor is not None and actor.id else data.get("signed_by")) or decision["owner_name"]
            # The human answer is the signed revision of this decision: the
            # signature names the exact text it covers, and a review flag
            # raised by an earlier correction is cleared by this answer.
            # The answer is the signer's signature on this exact text; every
            # earlier signature covered other text and is dropped.
            signature = json.dumps([{"by": signer, "at": stamp, "revision": stamp, "hash": answer_hash(answer)}])
            # What the ladder wrote while it looked for an answer (the memory
            # it scored, the records it rejected) is not where this answer
            # came from, and is cleared. Measured live on eb9d22d: signed
            # human answers showed as "Resolved from evidence" beside those
            # retrieval notes.
            db.execute("UPDATE decisions SET status='approved',answer=?,rationale=?,answered_by=?,updated_at=?,"
                       "prediction=NULL,source_id=NULL,source_revision='',source='human',evidence='',"
                       "signoff='signed',signed_by=?,signed_revision=?,signed_hash=?,needs_review=0,review_reason='',"
                       "supersedes=CASE WHEN ?='' THEN supersedes ELSE ? END,actor_id=?,actor_name=?,actor_basis=?,"
                       "signatures=?,applicability=? WHERE id=?",
                       (answer, rationale, decision["owner_name"], stamp, signer, stamp, answer_hash(answer),
                        supersedes, supersedes, actor.id if actor is not None else "", signer, basis, signature,
                        applicability_json, decision_id))
            self.event(db, kind, json.dumps({"answer": answer, "rationale": rationale, "owner": decision["owner_name"],
                                             "actor": signer, "basis": basis,
                                             "source": data.get("source") or "local operator",
                                             "applicability": applicability}), decision_id, decision["run_id"])
            if corrects:
                for dependent in db.execute("SELECT id,run_id FROM decisions WHERE source_id=? AND status='pending'", (decision_id,)).fetchall():
                    db.execute("UPDATE decisions SET prediction=NULL,source_id=NULL,updated_at=? WHERE id=?", (now(), dependent["id"]))
                    self.event(db, "prediction_withdrawn", f"Source decision {decision_id} was corrected; review fresh context", dependent["id"], dependent["run_id"])
            # Several required approvers: the answer is one signature, and
            # the decision is evidence until every one of them has signed.
            keys = decision.keys()
            required = []
            try:
                required = json.loads((decision["required_signers"] if "required_signers" in keys else "") or "[]")
            except ValueError:
                required = []
            remaining = [r for r in required if r.lower() != (signer or "").lower()]
            if remaining:
                # A person's answer awaiting co-signers: not authorized yet,
                # and not evidence either.
                db.execute("UPDATE decisions SET status='resolved', signoff='required', kind='answer', "
                           "signatures=?, signed_by=? WHERE id=?",
                           (json.dumps([{"by": signer, "at": stamp, "revision": stamp, "hash": answer_hash(answer)}]),
                            signer, decision_id))
                self.event(db, "signature", json.dumps({"by": signer, "remaining": remaining}), decision_id, decision["run_id"])
            pending = db.execute("SELECT 1 FROM decisions WHERE run_id=? AND status='pending'", (decision["run_id"],)).fetchone()
            from .execution_store import record_answer
            if not remaining:
                record_answer(db, decision, answer, rationale)
            managed = db.execute("SELECT 1 FROM executions WHERE run_id=?", (decision["run_id"],)).fetchone()
            if not pending and not managed:
                db.execute("UPDATE runs SET status=CASE WHEN status='completed' THEN status ELSE 'working' END,updated_at=? WHERE id=?", (now(), decision["run_id"]))
        # Every decision that took its answer from this one is now in
        # doubt: pending suggestions were withdrawn above, answered and
        # signed dependents are marked for review, transitively.
        flagged: list = []
        if corrects:
            with self.graph.transaction():
                flagged = self.graph.flag_dependents(decision_id, f"the answer of decision {decision_id} was corrected")
        for dependent_id in flagged:
            self.notify(dependent_id, "review")
        # The answer teaches routing: a referral to this person is accepted
        # by their answering, and a decision routed on inference alone
        # that they answered makes them someone who knows its categories.
        try:
            with self.graph.transaction():
                self.graph.learn_from_answer(decision_id, decision["owner_name"], decision["owner_evidence"] or "")
        except Exception as error:
            print(f"Raven: could not learn from answer {decision_id}: {error}")
        self.notify(decision_id, "answered")
        for name in remaining:
            self.notify(decision_id, "signoff", to=name)
        # A signed answer settles the still-open twins of this question in
        # other runs (deterministic bar only; no model call on this path).
        try:
            from .ladder import close_open_twins
            close_open_twins(self.graph, decision_id, decision["question"], decision["repo"] or "",
                             answer, decision["owner_name"])
        except Exception as error:
            print(f"Raven: could not settle open twins of {decision_id}: {type(error).__name__}: {error}",
                  file=sys.stderr)
        return self.get_decision(decision_id)

    def readiness(self) -> list[dict]:
        """What is not set up yet, in the order it bites. A repository's
        public history names authors, not deciders: a pilot that ingests
        one and stops finds every question landing on the coordinator,
        and nothing said so. Each finding names what to do about it."""
        graph = self.graph
        out: list[dict] = []
        from .config import backend_status
        inference = backend_status()
        if not inference['semantic_enabled']:
            out.append({'key': 'no_inference', 'level': 'warning', 'what': inference['notice'],
                        'do': 'Set up Anthropic or the Claude CLI on the server with ./setup --configure. '
                              'Do not send API keys in chat. Recheck bridge_connection_status afterward.'})
        people = graph.db.execute("SELECT count(*) c FROM people WHERE active=1").fetchone()["c"]
        coordinator = graph.coordinator()
        automatic = graph.get_setting("slack_discovery") == "1"
        fallback = self.delivery.fallback_channel or graph.get_setting("slack_fallback_channel")
        directory = json.loads(graph.get_setting("slack_directory") or "{}")
        if not self.delivery.enabled:
            out.append({"key": "no_slack", "level": "blocker", "what": "Slack is not connected; people receive no Slack notifications.",
                        "do": "Connect the Slack app with ./setup --configure. See docs/slack.md. No ownership map is required."})
        if directory.get("error"):
            out.append({"key": "slack_directory", "level": "blocker", "what": directory["error"],
                        "do": "Check the bot's users:read and users:read.email scopes, then refresh Slack contacts in Connections & setup."})
        if self.delivery.enabled and self.delivery.channel == "slack" and not fallback:
            out.append({"key": "no_triage_channel", "level": "warning", "what": "No Slack channel is configured for questions with no reachable contact.",
                        "do": "Set SLACK_FALLBACK_CHANNEL to a channel containing the bot. People can refer questions there without creating accounts."})
        repos = [r["repo"] for r in graph.db.execute("SELECT DISTINCT repo FROM ownership WHERE repo != ''")]
        # Authority counts where it applies. A row for another repository
        # covers nothing here, and one that has expired covers nothing
        # anywhere: either would otherwise clear this blocker while every
        # question still fell through to the coordinator.
        uncovered = [r for r in repos if not graph.deciders(r)]
        failed = graph.get_setting("bootstrap_ingest_error", "")
        if failed:
            out.append({"key": "ingest_failed", "level": "blocker",
                        "what": f"Raven could not read the repository it was set up with ({failed}), so it "
                                "started without it.",
                        "do": "Make the checkout readable by the container, or fix the path, then restart Raven; "
                              "it ingests on startup."})
        if not people:
            out.append({"key": "no_people", "level": "blocker",
                        "what": "Nobody is in Raven yet, so no question can reach a person.",
                        "do": "Connect Slack to import contacts automatically; no personal accounts or owner setup are needed."})
        if repos and uncovered and not automatic:
            everywhere = len(uncovered) == len(repos)
            named = ", ".join(sorted(uncovered)[:3]) + ("…" if len(uncovered) > 3 else "")
            out.append({"key": "no_authority" if everywhere else "no_authority:" + sorted(uncovered)[0],
                        "level": "warning",
                        "what": (f"Nobody is recorded as deciding for any area of {named}. Git history names who "
                                 "wrote code, which is not the same as who decides, and CODEOWNERS in most "
                                 "repositories names teams, which are not people you can ask."
                                 if everywhere else
                                 f"{len(uncovered)} of your {len(repos)} repositories have nobody recorded as "
                                 f"deciding for any area of them ({named}); every question about those reaches "
                                 "the coordinator."),
                        "do": "Connect Slack for automatic contact discovery and evidence-based routing. Manual authority overrides are optional."})
        elif not repos and not graph.deciders("") and not automatic:
            out.append({"key": "no_authority", "level": "warning",
                        "what": ("Nobody is recorded as deciding for any area. Git history names who wrote code, "
                                 "which is not the same as who decides, and CODEOWNERS in most repositories "
                                 "names teams, which are not people you can ask."),
                        "do": "Connect Slack and ingest repository or ticket history so Raven can infer a first contact. Ownership overrides are optional."})
        if not coordinator and not fallback and not automatic:
            out.append({"key": "no_coordinator", "level": "warning",
                        "what": "No coordinator is set, so a question nobody is verified to own reaches nobody.",
                        "do": "Configure a Slack triage channel for unplaced questions, or optionally choose a coordinator."})
        from .graph import is_team_handle
        teams_as_people = [r["name"] for r in graph.db.execute("SELECT name FROM people WHERE active=1")
                           if is_team_handle(r["name"])]
        if teams_as_people:
            out.append({"key": "teams_as_people", "level": "blocker",
                        "what": f"{len(teams_as_people)} of your people are teams, not people "
                                f"({', '.join(teams_as_people[:3])}). A question put to a team reaches nobody.",
                        "do": "Remove them and add the team with its members, or name the people directly."})
        unreachable = graph.db.execute(
            "SELECT count(*) c FROM people WHERE active=1 AND slack_id=''").fetchone()["c"]
        if people and unreachable and self.delivery.enabled:
            out.append({"key": "unreachable", "level": "warning",
                        "what": f"{unreachable} of {people} people have no Slack id, so messages for them fall "
                                "back to a channel or fail visibly.",
                        "do": "Refresh Slack contacts. Names with no reliable match are routed through the configured Slack channel."})
        # How the map is actually performing, which is the number an
        # operator can act on: questions that had to fall through.
        recent = graph.db.execute(
            "SELECT routing_reason FROM decisions WHERE draft=0 AND status != 'suggested' "
            "ORDER BY created_at DESC LIMIT 50").fetchall()
        fell_through = sum(1 for r in recent if "coordinator fallback" in (r["routing_reason"] or ""))
        if fell_through and len(recent) >= 3:
            out.append({"key": "falling_through", "level": "warning",
                        "what": f"{fell_through} of the last {len(recent)} decisions reached the coordinator "
                                "because no verified owner covered them.",
                        "do": "Record who decides for those areas; a hand-on in the inbox teaches it too."})
        for repo in repos[:5]:
            note = graph.get_setting(f"sync:{repo}")
            if not graph.db.execute("SELECT 1 FROM sync_state WHERE repo=?", (repo,)).fetchone() and not note:
                out.append({"key": f"never_synced:{repo}", "level": "note",
                            "what": f"{repo} has never been synced from GitHub, so approvals come from git "
                                    "trailers and merge commits only.",
                            "do": f"Run bridge sync {repo} with a token that can read it."})
        return out

    def state(self, full_history=False):
        """What the inbox polls: owners, the newest 500 runs and decisions
        by updated_at, the last 100 events, executions with their answer
        deliveries, and the graph's shape. full_history=True is the
        export: unbounded, with the ownership rows and revisions too."""
        from .execution_store import state as execution_state
        from .graph import BLOCKING_SQL
        limit = "" if full_history else " LIMIT 500"
        with self.connect() as db:
            execution = execution_state(db, full_history)
            # The newest 500 decisions, plus every decision that waits on a
            # person however old: actionable work is never hidden by the
            # cap, and the counts come from the database, not the payload.
            decisions_sql = f"""SELECT d.*,o.name AS owner_name,o.team AS owner_team,
                    r.title AS run_title,r.agent,r.repo FROM (SELECT * FROM decisions WHERE draft=0
                    ORDER BY updated_at DESC{limit}) d
                    LEFT JOIN owners o ON o.id=d.owner_id JOIN runs r ON r.id=d.run_id"""
            if not full_history:
                decisions_sql += f""" UNION SELECT d.*,o.name AS owner_name,o.team AS owner_team,
                    r.title AS run_title,r.agent,r.repo FROM decisions d
                    LEFT JOIN owners o ON o.id=d.owner_id JOIN runs r ON r.id=d.run_id
                    WHERE d.draft=0 AND {BLOCKING_SQL}"""
            decisions_sql += " ORDER BY created_at DESC"
            out = {
                "executions": execution["executions"],
                "deliveries": execution["deliveries"],
                "owners": [dict(row) for row in db.execute("SELECT * FROM owners ORDER BY created_at")],
                # The discovery digest is the kickoff's answer to the agent,
                # not something the inbox shows; it stays in the export.
                "runs": [{k: v for k, v in dict(row).items() if full_history or k != "discovery"}
                         for row in db.execute("SELECT * FROM runs ORDER BY updated_at DESC" + limit)],
                "decisions": [{k: v for k, v in dict(row).items() if k != "embedding"} for row in db.execute(decisions_sql)],
                "events": [dict(row) for row in db.execute("SELECT * FROM events ORDER BY id DESC" + ("" if full_history else " LIMIT 100"))],
                "graph": graph_summary(db),
                "counts": inbox_counts(db),
                "readiness": self.readiness(),
            }
            if full_history:
                out.update(revisions=execution["revisions"], ownership=ownership_rows(db, limit=0))
            return out

    def list_runs(self, page=1, size=50, status=""):
        """Tasks, newest first, one page at a time."""
        page, size = max(1, int(page)), max(1, min(200, int(size)))
        sql, args = "SELECT * FROM runs", []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        with self.connect() as db:
            total = db.execute(f"SELECT count(*) c FROM ({sql})", args).fetchone()["c"]
            rows = db.execute(sql + " ORDER BY updated_at DESC LIMIT ? OFFSET ?", args + [size, (page - 1) * size]).fetchall()
        return {"items": [{k: v for k, v in dict(r).items() if k != "discovery"} for r in rows],
                "total": total, "page": page, "size": size}

    def list_decisions(self, page=1, size=50, status="", owner_id="", run_id="", q=""):
        """Decisions, newest first, one page at a time, filtered in the
        database: by status, owner, task, or a word in the question."""
        page, size = max(1, int(page)), max(1, min(200, int(size)))
        sql = ("SELECT d.*,o.name AS owner_name,o.team AS owner_team,r.title AS run_title,r.agent,r.repo "
               "FROM decisions d LEFT JOIN owners o ON o.id=d.owner_id JOIN runs r ON r.id=d.run_id WHERE d.draft=0")
        args: list = []
        if status:
            sql += " AND d.status=?"
            args.append(status)
        if owner_id:
            sql += " AND d.owner_id=?"
            args.append(owner_id)
        if run_id:
            sql += " AND d.run_id=?"
            args.append(run_id)
        if q:
            sql += " AND (d.question LIKE ? OR d.answer LIKE ?)"
            args += [f"%{q}%", f"%{q}%"]
        with self.connect() as db:
            total = db.execute(f"SELECT count(*) c FROM ({sql})", args).fetchone()["c"]
            rows = db.execute(sql + " ORDER BY d.updated_at DESC LIMIT ? OFFSET ?", args + [size, (page - 1) * size]).fetchall()
        return {"items": [{k: v for k, v in dict(r).items() if k != "embedding"} for r in rows],
                "total": total, "page": page, "size": size}

    def backup(self, target) -> dict:
        """A consistent copy of the whole database (people, teams, the
        authority map, settings, tokens' hashes, integration state,
        every decision and revision) with SQLite's online backup, safe
        while the server runs. Restoring is starting Raven with --db
        pointed at the copy; nothing else needs doing."""
        import sqlite3
        from .database import is_postgres
        if is_postgres(self.path):
            raise Invalid("Use pg_dump for PostgreSQL backups; see make backup and DOCKER.md")
        target = str(Path(target).expanduser().resolve())
        if target == self.path:
            raise Invalid("The backup must be a different file")
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as src:
            dest = sqlite3.connect(target)
            try:
                src.backup(dest)
            finally:
                dest.close()
        with self.connect() as db:
            counts = {t: db.execute(f"SELECT count(*) c FROM {t}").fetchone()["c"]
                      for t in ("people", "authority", "runs", "decisions", "settings")}
        return {"backup": target, "source": self.path, "written_at": now(), **counts}

    def overdue_hours(self) -> int:
        try:
            return max(1, int(self.graph.get_setting("overdue_hours") or 72))
        except ValueError:
            return 72

    def inbox(self, page=1, size=50, owner_id="", overdue=False):
        """The needs-you queue, paginated on the server: every decision that
        waits on a person, oldest first so nothing ages out of sight, with
        the total and the counts. `overdue` keeps only the open questions
        older than the overdue window (settings.overdue_hours). A node
        abandoned mid-write surfaces here once the sweep recovers it."""
        self.graph.expire_drafts()
        from .graph import BLOCKING_SQL
        page, size = max(1, int(page)), max(1, min(200, int(size)))
        hours = self.overdue_hours()
        sql = f"""SELECT d.*,o.name AS owner_name,o.team AS owner_team,r.title AS run_title,r.agent,r.repo
                  FROM decisions d LEFT JOIN owners o ON o.id=d.owner_id JOIN runs r ON r.id=d.run_id
                  WHERE d.draft=0 AND {BLOCKING_SQL}"""
        args = []
        if owner_id:
            sql += " AND d.owner_id=?"
            args.append(owner_id)
        if overdue:
            sql += " AND d.status='pending' AND d.created_at < ?"
            args.append((datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat())
        with self.connect() as db:
            total = db.execute(f"SELECT count(*) c FROM ({sql})", args).fetchone()["c"]
            rows = db.execute(sql + " ORDER BY d.created_at ASC LIMIT ? OFFSET ?", args + [size, (page - 1) * size]).fetchall()
            return {"items": [{k: v for k, v in dict(row).items() if k != "embedding"} for row in rows],
                    "total": total, "page": page, "size": size, "counts": inbox_counts(db, hours),
                    "overdue_hours": hours}

    def add_record(self, data):
        """A record from outside git: a ticket, a document, a chat decision.
        Kind and ref identify it (the same pair updates it), the url is its
        immutable locator, the paths tie it to the areas it is about. It is
        evidence the ladder can cite, never sign-off."""
        repo = repo_key(str(data.get("repo") or ""))
        kind = re.sub(r"[^a-z0-9_-]", "", str(data.get("kind") or "note").lower())[:30] or "note"
        if kind in ("pr", "merge", "commit", "review"):
            raise Invalid("kinds pr, merge, commit and review come from git and GitHub; use ticket, doc, slack or note")
        ref = field(data, "ref", limit=200)
        title = str(data.get("title") or "")[:300]
        body = str(data.get("body") or "")[:20000]
        if not title and not body:
            raise Invalid("A record needs a title or a body")
        author = str(data.get("author") or "")[:100]
        created = str(data.get("created_at") or now())[:40]
        url = str(data.get("url") or "")[:1000]
        status = str(data.get("status") or "")[:40]
        resolved = data.get("resolved", True) not in (False, 0, "0", "false", "no")
        paths = data.get("paths") or []
        if isinstance(paths, str):
            paths = [p.strip().lstrip("/") for p in re.split(r"[,\s]+", paths) if p.strip()]
        graph = self.graph
        with graph.transaction():
            graph.upsert_intent(repo, kind, ref, title, body, author, created, status=status, resolved=resolved)
            if paths:
                graph.add_intent_paths(repo, kind, ref, paths[:40])
            if url:
                graph.set_source(repo, f"url:{kind}:{ref}", url)
            graph.append_event("record_added", {"repo": repo, "kind": kind, "ref": ref, "url": url, "author": author})
        return {"repo": repo, "kind": kind, "ref": ref, "url": url, "paths": paths[:40]}

    def ownership(self, repo="", limit=200):
        """The live ownership rows, of one repository when named; limit 0
        means all of them."""
        with self.connect() as db:
            return ownership_rows(db, repo, limit)

    def seed(self):
        # Local Docker login creates a person/owner row even in an otherwise
        # empty workspace; it must not prevent a later explicit --demo.
        if self.state()["runs"] or self.graph.db.execute(
                "SELECT 1 FROM owners WHERE name<>'Local developer' LIMIT 1").fetchone():
            return
        wes = self.add_owner({"name": "Wes Chen", "team": "Platform Data", "patterns": "billing/*,metering/*"})
        marisol = self.add_owner({"name": "Marisol Vega", "team": "Pricing", "patterns": "pricing/*,credits/*"})
        self.add_owner({"name": "Alex Morgan", "team": "Infrastructure", "patterns": "infra/*,auth/*"})
        prior_run = self.add_run({"title": "Preserve annual credit balances", "agent": "Demo agent", "repo": "acme/platform"})
        prior = self.request({"run_id": prior_run["id"], "question": "Should unused prepaid credits expire?", "context": "Annual contract renewal: preserve balances already purchased by customers.", "path": "credits/renewal.py", "owner_id": marisol["id"]})
        self.answer(prior["id"], {"answer": "Roll unused credits over for the remaining annual contract term.", "rationale": "Honor the contract and preserve what the customer has paid for."})
        self.update_run(prior_run["id"], {"status": "completed"})
        # One task on the canvas: a kickoff verdict, a node the agent
        # settled itself (sign-off wanted), and a follow-up a person added.
        # The deterministic rungs only: seeding never calls a model.
        from . import canvas
        from .config import Config
        cfg = Config(model_api="none")
        task = canvas.start_task(self, cfg, {"title": "Move the nightly backup job to the new scheduler", "agent": "Demo · Claude Code", "repo": "acme/platform", "requester": "Priya Natarajan", "paths": "infra/backup.py",
                                            "goal": "The cron host is being retired. Run the nightly database backup from the shared scheduler instead."})
        node = canvas.add_node(self, cfg, {"task_id": task["task_id"], "question": "Should the nightly backup keep its 30-day retention on the new scheduler, or drop to 14 days?", "paths": "infra/backup.py", "options": "30 days | 14 days",
                                           "context": "The scheduler's default retention is 14 days. Restores older than two weeks were requested twice last year."})
        canvas.settle_node(self, {"task_id": task["task_id"], "node_id": node["node_id"], "answer": "Keep 30 days.",
                                  "rationale": "Two restores last year reached past 14 days; the storage cost of the extra 16 days is small."})
        canvas.add_followups(self, cfg, node["node_id"], {"questions": "Should restores older than 14 days need an approval?", "by": "Alex Morgan"})
        run = self.add_run({"title": "Add usage-based pricing", "agent": "Demo · Claude Code", "repo": "acme/platform"})
        self.request({"run_id": run["id"], "question": "Should we bill the usage spike, or exclude it as a load test?", "context": "Usage on two enterprise accounts jumped to 11× normal during the new pricing rollout. The metering change is drafted. The accounts share a load-test tag, but billing policy does not define an exclusion.", "path": "billing/usage.py", "owner_id": wes["id"]})
        run = self.add_run({"title": "Ship prepaid credit rollover", "agent": "Demo · Codex", "repo": "acme/platform"})
        self.request({"run_id": run["id"], "question": "Should unused prepaid credits expire at the end of the month?", "context": "Monthly rollover is being added for annual enterprise plans. Confirm whether last quarter's policy applies to this cohort.", "path": "credits/rollover.py"})
        run = self.add_run({"title": "Tighten service token access", "agent": "Demo · Cursor", "repo": "acme/platform"})
        self.request({"run_id": run["id"], "question": "Can we shorten service token expiry to 24 hours?", "context": "The security hardening change is ready. Three legacy integrations currently rotate tokens every seven days; shortening expiry will require a migration window.", "path": "auth/tokens.py"})
