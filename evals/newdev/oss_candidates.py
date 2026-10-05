"""How spread out a public repository's contributors are, from its own git
history: human (non-bot) commits in the last twelve months, distinct
authors, authors with ten or more commits, the top author's and top five's
share, and whether CODEOWNERS names people or teams. Fetches commit
metadata only (a treeless, date-bounded clone), so a large repository
takes seconds to minutes.

    python3 evals/newdev/oss_candidates.py prometheus/prometheus apache/airflow --since 2025-09-30

A repository fits a Raven end-to-end test when many people each own a
part: dozens of regular authors, no single author far above a fifth of
the commits, and an ownership file that names people for areas."""
import argparse
import collections
import json
import re
import subprocess
import tempfile
from pathlib import Path

BOT = re.compile(r"\[bot\]|dependabot|pre-commit-ci|github-actions|renovate|bot@|-bot\b|\bbot\b|mergify|copilot"
                 r"|weblate|transifex|allcontributors", re.IGNORECASE)


def git(*args, cwd=None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=900).stdout


def measure(repo: str, since: str, work: Path) -> dict:
    target = work / repo.replace("/", "_")
    if not target.exists():
        subprocess.run(["git", "clone", "-q", "--filter=tree:0", f"--shallow-since={since}", "--no-checkout",
                        "--single-branch", f"https://github.com/{repo}", str(target)], check=True, timeout=900)
    authors: collections.Counter = collections.Counter()
    for line in git("log", "--no-merges", f"--since={since}", "--format=%ae|%an", cwd=target).splitlines():
        email, _, name = line.partition("|")
        if email and not BOT.search(email) and not BOT.search(name):
            authors[name.strip().lower() or email.lower()] += 1
    total = sum(authors.values())
    top = authors.most_common()
    owners_file, people, teams, rules = "", set(), set(), 0
    for candidate in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
        text = git("show", f"HEAD:{candidate}", cwd=target)
        if text:
            owners_file = candidate
            for line in text.splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    rules += 1
                    for handle in re.findall(r"@[\w.-]+(?:/[\w.-]+)?", line):
                        (teams if "/" in handle else people).add(handle)
            break
    return {"repo": repo, "since": since, "human_commits": total, "authors": len(authors),
            "authors_10_plus": sum(1 for _, n in top if n >= 10),
            "top_author_share": round(top[0][1] / total, 3) if total else 0.0,
            "top_five_share": round(sum(n for _, n in top[:5]) / total, 3) if total else 0.0,
            "codeowners": owners_file, "codeowners_rules": rules, "codeowners_people": len(people),
            "codeowners_teams": len(teams)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("repos", nargs="+", help="owner/name on GitHub")
    parser.add_argument("--since", required=True, help="start of the window, YYYY-MM-DD")
    parser.add_argument("--work", default="", help="where to keep the clones (default: a temporary directory)")
    args = parser.parse_args()
    work = Path(args.work or tempfile.mkdtemp(prefix="bridge-oss-"))
    work.mkdir(parents=True, exist_ok=True)
    print(json.dumps([measure(repo, args.since, work) for repo in args.repos], indent=1))


if __name__ == "__main__":
    main()
