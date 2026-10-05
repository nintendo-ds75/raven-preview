"""Datasets for the replay bench. Ground truth comes from git alone:
trailers (Reviewed-by, Acked-by, Helped-by), the committer who accepted a
patch when it is not the author, the merger who authored a merge commit,
and the MAINTAINERS or CODEOWNERS listing for the touched paths as of the
parent revision.

Clones are cached under a scratch directory outside the repository
(BRIDGE_BENCH_SCRATCH, default ~/.cache/bridge-bench). Every sample is
deterministic for a given seed."""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .identity import Directory, Person, is_machine, learn_nix_maintainers, learn_readme, parse_person
from .listings import Codeowners, Maintainers, git_show

CATCH_ALL_COMMITTER_SHARE = 0.50
RELATED_WINDOW = 400
ROLE_TRAILERS = ("reviewed-by", "acked-by", "helped-by", "approved-by")


@dataclass
class Dataset:
    name: str
    url: str
    since: str            # clone bound (--shallow-since)
    sample_from: str      # earliest commit eligible as a replay item
    kind: str             # trailers | merges
    role: str             # tune | holdout
    listing: str = ""     # maintainers | codeowners | ""
    listing_path: str = ""
    scope: list[str] = field(default_factory=list)   # subsystem paths, empty is whole repo
    blobless: bool = False
    sparse: list[str] = field(default_factory=list)
    directory_sources: list[str] = field(default_factory=list)  # readme | nix-maintainers | noreply
    notes: str = ""

    @property
    def repo_key(self) -> str:
        return self.name


DATASETS: dict[str, Dataset] = {
    "qemu": Dataset(
        name="qemu", url="https://github.com/qemu/qemu.git", since="2023-06-01", sample_from="2024-03-01",
        kind="trailers", role="tune", listing="maintainers", listing_path="MAINTAINERS",
        notes="Reviewed-by and Acked-by trailers; the committer is the accepting subsystem maintainer; "
              "MAINTAINERS lists M: and R: per F: pattern."),
    "linux-drm": Dataset(
        name="linux-drm", url="https://github.com/torvalds/linux.git", since="2025-06-01", sample_from="2025-09-01",
        kind="trailers", role="tune", listing="maintainers", listing_path="MAINTAINERS",
        scope=["drivers/gpu/drm"], blobless=True, sparse=["drivers/gpu/drm", "MAINTAINERS"],
        notes="One subsystem of the kernel via sparse checkout; Reviewed-by and Acked-by trailers; the "
              "committer is the driver maintainer who applied the patch."),
    "node": Dataset(
        name="node", url="https://github.com/nodejs/node.git", since="2024-06-01", sample_from="2025-01-01",
        kind="trailers", role="tune", listing="codeowners", listing_path=".github/CODEOWNERS",
        directory_sources=["readme", "noreply"],
        notes="Reviewed-By trailers on every landed commit; CODEOWNERS names teams only; the lander is "
              "the committer when the commit was not squash-merged by GitHub."),
    "git": Dataset(
        name="git", url="https://github.com/git/git.git", since="2023-06-01", sample_from="2024-03-01",
        kind="trailers", role="holdout",
        notes="Reviewed-by, Acked-by and Helped-by trailers; one integrator commits nearly everything, so "
              "the committer is not a label here."),
    "nixpkgs-nixos": Dataset(
        name="nixpkgs-nixos", url="https://github.com/NixOS/nixpkgs.git", since="2026-03-01",
        sample_from="2026-05-15", kind="merges", role="holdout", listing="codeowners", listing_path="ci/OWNERS",
        scope=["nixos"], blobless=True, sparse=["nixos", "ci", "maintainers/maintainer-list.nix"],
        directory_sources=["nix-maintainers", "noreply"],
        notes="Merge commits are authored by the human who merged the PR, never the PR author; ci/OWNERS is a "
              "CODEOWNERS file; maintainer-list.nix maps handles to names."),
}


@dataclass
class Item:
    dataset: str
    idx: int
    sha: str
    base: str
    ts: str
    author: dict
    subject: str
    body: str
    files: list[str]
    strong: list[dict]
    listing: list[dict]
    teams: list[str]
    label_sources: dict
    listing_patterns: list[str]
    related: list[dict]


def scratch_dir() -> Path:
    env = os.environ.get("BRIDGE_BENCH_SCRATCH")
    base = Path(env) if env else Path.home() / ".cache" / "bridge-bench"
    base.mkdir(parents=True, exist_ok=True)
    return base


def clone_dir(ds: Dataset) -> Path:
    return scratch_dir() / "clones" / _clone_name(ds)


def _clone_name(ds: Dataset) -> str:
    return ds.url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


def git(repo: Path | str, *args: str, timeout: int = 600) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} failed: {out.stderr.strip()[:300]}")
    return out.stdout


def ensure_clone(ds: Dataset, log=print) -> Path:
    """Clone once into the scratch dir, shallow since the dataset bound,
    blobless and sparse for the big trees."""
    target = clone_dir(ds)
    if (target / ".git").exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    args = ["clone", "-q", f"--shallow-since={ds.since}", "--single-branch"]
    if ds.blobless:
        args += ["--filter=blob:none", "--no-checkout"]
    log(f"[{ds.name}] cloning {ds.url} (since {ds.since}) into {target}")
    subprocess.run(["git", *args, ds.url, str(target)], check=True, timeout=3600)
    if ds.sparse:
        git(target, "sparse-checkout", "set", "--no-cone", *ds.sparse)
        git(target, "checkout", "-q")
    return target


# ---------------- commit parsing ----------------

_TRAILER_RE = re.compile(r"^\s*([A-Za-z][A-Za-z-]*-[Bb]y)\s*:\s*(.+?)\s*$")
_ANY_TRAILER_RE = re.compile(
    r"^\s*(?:[A-Za-z][\w-]*-by|Signed-off-by|Refs?|Fixes|Closes|Link|Message-I[dD]|Change-Id|Cc|BugLink|"
    r"PR-URL|Reviewed-on|Bug|Buglink|Tested-on|See-also|Suggested|Reported)\s*:", re.IGNORECASE)


def split_trailers(body: str) -> tuple[str, dict[str, list[Person]]]:
    """Prose body without trailer lines, plus trailers grouped by tag."""
    prose: list[str] = []
    trailers: dict[str, list[Person]] = defaultdict(list)
    for line in (body or "").splitlines():
        m = _TRAILER_RE.match(line)
        if m:
            tag = m.group(1).lower()
            value = re.sub(r"\s*#\s*v?\d.*$", "", m.group(2))
            trailers[tag].append(parse_person(value))
            continue
        if _ANY_TRAILER_RE.match(line):
            continue
        prose.append(line.rstrip())
    text = "\n".join(prose).strip()
    return text, trailers


@dataclass
class Commit:
    sha: str
    parents: list[str]
    author: Person
    committer: Person
    ts: str
    subject: str
    body: str
    files: list[str]


# The committer date is T: when the change was accepted onto the tree,
# which is when its approval happened (an author date can be months older).
_LOG_FMT = "--pretty=format:%x01%H%x02%P%x02%an%x02%ae%x02%cn%x02%ce%x02%cI%x02%s%x02%b%x03"


def iter_log(repo: Path, *args: str) -> list[Commit]:
    raw = git(repo, "log", _LOG_FMT, "--name-only", *args)
    out: list[Commit] = []
    for chunk in raw.split("\x01"):
        if not chunk.strip():
            continue
        header, sep, tail = chunk.partition("\x03")
        if not sep:
            continue
        f = header.split("\x02")
        if len(f) < 9:
            continue
        sha, parents, an, ae, cn, ce, ts, subject, body = f[:9]
        files = [ln.strip() for ln in tail.splitlines() if ln.strip() and "\x02" not in ln]
        out.append(Commit(sha, parents.split(), Person(an, ae), Person(cn, ce), ts, subject.strip(), body, files))
    return out


# ---------------- listings and directories at a revision ----------------

class RevCache:
    """Per-dataset cache of listings and directories keyed by revision,
    since many items share a MAINTAINERS blob."""

    def __init__(self, ds: Dataset, repo: Path):
        self.ds, self.repo = ds, repo
        self._maint: dict[str, Maintainers] = {}
        self._own: dict[str, Codeowners] = {}
        self._dir: dict[str, Directory] = {}
        self._blob: dict[tuple[str, str], str] = {}

    def blob(self, rev: str, rel: str) -> str:
        key = (rev, rel)
        if key not in self._blob:
            self._blob[key] = git_show(str(self.repo), rev, rel)
        return self._blob[key]

    def directory(self, rev: str) -> Directory:
        if rev not in self._dir:
            d = Directory()
            if "readme" in self.ds.directory_sources:
                learn_readme(d, self.blob(rev, "README.md"))
            if "nix-maintainers" in self.ds.directory_sources:
                learn_nix_maintainers(d, self.blob(rev, "maintainers/maintainer-list.nix"))
            self._dir[rev] = d
        return self._dir[rev]

    def maintainers(self, rev: str) -> Maintainers | None:
        if self.ds.listing != "maintainers":
            return None
        if rev not in self._maint:
            self._maint[rev] = Maintainers(self.blob(rev, self.ds.listing_path))
        return self._maint[rev]

    def codeowners(self, rev: str) -> Codeowners | None:
        if self.ds.listing != "codeowners":
            return None
        if rev not in self._own:
            self._own[rev] = Codeowners(self.blob(rev, self.ds.listing_path), self.directory(rev))
        return self._own[rev]


def listing_for(cache: RevCache, rev: str, files: list[str]) -> tuple[list[Person], list[str], list[str], str]:
    """(humans listed, team handles, matched patterns, source name)."""
    m = cache.maintainers(rev)
    if m is not None:
        people, patterns = m.people_for(files)
        return people, [], patterns, "MAINTAINERS"
    c = cache.codeowners(rev)
    if c is not None:
        humans, teams, patterns = c.people_for(files)
        return humans, teams, patterns, "CODEOWNERS"
    return [], [], [], ""


# ---------------- related people (for the wrong-person class) ----------------

def related_people(repo: Path, base: str, files: list[str], scope: list[str]) -> list[dict]:
    """Everyone who authored, committed, or was named in a review trailer
    on the last RELATED_WINDOW commits touching the touched paths (their
    directories to depth 2), as of base. role and last activity kept."""
    # The touched files' own directories: hw/riscv/ for hw/riscv/virt.c,
    # never hw/, which would make half the project "related".
    prefixes: list[str] = []
    for f in files:
        d = f.rsplit("/", 1)[0] + "/" if "/" in f else f
        if d not in prefixes:
            prefixes.append(d)
    prefixes = prefixes[:8]
    if not prefixes:
        return []
    try:
        commits = iter_log(repo, base, f"--max-count={RELATED_WINDOW}", "--no-merges", "--", *prefixes)
    except RuntimeError:
        return []
    seen: dict[str, dict] = {}

    def note(p: Person, role: str, ts: str):
        if not p.name and not p.email:
            return
        if is_machine(p.name):
            return
        key = p.key
        row = seen.get(key)
        if row is None:
            seen[key] = {"name": p.name, "email": p.email, "roles": [role], "last_ts": ts, "count": 1}
        else:
            if role not in row["roles"]:
                row["roles"].append(role)
            row["count"] += 1
            if ts > row["last_ts"]:
                row["last_ts"] = ts

    for c in commits:
        note(c.author, "author", c.ts)
        if c.committer.key != c.author.key:
            note(c.committer, "committer", c.ts)
        _, trailers = split_trailers(c.body)
        for tag, people in trailers.items():
            if tag in ROLE_TRAILERS:
                for p in people:
                    note(p, tag, c.ts)
    # The humans who merged pull requests into these directories relate
    # to them too (the merger label of the CODEOWNERS datasets).
    try:
        merges = iter_log(repo, base, f"--max-count={RELATED_WINDOW // 2}", "--merges",
                          "--diff-merges=first-parent", "--", *prefixes)
    except RuntimeError:
        merges = []
    for m in merges:
        if re.search(r"\(#\d+\)\s*$", m.subject) or m.subject.startswith("Merge pull request #"):
            note(m.author, "merger", m.ts)
    return sorted(seen.values(), key=lambda r: (-r["count"], r["name"]))


# ---------------- sampling ----------------

def _stratified(rows: list, n: int, seed: int) -> list:
    """n rows spread over time: sort by timestamp, cut into n bins, one
    seeded pick per bin."""
    rows = sorted(rows, key=lambda r: (r.ts, r.sha))
    if len(rows) <= n:
        return rows
    rng = random.Random(seed)
    out = []
    for b in range(n):
        lo = (b * len(rows)) // n
        hi = max(lo + 1, ((b + 1) * len(rows)) // n)
        out.append(rows[rng.randrange(lo, hi)])
    return out


def _person_dict(p: Person) -> dict:
    return {"name": p.name, "email": p.email, "handle": p.handle}


def _dedupe(people: list[Person]) -> list[Person]:
    out: list[Person] = []
    for p in people:
        if not any(p.matches(q) for q in out):
            out.append(p)
    return out


def _since(commits: list[Commit], date: str) -> list[Commit]:
    """Commits at or after the date by committer date, filtered here
    rather than by git's --since, whose traversal cutoff depends on the
    clone's commit-graph state and would make the sample drift."""
    return [c for c in commits if c.ts[:10] >= date]


def catch_all_committers(commits: list[Commit]) -> set[str]:
    """The integrator who commits more than half of everything is not an
    approval label for any one change."""
    share = Counter(c.committer.key for c in commits)
    return {k for k, v in share.items() if v / max(1, len(commits)) > CATCH_ALL_COMMITTER_SHARE}


def approval_labels(c: Commit, catch_all: set[str]) -> tuple[str, list[tuple[str, Person]]]:
    """(prose body, [(tag, person)]) for the people git says approved a
    commit: review trailers not by the author, and the committer who
    accepted someone else's patch, unless they are the integrator."""
    prose, trailers = split_trailers(c.body)
    strong: list[tuple[str, Person]] = []
    for tag, people in trailers.items():
        if tag in ROLE_TRAILERS:
            for p in people:
                if not p.matches(c.author) and not is_machine(p.name):
                    strong.append((tag, p))
    if (c.committer.key not in catch_all and not c.committer.matches(c.author)
            and not is_machine(c.committer.name) and c.committer.name):
        strong.append(("committer", c.committer))
    return prose, strong


def github_labels(db_path: str, repo: str, sha: str) -> list[tuple[str, Person]]:
    """Approval labels from a Bridge database that synced the repository
    from GitHub (bridge sync): the reviewers who approved the pull request
    the commit merged and the person who merged it, by the name Bridge
    knows them by (the verified person, else GitHub's display name, else
    the login). The GitHub-flow dataset: squash merges carry no trailer,
    the API knows who approved."""
    import sqlite3
    try:
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT author, merged_by, reviews FROM gh_pulls WHERE repo=? AND merge_sha=?",
                         (repo.lower(), sha)).fetchone()
        if row is None:
            return []

        def name_of(login: str) -> str:
            person = db.execute("SELECT name FROM people WHERE lower(github_login)=?", (login.lower(),)).fetchone()
            if person and person["name"]:
                return person["name"]
            user = db.execute("SELECT name FROM gh_users WHERE login=?", (login.lower(),)).fetchone()
            return (user["name"] if user and user["name"] else login)
        out: list[tuple[str, Person]] = []
        seen: set[str] = set()
        for r in json.loads(row["reviews"] or "[]"):
            login = r.get("user") or ""
            if (r.get("state") or "").upper() != "APPROVED" or not login or login == row["author"] or login in seen:
                continue
            seen.add(login)
            out.append(("approved-by", Person(name_of(login), "")))
        merger = row["merged_by"] or ""
        if merger and merger != row["author"] and merger not in seen:
            out.append(("merged-by", Person(name_of(merger), "")))
        return [(t, p) for t, p in out if not is_machine(p.name)]
    except sqlite3.Error:
        return []


def sample_trailer_items(ds: Dataset, repo: Path, n: int, seed: int, log=print) -> list[Item]:
    scope_args = ["--", *ds.scope] if ds.scope else []
    commits = _since(iter_log(repo, "--no-merges", *scope_args), ds.sample_from)
    log(f"[{ds.name}] {len(commits)} non-merge commits since {ds.sample_from}")
    catch_all = catch_all_committers(commits)
    if catch_all:
        log(f"[{ds.name}] integrator excluded as a committer label: "
            + ", ".join(next(c.committer.name for c in commits if c.committer.key == k) for k in catch_all))
    github_db = os.environ.get("BRIDGE_BENCH_GITHUB_DB", "").strip()
    if github_db:
        log(f"[{ds.name}] approval labels also from the GitHub sync in {github_db}")
    eligible = []
    for c in commits:
        if not c.parents or not c.files or is_machine(c.author.name):
            continue
        prose, strong = approval_labels(c, catch_all)
        if github_db:
            strong = strong + [(t, p) for t, p in github_labels(github_db, ds.repo_key, c.sha)
                               if not any(p.matches(q) for _, q in strong)]
        if strong:
            eligible.append((c, prose, strong))
    log(f"[{ds.name}] {len(eligible)} commits carry an approval label")
    picked = _stratified([e[0] for e in eligible], n, seed)
    by_sha = {e[0].sha: e for e in eligible}
    cache = RevCache(ds, repo)
    items: list[Item] = []
    for i, c in enumerate(picked):
        _, prose, strong = by_sha[c.sha]
        base = c.parents[0]
        files = [f for f in c.files if not ds.scope or any(f.startswith(s.rstrip("/") + "/") for s in ds.scope)] or c.files
        listed, teams, patterns, source = listing_for(cache, base, files)
        directory = cache.directory(base)
        directory.learn_email(c.author.name, c.author.email)
        sources: dict[str, list[str]] = defaultdict(list)
        for tag, p in strong:
            sources[tag].append(p.label)
        if listed:
            sources[source] = [p.label for p in listed]
        if teams:
            sources[source + " teams"] = list(teams)
        items.append(Item(
            dataset=ds.name, idx=i, sha=c.sha, base=base, ts=c.ts, author=_person_dict(c.author),
            subject=c.subject, body=prose[:1500], files=files[:40],
            strong=[_person_dict(p) for p in _dedupe([p for _, p in strong])],
            listing=[_person_dict(p) for p in listed], teams=teams, label_sources=dict(sources),
            listing_patterns=patterns, related=related_people(repo, base, files, ds.scope)))
        if (i + 1) % 25 == 0:
            log(f"[{ds.name}] labeled {i + 1}/{len(picked)}")
    return items


def sample_merge_items(ds: Dataset, repo: Path, n: int, seed: int, log=print) -> list[Item]:
    # --diff-merges=first-parent makes --name-only list what the merge
    # brought onto the main line. A shallow clone does not keep the main
    # line's first-parent chain intact, so every merge is walked and the
    # pull-request merges are recognized by their subject.
    scope_args = ["--", *ds.scope] if ds.scope else []
    merges = _since(iter_log(repo, "--merges", "--diff-merges=first-parent", *scope_args), ds.sample_from)
    log(f"[{ds.name}] {len(merges)} merges since {ds.sample_from}")
    eligible = []
    for m in merges:
        if len(m.parents) < 2 or is_machine(m.author.name) or not m.author.name:
            continue
        if not (re.search(r"\(#\d+\)\s*$", m.subject) or m.subject.startswith("Merge pull request #")):
            continue
        files = [f for f in m.files if any(f.startswith(s.rstrip("/") + "/") for s in ds.scope)] if ds.scope else m.files
        if not files:
            continue
        eligible.append((m, files))
    log(f"[{ds.name}] {len(eligible)} merges touch {', '.join(ds.scope) or 'the repo'}")
    picked = _stratified([e[0] for e in eligible], n * 2, seed)
    files_by = {e[0].sha: e[1] for e in eligible}
    cache = RevCache(ds, repo)
    items: list[Item] = []
    for m in picked:
        if len(items) >= n:
            break
        base, head = m.parents[0], m.parents[1]
        try:
            prs = iter_log(repo, f"{base}..{head}", "--no-merges", "--max-count=12")
        except RuntimeError:
            continue
        prs = [c for c in prs if not is_machine(c.author.name)]
        if not prs:
            continue
        first = prs[-1]  # oldest commit of the PR
        if m.author.matches(first.author) or any(m.author.matches(c.author) for c in prs):
            continue
        files = files_by[m.sha]
        prose, _ = split_trailers(first.body)
        listed, teams, patterns, source = listing_for(cache, base, files)
        strong = [m.author]
        sources = {"merger": [m.author.label]}
        if listed:
            sources[source] = [p.label for p in listed]
        if teams:
            sources[source + " teams"] = list(teams)
        subject = first.subject if len(prs) == 1 else re.sub(r"\s*\(#\d+\)\s*$", "", m.subject) or first.subject
        items.append(Item(
            dataset=ds.name, idx=len(items), sha=m.sha, base=base, ts=m.ts, author=_person_dict(first.author),
            subject=subject, body=prose[:1500], files=files[:40],
            strong=[_person_dict(p) for p in strong], listing=[_person_dict(p) for p in listed], teams=teams,
            label_sources=sources, listing_patterns=patterns,
            related=related_people(repo, base, files, ds.scope)))
        if len(items) % 25 == 0:
            log(f"[{ds.name}] labeled {len(items)}/{n}")
    return items


def build_items(ds: Dataset, n: int, seed: int, log=print, force: bool = False) -> list[Item]:
    """Sampled and labeled replay items, cached as JSON in the scratch dir."""
    repo = ensure_clone(ds, log)
    cache_file = scratch_dir() / "items" / f"{ds.name}-n{n}-s{seed}-v4.json"
    if cache_file.exists() and not force:
        rows = json.loads(cache_file.read_text())
        return [Item(**r) for r in rows]
    if ds.kind == "merges":
        items = sample_merge_items(ds, repo, n, seed, log)
    else:
        items = sample_trailer_items(ds, repo, n, seed, log)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps([asdict(i) for i in items], indent=1, ensure_ascii=False))
    return items
