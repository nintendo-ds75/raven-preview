"""What the replay bench and the tree exam share: the worker pool with
its progress log, the model-rungs environment, the common command line
options and the percent formatter. Neither metric nor condition lives
here."""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import time
from datetime import datetime
from pathlib import Path

from .datasets import DATASETS

WORKER: dict = {}


def init_worker(state: dict) -> None:
    """Runs once per worker (and once in-process for --jobs 1): the
    dataset, clone and conditions the work function reads, and the
    ladder's deterministic rungs only unless the bench was started with
    --model-rungs (BRIDGE_BENCH_MODEL=1); the key stays in the environment."""
    WORKER.clear()
    WORKER.update(state)
    if os.environ.get("BRIDGE_BENCH_MODEL") != "1":
        os.environ.pop("ANTHROPIC_API_KEY", None)


def run_pool(work, payload: list, jobs: int, state: dict, label: str, log=print) -> list:
    """Every payload entry through `work`, in order, with a progress line
    every ten items and at the end."""
    t0 = time.time()
    rows: list = []

    def progress(k: int) -> None:
        if k % 10 == 0 or k == len(payload):
            log(f"[{label}] {k}/{len(payload)} done ({time.time() - t0:.0f}s)")

    if jobs > 1:
        ctx = mp.get_context("fork")
        with ctx.Pool(jobs, initializer=init_worker, initargs=(state,)) as pool:
            for k, row in enumerate(pool.imap(work, payload, chunksize=1), 1):
                rows.append(row)
                progress(k)
    else:
        init_worker(state)
        for k, p in enumerate(payload, 1):
            rows.append(work(p))
            progress(k)
    return rows


def model_rungs_on() -> bool:
    return os.environ.get("BRIDGE_BENCH_MODEL") == "1"


def add_common_options(ap: argparse.ArgumentParser, datasets: str, n: int, n_help: str, conditions: tuple) -> None:
    ap.add_argument("--datasets", default=datasets, help="comma-separated; 'all' for every dataset")
    ap.add_argument("--n", type=int, default=n, help=n_help)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--conditions", default=",".join(conditions))
    ap.add_argument("--jobs", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    ap.add_argument("--limit", type=int, default=0, help="only the first K items (smoke runs)")
    ap.add_argument("--out", default="", help="results dir (default bench/routing/results/<prefix><timestamp>)")
    ap.add_argument("--label", default="")
    ap.add_argument("--model-rungs", action="store_true",
                    help="let the model-backed rungs run (needs ANTHROPIC_API_KEY in the environment; the fast "
                         "model maps pathless questions to areas and confirms record matches); off by default")


def resolve_common(ap: argparse.ArgumentParser, args: argparse.Namespace, conditions: tuple,
                   out_prefix: str = "") -> tuple[list[str], tuple, Path, str]:
    """The dataset names, the conditions, the results dir (created) and
    the label, with the model-rungs environment set from the options."""
    names = list(DATASETS) if args.datasets == "all" else [d.strip() for d in args.datasets.split(",") if d.strip()]
    unknown = [d for d in names if d not in DATASETS]
    if unknown:
        ap.error(f"unknown dataset(s): {', '.join(unknown)}; known: {', '.join(DATASETS)}")
    chosen = tuple(c.strip() for c in args.conditions.split(",") if c.strip())
    bad = [c for c in chosen if c not in conditions]
    if bad:
        ap.error(f"unknown condition(s): {', '.join(bad)}")
    os.environ["BRIDGE_BENCH_MODEL"] = "1" if args.model_rungs else "0"
    if args.model_rungs and not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        ap.error("--model-rungs needs ANTHROPIC_API_KEY in the environment")
    if args.model_rungs:
        # The fast points and the selector, on the fast model, so an ask
        # costs seconds; the deep rungs are a different, slower product.
        from bridge.config import load as load_config
        os.environ.setdefault("BRIDGE_DEEP", "0")
        os.environ.setdefault("BRIDGE_MODEL", load_config().fast_model)
    out = Path(args.out) if args.out else Path("bench/routing/results") / (out_prefix + datetime.now().strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    return names, chosen, out, args.label or out.name


def pct(x) -> str:
    return "n/a" if x is None else f"{round(100 * x)}%"
