"""Check a signed decision's reuse via a fresh authenticated MCP client.

Copy the completed run first: this follow-on measurement never changes the
host's canvas, task count, or finish result.
"""
import argparse
import json
import os
import sqlite3
import threading
from pathlib import Path
from urllib.request import Request, urlopen

from bridge.auth import Auth
from bridge.server import make_server
from bridge.store import Store


def run(source, out):
    os.environ.update(BRIDGE_MODEL_API="none", BRIDGE_SEMANTIC="0", BRIDGE_LIVE="0")
    out.mkdir(parents=True, exist_ok=True)
    if (out / "reuse.db").exists():
        raise RuntimeError("Choose a fresh output directory")
    with sqlite3.connect(source / "bridge.db") as src, sqlite3.connect(out / "reuse.db") as dst:
        src.backup(dst)
    store = Store(out / "reuse.db")
    pid = store.graph.add_person("Fresh Replay Host", source="eval")
    auth = Auth(store, enabled=True)
    token = auth.create_token(pid, "fresh host")["token"]
    server = make_server(store, port=0, auth=auth)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    calls = []

    def call(name, **args):
        req = Request(f"http://127.0.0.1:{server.server_port}/mcp",
                      json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": name, "arguments": args}}).encode(),
                      headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        with urlopen(req, timeout=30) as response:
            payload = json.load(response)["result"]
        text = payload["content"][0]["text"]
        result = {"result": text if payload.get("isError") else json.loads(text),
                  "isError": bool(payload.get("isError"))}
        calls.append({"name": name, "arguments": args, "response": result})
        return result

    try:
        routes = json.loads((source / "first-routes.json").read_text())
        nid = next(n for n, r in routes.items() if r["before_any_owner_reply"])
        original = store.get_decision(nid)
        task = call("bridge_start_task", repo="grafana/grafana", client_key="fresh-host-memory-reuse",
                    title="Revisit restricted-user LIST continuation policy",
                    goal="Prepare another server-side LIST change while preserving the previously agreed pagination contract.",
                    paths=original["path"])["result"]
        node = call("bridge_add_node", task_id=task["task_id"], client_ref="reuse-first-decision",
                    question=routes[nid]["question"], context=routes[nid]["context"], paths=original["path"])["result"]
        finish = call("bridge_finish_task", task_id=task["task_id"])
        result = {"source_node": nid, "source_owner": original["owner_name"],
                  "source_answer": original["answer"], "reused_node": node,
                  "finish_refused": bool(finish.get("isError")),
                  "same_answer": node.get("answer") == original["answer"],
                  "source_cited": nid in (node.get("evidence") or ""),
                  "same_owner": node.get("owner") == original["owner_name"], "calls": calls}
        (out / "results.json").write_text(json.dumps(result, indent=2))
        print(json.dumps({k: result[k] for k in ("source_cited", "same_owner", "same_answer", "finish_refused")}))
    finally:
        server.shutdown()
        server.server_close()
        store.graph.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    run(args.source, args.out)
