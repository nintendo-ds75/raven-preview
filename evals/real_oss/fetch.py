"""Build the evaluation's Bridge database and task file from a frozen
checkout of a real repository.

Everything Bridge is given is from before the cutoff: the git history
and blame at the cutoff commit, the CODEOWNERS file as it stood there,
and an authority map an operator would have written on that same day.
Everything the run is graded against is from after it: the change that
in fact landed, and the person who in fact made it.

    python3 -m evals.real_oss.fetch --repo /path/to/grafana --out evals/real_oss

Writes `<out>/bridge.db` (ingested and mapped) and `<out>/tasks.json`
(the briefs, the paths, and the key). The database is disposable and
git-ignored; the task file is committed so a run can be repeated.
"""

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bridge.ingest import index_repo, parse_codeowners_text  # noqa: E402
from bridge.store import Store  # noqa: E402
from evals.real_oss import spec  # noqa: E402

# How much of a team's area someone has to have touched before the
# cutoff to be counted a member of it. One commit is noise; three is a
# person who works there.
MEMBER_COMMITS = 3
# Names a person map should not carry: automation, and the aggregate
# identities a squash merge leaves behind.
BOTS = re.compile(r"\b(bot|\[bot\]|renovate|dependabot|grafanabot|github-actions)\b", re.I)
# Generated files are touched by whoever added a flag that week, in every
# area of the product; counting them would put half the company on every
# team. They are also the files CODEOWNERS hands to @grafanabot.
GENERATED = [":(exclude)**/toggles_gen.*", ":(exclude)**/*.gen.*", ":(exclude)**/testdata/**"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout


def cutoff_commit(repo: Path, cutoff: str) -> str:
    sha = git(repo, "log", f"--until={cutoff} 00:00:00", "--format=%H", "-1").strip()
    if not sha:
        raise SystemExit(f"no commit before {cutoff} in {repo}")
    return sha


def authors_under(repo: Path, rev: str, prefixes: list[str], since: str = "", until: str = "") -> Counter:
    """Who authored changes under these paths, in commits reachable from
    rev. Bots and the empty author a squash sometimes leaves are dropped."""
    args = ["log", rev, "--format=%an"]
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")
    args += ["--", *prefixes, *GENERATED]
    counts = Counter()
    for line in git(repo, *args).splitlines():
        name = line.strip()
        if name and not BOTS.search(name):
            counts[name] += 1
    return counts


def email_of(repo: Path, rev: str, author: str) -> str:
    out = git(repo, "log", rev, "-1", f"--author={author}", "--format=%ae").strip()
    return out


def build(repo: Path, out: Path, cutoff: str) -> dict:
    rev = cutoff_commit(repo, cutoff)
    db = out / "bridge.db"
    if db.exists():
        db.unlink()
    store = Store(db)
    graph = store.graph
    stats = index_repo(graph, repo, max_commits=0, repo_name=spec.REPO, rev=rev, paths=spec.SCOPE)

    # CODEOWNERS as it stood at the cutoff names teams. Expanding a team
    # into people is the operator's setup step; here it is done from
    # authorship before the cutoff only.
    owners_text = git(repo, "show", f"{rev}:.github/CODEOWNERS")
    codeowners = dict(parse_codeowners_text(owners_text))
    teams: dict[str, list[str]] = {}
    with graph.transaction():
        for team, prefixes in spec.TEAM_PREFIXES.items():
            declared = [p for p, owners in codeowners.items() if team in owners]
            members = [name for name, n in authors_under(repo, rev, prefixes).most_common()
                       if n >= MEMBER_COMMITS]
            teams[team] = members
            for name in members:
                pid = graph.add_person(name, email=email_of(repo, rev, name), team=team.split("/")[-1])
                for prefix in prefixes:
                    graph.add_authority("path", prefix, "decides", person_id=pid, repo=spec.REPO,
                                        source="codeowners",
                                        note=f"{team} owns {', '.join(declared) or prefix} in CODEOWNERS at {rev[:12]}")

    tasks = []
    for t in spec.TASKS:
        landed = git(repo, "log", "-1", "--format=%H|%an|%ae|%cI|%s", t["commit"]).strip().split("|")
        touched = [p for p in git(repo, "show", "--name-only", "--format=", t["commit"]).split()
                   if p.startswith(("pkg/", "packages/", "public/"))]
        tasks.append({
            "id": t["id"],
            "title": t["title"],
            "brief": t["brief"],
            "paths": t["paths"],
            "key": {
                "commit": landed[0],
                "landed_at": landed[3],
                "subject": landed[4],
                "owner": t["owner"],
                "author_of_record": landed[1],
                "team": t["team"],
                "acceptable": sorted(set(teams.get(t["team"], [])) | {t["owner"]}),
                "files_touched": touched,
                "decisions": t["decisions"],
                "fallback": t["fallback"],
                "agree": t["agree"],
                "probes": t.get("probes", {}),
                "followup": t.get("followup", ""),
                "followup_paths": t.get("followup_paths", []),
                "adherence": t["adherence"],
            },
        })
        if landed[1] != t["owner"]:
            print(f"  note: {t['id']} author of record is {landed[1]}, spec says {t['owner']}")

    payload = {
        "repo": spec.REPO,
        "checkout": str(repo),
        "cutoff": cutoff,
        "cutoff_commit": rev,
        "head": git(repo, "rev-parse", "HEAD").strip(),
        "scope": spec.SCOPE,
        "ingest": stats,
        "teams": teams,
        "member_commits": MEMBER_COMMITS,
        "tasks": tasks,
    }
    (out / "tasks.json").write_text(json.dumps(payload, indent=2) + "\n")
    return payload


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, help="a git checkout of the repository, frozen")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--cutoff", default=spec.CUTOFF)
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    payload = build(Path(args.repo).resolve(), out, args.cutoff)
    print(f"cutoff {payload['cutoff']} at {payload['cutoff_commit'][:12]}, head {payload['head'][:12]}")
    print(f"ingested {payload['ingest']['commits']} commits, {payload['ingest']['files']} files, "
          f"{payload['ingest']['owners']} ownership rows")
    for team, members in payload["teams"].items():
        print(f"  {team}: {len(members)} people ({', '.join(members[:4])}{'...' if len(members) > 4 else ''})")
    for t in payload["tasks"]:
        key = t["key"]
        print(f"  {t['id']}: key owner {key['owner']}, {len(key['acceptable'])} acceptable, "
              f"{len(key['decisions'])} decisions, landed {key['landed_at'][:10]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
