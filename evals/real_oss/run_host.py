"""Run a real coding agent on a real repository, once through Raven and
once on its own, and record what happened to the decisions inside the
task.

The host is the Claude Code CLI in print mode, given the brief and
nothing else: no decision list, no owner names, no answer key. In the
bridge arm it reaches Raven over MCP stdio, exactly as a pilot's agent
would. It runs twice: once before anyone replies, and then, as a fresh
process with no memory of the first, after the owners have acted. What
the second process knows, it gets from Raven.

The owners are simulated from the key: the person Raven routed to
answers if the key says the decision is theirs, and hands it on to the
person it belongs to if it is not. That is the one thing here that is
not real, and it is the same simplification `evals/e2e` makes.

    python3 -m evals.real_oss.run_host --tasks evals/real_oss/tasks.json \
        --out evals/real_oss/results/<name> [--only smallint-overflow] [--arm bridge]

Needs the `claude` CLI on PATH and authenticated, and the checkout that
`fetch.py` read (it takes a worktree of it per run).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bridge import canvas  # noqa: E402
from bridge.authz import Actor, Refused  # noqa: E402
from bridge.store import Invalid, Store  # noqa: E402

# What a pilot puts in its house rules, and all the host is told about
# Raven. It does not name a decision, an owner or an answer.
HOUSE_RULE = (
    "This team runs Raven. Before you start work on a task, call bridge_start_task with the task as "
    "given. When you hit something that is a judgment call rather than a fact you can look up -- a "
    "default, a policy, what to keep for existing users, what to break -- write it to the canvas with "
    "bridge_add_node instead of deciding it yourself, and carry on with the parts that do not depend on "
    "it. Read bridge_get_tree before any irreversible step. Call bridge_finish_task when you are done; "
    "it is refused while a decision still waits on a person."
)

TOOLS = "Read,Grep,Glob,Edit,Write,TodoWrite"
PHASE1_TIMEOUT = 900
PHASE2_TIMEOUT = 900


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def git(repo: Path, *args: str, check=True) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=check).stdout


class Host:
    """One run of the real CLI, recorded."""

    def __init__(self, cwd: Path, prompt: str, mcp: Path | None, max_turns: int, timeout: int,
                 log: Path, model: str = ""):
        self.cwd, self.prompt, self.mcp = cwd, prompt, mcp
        self.max_turns, self.timeout, self.log, self.model = max_turns, timeout, log, model

    def run(self) -> dict:
        cmd = ["claude", "-p", self.prompt, "--output-format", "stream-json", "--verbose",
               "--tools", TOOLS, "--setting-sources", "", "--permission-mode", "acceptEdits",
               "--permission-prompts", "none", "--max-turns", str(self.max_turns),
               "--strict-mcp-config"]
        if self.mcp:
            # A pilot pre-approves its own Raven server; without this the
            # host's calls are denied for want of anyone to ask.
            cmd += ["--mcp-config", str(self.mcp), "--allowedTools", "mcp__bridge",
                    "--append-system-prompt", HOUSE_RULE]
        if self.model:
            cmd += ["--model", self.model]
        started = time.monotonic()
        try:
            proc = subprocess.run(cmd, cwd=str(self.cwd), capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=self.timeout)
            out, err, code, timed_out = proc.stdout, proc.stderr, proc.returncode, False
        except subprocess.TimeoutExpired as expired:
            out = expired.stdout.decode() if isinstance(expired.stdout, bytes) else (expired.stdout or "")
            err = expired.stderr.decode() if isinstance(expired.stderr, bytes) else (expired.stderr or "")
            code, timed_out = -1, True
        elapsed = time.monotonic() - started
        self.log.write_text(out)
        return {**self._read(out), "exit": code, "timed_out": timed_out, "seconds": round(elapsed, 1),
                "stderr": err[-2000:], "transcript": str(self.log)}

    @staticmethod
    def _read(out: str) -> dict:
        calls, text, cost, turns = [], "", None, 0
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "assistant":
                turns += 1
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        # Trimmed: the record has to stay readable in the
                        # results file, and the full stream is on disk.
                        shown = {k: (v[:300] if isinstance(v, str) else v)
                                 for k, v in (block.get("input") or {}).items()}
                        calls.append({"name": block.get("name", ""), "input": shown})
            elif event.get("type") == "result":
                text = event.get("result", "") or text
                cost = event.get("total_cost_usd", cost)
        return {"tool_calls": calls, "answer": text, "cost_usd": cost, "turns": turns,
                "bridge_calls": [c["name"] for c in calls if c["name"].startswith("mcp__bridge__")]}


def worktree(checkout: Path, commit: str, at: Path) -> Path:
    """The repository as it stood just before the real change landed."""
    subprocess.run(["git", "-C", str(checkout), "worktree", "remove", "--force", str(at)],
                   capture_output=True, text=True)
    shutil.rmtree(at, ignore_errors=True)
    # A worktree whose directory was deleted from under git stays
    # registered and blocks the path; prune clears those.
    git(checkout, "worktree", "prune")
    parent = git(checkout, "rev-parse", f"{commit}^").strip()
    git(checkout, "worktree", "add", "--detach", "-q", str(at), parent)
    return at


def mcp_config(db: Path, at: Path, semantic: bool = False) -> Path:
    """The Raven the host talks to. Deterministic rungs only unless the
    run asks for the model-backed ones, which is what a pilot with a key
    actually has."""
    env = {"BRIDGE_SEMANTIC": "1"} if semantic else {"BRIDGE_MODEL_API": "none", "BRIDGE_SEMANTIC": "0"}
    at.write_text(json.dumps({"mcpServers": {"bridge": {
        "command": sys.executable,
        "args": [str(ROOT / "bridge_mcp.py"), "--db", str(db)],
        "env": env}}}, indent=2))
    return at


def match_decision(node: dict, key: dict) -> dict | None:
    """Which decision in the key this node is asking about. A node that
    matches nothing is recorded as unmatched and answered from the
    fallback, so a person can read it and disagree."""
    text = f"{node.get('question', '')} {node.get('answer', '')} {node.get('path', '')}".lower()
    best, score = None, 0
    for decision in key["decisions"]:
        hits = sum(1 for term in decision["match"] if term.lower() in text)
        if hits > score:
            best, score = decision, hits
    return best


def person_id(graph, name: str) -> str:
    row = graph.find_person(name)
    if row:
        return row["id"]
    with graph.transaction():
        return graph.add_person(name, source="eval")


def owners_act(store, task_id: str, key: dict, log: list) -> None:
    """The people do what the key says, through the same permission check
    a browser or Slack goes through: the person Raven asked answers when
    it is theirs, and hands it on when it is not."""
    graph = store.graph
    owner_id = person_id(graph, key["owner"])
    owner = Actor.person(graph.get_person(owner_id))
    acted: set[str] = set()
    for node in canvas.get_tree(store, task_id)["nodes"]:
        for n in _flatten([node]):
            if not n["blocking"]:
                continue
            view = canvas.node_view(store, n["node_id"])
            # A duplicate is a pointer: the person acts on the decision it
            # points at, and the stub follows. The same decision reached
            # twice is answered once.
            if view["status"] == "duplicate" and view.get("duplicate_of"):
                view = canvas.node_view(store, view["duplicate_of"])
            if view["node_id"] in acted:
                continue
            acted.add(view["node_id"])
            decision = match_decision(view, key)
            answer = (decision or {})["answer"] if decision else key["fallback"]
            rationale = (decision or {}).get("rationale", "") if decision else "what the team settled on"
            nid = view["node_id"]
            entry = {"node_id": nid, "stub_for": n["node_id"] if nid != n["node_id"] else "",
                     "question": view["question"], "status": view["status"],
                     "routed_to": view["owner"], "matched": bool(decision),
                     "matched_question": (decision or {}).get("question", "")}
            try:
                if view["owner"] and view["owner"] != key["owner"]:
                    # Not theirs. The routed person hands it to the person
                    # the key says owns it, which is the recovery a misroute
                    # is supposed to have.
                    routed = Actor.person(graph.get_person(person_id(graph, view["owner"])))
                    store.refer(nid, {"person": owner_id}, actor=routed)
                    entry["handed_on"] = True
                    view = canvas.node_view(store, nid)
                elif not view["owner"]:
                    # Raven said it did not know who owns this. The
                    # operator puts it in front of the right person, which
                    # is what the unrouted queue is for.
                    store.assign(nid, {"owner_id": graph.owner_id_for(key["owner"])})
                    entry["assigned"] = True
                    view = canvas.node_view(store, nid)
                if view["status"] in ("resolved", "partial", "assumed", "proposed", "predicted"):
                    # Raven answered it from the record: the owner signs
                    # what it says, or corrects it.
                    agrees = all(term.lower() in (view["answer"] or "").lower()
                                 for term in key["agree"])
                    if agrees:
                        canvas.sign_off(store, nid, {"by": key["owner"],
                                                     "expected_updated_at": view["updated_at"]}, actor=owner)
                        entry["action"] = "signed"
                    else:
                        canvas.sign_off(store, nid, {"by": key["owner"], "correction": answer,
                                                     "rationale": rationale,
                                                     "expected_updated_at": view["updated_at"]}, actor=owner)
                        entry["action"] = "corrected"
                else:
                    store.answer(nid, {"answer": answer, "rationale": rationale,
                                       "signed_by": key["owner"],
                                       "expected_updated_at": view["updated_at"]}, actor=owner)
                    entry["action"] = "answered"
            except (Invalid, Refused) as error:
                entry["action"] = f"refused: {error}"
            log.append(entry)


def _flatten(nodes):
    out = []
    for n in nodes:
        out.append(n)
        out.extend(_flatten(n.get("children", [])))
    return out


def canvas_snapshot(store, task_id: str) -> dict:
    tree = canvas.get_tree(store, task_id)
    nodes = _flatten(tree["nodes"])
    return {"status": tree["status"], "verdict": tree["verdict"], "counts": tree["counts"],
            "next": tree["next"],
            "nodes": [{k: n.get(k) for k in ("node_id", "question", "status", "owner", "answer",
                                             "signed_by", "signoff", "authorized", "blocking",
                                             "owner_evidence")} for n in nodes]}


def gate(store, task_id: str) -> str:
    """Would the finish be refused right now? A probe, not the finish: if
    nothing blocks, the task really would complete, so put it back to
    working and leave the run as it was."""
    try:
        status = canvas.finish_task(store, {"task_id": task_id})["status"]
    except Invalid as error:
        return f"refused: {error}"
    store.update_run(task_id, {"status": "working"})
    return status


def finish(store, task_id: str, diff: str = "") -> tuple[str, list]:
    """The status, and what Raven made of the diff against what was
    signed. The diff is passed here rather than left to the host: a real
    host supplied one on 1 of 3 tasks and truncated it to 300 characters,
    so leaving it to them measures their diligence and not the report."""
    try:
        done = canvas.finish_task(store, {"task_id": task_id, "diff": diff})
        return done["status"], done.get("follows") or []
    except Invalid as error:
        return f"refused: {error}", []


def adherence(diff: str, checks: dict) -> dict:
    """What the change itself did. A summary saying the right thing is
    not the right thing: these read the diff's own added and removed
    lines."""
    added = [l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:] for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
    touched = [l.split(" b/")[-1] for l in diff.splitlines() if l.startswith("+++ b/")]
    out = {
        "added": {t: any(t in l for l in added) for t in checks.get("added", [])},
        "removed": {t: any(t in l for l in removed) for t in checks.get("removed", [])},
        "not_added": {t: not any(t in l for l in added) for t in checks.get("not_added", [])},
        "files": {f: any(f in t for t in touched) for f in checks.get("files", [])},
    }
    out["followed"] = all(v for group in out.values() if isinstance(group, dict) for v in group.values())
    return out


def score(task: dict, arm: str, run: dict) -> dict:
    key = task["key"]
    out = {"arm": arm}
    checks = adherence(run.get("diff") or "", key["adherence"])
    if arm == "alone":
        out.update(asked_anyone=False, followed=checks["followed"], checks=checks,
                   wrote_code=bool(run.get("diff")))
        return out

    nodes = run["canvas_before"]["nodes"]
    owners = [n["owner"] for n in nodes if n["owner"]]
    after = run["canvas_after"]["nodes"]
    out.update(
        kicked_off="mcp__bridge__bridge_start_task" in run["phase1"]["bridge_calls"],
        kickoff_first=(run["phase1"]["bridge_calls"] or [""])[0] == "mcp__bridge__bridge_start_task",
        registered=len(nodes),
        routed=sum(1 for n in nodes if n["owner"]),
        first_contact=sum(1 for o in owners if o == key["owner"]),
        team_contact=sum(1 for o in owners if o in key["acceptable"]),
        acceptable_size=len(key["acceptable"]),
        settled_alone=sum(1 for n in nodes if n["status"] in ("resolved", "partial", "assumed", "proposed")),
        matched=sum(1 for r in run["replies"] if r["matched"]),
        handed_on=sum(1 for r in run["replies"] if r.get("handed_on")),
        corrected=sum(1 for r in run["replies"] if r.get("action") == "corrected"),
        # With nothing on the canvas there is nothing for the gate to hold.
        gate_held=run["finish_before"].startswith("refused") if nodes else None,
        finished=run["finish_after"] == "completed",
        authorized=all(n["authorized"] or not n["blocking"] for n in after),
        resumed=(run["phase2"]["bridge_calls"] or []) != [] and run["phase2"]["read_same_task"],
        saw_answers=run["phase2"]["saw_answers"],
        followed=checks["followed"],
        checks=checks,
        wrote_code=bool(run.get("diff")),
    )
    follow = run.get("followup")
    if follow is not None:
        nodes = follow.get("nodes") or []
        out.update(
            followup_nodes=len(nodes),
            # The sibling task's questions, and whether the answer the
            # owner gave on the first task reached any of them.
            followup_recalled=sum(1 for n in nodes if n["recalled"]),
            followup_as_evidence=sum(1 for n in nodes if n["recalled"] and n["kind"] == "evidence"),
            followup_authorized_alone=sum(1 for n in nodes if n["authorized"]),
        )
    return out


def run_one(task: dict, arm: str, tasks: dict, out: Path, work_root: Path, model: str,
            semantic: bool = False) -> dict:
    checkout = Path(tasks["checkout"])
    # Checkouts and databases are disposable and large; they live outside
    # the repository. Only the transcripts and the results are kept.
    work = work_root / f"{task['id']}.{arm}"
    work.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    wt = worktree(checkout, task["key"]["commit"], work / "repo")
    run = {"task": task["id"], "arm": arm, "started_at": stamp(), "checkout": str(checkout),
           "base": git(checkout, "rev-parse", f"{task['key']['commit']}^").strip()}

    brief = (f"{task['title']}\n\n{task['brief']}\n\n"
             f"Files to start from: {', '.join(task['paths'])}.\n"
             "Make the change in this checkout. Do not run any commands.")

    if arm == "alone":
        host = Host(wt, brief, None, 40, PHASE1_TIMEOUT, work / "alone.jsonl", model)
        run["phase1"] = host.run()
        run["diff"] = subprocess.run(["git", "-C", str(wt), "diff"], capture_output=True, text=True).stdout
        run["scores"] = score(task, arm, run)
        return run

    db = work / "bridge.db"
    shutil.copy(Path(tasks["db"]), db)
    cfg = mcp_config(db, work / "mcp.json", semantic)
    client_key = f"{task['id']}-eval"

    phase1 = (f"{brief}\n\nUse client_key \"{client_key}\" when you call bridge_start_task, so the task "
              f"can be picked up again if this session ends.")
    run["phase1"] = Host(wt, phase1, cfg, 40, PHASE1_TIMEOUT, work / "phase1.jsonl", model).run()

    store = Store(db)
    task_id = _task_id(store, client_key)
    run["task_id"] = task_id
    if not task_id:
        run["scores"] = {"arm": arm, "kicked_off": False, "note": "the host never started a task"}
        return run
    run["canvas_before"] = canvas_snapshot(store, task_id)
    run["finish_before"] = gate(store, task_id)

    run["replies"] = []
    owners_act(store, task_id, task["key"], run["replies"])
    run["canvas_replied"] = canvas_snapshot(store, task_id)

    phase2 = ("You are picking up a task a previous session started and did not finish. Call "
              f"bridge_start_task with client_key \"{client_key}\" to get the task back, read the canvas, "
              "and finish the work in line with what the people answered. "
              "Do not run any commands.\n\n" + brief)
    result = Host(wt, phase2, cfg, 40, PHASE2_TIMEOUT, work / "phase2.jsonl", model).run()
    result["read_same_task"] = any(
        task_id in json.dumps(c["input"]) for c in result["tool_calls"] if c["name"].startswith("mcp__bridge__"))
    answers = [r for r in run["replies"] if r.get("action") in ("answered", "corrected", "signed")]
    result["saw_answers"] = bool(answers) and "mcp__bridge__bridge_get_tree" in result["bridge_calls"]
    run["phase2"] = result

    run["canvas_after"] = canvas_snapshot(store, task_id)
    run["diff"] = subprocess.run(["git", "-C", str(wt), "diff"], capture_output=True, text=True).stdout
    run["finish_after"], run["follows"] = finish(store, task_id, run["diff"])

    # A sibling task, to a host that never saw the first one. Whether the
    # answer a person gave reaches it is the whole claim about memory.
    if task["key"].get("followup"):
        run["followup"] = _followup(task, store, db, cfg, work, model, run)
    run["scores"] = score(task, arm, run)
    return run


def _followup(task: dict, store, db: Path, cfg: Path, work: Path, model: str, run: dict) -> dict:
    """The second task, on ground the first one settled. The brief names
    no earlier decision; finding it is Raven's job."""
    key = task["key"]
    answered = [r["node_id"] for r in run["replies"] if r.get("action") in ("answered", "corrected", "signed")]
    wt = worktree(Path(run["checkout"]), key["commit"], work / "repo2")
    client_key = f"{task['id']}-followup"
    brief = (f"{key['followup']}\n\nFiles to start from: {', '.join(key['followup_paths'])}.\n"
             "Make the change in this checkout. Do not run any commands.\n\n"
             f"Use client_key \"{client_key}\" when you call bridge_start_task.")
    host = Host(wt, brief, cfg, 40, PHASE1_TIMEOUT, work / "followup.jsonl", model).run()
    task_id = _task_id(store, client_key)
    out = {"client_key": client_key, "task_id": task_id, "host": host,
           "answered_decisions": answered}
    if not task_id:
        out["nodes"] = []
        return out
    snapshot = canvas_snapshot(store, task_id)
    out["canvas"] = snapshot
    nodes = []
    for n in snapshot["nodes"]:
        view = canvas.node_view(store, n["node_id"])
        evidence = (view["evidence"] or "") + " " + (view["owner_evidence"] or "")
        nodes.append({"node_id": n["node_id"], "question": view["question"], "status": view["status"],
                      "kind": view["kind"], "owner": view["owner"], "authorized": view["authorized"],
                      "recalled": any(d in evidence for d in answered),
                      "answer": (view["answer"] or "")[:300], "evidence": evidence.strip()[:400]})
    out["nodes"] = nodes
    out["finish"] = gate(store, task_id)
    return out


_TASK_SQL = "SELECT id FROM runs WHERE client_key=? ORDER BY updated_at DESC LIMIT 1"


def _task_id(store, client_key: str) -> str:
    with store.connect() as db:
        row = db.execute(_TASK_SQL, (client_key,)).fetchone()
    return row["id"] if row else ""


def rescore(results: Path, tasks: dict) -> dict:
    """Grade recorded runs again with the current checks, without running
    the agents. The canvases, the replies and the diffs are already in
    the file; only the grader changes, and the file says it was
    regraded."""
    data = json.loads(results.read_text())
    by_id = {t["id"]: t for t in tasks["tasks"]}
    for run in data["runs"]:
        task = by_id.get(run["task"])
        if task is None:
            continue
        run["scores"] = score(task, run["arm"], run)
    data["rescored_at"] = stamp()
    results.write_text(json.dumps(data, indent=2) + "\n")
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tasks", default=str(Path(__file__).resolve().parent / "tasks.json"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="", help="one task id")
    ap.add_argument("--arm", default="bridge,alone")
    ap.add_argument("--model", default="", help="passed to the host CLI")
    ap.add_argument("--work", default="", help="where checkouts and databases go (default: a temp dir)")
    ap.add_argument("--semantic", action="store_true",
                    help="let the Raven the host talks to run its model-backed rungs")
    ap.add_argument("--rescore", action="store_true",
                    help="regrade the results already in --out; runs no agents")
    args = ap.parse_args(argv)

    tasks = json.loads(Path(args.tasks).read_text())
    tasks.setdefault("db", str(Path(args.tasks).resolve().parent / "bridge.db"))
    out = Path(args.out)
    if args.rescore:
        data = rescore(out / "results.json", tasks)
        for run in data["runs"]:
            shown = {k: v for k, v in run["scores"].items() if k != "checks"}
            print(f"{run['task']:18s} {run['arm']:7s} {json.dumps(shown)}")
        print(f"\nregraded {out / 'results.json'}")
        return 0
    out.mkdir(parents=True, exist_ok=True)
    work_root = Path(args.work or tempfile.mkdtemp(prefix="real-oss-"))
    work_root.mkdir(parents=True, exist_ok=True)
    print(f"checkouts and databases under {work_root}")

    chosen = [t for t in tasks["tasks"] if not args.only or t["id"] == args.only]
    arms = [a for a in args.arm.split(",") if a]
    runs = []
    for task in chosen:
        for arm in arms:
            print(f"--- {task['id']} / {arm}", flush=True)
            run = run_one(task, arm, tasks, out, work_root, args.model, args.semantic)
            runs.append(run)
            print("    " + json.dumps(run["scores"]), flush=True)
            (out / "results.json").write_text(json.dumps(
                {"repo": tasks["repo"], "cutoff": tasks["cutoff"], "cutoff_commit": tasks["cutoff_commit"],
                 "head": tasks["head"], "ran_at": stamp(), "host": _host_version(),
                 "semantic": bool(args.semantic), "runs": runs},
                indent=2) + "\n")
    print(f"\nwrote {out / 'results.json'}")
    return 0


def _host_version() -> str:
    try:
        return subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip()
    except OSError:
        return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
