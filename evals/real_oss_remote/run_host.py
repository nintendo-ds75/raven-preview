"""Run an actual coding host against shared Raven, with simulated people.

No real Slack or GitHub writes. The host sees a pre-change source snapshot
and the problem brief, not cases.json, public PR caches, or the Raven DB.
The simulation records routing before using labels to recover a misroute.
"""
import argparse
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from bridge import canvas
from bridge.auth import Auth
from bridge.authz import Actor
from bridge.config import Config
from bridge.server import make_server
from bridge.store import Invalid, Store

HERE = Path(__file__).resolve().parent


def dump(path, obj):
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False))


class LocalSlack:
    name = "slack"

    def __init__(self, path):
        self.path = path
        self.counter = 0

    def open_dm(self, user_id):
        return "D" + user_id

    def post_message(self, channel, text, blocks=None, thread_ts=""):
        self.counter += 1
        ts = f"{int(time.time())}.{self.counter:06}"
        with self.path.open("a") as out:
            out.write(json.dumps({"time": time.time(), "channel": channel, "ts": ts,
                                  "thread_ts": thread_ts, "text": text, "blocks": blocks}) + "\n")
        return ts


def read_trace(path):
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            pass
    return records


def trace_state(records):
    """Observe completed tool responses, never an in-flight DB placeholder.

    Each proxy session reuses JSON-RPC ids, so initialize resets the pending map.
    A wait is active only until its matching response, including error responses.
    """
    pending, completed = {}, []
    for record in records:
        message = record["message"]
        session = record.get('session', '')
        key = (session, message.get('id'))
        if record["direction"] == "request":
            if message.get("method") == "initialize":
                pending = {k: v for k, v in pending.items() if k[0] != session}
            if message.get("method") == "tools/call":
                pending[key] = record
        elif key in pending:
            request = pending.pop(key)
            result = message.get("result", {})
            if result.get("isError") or "error" in message:
                continue
            for content in result.get("content", []):
                if content.get("type") == "text":
                    try:
                        value = json.loads(content["text"])
                    except (ValueError, KeyError):
                        continue
                    completed.append((request, record, value))
                    break
    waits = [r for r in pending.values() if r["message"]["params"]["name"] == "bridge_wait"]
    return completed, waits


def host_prompt(case, phase):
    recovery = ("Your previous host process stopped while waiting. Recover the existing task with the same client_key; "
                "use the durable canvas to learn what happened. Do not create replacement copies of existing nodes.\n"
                if phase == "resume" else "")
    return f"""You are the coding host in a local evaluation of Bridge on the real Grafana repository.
The workspace is an exported, pre-change source snapshot with no future git history.
Work only in this workspace. Do not read evaluator files, other directories, future commits,
public PRs, or answer keys. Do not contact external people/services, spawn agents, push,
commit, or deploy. Repo instructions apply to source work. Go/dependencies may be absent;
do not install them or claim unrun tests passed. Produce a narrow patch and focused test
changes where feasible, plus an implementation summary with honest validation limits.

Task: {case['title']}
{case['brief']}
Relevant starting areas: {', '.join(case['scope'])}.

Use the installed Bridge MCP tools as the host workflow. Start with bridge_start_task,
repo=grafana/grafana, client_key=real-oss-{case['id']}, agent=codex-host,
requester=Evaluation Developer, and goal containing the complete task brief above.
Discover the actual decisions yourself from the brief,
code and returned discovery context. There is no supplied decision list. Record meaningful
decisions in the canvas; use evidence or a proposal for things you can establish, and ask
the owner where judgment is needed. Include concrete alternatives and useful context in
questions. Use parent/child or dependency relationships when one decision genuinely
depends on another. Avoid making an arbitrary question for every implementation detail.

Keep doing independent investigation/test preparation while waiting. Human replies are
simulated through the real Bridge inbox/Slack receiver. Use bridge_wait with timeout=60
when waiting, then re-read the tree, adopt relevant human follow-ups, and incorporate the
answers in your work. You cannot sign your own proposals or call human REST endpoints.
The simulator does not answer until you reach a wait. Use tree state before final action;
only finish the task when every required decision is authorized. If Bridge fails, report
the actual failure without bypassing it. Write EVALUATION_IMPLEMENTATION.md describing
your patch, decisions, and validation. This is a reviewable patch, not a production release.
{recovery}"""


def command(workspace, out, phase, host):
    if host == "claude":
        mcp = out / "mcp-config.json"
        dump(mcp, {"mcpServers": {"bridge": {"command": sys.executable,
                                          "args": [str(HERE / "mcp_proxy.py")]}}})
        return ["claude", "-p", "--output-format", "stream-json", "--verbose",
                "--tools", "Read,Grep,Glob,Edit,Write", "--setting-sources", "",
                "--permission-mode", "acceptEdits",
                "--max-turns", "60", "--strict-mcp-config", "--mcp-config", str(mcp),
                "--allowedTools", "mcp__bridge"]
    config = {
        "mcp_servers.bridge.command": sys.executable,
        "mcp_servers.bridge.args": [str(HERE / "mcp_proxy.py")],
        "mcp_servers.bridge.env_vars": ["BRIDGE_TOKEN", "BRIDGE_EVAL_URL", "BRIDGE_EVAL_TRACE", "BRIDGE_EVAL_AUDIT"],
        "mcp_servers.bridge.required": True,
        "mcp_servers.bridge.startup_timeout_sec": 20,
        "mcp_servers.bridge.tool_timeout_sec": 120,
        "mcp_servers.bridge.default_tools_approval_mode": "approve",
        "web_search": "disabled",
        "features.multi_agent": False,
        "approval_policy": "never",
    }
    cmd = ["codex", "exec", "--ignore-user-config", "--ephemeral", "--sandbox", "workspace-write",
           "--skip-git-repo-check", "--cd", str(workspace), "--json",
           "--output-last-message", str(out / f"host-final-{phase}.md")]
    for key, value in config.items():
        cmd += ["-c", key + "=" + json.dumps(value)]
    return cmd + ["-"]


def run(prepared, case_id, timeout, host="codex", restart=False):
    os.environ.update(BRIDGE_MODEL_API="none", BRIDGE_SEMANTIC="0", BRIDGE_LIVE="0")
    # Explicitly prevent inherited real connector credentials from enabling a
    # production transport. Codex auth remains its existing local login.
    for key in ("SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "TEAMS_WEBHOOK_URL", "GITHUB_TOKEN", "GH_TOKEN", "BRIDGE_ADMIN_TOKEN"):
        os.environ.pop(key, None)
    spec = json.loads((HERE / "cases.json").read_text())
    case = next(c for c in spec["cases"] if c["id"] == case_id)
    if restart:
        case = {**case, "recovery": "restart"}
    out = prepared / "runs" / case_id
    out.mkdir(parents=True, exist_ok=True)
    if (out / "bridge.db").exists():
        raise RuntimeError("Case already exists; use a fresh prepared fixture for an independent rerun")
    workspace = prepared / "workspaces" / case_id
    shutil.copytree(prepared / "snapshot", workspace)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "add", "."], check=True)
    subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Raven Evaluation", "-c", "user.email=eval@example.test",
                    "-c", "core.hooksPath=/dev/null", "commit", "-qm", "Frozen public source snapshot"], check=True)
    with sqlite3.connect(prepared / "baseline.db") as source, sqlite3.connect(out / "bridge.db") as dest:
        source.backup(dest)
    store = Store(out / "bridge.db")
    g = store.graph
    pid = g.add_person("Evaluation Developer", slack_id="UDEVELOPER", source="eval fixture")
    auth = Auth(store, enabled=True)
    token = auth.create_token(pid, "codex-host-evaluation")["token"]
    delivery = store.connect_delivery(LocalSlack(out / "slack-outbox.jsonl"), fallback_channel="CEVAL")
    server = make_server(store, port=0, auth=auth, wait_cap=30)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    trace_path = out / "mcp-trace.jsonl"
    env = {**os.environ, "BRIDGE_TOKEN": token, "BRIDGE_EVAL_URL": f"http://127.0.0.1:{server.server_port}",
           "BRIDGE_EVAL_TRACE": str(trace_path), "BRIDGE_EVAL_AUDIT": str(out / 'audit.jsonl')}
    from evals.audit.ledger import Ledger, ActionLog, TaskObserver
    audit = Ledger(out / 'audit.jsonl', secrets=(token,))
    audit.append('run_started', {'case': case_id, 'host': host,
                               'fixture_manifest': json.loads((prepared / 'manifest.json').read_text()),
                               'mode': 'legacy replay; not a sealed-fixture acceptance run'})
    actions = ActionLog(audit)
    observer = TaskObserver(audit)
    first_routes = {}
    replied = set()
    attempted_threads = set()
    followup_added = False
    had_wait = False
    interrupted = False
    first_tree = False
    task_id = ""
    started = time.time()
    proc = None
    phases = ["initial", "resume"] if case["recovery"] == "restart" else ["initial"]
    try:
        for phase in phases:
            prompt = host_prompt(case, phase).replace("agent=codex-host", "agent=" + host + "-host")
            (out / f"host-prompt-{phase}.txt").write_text(prompt)
            audit.append('host_prompt', {'phase': phase, 'text': prompt})
            with (out / f"host-events-{phase}.jsonl").open("w") as stdout, (out / f"host-stderr-{phase}.log").open("w") as stderr:
                prior_count = len(read_trace(trace_path))
                proc = subprocess.Popen(command(workspace, out, phase, host), stdin=subprocess.PIPE, stdout=stdout,
                                        stderr=stderr, text=True, env=env, cwd=workspace, start_new_session=True)
                proc.stdin.write(prompt)
                proc.stdin.close()
                phase_started = time.time()
                while proc.poll() is None:
                    if time.time() - phase_started > timeout:
                        actions.append({"event": "host_timeout", "phase": phase})
                        os.killpg(proc.pid, signal.SIGTERM)
                        proc.wait(timeout=20)
                        break
                    calls = read_trace(trace_path)[prior_count:]
                    completed, waits = trace_state(calls)
                    waiting = bool(waits)
                    if waiting:
                        had_wait = True
                    for request, response, node in completed:
                        params = request["message"]["params"]
                        if params["name"] != "bridge_add_node" or not isinstance(node, dict) or not node.get("node_id"):
                            continue
                        if node["node_id"] not in first_routes:
                            first_routes[node["node_id"]] = {"question": node["question"], "owner": node.get("owner"),
                                "status": node["status"], "evidence": node.get("owner_evidence"),
                                "context": params["arguments"].get("context", ""),
                                "parent_id": node.get("parent_id"), "depends_on": node.get("depends_on"),
                                "phase": phase, "before_any_owner_reply": not replied,
                                "response_time": response["time"]}
                    tasks = g.db.execute("SELECT id FROM runs WHERE client_key=?", ("real-oss-" + case_id,)).fetchall()
                    if tasks:
                        task_id = tasks[0]["id"]
                        tree = canvas.get_tree(store, task_id)
                        observer.observe(tree, canvas.trace(store, task_id))
                        nodes = canvas._flatten(tree["nodes"])
                        delivery.deliver_now()
                        if waiting and not first_tree:
                            dump(out / "first-tree-before-replies.json", tree)
                            first_tree = True
                            if any(n.get("blocking") for n in nodes):
                                try:
                                    canvas.finish_task(store, {"task_id": task_id})
                                except Invalid as error:
                                    actions.append({"event": "pre_answer_finish_refused", "error": str(error)})
                                else:
                                    actions.append({"event": "pre_answer_finish_allowed"})
                                    store.update_run(task_id, {"status": "working"})
                        if waiting and phase == "initial" and case["recovery"] == "restart":
                            dump(out / "tree-at-host-interruption.json", tree)
                            os.killpg(proc.pid, signal.SIGTERM)
                            proc.wait(timeout=20)
                            interrupted = True
                            actions.append({"event": "host_process_interrupted_while_waiting", "task_id": task_id})
                            break
                        if waiting and time.time() - min(r["time"] for r in waits) >= 3:
                            for node in nodes:
                                nid = node["node_id"]
                                if nid not in first_routes or node.get("authorized") or node["status"] in ("suggested", "adopted", "duplicate") or nid in replied:
                                    continue
                                expected_login = case["approvers"][0]
                                target = g.find_person("@" + expected_login)
                                if target is None:
                                    target_id = g.add_person(expected_login, github_login=expected_login,
                                        slack_id="U" + hashlib.sha256(expected_login.encode()).hexdigest()[:10].upper(),
                                        source="eval: hidden-key reviewer introduced only during recovery")
                                    target = g.get_person(target_id)
                                row = store.get_decision(nid)
                                if (node.get("owner") or "").lower() != target["name"].lower():
                                    who = node.get("owner") or "Evaluation Coordinator"
                                    referrer = g.find_person(who) or g.coordinator(spec["repository"])
                                    store.refer(nid, {"person": target["id"], "by": who, "expected_updated_at": row["updated_at"],
                                                     "note": "Simulated referral from the wrong first contact; historical reviewer answers"},
                                                actor=Actor.person(referrer, kind="session"))
                                    actions.append({"event": "simulated_referral", "node": nid, "from": who, "to": target["name"]})
                                    delivery.deliver_now()
                                note = g.db.execute("SELECT * FROM notifications WHERE decision_id=? AND state='sent' "
                                                    "AND person_name=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (nid, target["name"])).fetchone()
                                if note is None:
                                    delivery.enqueue(nid, "ask" if row["status"] == "pending" else "signoff", to=target["name"])
                                    delivery.deliver_now()
                                    note = g.db.execute("SELECT * FROM notifications WHERE decision_id=? AND state='sent' "
                                                        "AND person_name=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (nid, target["name"])).fetchone()
                                if note is None:
                                    actions.append({"event": "simulation_delivery_missing", "node": nid})
                                    continue
                                channel, ts = note["external_ref"].split(":", 1)
                                reply_key = nid + ":" + note["external_ref"]
                                if reply_key in attempted_threads:
                                    continue
                                attempted_threads.add(reply_key)
                                response = delivery.receive(channel, ts, target["slack_id"],
                                    "answer: " + case["owner_answer"] + " because this is the hidden historical decision for the simulated replay",
                                    event_id="eval-reply-" + hashlib.sha256(reply_key.encode()).hexdigest()[:24])
                                actions.append({"event": "simulated_slack_reply", "node": nid, "thread": note["external_ref"],
                                                "by": target["name"], "response": response})
                                current = canvas.node_view(store, nid)
                                if not current.get("authorized") or current.get("blocking"):
                                    actions.append({"event": "reply_did_not_authorize", "node": nid})
                                    continue
                                replied.add(nid)
                                if not followup_added:
                                    canvas.add_followups(store, Config(model_api="none"), nid,
                                        {"by": target["name"], "questions": [case["followup"]]}, actor=Actor.person(target, kind="session"))
                                    followup_added = True
                                    actions.append({"event": "human_followup_added", "parent": nid, "question": case["followup"]})
                    dump(out / "first-routes.json", first_routes)
                    dump(out / "simulation-actions.json", actions)
                    time.sleep(0.5)
                actions.append({"event": "host_exit", "phase": phase, "code": proc.returncode})
                if phase == "initial" and case["recovery"] == "restart" and not interrupted:
                    break
        if task_id:
            dump(out / "final-tree.json", canvas.get_tree(store, task_id))
            dump(out / "task-trace.json", canvas.trace(store, task_id))
            audit.append('task_snapshot', canvas.get_tree(store, task_id))
            audit.append('task_history', canvas.trace(store, task_id))
        dump(out / "first-routes.json", first_routes)
        dump(out / "simulation-actions.json", actions)
        # Include newly created tests/migrations in the review artifact.
        subprocess.run(["git", "-C", str(workspace), "add", "-N", "--", "."], check=True)
        patch = subprocess.check_output(["git", "-C", str(workspace), "diff", "--no-ext-diff"], text=True)
        (out / "host.patch").write_text(patch)
        if (workspace / "EVALUATION_IMPLEMENTATION.md").exists():
            shutil.copyfile(workspace / "EVALUATION_IMPLEMENTATION.md", out / "implementation.md")
        status = subprocess.check_output(["git", "-C", str(workspace), "status", "--short"], text=True)
        (out / "workspace-status.txt").write_text(status)
        summary = {"case": case_id, "task_id": task_id, "host_waited": had_wait, "host_interrupted": interrupted,
                   "host": host, "host_version": subprocess.check_output([host, "--version"], text=True).strip(),
                   "bridge_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=HERE, text=True).strip(),
                   "first_route_count": len(first_routes), "owner_replies": len(replied), "elapsed_seconds": round(time.time() - started, 1),
                   "patch_bytes": len(patch.encode()), "task_count": g.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                   "task_status": g.db.execute("SELECT status FROM runs WHERE id=?", (task_id,)).fetchone()[0] if task_id else "missing",
                   "host_exit_codes": [a["code"] for a in actions if a["event"] == "host_exit"]}
        dump(out / "summary.json", summary)
        audit.append('run_finished', summary)
        from evals.audit.viewer import render
        render(out / 'audit.jsonl', out / 'audit.html')
        print(json.dumps(summary), flush=True)
    finally:
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=20)
        server.shutdown()
        server.server_close()
        g.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        g.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepared", required=True, type=Path)
    parser.add_argument("--case", required=True)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--host", choices=("codex", "claude"), default="codex")
    parser.add_argument("--restart", action="store_true", help="Interrupt the first wait and recover with a fresh host")
    args = parser.parse_args()
    run(args.prepared, args.case, args.timeout, args.host, args.restart)
