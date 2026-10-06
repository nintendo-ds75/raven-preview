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
COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


def valid(subject: str) -> bool:
    return bool(HEADER.fullmatch(subject)) and len(subject) <= 100


def commit_subjects(revisions: str) -> list[str]:
    result = subprocess.run(["git", "log", "--format=%s", revisions], check=True,
                            capture_output=True, text=True)
    return result.stdout.splitlines()


def ensure_commit(sha: str, label: str) -> str:
    """Recover event commits omitted from the checkout after a force push.

    fetch-depth: 0 includes current reachable history, not necessarily the old
    branch tip. Fetch only the exact event SHA; never substitute HEAD or a
    merge base that would silently leave pushed commits unchecked.
    """
    if not isinstance(sha, str) or not COMMIT_SHA.fullmatch(sha) or sha == ZERO_SHA:
        raise ValueError(f"Invalid {label} commit SHA")

    def available():
        return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"],
                              capture_output=True, check=False).returncode == 0

    if available():
        return sha
    fetched = subprocess.run(["git", "fetch", "--no-tags", "--no-recurse-submodules", "origin", sha],
                             capture_output=True, text=True, check=False)
    if fetched.returncode == 0 and available():
        print(f"Fetched missing {label} commit {sha} from origin.")
        return sha
    raise ValueError(f"Cannot resolve {label} commit {sha}. Ensure checkout uses fetch-depth: 0 "
                     "and origin permits fetching the exact event commit, then rerun. "
                     "No replacement commit range was checked.")


def event_revisions(event: dict, kind: str) -> str | None:
    if kind == "pull_request":
        pull = event["pull_request"]
        base = ensure_commit(pull["base"]["sha"], "pull-request base")
        head = ensure_commit(pull["head"]["sha"], "pull-request head")
        return f"{base}..{head}"
    if kind == "push":
        if event.get("deleted") or event["after"] == ZERO_SHA:
            return None
        head = ensure_commit(event["after"], "push head")
        before = event["before"]
        if before != ZERO_SHA:
            before = ensure_commit(before, "push before")
            return f"{before}..{head}"
        default_branch = event["repository"]["default_branch"]
        common = subprocess.run(["git", "merge-base", f"refs/remotes/origin/{default_branch}", head],
                                capture_output=True, text=True, check=False)
        if common.returncode == 0:
            return f"{common.stdout.strip()}..{head}"
        # A genuinely new repository or disconnected branch has no shared
        # history to exclude, so validate every commit introduced at its tip.
        return head
    raise ValueError(f"Unsupported event: {kind}")


def main() -> int:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    kind = os.environ["GITHUB_EVENT_NAME"]
    failures = []
    if kind == "pull_request":
        title = event["pull_request"]["title"]
        if not valid(title):
            failures.append(f"PR title: {title}")
    try:
        revisions = event_revisions(event, kind)
        subjects = commit_subjects(revisions) if revisions is not None else []
    except (ValueError, subprocess.CalledProcessError) as error:
        print(f"Cannot validate commit headers: {error}", file=sys.stderr)
        return 1
    for subject in subjects:
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
