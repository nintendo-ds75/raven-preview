"""What Bridge answers by itself, on a real repository, with no agent in
the loop: who to ask, what the codebase already settles, and whether an
answer a person gave is reused the next time the same ground comes up.

Two panels.

**Routing.** For a sample of real directories, ask who decides. A good
answer names a person, says which signal named them, and that person is
someone the repository's own CODEOWNERS team map or its pre-cutoff
history puts there. That is a sanity check on who-to-ask, not an
accuracy score; `bench/routing` is the accuracy measurement.

**Memory.** After a person answers a question, four more go in: the same
question on a new task, a paraphrase of it, the same words about another
area, and something unrelated. The first two should come back resolved
from that answer with the citation, and still want a signature, because
a signed answer is evidence for a new question and not authorization for
it. The third should come back a prediction that says out loud it was
given for another scope. The fourth should reach nobody's memory at all.

    python3 -m evals.real_oss.probes --tasks evals/real_oss/tasks.json \
        --out evals/real_oss/results/<name>/probes.json
"""

import argparse
import json
import shutil
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bridge import canvas  # noqa: E402
from bridge.authz import Actor  # noqa: E402
from bridge.config import Config  # noqa: E402
from bridge.store import Store  # noqa: E402

# Deterministic rungs only by default. --semantic runs the same panel
# with the model rungs on, which is the configuration a pilot has.
CFG = Config(model_api="none")
REQUESTER = "Eval Harness <eval@example.invalid>"


def use_semantic() -> None:
    global CFG
    import os
    os.environ["BRIDGE_SEMANTIC"] = "1"
    os.environ.pop("BRIDGE_MODEL_API", None)
    CFG = Config()


def ask(store, title: str, question: str, paths: str, repo: str) -> dict:
    """One question on a task of its own, the way an agent asks it."""
    key = uuid.uuid4().hex[:12]
    task = canvas.start_task(store, CFG, {"title": title, "repo": repo, "agent": "probe",
                                          "client_key": key, "paths": paths, "requester": REQUESTER})
    node = canvas.add_node(store, CFG, {"task_id": task["task_id"], "client_ref": "q",
                                        "question": question, "paths": paths})
    return {"task_id": task["task_id"], "verdict": task["verdict"], **node}


def routing_panel(store, repo: str, teams: dict, prefixes: dict) -> list[dict]:
    """Who decides about each area CODEOWNERS names, and does the answer
    hold up against the team the repository itself lists there."""
    out = []
    for team, paths in prefixes.items():
        members = set(teams.get(team) or [])
        for prefix in paths:
            node = ask(store, "Who owns this area", f"Who decides about {prefix}?",
                       prefix + "/", repo)
            owner = node["owner"] or node.get("answered_by") or ""
            answer = node["answer"] or ""
            # The accountability rung answers in the node itself; a routed
            # node names the person it routed to instead.
            named = owner or (answer.split(" is the accountable owner")[0] if "accountable owner" in answer else "")
            out.append({
                "prefix": prefix, "team": team, "team_size": len(members),
                "status": node["status"], "kind": node["kind"],
                "named": named,
                "on_the_team": named in members,
                "cites_codeowners": "codeowners" in (answer + node["owner_evidence"]).lower(),
                "cites_history": any(w in (answer + node["owner_evidence"]).lower()
                                     for w in ("changes under", "blame", "reviewed or accepted")),
                "evidence": (answer or node["owner_evidence"])[:400],
            })
    return out


def memory_panel(store, repo: str, answered: dict, probes: dict) -> list[dict]:
    """The same ground asked again, once a person has answered it."""
    out = []
    plan = [("exact_repeat", answered["question"], answered["paths"], "reuse"),
            ("paraphrase", probes["paraphrase"]["question"], probes["paraphrase"]["paths"], "reuse"),
            ("other_scope", probes["other_scope"]["question"], probes["other_scope"]["paths"], "other_scope"),
            ("unrelated", probes["unrelated"]["question"], probes["unrelated"]["paths"], "none"),
            ("accountability", probes["accountability"]["question"], probes["accountability"]["paths"], "owner")]
    # The same decision asked in words that share almost nothing with the
    # original. A text score cannot reach this and is not supposed to:
    # it is here to measure what reading the near misses buys, and what
    # it costs on `distant_other`, which is just as far away lexically
    # and is a different decision.
    if probes.get("distant_paraphrase"):
        plan.insert(3, ("distant_paraphrase", probes["distant_paraphrase"]["question"],
                        probes["distant_paraphrase"]["paths"], "reuse"))
    if probes.get("distant_other"):
        plan.insert(4, ("distant_other", probes["distant_other"]["question"],
                        probes["distant_other"]["paths"], "not_this_one"))
    for label, question, paths, expect in plan:
        node = ask(store, "Follow-up work", question, paths, repo)
        evidence = (node["evidence"] or "") + " " + (node["owner_evidence"] or "")
        cited = answered["decision_id"] in evidence
        row = {"probe": label, "expect": expect, "question": question,
               "status": node["status"], "kind": node["kind"],
               "authorized": node["authorized"], "blocking": node["blocking"],
               "recalled": cited, "as_evidence": node["kind"] == "evidence" and cited,
               "answer": (node["answer"] or "")[:200], "evidence": evidence.strip()[:400]}
        if expect == "reuse":
            # The earlier answer reached the new question, with its
            # citation, and still wants a person: memory resolves a
            # question, it never authorizes one. A direct hit comes back
            # as evidence and a near one as a prediction to confirm;
            # as_evidence keeps the two apart.
            row["ok"] = cited and not node["authorized"] and node["blocking"]
        elif expect == "other_scope":
            # Offered, but as a prediction that says which scope it came
            # from, never as evidence for this one.
            row["ok"] = (node["kind"] != "evidence"
                         and (not cited or "another scope" in evidence or "other scope" in evidence))
        elif expect == "not_this_one":
            # A different decision, as far from the original in words as
            # the distant paraphrase is. The only thing being measured is
            # that the earlier answer was not reused for it; whether some
            # record in the repository answers it is another rung's
            # business and not evidence either way.
            row["ok"] = not cited
        elif expect == "none":
            row["ok"] = not cited and node["kind"] == "new"
        else:
            row["ok"] = node["kind"] == "evidence" and "accountable owner" in (node["answer"] or "")
        out.append(row)
    return out


def answer_as_owner(store, node: dict, answer: str, rationale: str) -> dict:
    """The owner Bridge routed to answers, through the permission check."""
    graph = store.graph
    name = node["owner"]
    person = graph.find_person(name)
    if person is None:
        with graph.transaction():
            pid = graph.add_person(name, source="eval")
        person = graph.get_person(pid)
    row = store.get_decision(node["node_id"])
    store.answer(node["node_id"], {"answer": answer, "rationale": rationale, "signed_by": name,
                                   "expected_updated_at": row["updated_at"]},
                 actor=Actor.person(person))
    canvas.finish_task(store, {"task_id": node["task_id"]})
    return {"decision_id": node["node_id"], "question": node["question"],
            "paths": node["path"], "owner": name, "answer": answer}


def run(tasks: dict, db: Path, work: Path) -> dict:
    repo = tasks["repo"]
    work.mkdir(parents=True, exist_ok=True)
    out = {"repo": repo, "cutoff": tasks["cutoff"], "cutoff_commit": tasks["cutoff_commit"],
           "head": tasks["head"], "semantic": CFG.semantic_retrieval, "routing": [], "memory": []}

    fresh = work / "routing.db"
    shutil.copy(db, fresh)
    out["routing"] = routing_panel(Store(fresh), repo, tasks["teams"], _prefixes(tasks))

    for task in tasks["tasks"]:
        key = task["key"]
        if not key.get("probes"):
            continue
        per_task = work / f"memory-{task['id']}.db"
        shutil.copy(db, per_task)
        store = Store(per_task)
        first = key["decisions"][0]
        node = ask(store, task["title"], first["question"], ", ".join(task["paths"]), repo)
        if not node["owner"]:
            # Nobody to answer it: assign it the way the unrouted queue does.
            store.assign(node["node_id"], {"owner_id": store.graph.owner_id_for(key["owner"])})
            node = {**node, **canvas.node_view(store, node["node_id"])}
        answered = answer_as_owner(store, node, first["answer"], first["rationale"])
        out["memory"].append({
            "task": task["id"], "asked": first["question"], "routed_to": node["owner"],
            "key_owner": key["owner"],
            "probes": memory_panel(store, repo, answered, key["probes"]),
        })
    return out


def _prefixes(tasks: dict) -> dict:
    from evals.real_oss import spec
    return spec.TEAM_PREFIXES


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tasks", default=str(Path(__file__).resolve().parent / "tasks.json"))
    ap.add_argument("--db", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--work", default="")
    ap.add_argument("--semantic", action="store_true", help="run the model-backed rungs too")
    args = ap.parse_args(argv)
    if args.semantic:
        use_semantic()

    tasks = json.loads(Path(args.tasks).read_text())
    db = Path(args.db or (Path(args.tasks).resolve().parent / "bridge.db"))
    import tempfile
    work = Path(args.work or tempfile.mkdtemp(prefix="real-oss-probes-"))
    result = run(tasks, db, work)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n")

    print("who decides about each area CODEOWNERS names:")
    for r in result["routing"]:
        mark = "ok " if r["named"] and r["on_the_team"] else ("?? " if r["named"] else "-- ")
        print(f"  {mark}{r['prefix']:48s} -> {r['named'] or '(nobody)':22s} "
              f"team of {r['team_size']:3d}  codeowners={r['cites_codeowners']} history={r['cites_history']}")
    named = [r for r in result["routing"] if r["named"]]
    print(f"  named a person on {len(named)} of {len(result['routing'])}, "
          f"on the listed team on {sum(1 for r in named if r['on_the_team'])}")
    print("\nthe same ground, after a person answered it:")
    for block in result["memory"]:
        print(f"  {block['task']} (answered by {block['routed_to']})")
        for p in block["probes"]:
            print(f"    {'ok ' if p['ok'] else 'NO '}{p['probe']:15s} {p['status']:9s} {p['kind']:11s} "
                  f"recalled={str(p.get('recalled')):5s} authorized={p['authorized']}")
    total = [p for b in result["memory"] for p in b["probes"]]
    reuse = [p for p in total if p["expect"] == "reuse"]
    print(f"  {sum(1 for p in total if p['ok'])} of {len(total)} probes behaved as the contract says")
    print(f"  reuse: recalled the earlier answer on {sum(1 for p in reuse if p.get('recalled'))} of {len(reuse)}, "
          f"as direct evidence on {sum(1 for p in reuse if p.get('as_evidence'))}"
          f"  (model rungs {'on' if result['semantic'] else 'off'})")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
