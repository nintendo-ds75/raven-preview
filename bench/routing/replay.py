"""Historical-replay routing bench.

    python3 -m bench.routing.replay --datasets qemu,linux-drm,node --n 150 --out bench/routing/results/run1

For every sampled commit C at time T: hide C and everything after it,
build the question C was deciding, ingest the repository as of C's parent
(WARM) or leave the graph empty (COLD), ask Bridge who should approve,
and score the answer against the people git says approved it. One
command, cached clones in a scratch dir outside the repo, JSON results,
a markdown summary, deterministic seeds.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import driver
from .datasets import DATASETS, Dataset, Item, build_items, ensure_clone, scratch_dir
from .driver import pct
from .harness import CONDITIONS, run_item, run_stale_series
from .metrics import score, summarize, summarize_strata
from .questions import rephrase_with_model


def _work(item_dict: dict) -> dict:
    w = driver.WORKER
    item = Item(**item_dict)
    return run_item(w["ds"], item, w["clone"], w["conditions"], w["questions"].get(item.sha), w["workdir"])


def run_dataset(ds: Dataset, n: int, seed: int, conditions: tuple, jobs: int, questions_mode: str,
                model: str, limit: int, stale: bool, log=print) -> dict:
    clone = ensure_clone(ds, log)
    items = build_items(ds, n, seed, log)
    if limit:
        items = items[:limit]
    questions: dict[str, str] = {}
    if questions_mode == "model":
        questions = rephrase_with_model(items, scratch_dir() / "questions" / f"{ds.name}-{model}.json", model, log)
    workdir = scratch_dir() / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    log(f"[{ds.name}] replaying {len(items)} items under {', '.join(conditions)} with {jobs} workers")
    t0 = time.time()
    rows = driver.run_pool(_work, [item.__dict__ for item in items], jobs,
                           {"ds": ds, "clone": clone, "conditions": conditions, "questions": questions,
                            "workdir": workdir}, ds.name, log)
    for row in rows:
        row["scores"] = {cond: score(row["item"], out) for cond, out in row["outcomes"].items()}
        # The related-people list is the label machinery's working set;
        # its size is kept, its rows stay in the item cache.
        row["item"]["related_n"] = len(row["item"].get("related", []))
        row["item"]["related"] = row["item"]["related"][:12]
    conds = list(conditions) + sorted({k for r in rows for k in r["scores"] if k not in conditions})
    summary = {cond: summarize([r["scores"][cond] for r in rows if cond in r["scores"]]) for cond in conds}
    strata = {cond: summarize_strata(rows, cond) for cond in conditions}
    from bridge.config import load as _load_cfg
    model_rungs = driver.model_rungs_on()
    result = {"dataset": ds.name, "role": ds.role, "n": len(rows), "seed": seed, "conditions": list(conditions),
              "questions": questions_mode, "model_rungs": model_rungs,
              "fast_model": _load_cfg().fast_model if model_rungs else "",
              "seconds": round(time.time() - t0, 1),
              "label_notes": ds.notes, "summary": summary, "strata": strata, "rows": rows}
    if stale:
        log(f"[{ds.name}] stale series (one ingest, asks slide forward)")
        series = run_stale_series(ds, items, clone, questions, workdir=workdir)
        if series:
            for r in series["rows"]:
                item = next(i for i in items if i.sha == r["sha"])
                r["score"] = score(item.__dict__, r["outcome"])
            result["stale"] = {"anchor_sha": series["anchor_sha"], "anchor_ts": series["anchor_ts"],
                               "buckets": stale_buckets(series)}
            result["stale_rows"] = series["rows"]
    return result


def stale_buckets(series: dict) -> list[dict]:
    anchor = datetime.fromisoformat(series["anchor_ts"])
    buckets: dict[int, list[dict]] = defaultdict(list)
    for r in series["rows"]:
        months = int(((datetime.fromisoformat(r["ts"]) - anchor).days) // 30)
        buckets[min(months, 12)].append(r["score"])
    out = []
    for m in sorted(buckets):
        s = summarize(buckets[m])
        out.append({"months_after_ingest": m, "n": s["n"], "hit1": s["hit1"], "alt": s["alt"],
                    "wrong_person": s["wrong_person"], "unknown": round(s["unknown_should"] + s["unknown_honest"], 3)})
    return out


def write_summary(results: list[dict], out: Path, label: str) -> str:
    lines = [f"# Routing replay: {label}", "", f"Generated {datetime.now().isoformat(timespec='seconds')}", ""]
    for res in results:
        ds = DATASETS[res["dataset"]]
        rungs = f", model rungs on ({res.get('fast_model')})" if res.get("model_rungs") else ", model rungs off"
        lines += [f"## {res['dataset']} ({res['role']}, n={res['n']}, questions={res['questions']}{rungs})", "",
                  ds.notes, "",
                  "| condition | top-1 | top-3 | alternate | wrong person | unknown (should know) | unknown (honest) | evidence honest | routed |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for cond, s in res["summary"].items():
            if not s.get("n"):
                continue
            lines.append(f"| {cond} | {pct(s['hit1'])} | {pct(s['hit3'])} | {pct(s['alt'])} | {pct(s['wrong_person'])} "
                         f"| {pct(s['unknown_should'])} | {pct(s['unknown_honest'])} | {pct(s['evidence_honest'])} "
                         f"| {pct(s['routed'])} |")
        lines += ["", "Reported apart (the baselines route on one signal each, no ladder):", "",
                  "| condition | coverage | precision among routed | acceptable contact among routed | authorized signer |",
                  "| --- | --- | --- | --- | --- |"]
        for cond, s in res["summary"].items():
            if not s.get("n"):
                continue
            lines.append(f"| {cond} | {pct(s.get('coverage', 0))} | {pct(s.get('precision_routed', 0))} "
                         f"| {pct(s.get('acceptable_routed', 0))} | {pct(s.get('authorized_signer', 0))} |")
        lines.append("")
        for cond, strata in (res.get("strata") or {}).items():
            if strata:
                parts = ", ".join(f"{k} (n={v['n']}) top-1 {pct(v['hit1'])} wrong {pct(v['wrong_person'])}"
                                  for k, v in strata.items())
                lines.append(f"- {cond} by stratum: {parts}")
        lines.append("")
        for cond, s in res["summary"].items():
            if s.get("mechanisms"):
                mech = ", ".join(f"{k} {v}" for k, v in s["mechanisms"].items())
                lines.append(f"- {cond} misses by mechanism: {mech}")
            if s.get("evidence_issues"):
                iss = ", ".join(f"{k} {v}" for k, v in s["evidence_issues"].items())
                lines.append(f"- {cond} evidence issues: {iss}")
            if s.get("hit_roles"):
                roles = ", ".join(f"{k} {v}" for k, v in s["hit_roles"].items())
                lines.append(f"- {cond} hits by label role: {roles}")
        if res.get("stale"):
            lines += ["", f"Staleness (one ingest at {res['stale']['anchor_ts'][:10]}, asks slide forward):", "",
                      "| months after ingest | n | top-1 | alternate | wrong person | unknown |", "| --- | --- | --- | --- | --- | --- |"]
            for b in res["stale"]["buckets"]:
                lines.append(f"| {b['months_after_ingest']} | {b['n']} | {pct(b['hit1'])} | {pct(b['alt'])} "
                             f"| {pct(b['wrong_person'])} | {pct(b['unknown'])} |")
        lines.append("")
    text = "\n".join(lines)
    (out / "summary.md").write_text(text)
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    driver.add_common_options(ap, "qemu,linux-drm,node", 150, "items per dataset", CONDITIONS)
    ap.add_argument("--questions", choices=("template", "model"), default="template")
    ap.add_argument("--model", default="claude-haiku-4-5-20251001", help="model for --questions model")
    ap.add_argument("--stale", action="store_true", help="also run the staleness series")
    args = ap.parse_args(argv)
    names, conditions, out, label = driver.resolve_common(ap, args, CONDITIONS)

    def log(msg: str) -> None:
        print(msg, flush=True)

    results = []
    for name in names:
        res = run_dataset(DATASETS[name], args.n, args.seed, conditions, args.jobs, args.questions, args.model,
                          args.limit, args.stale, log)
        (out / f"{name}.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
        results.append(res)
        log(f"[{name}] " + " | ".join(f"{c}: top-1 {pct(s['hit1'])}, wrong {pct(s['wrong_person'])}"
                                       for c, s in res["summary"].items() if s.get("n")))
    (out / "summary.json").write_text(json.dumps(
        [{k: v for k, v in r.items() if k not in ("rows", "stale_rows")} for r in results], indent=1))
    text = write_summary(results, out, label)
    log(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
