"""Prepare a temporally bounded real repository fixture, without future PRs.

Public PR cache files contain {pr, /reviews, /files}; fetch.py creates them.
The host receives only an exported baseline and its problem brief. No future
git objects, PR numbers, answer key, or reviewer labels are exported to it.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tarfile
from collections import Counter
from pathlib import Path

from bridge.github import _store_pull, apply_pull
from bridge.ingest import index_repo
from bridge.store import Store

HERE = Path(__file__).resolve().parent


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def bot(name):
    return "[bot]" in name.lower() or name.lower().endswith("-bot") or name == "GitHub"


def distribution(repo, revision, limit):
    lines = git(repo, "log", revision, f"-{limit}", "--format=%aN%x09%cN").splitlines()
    from evals.pseudonyms import label
    authors = Counter(label(line.split("\t")[0]) for line in lines)
    committers = Counter(label(line.split("\t")[1]) for line in lines)
    human = Counter({k: v for k, v in authors.items() if not bot(k)})
    total = sum(human.values())
    return {"commits": len(lines), "human_authors": len(human), "human_authored_commits": total,
            "top_author_share": max(human.values()) / total,
            "top_five_author_share": sum(n for _, n in human.most_common(5)) / total,
            "effective_authors": 1 / sum((n / total) ** 2 for n in human.values()),
            "authors": dict(authors.most_common()), "committers": dict(committers.most_common())}


def prepare(repo, public, out):
    os.environ.update(BRIDGE_MODEL_API="none", BRIDGE_SEMANTIC="0", BRIDGE_LIVE="0")
    spec = json.loads((HERE / "cases.json").read_text())
    base = spec["baseline"]
    out.mkdir(parents=True, exist_ok=True)
    db = out / "baseline.db"
    if db.exists():
        raise RuntimeError(f"Refusing to overwrite {db}; choose a new output directory")
    store = Store(db)
    stats = index_repo(store.graph, repo, max_commits=spec["history_limit"], repo_name=spec["repository"], rev=base)
    print("Ingested", json.dumps(stats), flush=True)
    g = store.graph
    # The directory is an evaluation contact fixture, not verified business
    # authority. It contains pre-cutoff git contributors, no authority rows.
    mapping = {}
    for e in g.db.execute("SELECT name,email FROM engineers ORDER BY name,email").fetchall():
        if bot(e["name"]):
            continue
        match = re.search(r"(?:\d+\+)?([^@]+)@users\.noreply\.github\.com$", e["email"])
        login = match.group(1) if match else ""
        try:
            pid = g.add_person(e["name"], email=e["email"], github_login=login,
                               slack_id="U" + hashlib.sha256(e["name"].encode()).hexdigest()[:10].upper(),
                               source="eval: public git identity; simulated contact")
        except sqlite3.IntegrityError:
            continue
        if login:
            mapping[login.lower()] = pid
    imported = []
    for number in spec["prior_prs"]:
        data = json.loads((public / f"{number}.json").read_text())
        pr = data["pr"]
        assert pr["merged_at"] <= spec["baseline_time"], number
        # Public profile names are not needed: use exact handles when git
        # supplies no mapping. No private team membership is invented.
        names = {pr["user"]["login"], (pr.get("merged_by") or {}).get("login", "")}
        reviews = [r for r in data["/reviews"] if r.get("submitted_at") and r["submitted_at"] <= spec["baseline_time"]]
        names.update(r["user"]["login"] for r in reviews)
        for name in sorted(names):
            if not name or bot(name):
                continue
            person = g.find_person("@" + name)
            if person is None:
                pid = g.add_person(name, github_login=name, slack_id="U" + hashlib.sha256(name.encode()).hexdigest()[:10].upper(),
                                   source="eval: pre-cutoff public PR identity; simulated contact")
            else:
                pid = person["id"]
            mapping[name.lower()] = pid
        row = {"number": number, "merge_sha": pr["merge_commit_sha"], "title": pr["title"],
               # GitHub does not expose historical PR-body versions. Omit
               # potentially edited bodies rather than use future evidence.
               "body": (pr.get("body") or "") if pr["updated_at"] <= spec["baseline_time"] else "",
               "author": pr["user"]["login"], "merged_by": (pr.get("merged_by") or {}).get("login", ""),
               "merged_at": pr["merged_at"], "updated_at": min(pr["updated_at"], spec["baseline_time"]),
               "files": [f["filename"] for f in data["/files"]], "truncated": 0,
               "reviews": [{"id": r["id"], "user": r["user"]["login"], "state": r["state"], "body": "",
                            "submitted_at": r["submitted_at"]} for r in reviews]}
        with g.transaction():
            _store_pull(g, spec["repository"], row)
            detail = apply_pull(g, spec["repository"], row)
        imported.append({"number": number, "merged_at": pr["merged_at"], **detail})
    # A named fallback receives genuinely unknown areas. It is not counted
    # as a correct automatic route and does not grant approval authority.
    coordinator = g.add_person("Evaluation Coordinator", slack_id="UCOORDINATOR", source="eval fixture")
    g.set_setting("coordinator", coordinator)
    g.set_setting("require_verified_route", "0")
    g.set_setting("slack_fallback_channel", "CEVAL")
    from evals.pseudonyms import pseudonymize
    scrub = pseudonymize(g)
    g.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    g.close()
    paths = {"AGENTS.md", "LICENSE", "README.md", "go.mod", "go.sum", "go.work", "go.work.sum", ".github/CODEOWNERS"}
    for case in spec["cases"]:
        paths.update(case["export"])
    tracked = set(git(repo, "ls-tree", "-r", "--name-only", base).splitlines())
    included = sorted(p for p in paths if p in tracked or any(f.startswith(p + "/") for f in tracked))
    archive = out / "snapshot.tar"
    with archive.open("wb") as fh:
        subprocess.run(["git", "-C", str(repo), "archive", base, *included], stdout=fh, check=True)
    snapshot = out / "snapshot"
    snapshot.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(snapshot, filter="data")
    archive.unlink()
    manifest = {"repository": spec["repository"], "baseline": base, "baseline_time": spec["baseline_time"],
                "bridge_revision": git(HERE.parents[1], "rev-parse", "HEAD"), "ingest": stats,
                "observed_distribution": distribution(repo, spec["observed_head"], 1600),
                "baseline_distribution": distribution(repo, base, spec["history_limit"]),
                "imported_prs": imported, "snapshot_files": len(list(snapshot.rglob("*"))),
                "mode": {"bridge_semantic": False, "bridge_live": False, "host": "codex exec", "people": "simulated"},
                "directory_logins": sorted(mapping)}
    manifest = scrub.scrub_json(manifest)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps({"output": str(out), "ingest": stats, "snapshot_files": manifest["snapshot_files"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--public", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.repo, args.public, args.out)
