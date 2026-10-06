"""One durable worker per local database. API calls never hold a write transaction."""

import base64
import fcntl
import hashlib
import json
import threading
import time
import uuid
from pathlib import Path

from .agents_api import INSTRUCTIONS, INSTRUCTION_VERSION, result_event, safe_error
from .execution_store import enqueue, revision
from .store import Invalid, field, now

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "billing"
REPOSITORIES = [{"id": "billing-fixture", "name": "Disposable billing fixture"}]
# A configured checkout is packaged whole into the hosted workspace: the
# files git tracks, text only, each under MAX_FILE bytes, all under
# MAX_CHECKOUT bytes; a bigger repository is refused with the reason.
MAX_FILE = 256 * 1024
MAX_CHECKOUT = 8 * 1024 * 1024
JUDGMENT_FIELDS = {"question": 2000, "context": 5500, "path": 1000,
                   "evidence": 2000, "options": 2000, "blocked_work": 1000, "independent_work": 1000}
OPTIONAL_JUDGMENT_FIELDS = {'facts': 2000, 'scope_request': 200}
TOOLS = [
    {"type": "function", "name": "search_decisions", "description": "Find reviewed prior decisions and provenance. These are evidence, not approval for a new request.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string", "maxLength": 2000}},
                    "required": ["query"], "additionalProperties": False}},
    {"type": "function", "name": "request_judgment", "description": "Ask the configured owner about a consequential unresolved choice. The call stays pending until reviewed. Use empty strings for facts and scope_request unless needed. If needs_scope_clarification returns, establish the actual missing facts and retry with facts as key=value pairs and scope_request set to the returned request_key; never copy historical facts as current truth.",
     "parameters": {"type": "object", "properties": {k: {"type": "string", "maxLength": v} for k, v in {**JUDGMENT_FIELDS, **OPTIONAL_JUDGMENT_FIELDS}.items()},
                    "required": list(JUDGMENT_FIELDS) + list(OPTIONAL_JUDGMENT_FIELDS), "additionalProperties": False}},
]
OUTPUT_INSTRUCTIONS = """
Work in /workspace/repo. Preserve the baseline git commit. When finished, run the
repository tests and capture real results. Write /workspace/outputs/changes.patch
using git diff HEAD (include newly added source and test files with git add -N).
Write /workspace/outputs/result.json with status ('completed' or 'incomplete'),
summary, verification, and limitations. Include any unresolved conditions.
Do not claim completion if tests failed or a decision is unresolved.
"""


def _config(model, repository, named_files):
    files = []
    digest = hashlib.sha256()
    for name, content in named_files:
        digest.update(name.encode() + b"\0" + content + b"\0")
        files.append({"type": "inline", "path": "/workspace/repo/" + name,
                      "data": base64.b64encode(content).decode()})
    return {"model": model, "instruction_version": INSTRUCTION_VERSION, "repository": repository,
            "fixture_revision": digest.hexdigest(), "instructions": INSTRUCTIONS + OUTPUT_INSTRUCTIONS,
            "environment": {"type": "openai_hosted", "network": {"access": "disabled"}, "files": files,
                "setup_commands": [{"cwd": "/workspace/repo", "command":
                    "git init -b main && git add . && git -c user.name=Raven -c user.email=fixture@localhost commit -m baseline"}]}}


def fixture_config(model):
    # Explicit allowlist keeps local credentials, DBs and unrelated code out of the environment.
    names = ("README.md", "usage.json", "billing/__init__.py", "billing/usage.py", "tests/test_usage.py")
    return _config(model, "billing-fixture", [(name, (FIXTURE / name).read_bytes()) for name in names])


def checkout_config(model, repository):
    """A configured local checkout, packaged from what git tracks at its
    HEAD: the working tree as committed, no untracked file, no ignored
    file, nothing outside the repository."""
    import subprocess
    root = Path(repository["path"]).resolve()
    try:
        listing = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--full-name"],
                                 check=True, capture_output=True, timeout=60).stdout
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as error:
        raise Invalid(f"{repository['id']}: not a git checkout that can be listed ({error})")
    named, total, skipped = [], 0, []
    for rel in listing.decode("utf-8", "replace").split("\0"):
        if not rel or rel.endswith("/"):
            continue
        path = root / rel
        if not path.is_file() or path.is_symlink():
            continue
        content = path.read_bytes()
        if len(content) > MAX_FILE or b"\0" in content[:8192]:
            skipped.append(rel)
            continue
        total += len(content)
        if total > MAX_CHECKOUT:
            raise Invalid(f"{repository['id']}: more than {MAX_CHECKOUT // (1024 * 1024)} MB of tracked text; "
                          "point Raven at a smaller checkout or a sparse one")
        named.append((rel, content))
    if not named:
        raise Invalid(f"{repository['id']}: git tracks no text file under {root}")
    config = _config(model, repository["id"], named)
    config["skipped"] = skipped[:50]
    config["checkout"] = str(root)
    return config


def parse_repositories(specs):
    """--managed-repo NAME=PATH, repeatable: the checkouts the inbox may
    launch a hosted task on, besides the disposable fixture."""
    out = list(REPOSITORIES)
    for spec in specs or []:
        name, sep, path = str(spec).partition("=")
        name, path = name.strip(), path.strip()
        if not sep or not name or not path:
            raise Invalid(f"--managed-repo takes NAME=PATH, got {spec!r}")
        if any(r["id"] == name for r in out):
            raise Invalid(f"--managed-repo {name}: that name is taken")
        if not (Path(path) / ".git").exists():
            raise Invalid(f"--managed-repo {name}: {path} is not a git checkout")
        out.append({"id": name, "name": f"{name} (checkout at {path})", "path": path})
    return out


class ExecutionService:
    """Managed execution is one more adapter on the canvas: a submitted
    task is kicked off like bridge_start_task (verdict and discovery on
    the run), every request_judgment is a node through the ladder, and
    the hosted agent gets its answer when a person answers or signs the
    node, never before."""

    def __init__(self, store, api, model="gpt-6-astra", repositories=None, cfg=None):
        self.store, self.api, self.model = store, api, model
        self.repositories = list(repositories) if repositories else list(REPOSITORIES)
        self.cfg = cfg
        self.stop = threading.Event()
        self.thread = None
        self.lock_file = None

    @property
    def config(self):
        """The ladder configuration for kickoffs and nodes: the one given
        (tests pass a model-free one), else the environment's."""
        if self.cfg is None:
            from .config import load
            self.cfg = load()
        return self.cfg

    def repository(self, repo_id):
        for entry in self.repositories:
            if entry["id"] == repo_id:
                return entry
        raise Invalid("Choose a configured repository")

    def submit(self, data):
        task = field(data, "task", limit=12000)
        key = field(data, "submission_key", limit=100)
        repo = field(data, "repository", limit=100)
        entry = self.repository(repo)
        requester = str(data.get("requester") or "")[:200]
        config = fixture_config(self.model) if entry["id"] == "billing-fixture" else checkout_config(self.model, entry)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM executions WHERE submission_key=?", (key,)).fetchone()
            if prior:
                if prior["task"] != task or json.loads(prior["config"])["repository"] != repo:
                    raise Invalid("This submission key already belongs to another task")
                return {"id": prior["run_id"], "status": prior["status"]}
            run_id, timestamp = uuid.uuid4().hex[:12], now()
            db.execute("INSERT INTO runs(id, title, agent, repo, status, requester, client_key, updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?)",
                       (run_id, task[:300], "OpenAI Agents API", repo, "queued", requester, "managed:" + key, timestamp))
            db.execute("INSERT INTO executions(run_id,submission_key,task,config,launch_state,status,created_at,updated_at) "
                       "VALUES(?,?,?,?,'queued','queued',?,?)", (run_id, key, task, json.dumps(config), timestamp, timestamp))
            self.store.event(db, "run_started", "Task queued for managed execution", run_id=run_id)
        # The kickoff every adapter gets: discovery and the verdict on the
        # run, so the inbox shows what Raven knew before the agent started.
        from . import canvas
        try:
            kickoff = canvas.kickoff(self.store, self.config, run_id, task[:300], task, repo, [], requester)
        except Exception as error:  # the launch does not depend on the verdict
            print(f"Raven: kickoff of managed run {run_id} failed: {type(error).__name__}: {error}")
            kickoff = {"verdict": "", "why": ""}
        return {"id": run_id, "status": "queued", "verdict": kickoff.get("verdict", ""), "why": kickoff.get("why", "")}

    def get(self, run_id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM executions WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise Invalid("Managed run not found")
            return dict(row)

    def update(self, run_id, **values):
        values["updated_at"] = now()
        with self.store.connect() as db:
            db.execute("UPDATE executions SET " + ",".join(k + "=?" for k in values) + " WHERE run_id=?",
                       (*values.values(), run_id))
            if "status" in values:
                db.execute("UPDATE runs SET status=?,updated_at=? WHERE id=?", (values["status"], now(), run_id))

    def launch(self, run):
        config = json.loads(run["config"])
        metadata = {"bridge_run": run["run_id"], "bridge_submission": run["submission_key"]}
        if run["launch_state"] != "queued":
            matches = self.api.find_sessions(metadata)
            if len(matches) != 1:
                self.update(run["run_id"], status="recovery_required", last_error="No unique session found after uncertain launch; automatic relaunch withheld")
                return
            session = matches[0]
        else:
            self.update(run["run_id"], launch_state="launching", status="launching")
            session = self.api.start_task(model=config["model"], instructions=config["instructions"],
                tools=TOOLS, environment=config["environment"], task=run["task"], metadata=metadata)
        self.update(run["run_id"], session_id=session["id"], launch_state="linked", status="working", last_error=None)

    def required_action(self, run, action):
        if action["type"] == "environment_connection":
            return  # Provisioning/reconnection belongs to the provider, never the inbox.
        if action["type"] != "function_call":
            raise Invalid("Unsupported provider action")
        sid, turn_id, call_id = run["session_id"], field(action, "turn_id", limit=200), field(action, "call_id", limit=200)
        with self.store.connect() as db:
            existing = db.execute("SELECT * FROM provider_calls WHERE session_id=? AND turn_id=? AND call_id=?",
                                  (sid, turn_id, call_id)).fetchone()
        if existing:
            if existing["tool"] != action["name"] or json.loads(existing["arguments"]) != action["arguments"]:
                raise Invalid("Provider changed an existing call's arguments")
            return
        call_key = uuid.uuid4().hex
        args = action.get("arguments")
        name = action.get("name")
        decision_id, output, error = None, None, None
        try:
            if not isinstance(args, dict):
                raise Invalid("Tool arguments must be an object")
            if name == "request_judgment":
                if not set(JUDGMENT_FIELDS) <= set(args) or set(args) - set(JUDGMENT_FIELDS) - set(OPTIONAL_JUDGMENT_FIELDS):
                    raise Invalid("Supply the documented judgment fields; run and owner are assigned by Raven")
                validated = {k: field(args, k, limit=v) for k, v in JUDGMENT_FIELDS.items()}
                optional = {k: field(args, k, limit=v) for k, v in OPTIONAL_JUDGMENT_FIELDS.items() if args.get(k)}
                scope_ref = optional.get('scope_request')
                if scope_ref and not self.store.graph.db.execute(
                        'SELECT 1 FROM scope_clarifications WHERE task_id=? AND request_key=?',
                        (run['run_id'], scope_ref)).fetchone():
                    raise Invalid('scope_request must name a pending clarification on this task')
                if validated["path"].startswith("/") or ".." in validated["path"].split("/"):
                    raise Invalid("Use a repository-relative routing path")
                context = validated["context"] + "\n\n" + "\n\n".join(
                    k.replace("_", " ").title() + ":\n" + validated[k]
                    for k in ("evidence", "options", "blocked_work", "independent_work"))
                # The question is a node on the run's canvas: the ladder
                # resolves or routes it, the owner is notified, and the
                # call is idempotent by the provider's own call id.
                from . import canvas
                node = canvas.add_node(self.store, self.config, {
                    "task_id": run["run_id"], "question": validated["question"], "context": context,
                    "paths": validated["path"], "client_ref": scope_ref or f"{turn_id}:{call_id}",
                    **({'facts': optional['facts']} if 'facts' in optional else {})})
                decision_id = node.get("node_id")
                if node.get('status') == 'needs_scope_clarification':
                    output = node
                elif node["authorized"]:
                    # A rule or a signed decision already covers it: the
                    # answer goes back now, with its provenance.
                    with self.store.connect() as db:
                        reviewed = revision(db, node.get("duplicate_of") or decision_id)
                    output = reviewed or {"answer": node["answer"], "rationale": node["rationale"],
                                          "responder": node["answered_by"] or node["signed_by"],
                                          "provenance": node["evidence"] or "signed decision"}
                    decision_id = None
            elif name == "search_decisions":
                if set(args) != {"query"}:
                    raise Invalid("Supply only query")
                with self.store.connect() as db:
                    matches = self.store.candidates(db, field(args, "query", limit=2000))
                    for match in matches:
                        match["revision"] = revision(db, match["id"])
                output = {"matches": matches, "notice": "Evidence for original circumstances; not approval for this request."}
            else:
                raise Invalid("Unknown tool; only search_decisions and request_judgment are configured")
        except Invalid as exc:
            error = str(exc)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM provider_calls WHERE session_id=? AND turn_id=? AND call_id=?",
                          (sid, turn_id, call_id)).fetchone():
                return
            db.execute("INSERT INTO provider_calls VALUES(?,?,?,?,?,?,?,?)", (
                call_key, run["run_id"], sid, turn_id, call_id, name or "unknown", json.dumps(args), decision_id))
            if not decision_id:
                enqueue(db, call_key, result_event(action, output, error))

    def deliver(self, run, session, items, turns):
        with self.store.connect() as db:
            jobs = [dict(r) for r in db.execute("SELECT d.*,c.session_id,c.turn_id,c.call_id AS remote_call_id,c.decision_id "
                "FROM deliveries d JOIN provider_calls c ON c.id=d.call_id WHERE c.run_id=? "
                "AND d.state IN ('queued','sending','uncertain') AND d.next_attempt<=? ORDER BY d.created_at",
                (run["run_id"], time.time()))]
        active_turn = any(t["status"] in ("queued", "waiting", "in_progress") and not t.get("subagent_id") for t in turns)
        for job in jobs:
            if job["session_id"] != session["id"]:
                raise Invalid("Cross-session delivery refused")
            payload = json.loads(job["payload"])
            matching = [i for i in items if job["kind"] == "result" and i["type"] == "function_call_output"
                        and i["call_id"] == job["remote_call_id"] and i["turn_id"] == job["turn_id"]]
            if job["kind"] == "correction":
                matching = [i for i in items if i["type"] == "message" and i.get("role") == "user"
                            and i.get("content") == payload["input"][0]["content"]]
            if matching:
                if job["kind"] == "result" and not any(i.get("output") == payload.get("output") and
                        i.get("error") == payload.get("error") for i in matching):
                    self.job_update(job["id"], "error", last_error="Provider output differs from queued revision")
                    self.update(run["run_id"], review_required=1)
                else:
                    self.job_update(job["id"], "delivered", acknowledgement=json.dumps(matching))
                continue
            if session["status"] == "failed" or (turns and not active_turn):
                self.job_update(job["id"], "stopped", last_error="Session turn ended; retained for review, not restarted")
                continue
            if job["state"] in ("sending", "uncertain"):
                # No proof of acceptance or rejection. Reconcile on later ticks, never replay blindly.
                self.job_update(job["id"], "uncertain", last_error="Awaiting acknowledgement in provider history")
                continue
            pending = any(a.get("turn_id") == job["turn_id"] and a.get("call_id") == job["remote_call_id"]
                          and a["type"] == "function_call" for a in session["required_actions"])
            if job["kind"] == "result" and not pending:
                self.job_update(job["id"], "stopped", last_error="Call no longer pending; delivery withheld")
                continue
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT state FROM deliveries WHERE id=?", (job["id"],)).fetchone()
                if current["state"] != "queued":
                    continue
                if job["revision_id"] and revision(db, job["decision_id"])["id"] != job["revision_id"]:
                    db.execute("UPDATE deliveries SET state='superseded' WHERE id=?", (job["id"],))
                    continue
                db.execute("UPDATE deliveries SET state='sending',attempts=attempts+1,updated_at=? WHERE id=?", (now(), job["id"]))
            try:
                self.api.send_events(session["id"], [payload], job["id"])
                self.job_update(job["id"], "delivered", acknowledgement="HTTP success for this event and idempotency key", last_error=None)
            except Exception as error:
                detail = json.dumps(safe_error(error))
                # An explicit rate-limit rejection is safe to retry with a bounded backoff.
                if getattr(error, "status_code", None) == 429 and job["attempts"] < 4:
                    self.job_update(job["id"], "queued", last_error=detail,
                                    next_attempt=time.time() + min(60, 2 ** (job["attempts"] + 1)))
                else:
                    self.job_update(job["id"], "uncertain", last_error=detail)

    def job_update(self, key, state, **values):
        values.update(state=state, updated_at=now())
        with self.store.connect() as db:
            db.execute("UPDATE deliveries SET " + ",".join(k + "=?" for k in values) + " WHERE id=?", (*values.values(), key))

    def reconcile(self, run):
        session = self.api.retrieve_session(run["session_id"])
        if session["id"] != run["session_id"]:
            raise Invalid("Provider returned the wrong session")
        turns, items = self.api.turns(session["id"]), self.api.items(session["id"])
        root_turns = [t for t in turns if not t.get("subagent_id")]
        latest = root_turns[-1] if root_turns else None
        terminal = session["status"] == "failed" or (latest and latest["status"] in ("failed", "cancelled", "completed"))
        if not terminal:
            for action in session["required_actions"]:
                self.required_action(run, action)
        self.deliver(run, session, items, turns)
        snapshot = {"session": session, "turns": turns, "items": items}
        status = "working"
        if session["status"] == "failed":
            status = "failed"
        elif latest and latest["status"] in ("failed", "cancelled"):
            status = latest["status"]
        elif latest and latest["status"] == "completed":
            status = "result_ready"
            snapshot["artifacts"] = self.api.artifacts(session["id"])
        elif any(a["type"] == "environment_connection" for a in session["required_actions"]):
            status = "environment_pending"
        from .graph import BLOCKING_SQL
        with self.store.connect() as db:
            # A node waits on a person until it is answered or signed; an
            # answer found in records is evidence, not the judgment asked
            # for; a duplicate waits while the decision it points at does.
            twin_blocking = BLOCKING_SQL.replace("d.", "d2.")
            pending = db.execute(
                f"SELECT 1 FROM decisions d WHERE d.run_id=? AND ({BLOCKING_SQL} OR (d.status='duplicate' AND EXISTS "
                f"(SELECT 1 FROM decisions d2 WHERE d2.id=d.superseded_by AND {twin_blocking})))",
                (run["run_id"],)).fetchone()
            pending = pending or db.execute('SELECT 1 FROM scope_clarifications WHERE task_id=?', (run['run_id'],)).fetchone()
            deliveries = db.execute("SELECT 1 FROM deliveries d JOIN provider_calls c ON c.id=d.call_id "
                "WHERE c.run_id=? AND d.state IN ('queued','sending','uncertain','error','stopped')", (run["run_id"],)).fetchone()
        if not terminal and pending:
            status = "needs_judgment"
        elif not terminal and deliveries:
            status = "delivery_pending"
        if status == "result_ready" and (pending or deliveries):
            status = "review_required"
        self.update(run["run_id"], status=status, snapshot=json.dumps(snapshot), last_error=None)
        with self.store.connect() as db:
            db.execute("UPDATE provider_events SET state='processed',attempts=attempts+1,last_error=NULL "
                       "WHERE session_id=? AND state='received'", (session["id"],))

    def tick(self):
        with self.store.connect() as db:
            runs = [dict(r) for r in db.execute("SELECT * FROM executions")]
        for run in runs:
            try:
                if not run["session_id"]:
                    self.launch(run)
                else:
                    self.reconcile(run)
            except Exception as error:
                self.update(run["run_id"], last_error=json.dumps(safe_error(error)))

    def start(self):
        from .database import connect, is_postgres, WORKER_LOCK
        if is_postgres(self.store.path):
            self.lock_file = connect(self.store.path, autocommit=True)
            if not self.lock_file.execute("SELECT pg_try_advisory_lock(?)", (WORKER_LOCK,)).fetchone()[0]:
                self.lock_file.close()
                self.lock_file = None
                raise Invalid("An execution worker already owns this database")
        else:
            self.lock_file = open(self.store.path + ".worker.lock", "a")
            try:
                fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.lock_file.close()
                self.lock_file = None
                raise Invalid("An execution worker already owns this database")
        def work():
            while not self.stop.is_set():
                self.tick()
                self.stop.wait(3)
        self.thread = threading.Thread(target=work, name="bridge-executions", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=35)
        if self.lock_file and (not self.thread or not self.thread.is_alive()):
            self.lock_file.close()
            self.lock_file = None

    def receive_event(self, payload):
        event_id = field(payload, "id", limit=200)
        session_id = field(payload.get("data", {}), "id", limit=200)
        with self.store.connect() as db:
            if not db.execute("SELECT 1 FROM executions WHERE session_id=?", (session_id,)).fetchone():
                return  # Unknown sessions cannot create runs or questions.
            db.execute("INSERT OR IGNORE INTO provider_events VALUES(?,?,?,'received',0,NULL,?)",
                       (event_id, session_id, json.dumps(payload), now()))
