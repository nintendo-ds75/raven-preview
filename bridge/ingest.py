"""Repository ingestion: the ownership map from repo signals. Ported.

Sources, all local, all read-only:
- git log: authors, per-path touch counts, merge commits and squashed PRs
  as intent records, plain commits with a real body as records
- CODEOWNERS: ownership hints
- Reviewed-by trailers: review participation

A full rebuild, idempotent: re-running replaces what the repository
contributed and refreshes counts (upserts), and a material change in a
blame share closes the old ownership row and opens a new one, so history
stays auditable.
"""

from __future__ import annotations

import datetime
import math
import re
import os
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .graph import Graph, ts_to_iso

MAX_COMMITS = 2000
OWNERSHIP_DEPTH = 2
RECENT_HALF_LIFE_DAYS = 90.0
MIN_BODY_CHARS = 80
MAX_SQUASH_INTENTS = 2000
LIVE_RESULTS_PER_SOURCE = 10
LAZY_BLAME_COMMITS = 300

_REVIEWED_BY = re.compile(r"^Reviewed-by:\s*(.+?)\s*(?:<|$)", re.MULTILINE)
_MERGE_PR = re.compile(r"Merge pull request #(\d+)")
_SQUASHED_PR = re.compile(r"\(#(\d+)\)\s*$")
_TRAILER_RE = re.compile(
    r"^\s*(?:[A-Z][\w-]+-by|Signed-off-by|Co-authored-by|Refs?|Fixes|Closes"
    r"|Change-Id|Claude-Session|Reviewed-on|Tested-by|Cc)\s*:", re.IGNORECASE | re.MULTILINE)


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {out.stderr.strip()[:200]}")
    return out.stdout


def _prefixes(path: str) -> list[str]:
    """Directory prefixes to aggregate ownership at. Root-level files return
    nothing: a prefix of '' would match every path and poison routing."""
    parts = path.split("/")
    out = []
    for depth in range(1, min(OWNERSHIP_DEPTH, len(parts) - 1) + 1):
        out.append("/".join(parts[:depth]) + "/")
    return out


CODEOWNERS_CANDIDATES = ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS")


def _read_at(repo: Path, rel: str, rev: str = "") -> str:
    """The text of one file in the checkout, or as of `rev` when given
    (read from the object store, so no checkout is needed)."""
    if rev:
        out = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{rel}"],
                             capture_output=True, text=True, timeout=60)
        return out.stdout if out.returncode == 0 else ""
    p = repo / rel
    return p.read_text(errors="replace") if p.exists() else ""


def parse_codeowners_text(text: str) -> list[tuple[str, list[str]]]:
    rules = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip() if not line.lstrip().startswith("#") else ""
        if not line:
            continue
        parts = line.split()
        pattern, owners = parts[0], [o.lstrip("@") for o in parts[1:] if "@" in o]
        rules.append((pattern, owners))
    return rules


def parse_codeowners(repo: Path, rev: str = "") -> list[tuple[str, list[str]]]:
    for candidate in CODEOWNERS_CANDIDATES:
        text = _read_at(repo, candidate, rev)
        if text:
            return parse_codeowners_text(text)
    return []


def _body_without_trailers(body: str) -> str:
    return " ".join(ln.strip() for ln in (body or "").splitlines()
                    if ln.strip() and not _TRAILER_RE.match(ln)).strip()


# Bots and agents are never people to ask: record_change keeps them out of
# change_people, and routing applies the same test to what listings and
# blame name.
_MACHINE_AUTHOR_RE = re.compile(
    r"\[bot\]$|^dependabot|^renovate|^github$|^github[- ]actions|^snyk-bot|^greenkeeper|^imgbot"
    r"|^pre-commit-ci|^allcontributors|^claude( code)?$|^copilot|^cursor( agent)?$|^devin$|^aider$|^(openai )?codex( cli)?$"
    r"|^semantic-release|^release-please|^netlify|^vercel$|github bot$|^copybara|^bors$|^k8s-ci-robot"
    r"|^openshift-merge|^nixpkgs-ci|^r-ryantm|^backportbot", re.IGNORECASE)


def _is_machine_author(name: str) -> bool:
    return bool(_MACHINE_AUTHOR_RE.search((name or "").strip()))


# ---------------- commits and the shared changes structure ----------------

MAX_INTENT_PATHS = 40
MAX_CHANGE_PATHS = 200
TOUCH_HALF_LIFE_DAYS = 180.0
INTEGRATOR_MERGE_SHARE = 0.15  # diagnostic only; routing weights retained merges by area evidence
LIVE_TOUCH_COMMITS = 300

_APPROVAL_TRAILER_RE = re.compile(
    r"^\s*(Reviewed-by|Acked-by|Helped-by|Approved-by)\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_ADDR_RE = re.compile(r"^\s*\"?([^\"<]*?)\"?\s*(?:<([^>]+)>)?\s*$")


@dataclass
class Commit:
    sha: str
    author: str
    email: str
    committer: str
    cemail: str
    date: str
    subject: str
    body: str
    files: list[str]


_LOG_FMT = "--pretty=format:%x01%H%x02%an%x02%ae%x02%cn%x02%ce%x02%cI%x02%s%x02%b%x03"


def read_log(repo: Path | str, head: str, count_args: list[str], scope_args: list[str],
             merges: bool = False) -> list[Commit]:
    """Commits newest first with the paths they touched. Merges list
    what they brought onto the main line (their first-parent diff)."""
    kind = ["--merges", "--diff-merges=first-parent"] if merges else ["--no-merges"]
    try:
        raw = _git(Path(repo), "log", head, *kind, *count_args, _LOG_FMT, "--name-only", *scope_args)
    except RuntimeError:
        return []
    out: list[Commit] = []
    for chunk in raw.split("\x01"):
        if not chunk.strip():
            continue
        header, sep, tail = chunk.partition("\x03")
        if not sep:
            continue
        f = header.split("\x02")
        if len(f) < 8:
            continue
        files = [ln.strip() for ln in tail.splitlines() if ln.strip() and "\x02" not in ln]
        out.append(Commit(f[0], f[1], f[2], f[3], f[4], f[5], f[6].strip(), f[7], files))
    return out


def _integrators(merges: list[Commit]) -> set[str]:
    """People who merge a large share of everything (release managers,
    a rotation of tree integrators): merging is their job, not a sign
    they own the area merged."""
    if len(merges) < 5:
        return set()
    counts: dict[str, int] = defaultdict(int)
    for m in merges:
        counts[m.author] += 1
    return {a for a, n in counts.items() if n / len(merges) > INTEGRATOR_MERGE_SHARE}


def history_now(commits: list[Commit]) -> float:
    """The newest commit's time: recency is measured against the history
    itself, so a replayed or stale checkout decays consistently."""
    best = 0.0
    for c in commits[:50]:
        try:
            best = max(best, datetime.datetime.fromisoformat(c.date).timestamp())
        except ValueError:
            continue
    return best or time.time()


def _decay(date: str, now: float, half_life: float = TOUCH_HALF_LIFE_DAYS) -> float:
    try:
        age_days = max(0.0, (now - datetime.datetime.fromisoformat(date).timestamp()) / 86400.0)
    except ValueError:
        return 0.0
    return math.exp(-age_days * math.log(2) / half_life)


def _split_addr(text: str) -> tuple[str, str]:
    m = _ADDR_RE.match(text or "")
    if not m:
        return (text or "").strip(), ""
    name, email = (m.group(1) or "").strip(), (m.group(2) or "").strip()
    name = re.sub(r"\s*#\s*v?\d.*$", "", name)
    return name, email


def approvers_of(commit: Commit) -> list[tuple[str, str, str]]:
    """(name, email, role) for every approval signal a commit carries:
    the trailers, and the committer who accepted someone else's patch."""
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for tag, value in _APPROVAL_TRAILER_RE.findall(commit.body):
        name, email = _split_addr(value)
        key = (email or name).lower()
        if not name or key in seen or name == commit.author or _is_machine_author(name):
            continue
        seen.add(key)
        out.append((name, email, tag.lower()))
    if (commit.committer and commit.committer != commit.author
            and not _is_machine_author(commit.committer) and (commit.cemail or "").lower() not in seen):
        out.append((commit.committer, commit.cemail, "committer"))
    return out


def record_change(store: Graph, repo: str, c: Commit, roles: tuple[str, ...] = ()) -> None:
    """Write one commit into the shared structure: its paths, its author,
    and every approver (reviewer trailers, accepting committer) or, for a
    merge, its merger."""
    if not c.files:
        return
    people: list[tuple[str, str, str]] = []
    if not _is_machine_author(c.author):
        people += [(c.author, c.email, r) for r in (roles or ("author",))]
    if not roles:
        people += approvers_of(c)
    if not people:
        return
    for name, email, _role in people:
        store.upsert_engineer(name, email)
    store.add_change(repo, c.sha, c.date, c.files[:MAX_CHANGE_PATHS], people)


# ---------------- MAINTAINERS (kernel and QEMU format) ----------------

_MAINT_TAG_RE = re.compile(r"^([A-Z]):\s*(.*)$")


def parse_maintainers(text: str) -> list[dict]:
    """Sections of a kernel-style MAINTAINERS file: title, M: maintainers,
    R: reviewers, F: patterns, X: exclusions."""
    sections: list[dict] = []
    cur: dict | None = None
    title: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            if cur is not None and cur["files"]:
                sections.append(cur)
            cur, title = None, []
            continue
        m = _MAINT_TAG_RE.match(line)
        if m and len(line) > 2 and line[1] == ":":
            if cur is None:
                cur = {"title": " ".join(title).strip(), "maintainers": [], "reviewers": [], "files": [], "excludes": []}
            tag, value = m.group(1), m.group(2).strip()
            if tag == "M":
                cur["maintainers"].append(_split_addr(value))
            elif tag == "R":
                cur["reviewers"].append(_split_addr(value))
            elif tag == "F":
                cur["files"].append(value)
            elif tag == "X":
                cur["excludes"].append(value)
            continue
        if cur is None and not set(line.strip()) <= {"-", "="}:
            title.append(line.strip())
    if cur is not None and cur["files"]:
        sections.append(cur)
    return sections


def index_maintainers(store: Graph, repo: str, text: str) -> int:
    n = 0
    for i, s in enumerate(parse_maintainers(text)):
        for pattern in s["files"]:
            for name, email in s["maintainers"]:
                store.add_listing(repo, "maintainers", pattern, name, email, "maintainer", s["title"], ord=i)
                n += 1
            for name, email in s["reviewers"]:
                store.add_listing(repo, "maintainers", pattern, name, email, "reviewer", s["title"], ord=i)
                n += 1
        for pattern in s["excludes"]:
            store.add_listing(repo, "maintainers", pattern, "", "", "exclude", s["title"], ord=i)
    return n


def index_repo(store: Graph, repo_path: str | Path, max_commits: int | None = None,
               repo_name: str = "", rev: str = "", paths: list[str] | None = None) -> dict[str, int]:
    """Index a local git checkout into the graph: a full rebuild of what
    the repository contributes, idempotent, written in one transaction so
    a concurrent reader sees the old index or the new one, never the gap.
    max_commits: history depth; None uses the default cap, 0 means the
    whole history. rev: index the repository as it was at that commit
    (history up to it, the tree and CODEOWNERS at it) without touching
    the working copy; the same bound then applies to live lookups at ask
    time. paths: restrict the history and the tree to these path prefixes
    (one subsystem of a monorepo)."""
    repo = Path(repo_path).resolve()
    if not (repo / ".git").exists():
        raise RuntimeError(f"{repo} is not a git checkout")
    name = (repo_name or repo.name).lower()
    scope = [p.strip("/") for p in (paths or []) if p.strip("/")]
    depth = MAX_COMMITS if max_commits is None else max_commits
    snap = _read_repo(repo, rev, scope, depth)
    # Every git read is done before the write lock is taken, so the lock
    # is held for the write alone, not for the seconds git takes.
    with store.transaction():
        store.set_source(name, "git", str(repo))
        store.set_source(name, "git_rev", rev or "")
        store.set_source(name, "git_paths", " ".join(scope))
        return _write_index(store, name, depth, snap)


class _Snapshot:
    """What one ingest read from git: the history window, the tree and
    the listings at the bound revision."""

    def __init__(self, commits: list[Commit], truncated: bool, files: list[str], merge_log: list[Commit],
                 codeowner_rules: list[tuple[str, list[str]]], maintainers_text: str):
        self.commits = commits
        self.truncated = truncated
        self.files = files
        self.merge_log = merge_log
        self.codeowner_rules = codeowner_rules
        self.maintainers_text = maintainers_text


def _read_repo(repo: Path, rev: str, scope: list[str], depth: int) -> _Snapshot:
    head = rev or "HEAD"
    scope_args = ["--", *scope] if scope else []
    count_args = [] if depth == 0 else [f"--max-count={depth}"]
    # One commit past the window tells whether the history was cut.
    commits = read_log(repo, head, [] if depth == 0 else [f"--max-count={depth + 1}"], scope_args)
    truncated = bool(depth) and len(commits) > depth
    if truncated:
        commits = commits[:depth]
    if rev or scope:
        files = [f for f in _git(repo, "ls-tree", "-r", "--name-only", head, *scope).splitlines() if f]
    else:
        files = [f for f in _git(repo, "ls-files").splitlines() if f]
    merge_log = read_log(repo, head, count_args, scope_args, merges=True)
    return _Snapshot(commits, truncated, files, merge_log, parse_codeowners(repo, rev),
                     _read_at(repo, "MAINTAINERS", rev))


def _write_index(store: Graph, name: str, depth: int, snap: _Snapshot) -> dict[str, int]:
    commits, truncated, files = snap.commits, snap.truncated, snap.files
    window = f" (of the last {depth} commits)" if truncated else ""
    stats = {"files": 0, "commits": 0, "merges": 0, "owners": 0, "prs": 0,
             "truncated": depth if truncated else 0, "repo": name}
    touch: dict[str, int] = defaultdict(int)

    now = history_now(commits)
    store.set_source(name, "git_now", ts_to_iso(now))
    store.clear_changes(name)
    store.clear_artifacts(name)
    store.clear_fetched(name)
    author_touches: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    author_recent: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    review_counts: dict[str, int] = defaultdict(int)
    _plain_commits: list[tuple[str, str, str, str, str]] = []
    for c in commits:
        stats["commits"] += 1
        store.upsert_engineer(c.author, c.email)
        decay = _decay(c.date, now)
        for reviewer in _REVIEWED_BY.findall(c.body):
            review_counts[reviewer.strip()] += 1
        if not c.subject.startswith("Merge "):
            _plain_commits.append((c.sha, c.author, c.date, c.subject, c.body))
        record_change(store, name, c)
        for path in c.files:
            touch[path] += 1
            for prefix in _prefixes(path):
                author_touches[prefix][c.author] += 1
                author_recent[prefix][c.author] += decay

    for f in files:
        store.upsert_artifact(name, f, touch.get(f, 0))
        stats["files"] += 1

    merge_log = snap.merge_log
    integrators = _integrators(merge_log)
    stats["integrators"] = sorted(integrators)
    for m in merge_log:
        pr = _MERGE_PR.search(m.subject)
        ref = pr.group(1) if pr else m.sha[:12]
        store.upsert_intent(name, "merge", ref, m.subject.strip(), m.body.strip(), m.author, m.date)
        store.add_intent_paths(name, "merge", ref, m.files[:MAX_INTENT_PATHS])
        # Preserve the evidence. Routing discounts general integration
        # relative to area-specific merges and independent participation.
        record_change(store, name, m, roles=("merger",))
        stats["merges"] += 1

    squashed = 0
    files_by_sha = {c.sha: c.files for c in commits}
    for sha, author, date, subject, body in _plain_commits:
        if squashed >= MAX_SQUASH_INTENTS:
            break
        pr = _SQUASHED_PR.search(subject)
        meaningful = len(_body_without_trailers(body)) >= MIN_BODY_CHARS
        if not pr and not meaningful:
            continue
        kind = "pr" if pr else "commit"
        ref = pr.group(1) if pr else sha[:12]
        store.upsert_intent(name, kind, ref, subject.strip(), body.strip(), author, date)
        store.add_intent_paths(name, kind, ref, files_by_sha.get(sha, [])[:MAX_INTENT_PATHS])
        squashed += 1
        if pr:
            stats["prs"] += 1
    stats["squashed"] = squashed

    store.clear_listings(name)
    for i, (pattern, owners) in enumerate(snap.codeowner_rules):
        prefix = pattern.lstrip("/")
        if prefix.endswith("*"):
            prefix = prefix.rstrip("*")
        for owner in owners:
            store.set_ownership(name, prefix, owner, "codeowners", 1.0,
                                f"CODEOWNERS lists {owner} for {pattern}")
            store.add_listing(name, "codeowners", pattern, owner, "",
                              "team" if "/" in owner else "owner", ord=i)
            stats["owners"] += 1
    stats["maintainers"] = index_maintainers(store, name, snap.maintainers_text)

    dropped_bots = set()
    for bucket in (author_touches, author_recent):
        for prefix in list(bucket):
            for author in list(bucket[prefix]):
                if _is_machine_author(author):
                    dropped_bots.add(author)
                    del bucket[prefix][author]
    for reviewer in list(review_counts):
        if _is_machine_author(reviewer):
            dropped_bots.add(reviewer)
            del review_counts[reviewer]
    stats["bots_skipped"] = len(dropped_bots)

    for prefix, authors in author_touches.items():
        total = sum(authors.values())
        if total < 3:
            continue
        for author, count in authors.items():
            share = count / total
            if share >= 0.15:
                store.set_ownership(name, prefix, author, "blame", share,
                                    f"{round(share * 100)}% of the {total} commit touches under {prefix}{window}")
                stats["owners"] += 1

    for prefix, authors in author_recent.items():
        if sum(author_touches[prefix].values()) < 3:
            continue
        total_recent = sum(authors.values())
        if total_recent <= 0:
            continue
        for author, weight_sum in authors.items():
            share = weight_sum / total_recent
            if share >= 0.15:
                store.set_ownership(name, prefix, author, "blame_recent", share,
                                    f"{round(share * 100)}% of the recency-weighted commit touches under {prefix}")

    total_reviews = sum(review_counts.values())
    if total_reviews:
        for reviewer, count in review_counts.items():
            store.set_ownership(name, "", reviewer, "review", count / total_reviews,
                                f"named as reviewer on {count} of the {total_reviews} commits "
                                f"carrying a Reviewed-by trailer")

    # What GitHub told Raven about this repository (approvals on merged
    # pull requests, their descriptions) lives in its own tables and is
    # laid on top of every rebuild, so a re-ingest never loses it.
    from .github import apply_pulls
    stats["github"] = apply_pulls(store, name)
    store.append_event("index", {"repo": name, **stats})
    return stats


# ---------------- live git lookups at ask time (BRIDGE_LIVE=1) ----------------

def _git_source(store: Graph, repo: str) -> str:
    path = store.get_source(repo, "git")
    if path and Path(path).is_dir():
        return path
    return ""


def _git_rev(store: Graph, repo: str) -> str:
    """The point-in-time bound set at ingest (or by a caller), else HEAD.
    Every live lookup runs against this revision so nothing after it
    leaks into an answer."""
    return store.get_source(repo, "git_rev") or "HEAD"


def _git_quiet(repo: str, *args: str) -> str:
    out = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=60)
    return out.stdout if out.returncode == 0 else ""


def probe_git(store: Graph, repo: str, terms: list[str], hint: str) -> int:
    """git log --grep per focus term, plus the hinted path's recent history.
    Commits land as records (kind=commit)."""
    path = _git_source(store, repo)
    if not path:
        return 0
    n = 0
    seen: set[str] = set()

    def _harvest(raw: str) -> None:
        nonlocal n
        for chunk in raw.split("\x01"):
            if not chunk.strip():
                continue
            fields = chunk.split("\x02")
            if len(fields) < 5:
                continue
            sha, author, date, subject, body = fields[:5]
            ref = sha[:12]
            if ref in seen:
                continue
            seen.add(ref)
            store.upsert_intent(repo, "commit", ref, subject.strip(), body.strip()[:3000], author, date)
            n += 1

    fmt = "--pretty=format:%x01%H%x02%an%x02%aI%x02%s%x02%b"
    rev = _git_rev(store, repo)
    for t in terms[:4]:
        _harvest(_git_quiet(path, "log", rev, "-i", f"--grep={t}", f"--max-count={LIVE_RESULTS_PER_SOURCE}", fmt))
    if hint:
        _harvest(_git_quiet(path, "log", rev, f"--max-count={LIVE_RESULTS_PER_SOURCE}", fmt, "--", hint))
    return n


def _live_now(store: Graph, repo: str) -> float:
    iso = store.get_source(repo, "git_now")
    if iso:
        try:
            return datetime.datetime.fromisoformat(iso).timestamp()
        except ValueError:
            pass
    path = _git_source(store, repo)
    rev = _git_rev(store, repo)
    raw = _git_quiet(path, "log", rev, "-1", "--pretty=format:%cI") if path else ""
    try:
        now = datetime.datetime.fromisoformat(raw.strip()).timestamp()
    except ValueError:
        now = time.time()
    store.set_source(repo, "git_now", ts_to_iso(now))
    return now


def fetch_changes(store: Graph, repo: str, prefixes: list[str]) -> int:
    """Cold start: the changes under a few prefixes, read from git at ask
    time and written into the same tables ingestion fills, so the next
    question finds them warm. Bounded at the revision the source is
    pinned to."""
    path = _git_source(store, repo)
    if not path:
        return 0
    todo = [p for p in prefixes if p and not store.was_fetched(repo, "changes", p)]
    if not todo:
        return 0
    _live_now(store, repo)
    rev = _git_rev(store, repo)
    n = 0
    for prefix in todo:
        target = prefix.rstrip("/") if prefix.endswith("/") else prefix
        commits = read_log(path, rev, [f"--max-count={LIVE_TOUCH_COMMITS}"], ["--", target])
        for c in commits:
            record_change(store, repo, c)
            n += 1
        merges = read_log(path, rev, [f"--max-count={LIVE_TOUCH_COMMITS // 3}"], ["--", target], merges=True)
        for m in merges:
            record_change(store, repo, m, roles=("merger",))
        store.mark_fetched(repo, "changes", prefix)
    return n


def fetch_listings(store: Graph, repo: str) -> int:
    """Cold start: MAINTAINERS and CODEOWNERS as of the pinned revision."""
    path = _git_source(store, repo)
    if not path or store.was_fetched(repo, "listings", "all"):
        return 0
    rev = _git_rev(store, repo)
    n = index_maintainers(store, repo, _read_at(Path(path), "MAINTAINERS", rev))
    for i, (pattern, owners) in enumerate(parse_codeowners(Path(path), rev)):
        for owner in owners:
            store.add_listing(repo, "codeowners", pattern, owner, "", "team" if "/" in owner else "owner", ord=i)
            n += 1
    store.mark_fetched(repo, "listings", "all")
    return n


def fetch_tree(store: Graph, repo: str) -> int:
    """Cold start: the file list at the pinned revision, so a question's
    words can be mapped to paths before any history is read."""
    path = _git_source(store, repo)
    if not path or store.was_fetched(repo, "tree", "all"):
        return 0
    rev = _git_rev(store, repo)
    scope = [s for s in (store.get_source(repo, "git_paths") or "").split() if s]
    raw = _git_quiet(path, "ls-tree", "-r", "--name-only", rev, *scope)
    n = 0
    for line in raw.splitlines():
        f = line.strip()
        if f:
            store.upsert_artifact(repo, f)
            n += 1
    store.mark_fetched(repo, "tree", "all")
    return n


BLAME_MAX_FILES = 3
BLAME_TIMEOUT_SECONDS = 8


def _partial_clone(path: str) -> bool:
    """A blobless or treeless clone fetches every blob blame needs one at
    a time; blame there costs seconds per file, so it is skipped."""
    out = _git_quiet(path, "config", "--get", "remote.origin.promisor")
    return out.strip().lower() == "true"


def blame_counts(path: str, rev: str, file: str) -> dict[tuple[str, str], int]:
    """Lines per (author, email) of one file at one revision, from git
    blame; empty when the file is missing or blame runs out of time."""
    try:
        out = subprocess.run(["git", "-C", path, "blame", "--line-porcelain", rev, "--", file],
                             capture_output=True, text=True, errors="replace", timeout=BLAME_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        return {}
    if out.returncode != 0:
        return {}
    counts: dict[tuple[str, str], int] = defaultdict(int)
    name = ""
    for line in out.stdout.splitlines():
        if line.startswith("author "):
            name = line[7:].strip()
        elif line.startswith("author-mail "):
            email = line[12:].strip().strip("<>").lower()
            if name and not _is_machine_author(name):
                counts[(name, email)] += 1
    return dict(counts)


def fetch_blame(store: Graph, repo: str, files: list[str]) -> dict[str, dict[str, tuple[str, str, float]]]:
    """Line ownership of a few named files at the pinned revision, read
    from the checkout once and cached in the graph. Per file: the
    engineer's display name to (name, email, share of lines). Off with
    BRIDGE_BLAME=0; skipped on partial clones."""
    path = _git_source(store, repo)
    if not path or not files or os.environ.get("BRIDGE_BLAME", "1") == "0":
        return {}
    rev = _git_rev(store, repo)
    out: dict[str, dict[str, tuple[str, str, float]]] = {}
    partial: bool | None = None
    for f in files[:BLAME_MAX_FILES]:
        key = f"{rev}:{f}"
        rows = store.blame_for(repo, rev, f)
        if not rows and not store.was_fetched(repo, "blame", key):
            if partial is None:
                partial = _partial_clone(path)
            if partial:
                return {}
            store.add_blame(repo, rev, f, blame_counts(path, rev, f))
            store.mark_fetched(repo, "blame", key)
            rows = store.blame_for(repo, rev, f)
        total = sum(r["lines"] for r in rows)
        if total:
            out[f] = {r["engineer"]: (r["engineer"], r["email"], r["lines"] / total) for r in rows}
    return out


def lazy_blame(store: Graph, repo: str, hint: str) -> int:
    if not hint:
        return 0
    have = store.db.execute(
        "SELECT count(*) c FROM ownership WHERE repo=? AND path_prefix=? AND source IN ('blame','blame_recent')",
        (repo, hint)).fetchone()["c"]
    if have:
        return 0
    path = _git_source(store, repo)
    if not path:
        return 0
    raw = _git_quiet(path, "log", _git_rev(store, repo), f"--max-count={LAZY_BLAME_COMMITS}",
                     "--pretty=format:%an", "--", hint)
    counts: dict[str, int] = defaultdict(int)
    for line in raw.splitlines():
        if line.strip():
            counts[line.strip()] += 1
    total = sum(counts.values())
    if not total:
        return 0
    for eng, c in counts.items():
        share = c / total
        store.upsert_engineer(eng)
        store.set_ownership(repo, hint, eng, "blame", share,
                            f"{round(share * 100)}% of the last {total} commit touches under {hint} (computed live)")
    return len(counts)


def live_probe(store: Graph, repo: str, terms: list[str], hint: str,
               refs: list[str] | None = None) -> dict:
    """Run the local git source for one question, best-effort. Only git is
    ever queried here; the counts name the sources actually searched."""
    counts: dict = {"git": 0, "blame": 0}
    searched: list[str] = []
    failed: dict[str, str] = {}
    if _git_source(store, repo):
        searched.append("git history")
        try:
            counts["git"] = probe_git(store, repo, terms, hint)
        except Exception as e:
            failed.setdefault("git", f"{type(e).__name__}: {str(e)[:120]}")
        try:
            counts["blame"] = lazy_blame(store, repo, hint)
        except Exception:
            pass
    counts["sources"] = ", ".join(searched)
    counts["failed"] = "; ".join(f"{s} ({msg})" for s, msg in failed.items())
    return counts
