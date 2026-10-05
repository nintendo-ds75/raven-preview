"""The end-to-end evaluation of what the site sells, on a team's own
tasks: the same tasks run under three arms and scored on what happens
to the decisions inside them.

    host-alone     the agent decides everything itself (its defaults)
    routing-only   every decision goes to whoever the map or git names;
                   no memory, no rules, no sign-off gate
    bridge         the ladder, the routing, the rules, the wait, the gate

Each task names its decisions with an answer key: who owns each one,
what the right answer is, and whether the organization had already
settled it (so asking anyone is an interruption). People are simulated
from the key: the owner answers or signs when asked; anyone else says
it is not theirs and hands it on. Scored per task and arm:

    discovered        decisions on the record before the agent acted
    asks              people asked
    interruptions     asks about decisions the key marks settled
    first_contact     asks that reached the key's owner first
    authorized        the agent acted only on authorized answers
    adherence         the agent's action matches the key
    lost              an answer given was not on the tree when read

Run on the example, or on a task file of the pilot's own:

    python3 -m evals.e2e.run --tasks evals/e2e/tasks.example.json --out evals/e2e/results/my-run

Blinding, the two human labelers and the shadow pilot on live work are
the team's to arrange; this runner scores what it can score alone and
records every outcome verbatim.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bridge import canvas  # noqa: E402
from bridge.config import Config  # noqa: E402
from bridge.mcp import dispatch  # noqa: E402
from bridge.routing import coordinator_route, route  # noqa: E402
from bridge.store import Invalid, Store  # noqa: E402

ARMS = ("host-alone", "routing-only", "bridge")
CFG = Config(model_api="none")


def load_tasks(path: str | Path) -> dict:
    spec = json.loads(Path(path).read_text())
    if "org" not in spec or "tasks" not in spec:
        raise ValueError("a task file has an org and a list of tasks")
    return spec


class Agent:
    """The host agent's side of the protocol, over MCP JSON-RPC."""

    def __init__(self, store: Store):
        self.store = store
        self.n = 0

    def call(self, name: str, **args):
        self.n += 1
        response = dispatch(self.store, {"jsonrpc": "2.0", "id": self.n, "method": "tools/call",
                                         "params": {"name": name, "arguments": args}})
        result = response["result"]
        text = result["content"][0]["text"]
        if result["isError"]:
            raise Invalid(text)
        return json.loads(text)


def setup_store(spec: dict, path: Path) -> Store:
    """The organization the key describes: people, the authority map,
    the coordinator, and the decisions already signed (rules included)."""
    org = spec["org"]
    repo = org.get("repo", "acme/platform")
    store = Store(path)
    graph = store.graph
    ids: dict[str, str] = {}
    with graph.transaction():
        for p in org.get("people", []):
            ids[p["name"]] = graph.add_person(p["name"], email=p.get("email", ""), github_login=p.get("github_login", ""),
                                              slack_id=p.get("slack_id", ""))
        for a in org.get("authority", []):
            graph.add_authority(a.get("scope_kind", "path"), a.get("scope", ""), a.get("role", "decides"),
                                person_id=ids[a["person"]], repo=repo, source="config", asserted_by="eval", accepted=True)
    if org.get("coordinator"):
        store.update_settings({"coordinator": ids[org["coordinator"]]})
    if org.get("settings"):
        store.update_settings(dict(org["settings"]))
    for i, prior in enumerate(org.get("prior", [])):
        task = canvas.start_task(store, CFG, {"title": f"prior decision {i}", "repo": repo, "paths": prior.get("paths", ""),
                                              "client_key": f"prior-{i}"})["task_id"]
        node = canvas.add_node(store, CFG, {"task_id": task, "question": prior["question"], "context": prior.get("context", ""),
                                            "paths": prior.get("paths", ""), "client_ref": f"prior-{i}"})
        if not node["owner"]:
            owner_id = graph.owner_id_for(prior["by"]) or store.add_owner({"name": prior["by"], "team": "", "patterns": "zz/*"})["id"]
            store.assign(node["node_id"], {"owner_id": owner_id})
        row = store.get_decision(node["node_id"])
        store.answer(node["node_id"], {"answer": prior["answer"], "rationale": prior.get("rationale", "prior"),
                                       "signed_by": prior["by"], "expected_updated_at": row["updated_at"]})
        if prior.get("rule") is not None:
            row = store.get_decision(node["node_id"])
            rule = prior["rule"] if isinstance(prior["rule"], dict) else {}
            store.make_rule(node["node_id"], {"by": prior["by"], "expected_updated_at": row["updated_at"],
                                              "conditions": rule.get("conditions", ""), "expires": rule.get("expires", "")})
        canvas.finish_task(store, {"task_id": task})
    return store


def _person_answers(store: Store, node: dict, key: dict, tally: dict) -> None:
    """The simulated people: whoever Raven asked either owns it (per the
    key) and answers or signs, or hands it on to the key's owner, who
    then does."""
    graph = store.graph
    asked = node["owner"]
    tally["asks"] += 1
    if key.get("settled"):
        tally["interruptions"] += 1
    if asked == key["owner"]:
        tally["first_contact"] += 1
    else:
        owner_person = graph.find_person(key["owner"])
        if asked and owner_person is not None:
            store.refer(node["node_id"], {"person": owner_person["id"], "by": asked})
        elif owner_person is not None:
            owner_id = graph.owner_id_for(key["owner"]) or store.add_owner({"name": key["owner"], "team": "", "patterns": "zz/*"})["id"]
            store.assign(node["node_id"], {"owner_id": owner_id})
    current = store.get_decision(node["node_id"])
    if node["status"] == "pending" or node["status"] == "unrouted":
        store.answer(node["node_id"], {"answer": key["answer"], "rationale": "per the key", "signed_by": key["owner"],
                                       "expected_updated_at": current["updated_at"]})
    else:
        canvas.sign_off(store, node["node_id"], {"by": key["owner"], "expected_updated_at": current["updated_at"]})


def run_bridge(store: Store, spec: dict, task: dict) -> dict:
    repo = spec["org"].get("repo", "acme/platform")
    agent = Agent(store)
    tally = {"discovered": 0, "asks": 0, "interruptions": 0, "first_contact": 0, "authorized": True,
             "adherence": False, "lost": False, "notes": []}
    kick = agent.call("bridge_start_task", title=task["title"], repo=repo, requester=task.get("requester", ""),
                      paths=task.get("paths", ""), client_key=f"eval-{task['id']}")
    tid = kick["task_id"]
    tally["verdict"] = kick["verdict"]
    nodes = []
    for d in task["decisions"]:
        node = agent.call("bridge_add_node", task_id=tid, question=d["question"], context=d.get("context", ""),
                          paths=d.get("paths", ""), client_ref=d["ref"])
        nodes.append((d, node))
        tally["discovered"] += 1
    seen = agent.call("bridge_get_tree", task_id=tid)["observed_at"]
    for d, node in nodes:
        if node["authorized"]:
            tally["notes"].append(f"{d['ref']}: authorized without asking ({node['evidence'][:80]})")
            continue
        if node["status"] == "duplicate":
            continue
        _person_answers(store, node, d["key"], tally)
    waited = agent.call("bridge_wait", task_id=tid, timeout="1", since=seen)
    tree = agent.call("bridge_get_tree", task_id=tid)
    by_ref = {n["client_ref"]: n for n in canvas._flatten(tree["nodes"])}
    answers = []
    for d, _node in nodes:
        n = by_ref.get(d["ref"]) or {}
        if not n.get("authorized"):
            tally["authorized"] = False
        if not (n.get("answer") or "").strip():
            tally["lost"] = True
        answers.append(n.get("answer") or "")
    if tally["asks"] and not waited["changed"] and not waited.get("notes"):
        tally["lost"] = True
    try:
        agent.call("bridge_finish_task", task_id=tid)
    except Invalid as error:
        tally["authorized"] = False
        tally["notes"].append(f"finish refused: {str(error)[:120]}")
    action = " | ".join(answers)
    tally["action"] = action
    tally["adherence"] = action == task["action"]["expected"]
    return tally


def run_routing_only(store: Store, spec: dict, task: dict) -> dict:
    repo = spec["org"].get("repo", "acme/platform")
    graph = store.graph
    tally = {"discovered": 0, "asks": 0, "interruptions": 0, "first_contact": 0, "authorized": False,
             "adherence": False, "lost": False, "notes": []}
    answers = []
    for d in task["decisions"]:
        paths = [p for p in (d.get("paths") or "").split(",") if p.strip()]
        found = route(graph, graph.resolve_repo(repo), d["question"], path=paths[0] if paths else "",
                      context=d.get("context", ""), requester=task.get("requester", ""))
        if found is None:
            found = coordinator_route(graph, graph.resolve_repo(repo))
        asked = found[0] if found else ""
        tally["asks"] += 1
        tally["discovered"] += 1
        if d["key"].get("settled"):
            tally["interruptions"] += 1
        if asked == d["key"]["owner"]:
            tally["first_contact"] += 1
        answers.append(d["key"]["answer"])
    action = " | ".join(answers)
    tally["action"] = action
    tally["adherence"] = action == task["action"]["expected"]
    return tally


def run_host_alone(store: Store, spec: dict, task: dict) -> dict:
    answers = [d.get("default", "") for d in task["decisions"]]
    action = " | ".join(answers)
    return {"discovered": 0, "asks": 0, "interruptions": 0, "first_contact": 0, "authorized": False,
            "adherence": action == task["action"]["expected"], "lost": False, "action": action, "notes": []}


RUNNERS = {"host-alone": run_host_alone, "routing-only": run_routing_only, "bridge": run_bridge}


def run_suite(spec: dict, arms: tuple = ARMS, db_dir: str | Path | None = None) -> dict:
    results = {"generated": datetime.now().isoformat(timespec="seconds"), "arms": list(arms), "tasks": [], "summary": {}}
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(db_dir) if db_dir else Path(tmp)
        for arm in arms:
            store = setup_store(spec, base / f"{arm}.db")
            for task in spec["tasks"]:
                tally = RUNNERS[arm](store, spec, task)
                results["tasks"].append({"task": task["id"], "arm": arm, **tally})
            store.graph.close()
    for arm in arms:
        rows = [r for r in results["tasks"] if r["arm"] == arm]
        n = len(rows) or 1
        asks = sum(r["asks"] for r in rows)
        results["summary"][arm] = {
            "tasks": len(rows),
            "discovered": sum(r["discovered"] for r in rows),
            "asks": asks,
            "interruptions": sum(r["interruptions"] for r in rows),
            "first_contact_rate": round(sum(r["first_contact"] for r in rows) / asks, 3) if asks else None,
            "authorized_rate": round(sum(1 for r in rows if r["authorized"]) / n, 3),
            "adherence_rate": round(sum(1 for r in rows if r["adherence"]) / n, 3),
            "lost_rate": round(sum(1 for r in rows if r["lost"]) / n, 3)}
    return results


def write_report(results: dict, out: Path) -> str:
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(results, indent=1))
    lines = ["# End-to-end evaluation", "", f"Generated {results['generated']}", "",
             "| arm | tasks | decisions on record | asks | interruptions | first contact | authorized | adherence | lost |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for arm, s in results["summary"].items():
        fc = "n/a" if s["first_contact_rate"] is None else f"{s['first_contact_rate']:.0%}"
        lines.append(f"| {arm} | {s['tasks']} | {s['discovered']} | {s['asks']} | {s['interruptions']} | {fc} | "
                     f"{s['authorized_rate']:.0%} | {s['adherence_rate']:.0%} | {s['lost_rate']:.0%} |")
    lines += ["", "## Per task", ""]
    for r in results["tasks"]:
        lines.append(f"- {r['task']} / {r['arm']}: asks {r['asks']}, interruptions {r['interruptions']}, first contact "
                     f"{r['first_contact']}, authorized {r['authorized']}, adherence {r['adherence']}, lost {r['lost']}"
                     + (f"; {'; '.join(r['notes'])}" if r.get("notes") else ""))
    text = "\n".join(lines) + "\n"
    (out / "summary.md").write_text(text)
    return text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default=str(Path(__file__).resolve().parent / "tasks.example.json"))
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    spec = load_tasks(args.tasks)
    arms = tuple(a.strip() for a in args.arms.split(",") if a.strip() in ARMS)
    results = run_suite(spec, arms)
    out = Path(args.out) if args.out else Path(__file__).resolve().parent / "results" / datetime.now().strftime("%Y%m%d-%H%M%S")
    print(write_report(results, out))
    print(f"written {out}")


if __name__ == "__main__":
    main()
