"""Run Bridge on one replay item under each condition.

WARM: a fresh database ingested from the repository as of the parent
revision (history, tree, CODEOWNERS all bounded at T), live lookups off.
COLD: an empty graph that knows only where the checkout is and the
revision bound; Bridge may fetch static context at ask time.
PATHLESS and PATHLESS-COLD: the same with every path token removed from
the question and its context.

A warm database depends only on the dataset, the parent revision and the
ingest code, so it is kept under the scratch dir and copied on the next
run; nothing is ever shared across bases."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import bridge.graph
import bridge.ingest
from bridge.config import load as load_config
from bridge.ingest import MAX_COMMITS, index_repo
from bridge.ladder import ask
from bridge.store import Store

from .datasets import Dataset, Item, scratch_dir
from .questions import TreeTokens, context_from_body, routing_form, template_question

CONDITIONS = ("warm", "cold", "pathless", "pathless-cold")


@dataclass
class Outcome:
    condition: str
    question: str
    context: str
    owner: str = ""
    evidence: list[str] = field(default_factory=list)
    ranked: list[str] = field(default_factory=list)
    ranked_evidence: list[list[str]] = field(default_factory=list)
    kind: str = ""
    status: str = ""
    routing_reason: str = ""
    answer: str = ""
    seconds: float = 0.0
    error: str = ""


def _fresh_store(path: Path) -> Store:
    store = Store(path)
    g = store.graph
    g.db.execute("PRAGMA synchronous=OFF")
    return store


def ingest_at(store: Store, ds: Dataset, clone: Path, base: str, depth: int = MAX_COMMITS) -> dict:
    # index_repo writes in one transaction of its own.
    return index_repo(store.graph, clone, max_commits=depth, repo_name=ds.repo_key, rev=base,
                      paths=ds.scope or None)


def _ingest_hash() -> str:
    h = hashlib.sha256()
    for mod in (bridge.ingest, bridge.graph):
        h.update(Path(mod.__file__).read_bytes())
    h.update(str(MAX_COMMITS).encode())
    return h.hexdigest()[:12]


WARM_CACHE_BYTES = int(float(os.environ.get("BRIDGE_BENCH_WARM_CACHE_GB", "4")) * 1024 ** 3)


def _trim_warm_cache(root: Path, keep: Path) -> None:
    """Keep the warm cache under WARM_CACHE_BYTES: databases built by an
    older ingest code go first, then the least recently used, never the
    one just written. A cache that grew without bound once filled the
    disk mid-run."""
    current = f"-{_ingest_hash()}.db"
    entries = []
    for p in root.glob("*/*.db"):
        if p == keep:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        entries.append((p.name.endswith(current), st.st_atime, p, st.st_size))
    total = sum(e[3] for e in entries) + (keep.stat().st_size if keep.exists() else 0)
    for is_current, _, p, size in sorted(entries):
        if total <= WARM_CACHE_BYTES and is_current:
            break
        try:
            p.unlink()
            total -= size
        except OSError:
            pass


def warm_db(ds: Dataset, clone: Path, base: str, target: Path) -> tuple[dict, float]:
    """The warm database for one base at `target`: copied from the scratch
    cache when this ingest code already built it, otherwise ingested,
    checkpointed and stored there. Returns the ingest stats (empty on a
    cache hit) and the seconds the ingest took (0.0 on a hit)."""
    root = scratch_dir() / "warm"
    cached = root / ds.name / f"{base}-{_ingest_hash()}.db"
    if cached.exists():
        shutil.copyfile(cached, target)
        os.utime(cached)
        return {}, 0.0
    store = _fresh_store(target)
    t0 = time.time()
    stats = ingest_at(store, ds, clone, base)
    seconds = round(time.time() - t0, 2)
    store.graph.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store.graph.close()
    cached.parent.mkdir(parents=True, exist_ok=True)
    partial = cached.with_name(cached.name + f".{os.getpid()}.tmp")
    shutil.copyfile(target, partial)
    os.replace(partial, cached)
    _trim_warm_cache(root, cached)
    return stats, seconds


def cold_store(path: Path, ds: Dataset, clone: Path, base: str) -> Store:
    store = _fresh_store(path)
    g = store.graph
    g.set_source(ds.repo_key, "git", str(clone))
    g.set_source(ds.repo_key, "git_rev", base)
    g.set_source(ds.repo_key, "git_paths", " ".join(s.strip("/") for s in ds.scope))
    return store


def requester_of(item: Item) -> str:
    """The commit's author as the requester: the person the question is
    asked for, whose usual approvers count and who is never routed to."""
    a = item.author or {}
    name, email = (a.get("name") or "").strip(), (a.get("email") or "").strip()
    return f"{name} <{email}>" if name and email else name or email


def ask_bridge(store: Store, ds: Dataset, question: str, context: str, live: bool,
               condition: str, requester: str = "") -> Outcome:
    # The model rungs run only when the bench was started with
    # --model-rungs (BRIDGE_BENCH_MODEL=1); the key stays in the environment.
    os.environ["BRIDGE_SEMANTIC"] = "1" if os.environ.get("BRIDGE_BENCH_MODEL") == "1" else "0"
    os.environ["BRIDGE_LIVE"] = "1" if live else "0"
    cfg = load_config()
    out = Outcome(condition=condition, question=question, context=context)
    t0 = time.time()
    try:
        run = store.add_run({"title": question[:300], "agent": "replay-bench", "repo": ds.repo_key})
        row = ask(store, cfg, run["id"], question, context=context or "replay", path="unknown",
                  requester=requester)
        out.owner = row.get("owner_name") or ""
        out.evidence = [ln for ln in (row.get("owner_evidence") or "").split("; ") if ln]
        out.kind = row.get("kind") or ""
        out.status = row.get("status") or ""
        out.routing_reason = row.get("routing_reason") or ""
        out.answer = (row.get("answer") or "")[:300]
        # The route the ladder took, not a second one: absent when no rung
        # routed, and then the owner alone (a duplicate's twin, say).
        ranked = row.get("ranked") or ([{"owner": out.owner, "evidence": out.evidence}] if out.owner else [])
        out.ranked = [r["owner"] for r in ranked]
        out.ranked_evidence = [list(r["evidence"][:3]) for r in ranked]
    except Exception as e:  # the bench records failures, it never hides them
        out.error = f"{type(e).__name__}: {str(e)[:200]}"
    out.seconds = round(time.time() - t0, 3)
    return out


def run_item(ds: Dataset, item: Item, clone: Path, conditions: tuple[str, ...], question: str | None = None,
             workdir: Path | None = None) -> dict:
    """All conditions for one item. Returns a JSON-ready dict."""
    tmp = Path(tempfile.mkdtemp(prefix=f"replay-{ds.name}-", dir=str(workdir) if workdir else None))
    decision = question or template_question(item.subject)
    context = context_from_body(item.body)
    outcomes: dict[str, dict] = {}
    ingest_stats: dict = {}
    ingest_seconds = 0.0
    try:
        tokens = None
        if any(c.startswith("pathless") for c in conditions):
            tokens = TreeTokens(str(clone), item.base, ds.scope or None)
        warm_path = tmp / "ingested.db"
        if any(c in ("warm", "pathless") for c in conditions):
            ingest_stats, ingest_seconds = warm_db(ds, clone, item.base, warm_path)
        for cond in conditions:
            pathless = cond.startswith("pathless")
            live = cond.endswith("cold")
            q = routing_form(tokens.strip(decision) if pathless else decision)
            ctx = tokens.strip(context) if pathless else context
            db = tmp / f"{cond}.db"
            if live:
                store = cold_store(db, ds, clone, item.base)
            else:
                shutil.copyfile(warm_path, db)
                store = _fresh_store(db)
            outcomes[cond] = asdict(ask_bridge(store, ds, q, ctx, live, cond, requester_of(item)))
            store.graph.close()
        # The baselines see the same item: one signal each, no ladder.
        from .baselines import baseline_outcomes
        outcomes.update(baseline_outcomes(item.__dict__, decision, context))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"item": asdict(item), "decision": decision, "outcomes": outcomes,
            "ingest": {k: ingest_stats.get(k) for k in ("commits", "files", "owners", "truncated")},
            "ingest_seconds": ingest_seconds}


def run_stale_series(ds: Dataset, items: list[Item], clone: Path, questions: dict[str, str] | None,
                     anchor_fraction: float = 0.2, workdir: Path | None = None) -> dict:
    """Ingest once at an early item's parent, then ask every later item
    without re-ingesting: how fast does the map go stale?"""
    ordered = sorted(items, key=lambda i: i.ts)
    if len(ordered) < 10:
        return {}
    anchor = ordered[max(1, int(len(ordered) * anchor_fraction))]
    tmp = Path(tempfile.mkdtemp(prefix=f"stale-{ds.name}-", dir=str(workdir) if workdir else None))
    rows = []
    try:
        base_db = tmp / "anchor.db"
        warm_db(ds, clone, anchor.base, base_db)
        for it in ordered:
            if it.ts <= anchor.ts:
                continue
            decision = (questions or {}).get(it.sha) or template_question(it.subject)
            db = tmp / "ask.db"
            shutil.copyfile(base_db, db)
            s = _fresh_store(db)
            out = ask_bridge(s, ds, routing_form(decision), context_from_body(it.body), False, "stale",
                             requester_of(it))
            s.graph.close()
            rows.append({"sha": it.sha, "ts": it.ts, "outcome": asdict(out)})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"anchor_sha": anchor.sha, "anchor_ts": anchor.ts, "rows": rows}
