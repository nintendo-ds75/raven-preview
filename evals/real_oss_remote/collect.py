"""Export reviewable artifacts without databases, credentials or CLI metadata."""
import argparse
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from .run_host import read_trace, trace_state


def bounded(value):
    if isinstance(value, dict):
        return {k: bounded(v) for k, v in value.items() if k != "_meta"}
    if isinstance(value, list):
        return [bounded(v) for v in value]
    if isinstance(value, str) and len(value) > 1600:
        return {"excerpt": value[:600], "characters": len(value),
                "sha256": hashlib.sha256(value.encode()).hexdigest(), "truncated": True}
    return value


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def collect(prepared, case, out):
    source = prepared / "runs" / case
    summary = json.loads((source / "summary.json").read_text())
    out.mkdir(parents=True, exist_ok=True)
    for name in ("summary.json", "first-routes.json", "first-tree-before-replies.json", "final-tree.json",
                 "simulation-actions.json", "tree-at-host-interruption.json", "task-trace.json"):
        if (source / name).exists():
            dump(out / name, bounded(json.loads((source / name).read_text())))
    for name in ("host.patch", "implementation.md", "workspace-status.txt", "host-prompt-initial.txt",
                 "host-prompt-resume.txt", "host-final-initial.md", "host-final-resume.md"):
        if (source / name).exists():
            shutil.copyfile(source / name, out / name)
    dump(out / "manifest.json", json.loads((prepared / "manifest.json").read_text()))
    complete, _ = trace_state(read_trace(source / "mcp-trace.jsonl"))
    calls = [{"requested_at": req["time"], "returned_at": resp["time"],
              "name": req["message"]["params"]["name"],
              "arguments": bounded(req["message"]["params"].get("arguments", {})),
              "result": bounded(value)} for req, resp, value in complete]
    # Include errors and the interrupted wait separately, rather than letting
    # successful-response scoring make them disappear from the audit trail.
    raw = read_trace(source / "mcp-trace.jsonl")
    errors = []
    for rec in raw:
        msg = rec["message"]
        if msg.get("error") or msg.get("result", {}).get("isError"):
            errors.append(bounded(rec))
    dump(out / "mcp-calls.json", {"completed": calls, "errors": errors,
                                  "raw_trace_sha256": hashlib.sha256((source / "mcp-trace.jsonl").read_bytes()).hexdigest()})
    messages = [{k: r[k] for k in ("time", "channel", "ts", "thread_ts", "text")}
                for r in read_trace(source / "slack-outbox.jsonl")]
    dump(out / "slack-messages.json", bounded(messages))
    events = []
    for phase in ("initial", "resume"):
        for rec in read_trace(source / f"host-events-{phase}.jsonl"):
            item = rec.get("item", {})
            if rec.get("type") == "item.completed" and item.get("type") in ("agent_message", "command_execution"):
                events.append({"phase": phase, **bounded(item)})
            elif rec.get("type") in ("error", "turn.failed", "turn.completed"):
                events.append({"phase": phase, **bounded(rec)})
    dump(out / "host-events.json", events)
    routes = json.loads((source / "first-routes.json").read_text())
    before = [r for r in routes.values() if r["before_any_owner_reply"]]
    actions = json.loads((source / "simulation-actions.json").read_text())
    with sqlite3.connect(source / "bridge.db") as db:
        rows = db.execute("SELECT status,signoff,parent_id,origin FROM decisions WHERE run_id=?", (summary["task_id"],)).fetchall()
    metrics = {"initial_discovered_nodes": len(before), "initial_owners": [r["owner"] for r in before],
               "referrals": sum(a["event"] == "simulated_referral" for a in actions),
               "reply_attempts": sum(a["event"] == "simulated_slack_reply" for a in actions),
               "rejected_replies": sum(a["event"] == "reply_did_not_authorize" for a in actions),
               "finish_refused_before_answers": any(a["event"] == "pre_answer_finish_refused" for a in actions),
               "adopted_followups": sum(r[0] == "adopted" for r in rows),
               "parented_active_nodes": sum(bool(r[2]) and r[0] != "adopted" for r in rows),
               "authorized_active_nodes": sum(r[1] == "signed" and r[0] == "approved" for r in rows),
               "tool_errors": len(errors), "outbound_slack_messages": len(messages),
               "outbound_slack_root_messages": sum(not m["thread_ts"] for m in messages),
               "completed_mcp_calls": len(calls)}
    meta = [r["message"].get("params", {}).get("_meta", {}).get("x-codex-turn-metadata", {}) for r in raw]
    metrics["host_models"] = sorted({m["model"] for m in meta if m.get("model")})
    metrics["host_reasoning_efforts"] = sorted({m["reasoning_effort"] for m in meta if m.get("reasoning_effort")})
    dump(out / "metrics.json", metrics)
    print(json.dumps({**summary, **metrics}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    collect(args.prepared, args.case, args.out)
