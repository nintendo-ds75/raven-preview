"""Does the kickoff verdict come from the task, or from the host?

A reviewer running a real host against a frozen Grafana checkout found
that the original brief for the feature-toggle task got `pass`, and that
the run only engaged once the host's own workflow instructions were
appended, because they contained the words "authorization" and
"release". The verdict was true of the host's boilerplate and said
nothing about the work, so a successful run established nothing about
decision discovery.

This probe measures both halves against the held-out briefs in
`evals/real_oss/spec.py`, with no network and no model:

- **the brief alone** should engage where the brief in fact contains a
  decision, and the reason should quote the words it read, so somebody
  can check it against the task;
- **the brief with a host's boilerplate appended**, labelled and
  unlabelled, must reach the same verdict for the same reason; and
- **boilerplate alone**, on a task with nothing in it, must not engage.

    python3 -m evals.real_oss_remote.probe_discovery
    python3 -m evals.real_oss_remote.probe_discovery --out results/discovery.json

Exits non-zero if any probe reports the unsafe outcome.
"""
import argparse
import json
import os
from pathlib import Path

os.environ.setdefault("BRIDGE_MODEL_API", "none")
os.environ.setdefault("BRIDGE_SEMANTIC", "0")
os.environ.setdefault("BRIDGE_LIVE", "0")

from bridge.canvas import candidates, triage  # noqa: E402
from evals.real_oss.spec import TASKS  # noqa: E402

# What a host puts around the task it was given. Both shapes are real:
# the first is the labelled block a host appends, the second the same
# rules run together into prose.
LABELLED = ("\n\nWorkflow instructions:\n"
            "1. Call bridge_start_task before you begin, and bridge_finish_task when you are done.\n"
            "2. You are authorized to edit any file in the repository.\n"
            "3. Do not cut a release; the maintainers handle releases.\n"
            "4. Report what you changed and what you ran.\n")
LOOSE = ("\n\nYou are authorized to edit any file in the repository. Do not cut a release; the maintainers "
         "handle releases. Report what you changed and what you ran.")
# A task with no decision in it. Anything this engages on came from the
# boilerplate, because there is nothing else for it to have come from.
INERT = {"title": "Bump the pinned toolchain",
         "brief": "Bump the pinned toolchain in scripts/build to the version the build already uses."}


def discovery_for(task: dict) -> dict:
    """What Raven would have found: the paths the task names, with no
    people and nothing decided before. Kept deliberately bare so the
    verdict can only come from the task's own words."""
    paths = task.get("paths") or []
    return {"areas": [{"path": p, "why": f"the agent is working in {p}", "weight": 1.0} for p in paths],
            "people": [], "listings": [], "prior_decisions": [], "pending": []}


def run() -> list[dict]:
    out: list[dict] = []
    for task in TASKS:
        discovery = discovery_for(task)
        verdict, why = triage(discovery, task["title"], task["brief"])
        out.append({"probe": "the brief alone", "case": task["id"], "expected": "engage",
                    "got": verdict, "ok": verdict == "engage", "why": why})
        out.append({"probe": "the reason quotes the task", "case": task["id"], "expected": "a quotation",
                    "got": 'in "' in why, "ok": verdict != "engage" or 'in "' in why, "why": why})
        named = [c["question"] for c in candidates(discovery, task["title"], task["brief"])]
        out.append({"probe": "decisions named before asking", "case": task["id"],
                    "expected": ">= 1", "got": len(named), "ok": bool(named), "why": "; ".join(named)})
        for label, extra in (("labelled", LABELLED), ("in prose", LOOSE)):
            other, other_why = triage(discovery, task["title"], task["brief"] + extra)
            out.append({"probe": f"the host's own rules, {label}", "case": task["id"],
                        "expected": f"{verdict}, unchanged", "got": other,
                        "ok": other == verdict and other_why == why, "why": other_why})
    inert = discovery_for({"paths": ["pkg/services/featuremgmt/registry.go"]})
    base, base_why = triage(inert, INERT["title"], INERT["brief"])
    out.append({"probe": "a task with nothing in it", "case": "the brief alone", "expected": "pass",
                "got": base, "ok": base == "pass", "why": base_why})
    for label, extra in (("labelled", LABELLED), ("in prose", LOOSE)):
        got, got_why = triage(inert, INERT["title"], INERT["brief"] + extra)
        out.append({"probe": "a task with nothing in it", "case": f"the host's own rules, {label}",
                    "expected": "pass", "got": got, "ok": got == "pass", "why": got_why})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="write the results as JSON")
    args = parser.parse_args()
    results = run()
    probe_width = max(len(r["probe"]) for r in results)
    case_width = max(len(r["case"]) for r in results)
    for r in results:
        print(f"  {'ok  ' if r['ok'] else 'FAIL'}  {r['probe']:{probe_width}}  {r['case']:{case_width}}  "
              f"expected {r['expected']}, got {r['got']}")
        if not r["ok"]:
            print(f"        {r['why'][:200]}")
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)} of {len(results)} probes held")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, indent=2) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
