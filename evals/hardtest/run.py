"""Condition A: Raven alone, through the MCP surfaces an agent would use
(bridge_search_decisions, then bridge_add_node, which runs the ladder, and
bridge_get_decision for the full record). RAW_TAG names the output files.
The model key, if any, reaches the
MCP subprocess through the environment only; run with BRIDGE_MODEL_API=none
for the deterministic condition. Grade the output by hand against the
sealed key in questions.py."""
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mcp_client import BridgeMCP  # noqa: E402
from questions import QUESTIONS  # noqa: E402

HERE = Path(__file__).parent
TAG = os.environ.get("RAW_TAG", "run")
DB = HERE / f"raw_{TAG}.db"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB) + suffix)
    if p.exists():
        p.unlink()
shutil.copy(HERE / "hardtest.db", DB)
codes = json.loads((HERE / "codes.json").read_text())
mcp = BridgeMCP(str(DB))
run = mcp.call("bridge_start_task", title="Hard test: Raven alone", agent="Evaluator", repo="acme/platform")

results = []
def ask(item):
    t0 = time.time()
    search = mcp.call("bridge_search_decisions", query=item["q"], repo="platform")
    matches = [{"code": codes.get(m["id"], m["id"]), "similarity": m["similarity"], "owner": m["owner_name"],
                "question": m["question"], "answer": m["answer"], "updated_at": m["updated_at"][:10]} for m in search["matches"]]
    node = mcp.call("bridge_add_node", task_id=run["task_id"], question=item["q"],
                    context="Evaluator question. Answer from org records if settled; otherwise route.", paths=item["path"])
    req = node if "error" in node else mcp.call("bridge_get_decision", decision_id=node["node_id"])
    if "error" in req:
        return {"id": item["id"], "question": item["q"], "path": item["path"], "error": req["error"], "search_matches": matches}
    return {"id": item["id"], "question": item["q"], "path": item["path"], "search_matches": matches,
            "status": req.get("status"), "kind": req.get("kind"), "answer": req.get("answer"),
            "evidence": req.get("evidence"), "routed_owner": req.get("owner_name"),
            "routing_reason": req.get("routing_reason"), "owner_evidence": req.get("owner_evidence"),
            "prediction": req.get("prediction"), "prediction_source": codes.get(req.get("source_id")),
            "note": req.get("note"), "answered_by": req.get("answered_by"), "seconds": round(time.time() - t0, 1)}

for item in QUESTIONS:
    results.append(ask(item))
    results.append(ask(item["followup"]))
mcp.close()
(HERE / "results" / f"{TAG}.json").write_text(json.dumps(results, indent=1))
for r in results:
    print(f"\n=== {r['id']} [{r['path']}] {r['question']}")
    if "error" in r:
        print("  ERROR:", r["error"])
        continue
    print(f"  status={r['status']} kind={r['kind']} owner={r['routed_owner']} ({r['seconds']}s)")
    print(f"  answer: {(r['answer'] or '')[:300]}")
    print(f"  evidence: {(r['evidence'] or '')[:200]}")
    if r["prediction"]:
        print(f"  prediction: {r['prediction'][:120]} (from {r['prediction_source']})")
    if r["note"]:
        print(f"  note: {r['note'][:160]}")
    for m in r["search_matches"][:3]:
        print(f"  search {m['similarity']:.2f} {m['code']} {m['updated_at']}: {m['answer'][:90]}")
    if not r["search_matches"]:
        print("  search: NO MATCHES")
