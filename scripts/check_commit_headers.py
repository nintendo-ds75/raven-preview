"""Validate Conventional Commit subjects and PR titles in GitHub Actions."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path


HEADER = re.compile(
    r"^(feat|fix|chore|docs|test|refactor|perf|build|ci|style|revert)"
    r"(\([a-z0-9][a-z0-9._/-]*\))?!?: (?:[^\s.]|[^\s].*[^\s.])$"
)
ZERO_SHA = "0" * 40


def valid(subject: str) -> bool:
    return bool(HEADER.fullmatch(subject)) and len(subject) <= 100


def commit_subjects(revisions: str) -> list[str]:
    result = subprocess.run(["git", "log", "--format=%s", revisions], check=True,
                            capture_output=True, text=True)
    return result.stdout.splitlines()


def main() -> int:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    kind = os.environ["GITHUB_EVENT_NAME"]
    failures = []
    if kind == "pull_request":
        title = event["pull_request"]["title"]
        if not valid(title):
            failures.append(f"PR title: {title}")
        base = event["pull_request"]["base"]["sha"]
        head = event["pull_request"]["head"]["sha"]
        revisions = f"{base}..{head}"
    elif kind == "push":
        before = event["before"]
        head = event["after"]
        if before == ZERO_SHA:
            default_branch = event["repository"]["default_branch"]
            common = subprocess.run(["git", "merge-base", f"origin/{default_branch}", head],
                                    capture_output=True, text=True, check=False)
            before = common.stdout.strip() if common.returncode == 0 else ""
        revisions = head if not before else f"{before}..{head}"
    else:
        raise ValueError(f"Unsupported event: {kind}")
    for subject in commit_subjects(revisions):
        if not valid(subject):
            failures.append(f"Commit subject: {subject}")
    if failures:
        print("Expected type(optional-scope): imperative summary (100 characters max).", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("PR title and new commit subjects follow Conventional Commits.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
