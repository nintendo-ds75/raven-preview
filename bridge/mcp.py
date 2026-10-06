"""Small stdio MCP server. Approval is deliberately a human-inbox operation.

Every node an agent writes runs through the resolution ladder first
(bridge/ladder.py): a question the org's records or memory settle comes
back resolved with its citation (kind evidence); a precedent-based default
or an unratified proposal comes back as a prediction to confirm; anything
else is routed to the accountable owner (kind new) and deduplicated
against questions already pending.
"""

import json
import sys

from . import canvas
from .config import load
from .ingest import index_repo
from .store import Invalid, graph_summary, ownership_rows

MAX_LINE = 1 << 20
PROTOCOL_VERSION = '2025-06-18'
SUPPORTED_HTTP_VERSIONS = frozenset((PROTOCOL_VERSION, '2025-03-26'))

# Viewer-owned credentials retain evidence/review access, but never acquire
# write privileges merely because they use the agent protocol. New tools are
# write-restricted unless explicitly classified here.
READ_ONLY_TOOLS = frozenset({
    "bridge_get_tree", "bridge_wait", "bridge_get_decision", "bridge_search_decisions",
    "bridge_list_owners", "bridge_connection_status", "bridge_export_proof",
})


def tool(name, description, properties, required):
    return {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": {key: value if isinstance(value, dict) else {"type": "string", "description": value} for key, value in properties.items()},
        "required": required, "additionalProperties": False}}


TOOLS = [
    tool("bridge_start_task", "Call this the moment a task is kicked off, before any work, passing the task as it was given to you in `goal` and not only a title of your own: Raven looks at the task with what it knows (the tree, MAINTAINERS and CODEOWNERS, git history and blame, prior decisions, what is pending) and returns a verdict, engage or pass, with its reason and a discovery digest (areas, people, listings, prior decisions, pending questions). Most tasks are trivial and get pass. A task whose words name no area, kicked off with no paths, gets unplaced: call again with the same title, repo and client_key once you know the files, passing them as paths, and Raven judges it then. Returns task_id: the canvas every later node is written to. Pass client_key (your own id for this kickoff) so a retry returns the same task instead of starting a second one.",
         {"task_id": "Existing task ID to resume without changing its repository. To replace a mistaken repository scope, abandon that task and start without task_id using a new client_key.", "title": "The task as kicked off, one line", "goal": "The task as it was given to you, in the requester's own words and in full. Raven triages on this: a one-line title of your own is thinner than what you were asked, and passes tasks that would otherwise engage", "repo": "Repository identity as the organization knows it: owner/name (acme/platform) or the git URL. This is what Raven matches its map and its memory against, so a path to your working copy or a scratch directory finds nothing and the verdict reads no repository at all; bridge_list_owners names the ones it holds", "agent": "Agent name", "requester": "Who kicked the task off (name, email, or both): their usual approvers count, and nothing is routed back to them", "paths": "Files or directories the task will touch, comma separated (optional)", "client_key": "Your own id for this kickoff; the same key returns the same task on a retry (optional, recommended)", "facts": "What holds for the whole task as key=value pairs, comma separated (release=library-next, customer=globex), copied from the task or its tickets and never invented: every node inherits them unless it states its own, so reuse and rules compare the same values (optional)"}, []),
    tool("bridge_add_node", "Write one decision you discovered to the canvas as a node under the decision it grew from (parent_id). You break the task down, Raven does not. Raven resolves the node from memory and records where it can (status resolved: evidence with a citation, marked for a person's sign-off, not authorization), predicts from precedent or from an unsigned earlier answer (predicted, to confirm), routes it to the person the signals name (pending), or says it does not know (unrouted). Idempotent: the same client_ref, or the same question in the same scope on this tree, returns the existing node; the same question about other files or another named customer is a new node, linked as related. To take up a follow-up question a person added, pass adopt=<its node_id>.",
         {"task_id": "Task ID from bridge_start_task", "question": "One specific decision", "context": "Evidence, constraints, consequences; name the customer, account, environment or case this decision is about", "parent_id": "The node this decision grew from (optional for a root decision)", "paths": "Files or directories this decision is about, comma separated", "options": "Candidate answers separated by | (a comma stays inside its option), each saying what it leads to; at most 8 (optional)", "category": "Optional: definition, policy, data-source, rollout, compat, ux, ops", "client_ref": "Your own id for this node, for idempotent retries", "requester": "Overrides the task's requester for this node", "adopt": "node_id of a suggested follow-up question to adopt", "depends_on": "Node ids on this task this decision cannot be acted on without, comma separated (optional)", "facts": "What you know for certain about this decision as key=value pairs, comma separated (plan=enterprise, customer=globex); a reusable rule applies only to facts you state (optional)"}, ["task_id", "question"]),
    tool("bridge_settle_node", "Record the answer you settled a node with yourself and why. It stays on the tree, unconfirmed and marked for sign-off, so the people who own the area see the whole decision record and can correct it. It is not authorization: prepare on it, do not ship on it.",
         {"task_id": "Task ID", "node_id": "Node ID", "answer": "The answer you acted on", "rationale": "Why"}, ["task_id", "node_id", "answer"]),
    tool("bridge_get_tree", "Read the whole canvas: the verdict, every node nested under its parent with status (resolved, predicted, pending, unrouted, answered, duplicate, suggested), whether a person authorized it (authorized) or it still waits on one (blocking), the answers people gave, the follow-up questions they added for the next level (adopt them with bridge_add_node), nodes whose parent was answered after they were written, nodes that need review because an answer they leaned on was corrected, what each node depends on, the notes people added to the task, and what to do next. Read it before an irreversible step and whenever you would otherwise wait.",
         {"task_id": "Task ID"}, ["task_id"]),
    tool("bridge_wait", "Wait for human changes or a running advisory diff review instead of polling. After finishing a task, a whole-task wait also waits for its review and returns review.status plus the result; once terminal, repeat bridge_finish_task with the same complete diff and checks to refresh the saved proof before exporting. For people: blocks up to timeout seconds (default 300) until something on the task changes because a person acted (an answer landed, a sign-off, a correction, a hand-on, a follow-up question added, a node put in doubt) and returns what changed with the next step for each node; on timeout it names the nodes still waiting and on whom. Call it once your independent work is done. Nothing is lost by not waiting: an answer that lands meanwhile is on the tree when you read it. While it waits it sends a progress notification every 15 seconds to a client that asked for progress. Every call remains bounded by the MCP call budget (50 seconds by default); call again with since to continue waiting. If Raven stops or restarts while you wait, the wait answers at once with interrupted set: call it again in a few seconds, nothing is lost.",
         {"task_id": "Task ID", "node_id": "Watch this node only (optional)", "timeout": "Seconds requested, capped at the MCP call budget (50 by default); call again with since to keep waiting", "since": "The observed_at of your last bridge_get_tree or bridge_wait: what people did since then is returned at once (optional, recommended)"}, ["task_id"]),
    tool("bridge_finish_task", "Close the task. Refused while a node still waits on a person: an open question, an answer nobody signed, a decision put in doubt by a correction, or a follow-up question a person marked required that you have not adopted. The refusal names the nodes. Pass checks with what you actually ran (tests, build, lint) and their outcome, and diff with the change you made: both are recorded as your claim beside the decisions people authorized. Preserve the complete patch exactly, including new files and its final newline; pass diff_sha256 computed from the original bytes to catch transport changes before completion. This host-reported hash is not independent attestation. Given a diff, a model reads each authorized decision against it and returns follows: for each one a verdict, the line of the diff each requirement was read at, any counterexample it found (a path through the change that does what was ruled out) and the code it depends on that the diff does not show. That is a model's reading of what you supplied, not a test and never an approval: report it as read, name every counterexample, and the finish is not refused on a departure. Finishing means every decision was authorized, never that the change does what was authorized. Reading the diff can take minutes. Every finish call answers within about 45 seconds, even when progress notifications are enabled, with review.status running if needed; the reading goes on and is kept. Use a whole-task bridge_wait or bridge_get_tree to read its result, then repeat bridge_finish_task with the same complete diff and checks to save the terminal review in a refreshed proof; completed reads are reused without another model call. A reading is about the signed answers it read: when one changes afterwards, the tree marks the review stale and names the decisions, and you call bridge_finish_task again with your current diff. On a task Raven engaged, a finish whose diff changes a file someone else decides, when nothing on the task was put to them, is refused and names the file: write what the change there settles as a node for them, or call again with uncovered giving one line per file on why it settles nothing.",
         {"task_id": "Task ID", "status": "completed (default), working, or abandoned to close a mistaken task",
          "reason": "Required explanation when status is abandoned",
          "checks": "What you ran and what it said: the test command and its result, the build, the lint, in a few lines. Recorded as your claim; the first 2000 characters are kept (optional)",
          "diff": "The complete unified diff against the intended base, including staged, unstaged and new files (plain git diff omits untracked files). Read the patch as UTF-8 and serialize it directly, preserving exact bytes, whitespace and the final newline; do not trim, summarize, reconstruct or use shell command substitution that removes trailing newlines. A source file without a final newline still has a line-terminated patch with a No newline at end of file marker. Raven reads the authorized decisions against what you supply, so an excerpt can hide a requirement you implemented (optional)",
          "diff_sha256": "Expected SHA-256 computed by the coding host from the complete original patch bytes before serialization: exactly 64 hexadecimal characters. Raven rejects a mismatch with the submitted diff encoded as UTF-8 before finishing. This checks transport consistency of a host-reported hash, not independent repository or test attestation (optional, recommended with diff)",
          "uncovered": "Only when a finish was refused for changed files whose decider was asked nothing: one line per file, `<path>: why the change there settles nothing`. A file whose change does settle something gets a node instead (optional)"}, ["task_id"]),
    tool("bridge_get_decision", "Retrieve one node as a decision record: the human answer, provenance, and revision history. options are proposals, not the signed answer: people may refine them or answer outside them. Summarize the recorded answer and rationale; do not claim it selected an original option unless the record supports that. approval_pending is true until a person has answered or signed it (evidence found in records or memory does not clear it); authorized says whether a person stands behind the answer. While approval_pending, continue independent work and do not ship on the answer.",
         {"decision_id": "Decision ID (a node_id)"}, ["decision_id"]),
    tool("bridge_export_proof", "Retrieve the portable change-proof bundle saved by bridge_finish_task. Includes the complete submitted diff and its SHA-256, signed decision revisions, scope, attribution, citations and host-reported checks, plus a review-ready Markdown summary. bundle.payload.review is the immutable saved snapshot; top-level review is the current reading. review_comparison states their IDs and statuses separately. review_snapshot_current=false does not imply a new review ID or attempt, and current findings must not be attributed to the saved snapshot. For historical lookup, report both without rewriting history. When preparing an updated proof, wait if review_pending, then repeat bridge_finish_task with the same complete diff and checks if review_snapshot_current is false. The digest detects tampering but is not a human digital signature or proof that tests ran.",
         {"task_id": "Completed task ID, previously finished with the complete diff"}, ["task_id"]),
    tool("bridge_search_decisions", "Find similar approved or evidence-resolved decisions as evidence (stemmed lexical overlap, hashed cosine, FTS5, recency-weighted). These are approvals for their original context, not blanket authorization for new work. options are proposals; the recorded human answer may refine them or fall outside them. Read the answer and rationale rather than infer a selected option.",
         {"query": "Question to search", "repo": "Optional repository scope"}, ["query"]),
    tool("bridge_list_owners", "List discovered Slack contacts, any optional verified authority map (who knows, decides or approves which paths, decision categories or repositories), any optional coordinator, the configured owners with their path patterns, and the ownership graph inferred from git (blame, CODEOWNERS, reviews) per repository.", {"repo": "Optional repository scope"}, []),
    tool("bridge_ingest_repo", "Build or refresh the ownership map and record graph from a local git checkout (shared HTTP instances require an operator credential; ordinary agent credentials use the checkout ingested during setup): git log, blame shares, CODEOWNERS, Reviewed-by trailers, merged and squashed PRs.",
         {"path": "Absolute path to a local git checkout", "repo": "Optional repository name override", "max_commits": "Optional history depth (0 = all)"}, ["path"]),
    tool("bridge_import_record", "Import a ticket, document or Slack record retrieved through your connected tools. Copy the source accurately, including status, author and permalink. This is evidence and a routing signal, never human authorization. Do not import secrets, unrelated private records, or Slack Real-time Search results (they must stay transient).",
         {"repo": "Repository owner/name", "kind": "ticket, jira, issue, doc, slack or note; kinds retain separate identities", "ref": "Stable source identifier such as NET-102",
          "title": "Source title", "body": "Source text", "author": "Source author's email, Slack member ID or full name",
          "url": "Original permalink", "status": "Source status including Cancelled, Superseded or Won't Do",
          "resolved": {"type": ["boolean", "string", "integer"], "description": "Source resolution flag, never Raven approval. False prevents settled evidence; open or unknown status also prevents it."},
          "paths": {"type": ["string", "array"], "items": {"type": "string"}, "description": "Complete related-path snapshot, comma separated or array. Supplied replaces old paths, [] clears; omitted retains."},
          "created_at": "Source timestamp in ISO format"}, ["repo", "kind", "ref"]),
    tool("bridge_connection_status", "Check ingestion, Slack contact discovery and delivery problems without opening the web UI. No manual ownership map or recipient accounts are required.", {}, []),
]


INSTRUCTIONS = (
    "Kickoff and node writes return their static reading promptly. model_pending means inference is still running; "
    "use bridge_wait or bridge_get_tree to read the update. Resume with bridge_start_task(task_id=...). "
    "To close an accidental task nobody has answered, use bridge_finish_task(status=abandoned, reason=...). "
    "Raven is the canvas for the decisions inside a task. You break the task down; Raven finds out who owns "
    "what, what the org already settled, and who has to be asked. People reply in Slack; "
    "the web overview and personal Raven accounts are optional. Do not ask the user to maintain an ownership map. "
    "Use bridge_connection_status to check connected sources and Slack. Ingest a local checkout when permitted, "
    "and import relevant tickets or documents obtained through your connected tools with bridge_import_record. Slack Real-time Search results are transient context, not records to import. "
    "1. Call bridge_start_task the moment a task is kicked off, before any work, with the task as given, the "
    "repository, who kicked it off if known (requester), and a client_key of your own so a "
    "retry returns the same task. Paths are optional: discover them yourself in the repository; do not ask the user to supply file paths, owners, or a decision list. It returns engage or pass with the reason. On pass, proceed without Raven "
    "(still add a node if a real decision appears). It returns unplaced when the task's words name no area and "
    "you gave no paths: find the files first, then call bridge_start_task again with the same title, repo and "
    "client_key and those files as paths, before your first edit. "
    "2. On engage, write each decision you discover as a node with bridge_add_node, with parent_id set to the "
    "decision it grew from; give client_ref so a retry is idempotent, and name in the context the customer, "
    "account or case the decision is about. Kickoff candidates are prompts, not a required list of new nodes. "
    "Read the existing questions and the current parent answer in bridge_get_tree before adding overlapping "
    "questions; add only decisions not already covered in their actual scope. A node's parent context is not "
    "authorization for that node or an ownership transfer. A node comes back resolved (evidence from records or "
    "signed answers, "
    "with a citation, marked for sign-off by the person the signals name), predicted (an unconfirmed default or an "
    "unsigned earlier answer), pending (routed to the person the signals name, waiting), unrouted (Raven does not "
    "know), or duplicate (the same open decision elsewhere; read through to it). "
    "3. Use bridge_settle_node for what you settle yourself; it stays on the tree for sign-off. "
    "4. Evidence is not authorization. A resolved or predicted node is unconfirmed until a person signs it "
    "(authorized true): prepare on it, do not ship on it, and never treat a prediction as sign-off. "
    "5. Read bridge_get_tree before any irreversible step: it carries the answers people gave, the follow-up "
    "questions they added for the next level (adopt them with bridge_add_node adopt=...; one they marked required "
    "must be adopted, and the task cannot finish until it is answered), nodes whose parent was "
    "answered after they were written, and nodes that need review because an answer they leaned on was corrected. "
    "When a node waits on a person, do the independent work first, then bridge_wait (it returns when a person "
    "acts, or at the timeout with who is still being waited on) rather than polling or giving up; people answer "
    "in Slack or the inbox, minutes or hours later, and the answer is on the tree whenever you read it. If your "
    "session restarts, bridge_start_task with the same client_key returns the same task. "
    "6. bridge_finish_task when done; it is refused while a node waits on a person, or while a person has acted "
    "on the task since you last read the tree, and names which. Submit the complete diff against the intended base, "
    "including staged, unstaged and new files; plain git diff omits untracked files. Capture the patch once, read "
    "it as UTF-8 without newline conversion, and serialize it directly, preserving exact bytes and the final newline. "
    "Do not trim it, reconstruct it, or use shell command substitution that removes trailing newlines. Compute "
    "diff_sha256 from the original patch bytes before serialization and pass it with diff so Raven can reject "
    "transport damage before finishing. A matching host-reported hash is not independent attestation of the "
    "repository or tests. If the advisory review is running, use a whole-task bridge_wait to read its result, "
    "then repeat bridge_finish_task with the same complete diff and checks to refresh the saved proof and "
    "call bridge_export_proof. Review findings remain advisory, never authorization or a test run.")


def _list_pending(store, run_id=None, path=None):
    sql = ("SELECT d.id, d.run_id, d.question, d.path, o.name AS owner_name, d.kind, d.prediction, d.created_at "
           "FROM decisions d LEFT JOIN owners o ON o.id=d.owner_id WHERE d.status='pending' AND d.draft=0")
    args = []
    if run_id:
        sql += " AND d.run_id=?"
        args.append(run_id)
    with store.connect() as db:
        rows = [dict(row) for row in db.execute(sql + " ORDER BY d.created_at DESC", args)]
    if path:
        rows = [d for d in rows if (d["path"] or "").lstrip("/").startswith(path.lstrip("/"))]
    return {"pending": rows}


def _github_status(store):
    """What GitHub has told this instance per repository, and how an
    operator makes it current. The operator instructions live here, not
    in the messages people read in Slack."""
    from .github import SYNC_HOW, sync_note, sync_states
    synced = [{"repo": s["repo"], "last_success_at": s.get("last_success_at") or "", "last_error": s.get("last_error") or ""}
              for s in sync_states(store.graph)]
    seen = {s["repo"] for s in synced}
    not_synced = [{"repo": r, "note": sync_note(store.graph, r, operator=True)}
                  for r in sorted(store.graph.known_repos()) if r not in seen and "/" in r]
    return {"synced": synced, "not_synced": not_synced, "how": SYNC_HOW}


def _list_owners(store, args):
    repo = args.get("repo", "")
    with store.connect() as db:
        owners = [dict(row) for row in db.execute("SELECT * FROM owners ORDER BY created_at")]
        graph = graph_summary(db)
        rows = ownership_rows(db, repo, limit=0)
    settings = store.settings()
    return {"owners": owners, "ownership": rows, "graph": graph,
            "people": [{k: p[k] for k in ("id", "name", "email", "github_login", "team", "teams", "active")}
                       for p in store.people()],
            "authority": [{k: a[k] for k in ("id", "who", "is_team", "scope_kind", "scope", "role", "repo", "source",
                                              "asserted_by", "accepted", "effective_to")}
                          for a in store.authority(repo)],
            "coordinator": (settings["coordinator"] or {}).get("name", ""),
            "notice": "Verified authority (people, teams, the authority map) outranks what git history suggests; "
                      "otherwise Raven infers a first contact from connected sources and asks in Slack. "
                      "An optional coordinator or Slack triage channel receives questions it cannot route."}


def _ingest_repo(store, args):
    depth = args.get("max_commits")
    try:
        depth = int(depth) if depth not in (None, "") else None
    except ValueError:
        raise Invalid("max_commits must be an integer")
    try:
        return index_repo(store.graph, args["path"], max_commits=depth, repo_name=args.get("repo", ""))
    except RuntimeError as error:
        raise Invalid(str(error))


HANDLERS = {
    "bridge_start_task": lambda store, args: canvas.start_task(store, load(), args),
    "bridge_add_node": lambda store, args: canvas.add_node(store, load(), args),
    "bridge_settle_node": lambda store, args: canvas.settle_node(store, args),
    "bridge_get_tree": lambda store, args: canvas.get_tree(store, args["task_id"]),
    "bridge_wait": lambda store, args: canvas.wait(store, args),
    "bridge_finish_task": lambda store, args: canvas.finish_task(store, args),
    "bridge_get_decision": lambda store, args: store.get_decision(args["decision_id"]),
    "bridge_export_proof": lambda store, args: __import__("bridge.proof", fromlist=["export"]).export(store, args),
    "bridge_search_decisions": lambda store, args: store.search(args["query"], repo=args.get("repo", "")),
    "bridge_list_owners": _list_owners,
    "bridge_ingest_repo": _ingest_repo,
    "bridge_import_record": lambda store, args: store.add_record(args),
    "bridge_connection_status": lambda store, args: {
        "delivery": {"enabled": store.delivery.enabled, "channel": store.delivery.channel,
                     "teams_reply_enabled": bool(store.delivery._teams_destination()),
                     "failed": store.delivery.list(state='failed')},
        "inference": __import__("bridge.config", fromlist=["backend_status"]).backend_status(),
        "readiness": store.readiness(),
        "github": _github_status(store),
        "sources": [dict(r) for r in store.graph.db.execute("SELECT repo,kind FROM connector_sources ORDER BY repo,kind")],
        "records": [dict(r) for r in store.graph.db.execute("SELECT repo,kind,count(*) AS count FROM intents GROUP BY repo,kind ORDER BY repo,kind")],
        "slack": {
            "enabled": store.delivery.enabled and store.delivery.channel == "slack",
            "directory": json.loads(store.graph.get_setting("slack_directory") or "{}"),
            "failed": store.delivery.list(state="failed"),
            "inbound_failed": store.delivery.inbound_failed(),
            "reply_failures": store.delivery.reply_failures(),
            "triage_channel": store.delivery.fallback_channel or store.graph.get_setting("slack_fallback_channel")}},
}


PROGRESS_EVERY = 15.0


def progress_sleep(token, notify, every: float | None = None, clock=None, sleep=None,
                   message: str = "waiting for a person to act on this task"):
    """A sleep for canvas.wait that sends an MCP progress notification for
    `token` every `every` seconds, so a client's idle timer sees a live
    call. Measured live on a397f1c: Claude Code aborts a tool call that
    "sent no response or progress" for long enough, and without this an
    HTTP wait was cut to 50 seconds per call, 28 calls on one task."""
    import time as _time
    clock = clock or _time.monotonic
    sleep = sleep or _time.sleep
    started = clock()
    last = [started]

    def tick(seconds):
        sleep(seconds)
        now = clock()
        if now - last[0] >= (PROGRESS_EVERY if every is None else every):
            last[0] = now
            notify({"jsonrpc": "2.0", "method": "notifications/progress",
                    "params": {"progressToken": token, "progress": round(now - started, 1),
                               "message": message}})
    return tick


PROGRESS_MESSAGES = {"bridge_finish_task": "reading the diff against what was signed",
                     None: "waiting for a person or the advisory diff review"}


def _progress_token(params) -> object:
    meta = params.get("_meta") if isinstance(params, dict) else None
    return meta.get("progressToken") if isinstance(meta, dict) else None


def call_tool(store, name, args, wait_cap=None, sleep=None, stop=None):
    """One tool call after schema validation. wait_cap bounds one
    bridge_wait on this transport: the canvas cap on stdio and on an HTTP
    call kept alive with progress notifications, the server's request
    bound on a plain HTTP call. `sleep` is the wait's sleep, the one that
    sends those notifications."""
    schema = next((item["inputSchema"] for item in TOOLS if item["name"] == name), None)
    if schema is None:
        raise Invalid(f"Unknown tool: {name}")
    if not isinstance(args, dict):
        raise Invalid("Tool arguments must be an object")
    for key in args:
        if key not in schema["properties"]:
            raise Invalid(f"Unknown argument: {key}")
    for key in schema["required"]:
        if key not in args:
            raise Invalid(f"Missing required argument: {key}")
    for key, value in args.items():
        spec = schema["properties"][key]
        allowed = spec["type"] if isinstance(spec["type"], list) else [spec["type"]]
        actual = ("boolean" if isinstance(value, bool) else "string" if isinstance(value, str)
                  else "integer" if isinstance(value, int) else "array" if isinstance(value, list) else "unknown")
        if actual not in allowed or actual == "array" and not all(isinstance(v, str) for v in value):
            raise Invalid(f"Argument {key} must be {' or '.join(allowed)}")
    # A node still waiting on a person is refused by the finish itself,
    # naming who; an answer the agent has not read is the next gate.
    if name == "bridge_finish_task":
        canvas.require_agent_read(store, args)
    if name == "bridge_wait":
        result = canvas.wait(store, args, cap=canvas.call_budget() if wait_cap is None else min(wait_cap, canvas.call_budget()), sleep=sleep,
                             stop=stop)
    elif name == "bridge_finish_task":
        # With a progress sleep the finish waits for the whole reading of
        # the diff; without one it answers in time and keeps the reading.
        result = canvas.finish_task(store, args, sleep=sleep, stop=stop)
    else:
        result = HANDLERS[name](store, args)
    if name in ("bridge_start_task", "bridge_add_node"):
        result["delivery"] = {"enabled": store.delivery.enabled,
                              "channel": store.delivery.channel if store.delivery.enabled else "",
                              "notice": ("People answer in Slack; use bridge_wait and bridge_get_tree. No web account is required."
                                         if store.delivery.enabled and store.delivery.channel == "slack" else
                                         "Slack is not connected. Nobody will be notified in Slack; the operator must connect it.")}
        if name == "bridge_add_node" and result.get("node_id"):
            result["delivery"]["notifications"] = [dict(r) for r in store.graph.db.execute(
                "SELECT kind,state,person_name,last_error FROM notifications WHERE decision_id=? ORDER BY created_at",
                (result["node_id"],))]
    # A read of the whole tree: a wait on one node shows only that node.
    if name == "bridge_get_tree" or (name == "bridge_wait" and not args.get("node_id")):
        shown = canvas._flatten(result.get('nodes') or []) if name == 'bridge_get_tree' else result.get('changed') or []
        result['read_acknowledged'] = canvas.note_agent_read(
            store, args.get('task_id', ''), result.get('observed_at', ''), result.get('observed_revision'),
            {n['node_id'] for n in shown if n.get('node_id') and 'answer' in n})
        if not result['read_acknowledged']:
            result['next'] += ' This response omitted unread decisions; read bridge_get_tree before finishing.'
    return result


def params_error(method, params):
    """Validate the shared envelope before either transport indexes it.

    Tool argument schemas are checked by call_tool; malformed protocol
    fields must instead return Invalid params under the original request ID.
    """
    if not isinstance(params, dict):
        return "Invalid params"
    if method == "initialize" and "protocolVersion" in params and not isinstance(params["protocolVersion"], str):
        return "protocolVersion must be a string"
    if method == "tools/call":
        if not isinstance(params.get("name"), str):
            return "Tool name must be a string"
        if "arguments" in params and not isinstance(params["arguments"], dict):
            return "Tool arguments must be an object"
    return None


def dispatch(store, message, notify=None):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    if "id" not in message:
        return None
    response = {"jsonrpc": "2.0", "id": message["id"]}
    method, params = message["method"], message.get("params", {})
    error = params_error(method, params)
    if error:
        return {**response, "error": {"code": -32602, "message": error}}
    if method == "initialize":
        result = {"protocolVersion": params.get('protocolVersion') if params.get('protocolVersion') in SUPPORTED_HTTP_VERSIONS else PROTOCOL_VERSION,
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "bridge", "version": "0.2.0"},
                  "instructions": INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        token = _progress_token(params)
        tick = (progress_sleep(token, notify, message=PROGRESS_MESSAGES.get(params.get("name"), PROGRESS_MESSAGES[None]))
                if notify is not None and token is not None else None)
        try:
            value = call_tool(store, params.get("name"), params.get("arguments", {}), sleep=tick)
            result = {"content": [{"type": "text", "text": json.dumps(value)}], "isError": False}
        except Invalid as error:
            result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
        except Exception as error:
            # Any other failure is still this request's failure: the agent
            # gets it back under the id it sent, not an id-less crash.
            print(f"Raven: {params.get('name')}: {type(error).__name__}: {error}", file=sys.stderr)
            result = {"content": [{"type": "text", "text": f"{type(error).__name__}: {error}"}], "isError": True}
    else:
        return {**response, "error": {"code": -32601, "message": "Method not found"}}
    return {**response, "result": result}


def serve_stdio(store):
    parse_error = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
    for line in sys.stdin.buffer:
        if len(line) > MAX_LINE:
            response = parse_error
        else:
            try:
                message = json.loads(line)
                response = dispatch(store, message, notify=lambda note: print(json.dumps(note), flush=True))
            except (json.JSONDecodeError, UnicodeDecodeError):
                response = parse_error
            except Exception as error:
                print(f"Raven: {error}", file=sys.stderr)
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "Internal error"}}
        if response is not None:
            print(json.dumps(response), flush=True)
    canvas.wait_for_background(timeout=900.0)
