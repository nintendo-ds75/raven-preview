"""Which of a task's real decisions does Raven name before it is asked?

The reviewer's largest remaining gap: Raven cannot route a decision the
agent never writes down, and a less cooperative host could register none
and finish. `bridge_start_task` answers that with `candidates`, the
decisions it thinks the task contains, as prompts for the agent. Nothing
measured whether they are the right ones.

The held-out tasks in spec.py carry the decisions the change in fact
turned on, each with the words that identify it. This panel asks how
many of those the candidates name, before any agent has looked at
anything, and how many candidates are named that no real decision
corresponds to.

    python3 -m evals.real_oss.coverage
    BRIDGE_MODEL_API=claude-cli python3 -m evals.real_oss.coverage --semantic

Coverage is the number worth moving. Noise is the cost: a candidate the
agent has to read and dismiss. Neither is a gate, because a candidate is
a prompt and not a decision, so this panel reports and never fails.
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# A decision's identifying words were written to recognise its ANSWER,
# so matching them against a question undercounts: "deprecated first"
# and "hidden from docs" name decisions whose terms are `deprecat` and
# `docs`. One distinctive term is evidence; one generic word is not.
COVER_SCORE = 1.0


def _weight(term: str) -> float:
    distinctive = len(term) >= 8 or "_" in term or "." in term or " " in term
    return 1.0 if distinctive else 0.5


def score(text: str, decision: dict) -> float:
    low = text.lower()
    return sum(_weight(t.lower()) for t in decision["match"] if t.lower() in low)


def covers(text: str, decision: dict) -> bool:
    return score(text, decision) >= COVER_SCORE


def run(semantic: bool, db: Path) -> dict:
    os.environ["BRIDGE_SEMANTIC"] = "1" if semantic else "0"
    os.environ.setdefault("BRIDGE_LIVE", "0")
    if not semantic:
        os.environ["BRIDGE_MODEL_API"] = "none"
    from bridge import canvas  # noqa: E402
    from bridge.config import load  # noqa: E402
    from bridge.store import Store  # noqa: E402
    from evals.real_oss import spec  # noqa: E402

    store = Store(db)
    cfg = load()
    repo = store.graph.resolve_repo(spec.REPO)
    out = {"semantic": semantic, "tasks": []}
    for task in spec.TASKS:
        discovery = canvas.discover(store, cfg, repo, task["title"], task["brief"], task["paths"],
                                    "Eval requester")
        # What the kickoff does, in the same order: read the task, then
        # build the candidate list from that and from Raven's signals.
        if cfg.semantic_retrieval:
            from bridge.llm import name_decisions  # noqa: E402
            read = name_decisions(cfg, task["title"], canvas.task_statement(task["brief"]),
                                  [a.get("path", "") for a in (discovery.get("areas") or [])],
                                  [p.get("name", "") for p in (discovery.get("people") or [])])
            if read:
                discovery["named_decisions"] = read
        named = canvas.candidates(discovery, task["title"], task["brief"])
        rows = []
        for decision in task["decisions"]:
            hit = next((c for c in named if covers(c["question"] + " " + c.get("why", ""), decision)), None)
            rows.append({"decision": decision["question"], "covered": hit is not None,
                         "by": (hit or {}).get("question", ""), "source": (hit or {}).get("source", "")})
        spare = []
        for c in named:
            text = c["question"] + " " + c.get("why", "")
            if any(covers(text, d) for d in task["decisions"]):
                continue
            best = max(task["decisions"], key=lambda d: score(text, d))
            spare.append({"question": c["question"], "source": c["source"],
                          "nearest": best["question"], "near_score": score(text, best)})
        out["tasks"].append({
            "id": task["id"], "decisions": rows, "candidates": len(named),
            "covered": sum(1 for r in rows if r["covered"]), "of": len(rows), "spare": spare})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--semantic", action="store_true", help="let the model name candidates too")
    parser.add_argument("--db", type=Path, default=Path(__file__).resolve().parent / "bridge.db")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if not args.db.exists():
        print(f"No {args.db}: run python3 -m evals.real_oss.fetch --repo /path/to/grafana first")
        return 2
    result = run(args.semantic, args.db)
    covered = sum(t["covered"] for t in result["tasks"])
    total = sum(t["of"] for t in result["tasks"])
    spare = sum(len(t["spare"]) for t in result["tasks"])
    named = sum(t["candidates"] for t in result["tasks"])
    for t in result["tasks"]:
        print(f"\n{t['id']}  ({t['covered']} of {t['of']} named, {t['candidates']} candidates)")
        for r in t["decisions"]:
            mark = "ok  " if r["covered"] else "MISS"
            print(f"  {mark}  {r['decision'][:78]}")
            if r["covered"]:
                print(f"          by: {r['by'][:74]}  ({r['source']})")
        for s in t["spare"]:
            print(f"  ..    matches no real decision: {s['question'][:62]}  ({s['source']})")
            if s.get("nearest"):
                print(f"          nearest ({s['near_score']:.1f}): {s['nearest'][:66]}")
    print(f"\nnamed {covered} of {total} real decisions from {named} candidates, {spare} of which match none "
          f"({'model naming on' if result['semantic'] else 'deterministic only'})")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
