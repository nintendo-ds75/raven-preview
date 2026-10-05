"""Concurrent, authenticated HTTP/MCP probes using the real Grafana fixture.

The pause controls request timing, not routing or product results. No live
model is required. Results distinguish a contract failure from a crash.
"""
import argparse
import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

from bridge import canvas
from bridge import ladder
from bridge.auth import Auth
from bridge.server import make_server
from bridge.store import Store


def probe(prepared, out):
    os.environ.update(BRIDGE_MODEL_API="none", BRIDGE_SEMANTIC="0", BRIDGE_LIVE="0")
    out.mkdir(parents=True, exist_ok=True)
    db = out / "contract.db"
    if db.exists():
        raise RuntimeError("Choose a fresh output directory")
    with sqlite3.connect(prepared / "baseline.db") as source, sqlite3.connect(db) as target:
        source.backup(target)
    store = Store(db)
    pid = store.graph.add_person("Contract Probe Host", source="eval")
    auth = Auth(store, enabled=True)
    token = auth.create_token(pid, "probe agent")["token"]
    server = make_server(store, port=0, auth=auth)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(name, **args):
        req = Request(f"http://127.0.0.1:{server.server_port}/mcp",
                      json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": name, "arguments": args}}).encode(),
                      headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        with urlopen(req, timeout=30) as response:
            result = json.load(response)["result"]
        text = result["content"][0]["text"]
        return {"result": text if result.get("isError") else json.loads(text),
                "isError": bool(result.get("isError"))}

    results = {}
    try:
        task = call("bridge_start_task", title="Keep search pagination compatible after authorization filtering",
                    repo="grafana/grafana", paths="pkg/storage/unified/resource/list_with_selectors.go",
                    client_key="concurrent-node-retry")["result"]
        args = {"task_id": task["task_id"], "client_ref": "pagination-cursor",
                "question": "Should the continuation cursor use the last scanned row after authorization filtering?",
                "paths": "pkg/storage/unified/resource/list_with_selectors.go",
                "context": "The caller retries the same request while the original is still being routed."}
        entered, release = threading.Event(), threading.Event()
        real_place = canvas._place_node
        lock = threading.Lock()
        calls = 0

        def pause_first(*a, **kw):
            nonlocal calls
            with lock:
                calls += 1
                first = calls == 1
            if first:
                entered.set()
                if not release.wait(20):
                    raise RuntimeError("probe failed to release first request")
            return real_place(*a, **kw)

        with patch.object(canvas, "_place_node", pause_first), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(call, "bridge_add_node", **args)
            if not entered.wait(15):
                raise RuntimeError("first call never reached placement")
            try:
                during = call("bridge_get_tree", task_id=task["task_id"])["result"]
                finish = call("bridge_finish_task", task_id=task["task_id"])
                second = pool.submit(call, "bridge_add_node", **args)
                try:
                    second_result = second.result(timeout=1)
                except FutureTimeout:
                    # Correct retries wait for the first writer: let that
                    # writer finish before expecting the retry's result.
                    release.set()
                    second_result = second.result(timeout=15)
            finally:
                release.set()
            first_result = first.result(timeout=15)
        after = call("bridge_get_tree", task_id=task["task_id"])["result"]
        ids = [r.get("result", {}).get("node_id") for r in (first_result, second_result)]
        results["draft_hidden"] = {"passed": not during["nodes"], "visible_nodes": during["nodes"]}
        results["draft_blocks_finish"] = {"passed": bool(finish.get("isError")), "response": finish}
        results["overlapping_same_client_ref"] = {
            "passed": len(set(ids)) == 1 and bool(ids[0]), "returned_node_ids": ids,
            "visible_node_count": len(canvas._flatten(after["nodes"])),
            "nodes": after["nodes"], "responses": [first_result, second_result]}

        # Admission happens before the first draft exists. A simultaneous
        # finish must not leave a completed run with a newly pending node.
        task2 = call("bridge_start_task", title="Handle Git Sync authentication failures safely",
                     repo="grafana/grafana", paths="pkg/registry/apis/provisioning/controller",
                     client_key="finish-versus-node-admission")["result"]
        entered2, release2 = threading.Event(), threading.Event()
        real_ask = ladder.ask

        def pause_before_insert(*a, **kw):
            entered2.set()
            if not release2.wait(20):
                raise RuntimeError("probe failed to release admission")
            return real_ask(*a, **kw)

        with patch.object(ladder, "ask", pause_before_insert), ThreadPoolExecutor(max_workers=1) as pool:
            adding = pool.submit(call, "bridge_add_node", task_id=task2["task_id"], client_ref="retry-policy",
                                 question="Should permanent installation credential errors stop automatic retries?",
                                 paths="pkg/registry/apis/provisioning/controller/repository.go")
            if not entered2.wait(15):
                raise RuntimeError("request never reached admission")
            try:
                concurrent_finish = call("bridge_finish_task", task_id=task2["task_id"])
            finally:
                release2.set()
            added = adding.result(timeout=15)
        final2 = call("bridge_get_tree", task_id=task2["task_id"])["result"]
        status = store.graph.db.execute("SELECT status FROM runs WHERE id=?", (task2["task_id"],)).fetchone()[0]
        pending = [n for n in canvas._flatten(final2["nodes"]) if n["blocking"]]
        results["inflight_add_versus_finish"] = {
            "passed": not (status == "completed" and pending), "run_status": status,
            "blocking_nodes": len(pending), "finish_response": concurrent_finish,
            "add_response": added, "final_tree": final2}
        (out / "results.json").write_text(json.dumps(results, indent=2))
        print(json.dumps({k: v["passed"] for k, v in results.items()}))
    finally:
        server.shutdown()
        server.server_close()
        store.graph.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    probe(args.prepared, args.out)
