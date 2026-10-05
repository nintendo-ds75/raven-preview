"""Who Bridge would ask, on a real open-source repository, before anyone is
mapped: ingest a local checkout into a scratch database and route each probe
question from its path and words alone. No model and no network unless
--sync is given (then GITHUB_TOKEN must be set and review data is added).

    python3 evals/newdev/oss_routing_probe.py --checkout ~/oss/prometheus \\
        --repo prometheus/prometheus --probes evals/newdev/oss_probes/prometheus.json

Prints one line per probe and a JSON summary. It counts a probe as "listed"
when Bridge's own evidence says the repository's CODEOWNERS or MAINTAINERS
names the person it chose; judge "inferred" ones by hand against the
repository's maintainer list or review history. A CODEOWNERS handle (@login)
is tied to the name its owner commits under only once GitHub is synced, so
run with --sync before judging a repository whose handles differ from its
commit names. Real maintainers are named in the output: keep it local, and
never contact them or answer as them."""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkout", required=True, help="a full (not shallow) local git clone")
    parser.add_argument("--repo", required=True, help="owner/name, as GitHub knows it")
    parser.add_argument("--probes", required=True, help="JSON list of {path, question}")
    parser.add_argument("--db", default="", help="scratch SQLite file (default: a temporary one)")
    parser.add_argument("--commits", type=int, default=None, help="history depth (default 2000, 0 = all)")
    parser.add_argument("--sync", action="store_true", help="also sync pull request reviews (needs GITHUB_TOKEN)")
    args = parser.parse_args()
    os.environ.setdefault("BRIDGE_SEMANTIC", "0")
    os.environ.setdefault("BRIDGE_LIVE", "0")
    from bridge.config import load
    from bridge.ingest import index_repo
    from bridge.ladder import ask
    from bridge.store import Store
    db = args.db or str(Path(tempfile.mkdtemp(prefix="bridge-probe-")) / "probe.db")
    store = Store(db)
    stats = index_repo(store.graph, os.path.expanduser(args.checkout), max_commits=args.commits, repo_name=args.repo)
    if args.sync:
        from bridge.github import GitHubAPI, sync_repo
        stats["github"] = sync_repo(store.graph, GitHubAPI(os.environ["GITHUB_TOKEN"]), args.repo)
    cfg = load()
    rows = []
    for probe in json.loads(Path(args.probes).read_text()):
        run = store.add_run({"title": probe["question"][:300], "agent": "routing probe", "repo": args.repo})
        row = ask(store, cfg, run["id"], probe["question"], context="routing probe", path=probe["path"])
        evidence = row.get("owner_evidence") or ""
        owner = row.get("owner_name") or ""
        # Listed: the repository's own CODEOWNERS or MAINTAINERS names the
        # person Bridge chose (a note naming someone else does not count).
        # The chosen person's evidence opens with their listing line; a note
        # about someone else's listing comes later and starts "note:".
        named = owner and (evidence.startswith(("CODEOWNERS lists", "MAINTAINERS lists"))
                           or any(f"{label} lists {owner}" in evidence for label in ("CODEOWNERS", "MAINTAINERS")))
        outcome = "unrouted" if not owner else "listed" if named else "inferred"
        rows.append({"path": probe["path"], "owner": owner, "outcome": outcome, "evidence": evidence})
        print(f"{probe['path'][-58:]:58s} -> {owner or '(nobody)':26s} {outcome:8s} {evidence[:110]}")
    counts = {k: sum(1 for r in rows if r["outcome"] == k) for k in ("listed", "inferred", "unrouted")}
    print(json.dumps({"repo": args.repo, "db": db, "ingest": {k: stats.get(k) for k in ("files", "commits", "owners", "prs")},
                      "probes": len(rows), **counts, "distinct_owners": len({r["owner"] for r in rows if r["owner"]})},
                     indent=1))


if __name__ == "__main__":
    main()
