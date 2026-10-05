"""Task-level replay: a merged series as one task on the canvas.

    python3 -m bench.routing.tree_exam --datasets qemu --n 50 --out bench/routing/results/tree-01

A pull that lands a series of commits by one author is a task the way an
agent would run it: kicked off with a title, a goal and the paths it
expects to touch, then one node per change, each grown from the previous
one. The exam replays it at the merge's first parent (nothing after it
visible), calls bridge_start_task for the verdict, writes every commit as
a node with bridge_add_node, and scores:

  per node   who Raven routed to, against the people git says approved
             that commit (the routing bench's labels and metrics);
  per task   the people asked across the tree against the approvers of
             the whole series (precision and recall), asks per task,
             nodes deduplicated as the same open question;
  triage     the kickoff verdict against two labels git gives: whether an
             approver other than the requester was required, and
             whether the series crossed areas with different listed
             owners.

Conditions: tree (node questions from the commit subjects) and
tree-pathless (every path, filename and directory name stripped from the
node questions; the task's kickoff paths stay, since the agent knows
what it plans to touch, and a node inherits its parent's area).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from bridge import canvas
from bridge.config import load as load_config

from . import driver
from .datasets import (DATASETS, Dataset, RevCache, _dedupe, _person_dict, _since, _stratified, approval_labels,
                       catch_all_committers, ensure_clone, is_machine, iter_log, listing_for, related_people,
                       scratch_dir, split_trailers)
from .driver import pct
from .harness import _fresh_store, warm_db
from .identity import Person, name_matches
from .metrics import score, summarize
from .questions import TreeTokens, context_from_body, template_question

CONDITIONS = ("tree", "tree-pathless")
MIN_SERIES = 2
MAX_SERIES = 8


@dataclass
class Node:
    sha: str
    subject: str
    body: str
    files: list[str]
    strong: list[dict]
    listing: list[dict]
    listing_patterns: list[str]
    label_sources: dict
    related: list[dict]


@dataclass
class Series:
    dataset: str
    idx: int
    merge_sha: str
    base: str
    ts: str
    author: dict
    title: str
    goal: str
    paths: list[str]
    nodes: list[Node]
    approvers: list[dict]         # union of the nodes' strong labels
    approver_required: bool       # someone other than the requester approved
    cross_owner: bool             # the series touched areas with different listed owners
    listed_owners: list[str]
    related: list[dict] = field(default_factory=list)


def _dirs(files: list[str], cap: int = 6) -> list[str]:
    out: list[str] = []
    for f in files:
        d = f.rsplit("/", 1)[0] + "/" if "/" in f else f
        if d not in out:
            out.append(d)
    return out[:cap]


def sample_series(ds: Dataset, repo: Path, n: int, seed: int, log=print) -> list[Series]:
    """Single-author runs of 2 to 8 commits inside merged pulls since the
    dataset's sample bound, spread over time."""
    scope_args = ["--", *ds.scope] if ds.scope else []
    all_commits = _since(iter_log(repo, "--no-merges", *scope_args), ds.sample_from)
    catch_all = catch_all_committers(all_commits)
    merges = _since(iter_log(repo, "--merges", *scope_args), ds.sample_from)
    log(f"[{ds.name}] {len(merges)} merges since {ds.sample_from}")
    candidates: list[tuple] = []
    for m in merges:
        if len(m.parents) < 2 or not m.subject.startswith("Merge "):
            continue
        base, head = m.parents[0], m.parents[1]
        try:
            commits = iter_log(repo, f"{base}..{head}", "--no-merges", "--reverse", "--max-count=60", *scope_args)
        except RuntimeError:
            continue
        run: list = []
        for c in commits + [None]:
            if c is not None and run and c.author.key == run[-1].author.key and len(run) < MAX_SERIES:
                run.append(c)
                continue
            if len(run) >= MIN_SERIES and not is_machine(run[0].author.name):
                candidates.append((m, base, run))
            run = [c] if c is not None else []
    log(f"[{ds.name}] {len(candidates)} single-author series of {MIN_SERIES} to {MAX_SERIES} commits")

    class _Row:
        def __init__(self, m, base, run):
            self.ts, self.sha, self.m, self.base, self.run = run[-1].ts, run[0].sha, m, base, run

    picked = _stratified([_Row(*c) for c in candidates], n, seed)
    cache = RevCache(ds, repo)
    out: list[Series] = []
    for i, row in enumerate(picked):
        m, base, run = row.m, row.base, row.run
        nodes: list[Node] = []
        approvers: list[Person] = []
        all_files: list[str] = []
        owners: set[str] = set()
        for c in run:
            prose, strong = approval_labels(c, catch_all)
            files = [f for f in c.files if not ds.scope or any(f.startswith(s.rstrip("/") + "/") for s in ds.scope)] or c.files
            listed, teams, patterns, source = listing_for(cache, base, files)
            sources: dict[str, list[str]] = defaultdict(list)
            for tag, p in strong:
                sources[tag].append(p.label)
            if listed:
                sources[source] = [p.label for p in listed]
                owners |= {p.label for p in listed}
            people = _dedupe([p for _, p in strong])
            approvers = _dedupe(approvers + people)
            all_files += [f for f in files if f not in all_files]
            nodes.append(Node(sha=c.sha, subject=c.subject, body=prose[:1500], files=files[:40],
                              strong=[_person_dict(p) for p in people], listing=[_person_dict(p) for p in listed],
                              listing_patterns=patterns, label_sources=dict(sources),
                              related=related_people(repo, base, files, ds.scope)))
        first = run[0]
        prose, _ = split_trailers(first.body)
        title = first.subject if len(run) == 1 else f"{first.subject} (a series of {len(run)} changes)"
        related = related_people(repo, base, all_files, ds.scope)
        out.append(Series(
            dataset=ds.name, idx=i, merge_sha=m.sha, base=base, ts=run[-1].ts, author=_person_dict(first.author),
            title=title, goal=prose[:1500], paths=_dirs(all_files), nodes=nodes,
            approvers=[_person_dict(p) for p in approvers],
            approver_required=any(not p.matches(first.author) for p in approvers),
            cross_owner=len(owners) > 1, listed_owners=sorted(owners), related=related))
        if (i + 1) % 10 == 0:
            log(f"[{ds.name}] labeled {i + 1}/{len(picked)} series")
    return out


def build_series(ds: Dataset, n: int, seed: int, log=print) -> list[Series]:
    repo = ensure_clone(ds, log)
    cache_file = scratch_dir() / "items" / f"{ds.name}-series-n{n}-s{seed}-v1.json"
    if cache_file.exists():
        rows = json.loads(cache_file.read_text())
        return [Series(**{**r, "nodes": [Node(**nd) for nd in r["nodes"]]}) for r in rows]
    items = sample_series(ds, repo, n, seed, log)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps([asdict(s) for s in items], indent=1, ensure_ascii=False))
    return items


# ---------------- replay ----------------

def _requester(s: Series) -> str:
    a = s.author
    return f"{a.get('name', '')} <{a.get('email', '')}>" if a.get("name") and a.get("email") else a.get("name") or a.get("email") or ""


def replay_series(ds: Dataset, s: Series, clone: Path, conditions: tuple[str, ...], workdir: Path | None = None) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix=f"tree-{ds.name}-", dir=str(workdir) if workdir else None))
    os.environ["BRIDGE_SEMANTIC"] = "1" if os.environ.get("BRIDGE_BENCH_MODEL") == "1" else "0"
    os.environ["BRIDGE_LIVE"] = "0"
    result: dict = {"series": {k: v for k, v in asdict(s).items() if k not in ("nodes", "related")},
                    "n_nodes": len(s.nodes), "conditions": {}}
    result["series"]["related_n"] = len(s.related)
    try:
        warm = tmp / "ingested.db"
        _, result["ingest_seconds"] = warm_db(ds, clone, s.base, warm)
        tokens =TreeTokens(str(clone), s.base, ds.scope or None) if any(c.endswith("pathless") for c in conditions) else None
        cfg = load_config()
        for cond in conditions:
            pathless = cond.endswith("pathless")
            db = tmp / f"{cond}.db"
            shutil.copyfile(warm, db)
            store = _fresh_store(db)
            t0 = time.time()
            out: dict = {"nodes": [], "error": ""}
            try:
                title = tokens.strip(s.title) if pathless else s.title
                goal = tokens.strip(s.goal) if pathless else s.goal
                task = canvas.start_task(store, cfg, {"title": title[:300], "goal": goal, "repo": ds.repo_key,
                                                      "agent": "tree-exam", "requester": _requester(s),
                                                      "paths": ",".join(s.paths)})
                out["verdict"], out["why"] = task["verdict"], task["why"]
                out["discovery_people"] = [p["name"] for p in task["discovery"].get("people", [])]
                parent = ""
                for k, node in enumerate(s.nodes):
                    # The node is the decision itself (Should X do Y?), the
                    # way an agent writes it; who to ask is Raven's call.
                    q = tokens.strip(template_question(node.subject)) if pathless else template_question(node.subject)
                    ctx = context_from_body(node.body)
                    ctx = tokens.strip(ctx) if pathless else ctx
                    n = canvas.add_node(store, cfg, {"task_id": task["task_id"], "question": q, "context": ctx,
                                                     "parent_id": parent, "client_ref": f"node-{k}"})
                    owner = n["owner"]
                    # The route the node's ask took; a node no rung routed
                    # (a duplicate, a record answer) has the owner alone.
                    ranked = n.get("ranked") or ([{"owner": owner, "evidence": []}] if owner else [])
                    evidence = [ln for ln in n["owner_evidence"].split("; ") if ln] or (ranked[0]["evidence"] if ranked else [])
                    out["nodes"].append({"question": q, "status": n["status"], "owner": owner, "asked": n["status"] == "pending",
                                         "evidence": evidence[:6], "ranked": [r["owner"] for r in ranked],
                                         "duplicate_of": n.get("duplicate_of", ""), "path": n["path"],
                                         "kind": n["kind"], "answer": (n["answer"] or "")[:200]})
                    parent = n["node_id"]
                tree = canvas.get_tree(store, task["task_id"])
                out["counts"] = tree["counts"]
            except Exception as e:  # recorded, never hidden
                out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            out["seconds"] = round(time.time() - t0, 2)
            store.graph.close()
            result["conditions"][cond] = out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return result


# ---------------- scoring ----------------

def score_task(s: Series, out: dict) -> dict:
    nodes_scored = []
    unlabeled = 0
    for node, res in zip(s.nodes, out.get("nodes", [])):
        if not node.strong:
            # git names no approver for this change (the integrator
            # applied it): the node still counts as an ask, not as a miss.
            unlabeled += 1
            continue
        item = {"strong": node.strong, "listing": node.listing, "related": node.related or s.related,
                "files": node.files, "listing_patterns": node.listing_patterns, "ts": s.ts,
                "label_sources": node.label_sources}
        outcome = {"owner": res["owner"], "ranked": res["ranked"], "evidence": res["evidence"], "error": out.get("error", "")}
        sc = score(item, outcome)
        sc["status"] = res["status"]
        sc["inherited"] = any("inherited" in ln or "working under" in ln or "working in" in ln for ln in res["evidence"])
        nodes_scored.append(sc)
    approvers = [Person(**p) for p in s.approvers]
    asked = []
    for res in out.get("nodes", []):
        if res.get("asked") and res["owner"] and res["owner"] not in asked:
            asked.append(res["owner"])
    hit = [a for a in asked if name_matches(a, approvers) is not None]
    covered = {name_matches(a, approvers).label for a in asked if name_matches(a, approvers) is not None}
    verdict = out.get("verdict", "")
    return {"nodes": nodes_scored, "unlabeled": unlabeled, "asked": asked, "n_asked": len(asked),
            "people_precision": len(hit) / len(asked) if asked else None,
            "people_recall": len(covered) / len(approvers) if approvers else None,
            "asks": sum(1 for r in out.get("nodes", []) if r.get("asked")),
            "duplicates": sum(1 for r in out.get("nodes", []) if r["status"] == "duplicate"),
            "statuses": dict(Counter(r["status"] for r in out.get("nodes", []))),
            "verdict": verdict, "approver_required": s.approver_required, "cross_owner": s.cross_owner,
            "error": out.get("error", "")}


def summarize_tasks(scored: list[dict]) -> dict:
    n = len(scored)
    if not n:
        return {"n": 0}
    nodes = [nd for t in scored for nd in t["nodes"]]
    node_summary = summarize(nodes)
    pending_nodes = [nd for nd in nodes if nd["status"] == "pending"]
    with_prec = [t["people_precision"] for t in scored if t["people_precision"] is not None]
    with_rec = [t["people_recall"] for t in scored if t["people_recall"] is not None]
    engaged = [t for t in scored if t["verdict"] == "engage"]
    passed = [t for t in scored if t["verdict"] == "pass"]
    required = [t for t in scored if t["approver_required"]]
    cross = [t for t in scored if t["cross_owner"]]
    statuses: Counter = Counter()
    for t in scored:
        statuses.update(t.get("statuses", {}))
    return {
        "n": n, "nodes": len(nodes), "unlabeled_nodes": sum(t.get("unlabeled", 0) for t in scored),
        "errors": sum(1 for t in scored if t["error"]),
        "node_statuses": dict(statuses),
        "node": {k: node_summary[k] for k in ("hit1", "hit3", "alt", "wrong_person", "unknown_should", "unknown_honest",
                                              "evidence_honest", "routed")},
        "node_mechanisms": node_summary["mechanisms"],
        "node_inherited_area": round(sum(1 for nd in nodes if nd.get("inherited")) / len(nodes), 3) if nodes else 0,
        "node_inherited_hit1": round(sum(1 for nd in nodes if nd.get("inherited") and nd["hit1"])
                                     / max(1, sum(1 for nd in nodes if nd.get("inherited"))), 3),
        "asks_per_task": round(sum(t["asks"] for t in scored) / n, 2),
        "people_asked_per_task": round(sum(t["n_asked"] for t in scored) / n, 2),
        "duplicates_per_task": round(sum(t["duplicates"] for t in scored) / n, 2),
        "people_precision": round(sum(with_prec) / len(with_prec), 3) if with_prec else None,
        "people_recall": round(sum(with_rec) / len(with_rec), 3) if with_rec else None,
        "engage_rate": round(len(engaged) / n, 3),
        "approver_required_rate": round(len(required) / n, 3),
        "engage_recall_when_required": round(sum(1 for t in required if t["verdict"] == "engage") / len(required), 3) if required else None,
        "pass_precision_when_self_owned": round(sum(1 for t in passed if not t["approver_required"]) / len(passed), 3) if passed else None,
        "cross_owner_rate": round(len(cross) / n, 3),
        "cross_owner_engaged": round(sum(1 for t in cross if t["verdict"] == "engage") / len(cross), 3) if cross else None,
        "pending_node_hit1": round(sum(1 for nd in pending_nodes if nd["hit1"]) / len(pending_nodes), 3) if pending_nodes else None,
    }


# ---------------- driver ----------------

def _work(raw: dict) -> dict:
    w = driver.WORKER
    s = Series(**{**raw, "nodes": [Node(**nd) for nd in raw["nodes"]]})
    return replay_series(w["ds"], s, w["clone"], w["conditions"], w["workdir"])


def run_dataset(ds: Dataset, n: int, seed: int, conditions: tuple, jobs: int, limit: int, log=print) -> dict:
    clone = ensure_clone(ds, log)
    series = build_series(ds, n, seed, log)
    if limit:
        series = series[:limit]
    workdir = scratch_dir() / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    log(f"[{ds.name}] replaying {len(series)} series ({sum(len(s.nodes) for s in series)} nodes) under "
        f"{', '.join(conditions)} with {jobs} workers")
    t0 = time.time()
    rows = driver.run_pool(_work, [asdict(s) for s in series], jobs,
                           {"ds": ds, "clone": clone, "conditions": conditions, "workdir": workdir}, ds.name, log)
    by_key = {(s.merge_sha, s.idx): s for s in series}
    for row in rows:
        s = by_key[(row["series"]["merge_sha"], row["series"]["idx"])]
        row["scores"] = {cond: score_task(s, out) for cond, out in row["conditions"].items()}
    summary = {cond: summarize_tasks([r["scores"][cond] for r in rows if cond in r["scores"]]) for cond in conditions}
    model_rungs = driver.model_rungs_on()
    return {"dataset": ds.name, "role": ds.role, "n": len(rows), "seed": seed, "conditions": list(conditions),
            "model_rungs": model_rungs, "fast_model": load_config().fast_model if model_rungs else "",
            "seconds": round(time.time() - t0, 1), "summary": summary, "rows": rows}


def write_summary(results: list[dict], out: Path, label: str) -> str:
    lines = [f"# Tree exam: {label}", "", f"Generated {datetime.now().isoformat(timespec='seconds')}", ""]
    for res in results:
        rungs = f"model rungs on ({res.get('fast_model')})" if res.get("model_rungs") else "model rungs off"
        lines += [f"## {res['dataset']} ({res['role']}, {res['n']} tasks, {rungs})", ""]
        for cond, s in res["summary"].items():
            if not s.get("n"):
                continue
            nd = s["node"]
            lines += [f"### {cond}", "",
                      f"- nodes: {s['nodes']} labeled" + (f" ({s['unlabeled_nodes']} more with no approver in git)" if s.get("unlabeled_nodes") else "")
                      + f"; per node top-1 {pct(nd['hit1'])}, top-3 {pct(nd['hit3'])}, alternate "
                      f"{pct(nd['alt'])}, wrong person {pct(nd['wrong_person'])}, unknown (should know) "
                      f"{pct(nd['unknown_should'])}, unknown (honest) {pct(nd['unknown_honest'])}, evidence honest "
                      f"{pct(nd['evidence_honest'])}, routed {pct(nd['routed'])}",
                      f"- nodes that took their area from the tree: {pct(s['node_inherited_area'])} (top-1 among them "
                      f"{pct(s['node_inherited_hit1'])})",
                      f"- per task: {s['asks_per_task']} asks, {s['people_asked_per_task']} people asked, "
                      f"{s['duplicates_per_task']} nodes deduplicated; people precision {pct(s['people_precision'])}, "
                      f"people recall {pct(s['people_recall'])}",
                      f"- triage: engaged {pct(s['engage_rate'])}; an approver other than the requester was required "
                      f"on {pct(s['approver_required_rate'])} (engaged on {pct(s['engage_recall_when_required'])} of "
                      f"those); of the passes {pct(s['pass_precision_when_self_owned'])} needed nobody else; "
                      f"cross-owner series {pct(s['cross_owner_rate'])} (engaged on {pct(s['cross_owner_engaged'])})",
                      f"- node statuses: " + ", ".join(f"{k} {v}" for k, v in s["node_statuses"].items()),
                      f"- node misses by mechanism: " + ", ".join(f"{k} {v}" for k, v in s["node_mechanisms"].items()),
                      f"- errors: {s['errors']}", ""]
    text = "\n".join(lines)
    (out / "summary.md").write_text(text)
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    driver.add_common_options(ap, "qemu", 50, "tasks (series) per dataset", CONDITIONS)
    args = ap.parse_args(argv)
    names, conditions, out, label = driver.resolve_common(ap, args, CONDITIONS, "tree-")

    def log(msg: str) -> None:
        print(msg, flush=True)

    results = []
    for name in names:
        res = run_dataset(DATASETS[name], args.n, args.seed, conditions, args.jobs, args.limit, log)
        (out / f"{name}.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
        results.append(res)
    (out / "summary.json").write_text(json.dumps([{k: v for k, v in r.items() if k != "rows"} for r in results], indent=1))
    log(write_summary(results, out, label))
    return 0


if __name__ == "__main__":
    sys.exit(main())
