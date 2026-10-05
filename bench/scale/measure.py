"""SCALE-01 and SCALE-02 in one script: seed a memory of N signed
decisions and time what one question costs on it, whole-scan and
bounded. No network, no model; the hashed embeddings are computed as
the rows are written, as they are in the inbox.

    python3 -m bench.scale.measure --sizes 500,2000,10000 --questions 20

Prints one line per size and writes bench/scale/results/<stamp>.json.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bridge import graph as graph_mod  # noqa: E402
from bridge.llm import embed  # noqa: E402
from bridge.store import Store  # noqa: E402

AREAS = ["billing", "pricing", "auth", "metering", "exports", "invoices", "refunds", "quota", "tiers", "webhooks",
         "retention", "rollout", "schema", "alerts", "onboarding", "partners", "contracts", "credits", "tax", "audit"]
VERBS = ["round", "cap", "bill", "expire", "retry", "notify", "migrate", "deprecate", "throttle", "archive"]
OBJECTS = ["overage", "the quota", "partner accounts", "annual contracts", "trial usage", "refunds", "late invoices",
           "the enterprise rate card", "duplicate charges", "credit balances"]


def seed(store: Store, n: int, rng: random.Random) -> None:
    graph = store.graph
    run = store.add_run({"title": "scale seed", "agent": "bench", "repo": "acme/platform"})
    stamp = datetime.now().isoformat()
    with graph.transaction():
        for i in range(n):
            question = (f"Should {rng.choice(AREAS)} {rng.choice(VERBS)} {rng.choice(OBJECTS)} "
                        f"for {rng.choice(AREAS)} customers in case {i}?")
            did = f"seed{i:08x}"
            graph.db.execute(
                "INSERT INTO decisions(id, run_id, question, context, path, owner_id, routing_reason, status, prediction, "
                "source_id, answer, rationale, answered_by, created_at, updated_at, kind, category, repo, signoff, "
                "signed_by, embedding) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (did, run["id"], question, "seeded", f"{rng.choice(AREAS)}/x.py", None, "seed", "approved", None, None,
                 f"Answer {i}: yes, with a cap of {rng.randint(1, 900)}.", "seeded rationale", "Seed Owner",
                 stamp, stamp, "evidence", "policy", "acme/platform", "signed", "Seed Owner",
                 graph_mod._f32blob(embed(question))))


def measure(n: int, questions: int, seed_value: int) -> dict:
    rng = random.Random(seed_value)
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "scale.db")
        t0 = time.perf_counter()
        seed(store, n, rng)
        seeded = time.perf_counter() - t0
        graph = store.graph
        asks = [f"Should {rng.choice(AREAS)} {rng.choice(VERBS)} {rng.choice(OBJECTS)} for {rng.choice(AREAS)} customers?"
                for _ in range(questions)]
        out = {"n": n, "seed_seconds": round(seeded, 2), "fts": graph.has_fts}
        for mode, cap in (("whole", 10 ** 9), ("bounded", 0)):
            graph_mod.FULL_SCAN_MAX = cap
            sims, searches, agree = [], [], 0
            for q in asks:
                emb = embed(q)
                t = time.perf_counter()
                top = graph.similar_answered(emb, top_k=3, min_score=0.0, repo="acme/platform", query=q)
                sims.append(time.perf_counter() - t)
                t = time.perf_counter()
                graph.memory_search(q, limit=5, repo="acme/platform", min_score=0.0)
                searches.append(time.perf_counter() - t)
                if mode == "bounded":
                    graph_mod.FULL_SCAN_MAX = 10 ** 9
                    whole = graph.similar_answered(emb, top_k=1, min_score=0.0, repo="acme/platform", query=q)
                    graph_mod.FULL_SCAN_MAX = 0
                    agree += int(bool(top) and bool(whole) and top[0][1].id == whole[0][1].id)
            out[mode] = {"similar_ms": round(statistics.median(sims) * 1000, 1),
                         "search_ms": round(statistics.median(searches) * 1000, 1)}
            if mode == "bounded":
                out["bounded"]["top1_agrees_with_whole"] = round(agree / max(1, len(asks)), 2)
        graph_mod.FULL_SCAN_MAX = 2000
        store.graph.close()
        return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="500,2000,10000")
    ap.add_argument("--questions", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    results = []
    for size in [int(x) for x in args.sizes.split(",") if x.strip()]:
        r = measure(size, args.questions, args.seed)
        results.append(r)
        print(f"n={r['n']:>6}  seed {r['seed_seconds']:>6.1f}s  whole: similar {r['whole']['similar_ms']:>7.1f} ms, "
              f"search {r['whole']['search_ms']:>7.1f} ms   bounded: similar {r['bounded']['similar_ms']:>7.1f} ms, "
              f"search {r['bounded']['search_ms']:>7.1f} ms, top-1 agrees {r['bounded']['top1_agrees_with_whole']:.2f}"
              + ("" if r["fts"] else "  (no FTS5: bounded by recency alone)"), flush=True)
    out = Path(__file__).resolve().parent / "results"
    out.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out / f"{stamp}.json").write_text(json.dumps({"generated": stamp, "questions": args.questions, "results": results}, indent=1))
    print(f"written {out / (stamp + '.json')}")


if __name__ == "__main__":
    main()
