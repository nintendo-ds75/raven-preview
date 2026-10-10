"""Who-to-ask routing from the shared people-to-paths structure.

One structure (changes, the paths they touched, the people on them with
their roles) serves the question to owner and the question to record
paths alike. For a question the
resolver maps it to paths; under each path the signals are summed at the
deepest level that has enough history: approvals (review trailers, the
accepting committer, the merger) outrank raw authorship, recency weighs
in, listed maintainers and code owners count, bots and teams are never
candidates, and someone inactive for a year is not routed to. Cold start
reads the same signals from git at ask time and writes them into the
same table. The evidence names the signal, never a score. When nothing
relates the question to a person, the answer is that Raven does not
know, with the reason.
"""

from __future__ import annotations

import datetime
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

from .graph import Graph
from .ingest import _is_machine_author
from .resolve import PathHit, resolve_paths

MIN_SCORE = 0.30
APPROVAL_LEADER_SHARE = 0.30
MIN_SCOPE_CHANGES = 4
INACTIVE_DAYS = 365
CO_OWNER_MARGIN = 0.05
LIVE_MAJORITY = 0.50
# Someone who merges this share of everything in the repository merges as
# a job, whatever area a change is in; a listed owner holding this share
# of an area's recent changes is active there.
BROAD_MERGER_SHARE = 0.25
LISTED_ACTIVE_SHARE = 0.15
GATEKEEPER_SPECIALIZATION = 0.5
APPROVAL_ROLES = ("reviewed-by", "acked-by", "helped-by", "approved-by", "committer", "merger", "merged-by")
_W = {"user": 1.5, "maintainer": 0.50, "owner": 0.50, "reviewer": 0.30, "approval": 0.70, "author": 0.50,
      "author_peer": 0.75, "affinity": 0.35, "affinity_repo": 0.20, "blame": 0.15,
      # A verified authority (the organization said who decides or
      # approves this scope) outranks every inferred signal; someone the
      # organization says merely knows the area is a strong candidate.
      "authority": 2.0, "knows": 0.5}
# How many people have to share one area's authority before naming one of
# them is a placement rather than a finding. A squad of six is a real
# answer; a CODEOWNERS team expanded into a hundred is not.
SHARED_AUTHORITY_MANY = 12
# The first level with enough history anchors a hit at full weight; the
# levels above it speak at half and a quarter; a thinner level below
# it (the very file, with a change or two) adds specificity at half
# weight, discounted by how thin it is.
LEVEL_WEIGHTS = (1.0, 0.5, 0.25)
THIN_LEVEL_WEIGHT = 0.5
# Affinity needs a few of the requester's changes before it counts fully.
AFFINITY_FULL_CHANGES = 3


def signals_available(store: Graph, repo: str) -> bool:
    if store.has_changes(repo):
        return True
    from .config import load
    from .ingest import _git_source
    return bool(load().live_retrieval and _git_source(store, repo))


def _norm_person(name: str) -> str:
    return " ".join((name or "").strip().lstrip("@").lower().split())


_NOREPLY_LOGIN = re.compile(r"^(?:\d+\+)?([A-Za-z0-9-]+)@users\.noreply\.github\.com$", re.IGNORECASE)


def _resolve_handle(person: str, display: dict[str, str], logins=None, emails=None) -> str:
    """Join a listing handle to one unambiguous contributor, never grant authority."""
    key = _norm_person(person)
    if " " in key or key in display:
        return key
    login = key.lstrip("@").lower()
    if logins and login in logins:
        return logins[login]
    matched = [k for k, addresses in (emails or {}).items()
               if any(a.lower().split("@", 1)[0] == login for a in addresses)]
    if len(matched) == 1:
        return matched[0]
    matches = [k for k in display if k.split()[0] == key or k.replace(" ", "") == key]
    if len(matches) == 1:
        return matches[0]
    first = re.split(r"[-_.\d]", login, 1)[0]
    if first and first != login:
        starts = [k for k in display if k.split()[0] == first]
        if len(starts) == 1:
            return starts[0]
    # First initial and surname (@wchen, @wchen-acme), as many logins are
    # spelled: exactly one person it can stand for. Measured live: the
    # CODEOWNERS handle for billing/ stayed a stranger beside the Slack
    # contact who wrote every change there.
    if first and len(first) >= 4:
        initials = [k for k in display if len(k.split()) >= 2 and k.split()[0][0] + k.split()[-1] == first]
        if len(initials) == 1:
            return initials[0]
    return key


@dataclass
class Candidate:
    name: str
    score: float = 0.0
    approval: float = 0.0       # share of approval signal in scope
    author: float = 0.0         # share of authorship in scope
    best: float = 0.0           # the strongest per-path signal, whose lines and scope are kept
    per_hit: list[float] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    scope: str = ""
    last_ts: str = ""
    verified: float = 0.0       # the weight of the authority the organization verified, if any
    verified_role: str = ""     # "decides" or "approves" when that authority is a strong one
    topic: str = ""             # the category this decision is about that they verifiably decide
    primary: bool = False       # verified decider of the agent's primary decision path
    merge_led: bool = False     # their approval here comes mostly from merging


def _now(store: Graph, repo: str) -> float:
    iso = store.get_source(repo, "git_now")
    try:
        return datetime.datetime.fromisoformat(iso).timestamp() if iso else 0.0
    except ValueError:
        return 0.0


def _levels(hit: PathHit) -> list[str]:
    parts = hit.path.rstrip("/").split("/")
    out = [hit.path]
    for k in range(len(parts) - 1, 0, -1):
        out.append("/".join(parts[:k]) + "/")
    return out


# ---------------- listings ----------------

def _pattern_re(pattern: str, kind: str) -> re.Pattern:
    pat = pattern.strip()
    if kind == "maintainers":
        if pat.endswith("/"):
            body = re.escape(pat).replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
            return re.compile("^" + body + ".*$")
        body = re.escape(pat).replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
        return re.compile("^" + body + "$")
    anchored = pat.startswith("/")
    pat = pat.lstrip("/")
    dir_only = pat.endswith("/")
    pat = pat.rstrip("/")
    parts = []
    i = 0
    while i < len(pat):
        if pat.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
            continue
        if pat.startswith("**", i):
            parts.append(".*")
            i += 2
            continue
        c = pat[i]
        parts.append("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c))
        i += 1
    prefix = "^" if (anchored or "/" in pat) else "^(?:.*/)?"
    suffix = "/.*$" if dir_only else "(?:/.*)?$"
    return re.compile(prefix + "".join(parts) + suffix)


def _catch_all(pattern: str) -> bool:
    return pattern.strip("/").replace("*", "") == ""


def _listing_index(store: Graph, repo: str) -> tuple[list[tuple], list[tuple]]:
    """The MAINTAINERS rows and the CODEOWNERS rows in file order, each
    with its pattern compiled once per distinct (kind, pattern), kept
    per repo generation."""
    def build(rows):
        compiled: dict[tuple[str, str], re.Pattern | None] = {}
        maint: list[tuple] = []
        owners: list[tuple] = []
        for r in rows:
            key = (r["kind"], r["pattern"])
            if key not in compiled:
                try:
                    compiled[key] = _pattern_re(r["pattern"], r["kind"])
                except re.error:
                    compiled[key] = None
            rx = compiled[key]
            if rx is None:
                continue
            row = (r["kind"], r["pattern"], rx, r["person"], r["email"], r["role"], r["section"], r["ord"])
            (maint if r["kind"] == "maintainers" else owners).append(row)
        return maint, owners
    return store.memo(repo, "listings", (), lambda: store.listings(repo), build)


def listed_for(store: Graph, repo: str, path: str) -> list[dict]:
    """Listing entries covering a path. MAINTAINERS: every matching
    section, minus the sections whose own X: line excludes the path.
    CODEOWNERS: the last matching rule in file order."""
    probe = path.rstrip("/") if path.endswith("/") else path
    out: list[dict] = []
    maint, owners = _listing_index(store, repo)
    excluded_sections = {c[6] for c in maint if c[5] == "exclude" and c[2].match(probe)}
    for kind, pattern, rx, person, email, role, section, _ord in maint:
        if role == "exclude" or not rx.match(probe) and not (path.endswith("/") and rx.match(probe + "/x")):
            continue
        if section in excluded_sections:
            continue
        out.append({"kind": kind, "pattern": pattern, "person": person, "email": email, "role": role,
                    "section": section, "catch_all": _catch_all(pattern)})
    last: tuple[int, str] | None = None
    for kind, pattern, rx, person, email, role, section, ord_ in owners:
        if (rx.match(probe) or (path.endswith("/") and rx.match(probe + "/x"))) and (last is None or ord_ >= last[0]):
            last = (ord_, pattern)
    if last is not None:
        for kind, pattern, rx, person, email, role, section, ord_ in owners:
            if ord_ == last[0] and pattern == last[1]:
                out.append({"kind": kind, "pattern": pattern, "person": person, "email": email, "role": role,
                            "section": section, "catch_all": _catch_all(pattern)})
    return out


# ---------------- the route ----------------

SWEEP_CAP_FILES = 20   # a change over more files than this counts as a fraction of a change
# A change whose subject says it only ran a linter, formatter, type or
# spelling tool over the code counts as a fraction of a change: it says who
# maintains the tooling, not who decides what the code does. Measured on
# pypa/packaging: four "Apply ruff rules" / codespell commits (19 of 804
# lines of metadata.py) outranked the author of 701 of its lines.
MECHANICAL_WEIGHT = 0.25
_MECHANICAL = re.compile(
    r"\b(?:ruff|flake8|pyupgrade|isort|codespell|pre-commit|autoupdate|linters?|linting|lint|reformat(?:ted|ting)?|"
    r"whitespace|mypy|pyright|type annotations?|typos?|spelling|eslint|prettier|gofmt|clang-format|rustfmt)\b",
    re.IGNORECASE)


def _change_weight(head, now: float) -> float:
    """How much one change counts: recency, a cap for sweeps over many
    files, and a fraction for tooling-only changes."""
    w = min(1.0, SWEEP_CAP_FILES / max(1, head["nfiles"])) * _decay(head["ts"], now)
    subject = head["subject"] if "subject" in head.keys() else ""
    return w * MECHANICAL_WEIGHT if subject and _MECHANICAL.search(subject) else w
HALF_LIFE_DAYS = 180.0


def _decay(ts: str, now: float) -> float:
    if not now:
        return 1.0
    try:
        age = max(0.0, (now - datetime.datetime.fromisoformat(ts).timestamp()) / 86400.0)
    except (TypeError, ValueError):
        return 0.5
    return math.exp(-age * math.log(2) / HALF_LIFE_DAYS)


def requester_keys(requester: str, store: Graph | None = None, repo: str = "") -> tuple[set[str], set[str]] | None:
    """The person asking, as every normalized name and lowercase email
    that identifies them in the changes table; None when nobody is
    named. The people table adds the person's aliases and identities,
    and a bare first name resolves to the one engineer of the repository
    it starts, so 'Priya' is Priya Natarajan and is never asked her own
    question."""
    from .identity import first_name_match, norm_handle
    from .ingest import _split_addr
    name, email = _split_addr(requester or "")
    if not email and "@" in name and " " not in name:
        name, email = "", name
    names = {_norm_person(name)} if name else set()
    emails = {email.lower()} if email else set()
    if store is not None:
        person = store.find_person(requester or "") or (store.find_person(email) if email else None) \
            or (store.find_person(name) if name else None)
        if person is not None:
            names.add(_norm_person(person["name"]))
            if person.get("email"):
                emails.add(person["email"].lower())
            if person.get("github_login"):
                names.add(norm_handle(person["github_login"]))
            for alias in person.get("aliases") or []:
                an, ae = _split_addr(alias)
                if ae:
                    emails.add(ae.lower())
                elif "@" in an and " " not in an:
                    emails.add(an.lower())
                elif an:
                    names.add(_norm_person(an))
        if name and " " not in name.strip() and repo:
            known = sorted({k for k in store.last_activity(repo)})
            full = first_name_match(name, known)
            if full != name:
                names.add(_norm_person(full))
    return (names, emails) if names or emails else None


def _is_requester(name: str, email: str, req: tuple[set[str], set[str]] | None) -> bool:
    if not req:
        return False
    names, emails = req
    return _norm_person(name) in names or bool(email) and email.lower() in emails


MERGE_BASE_WEIGHT = 0.25
MERGE_CORROBORATION_SHARE = 0.25


def _merge_distribution(rows: list, now: float) -> tuple[float, dict[str, float]]:
    """Recency/sweep-weighted share of merge events per person, not all commits."""
    events: dict[str, tuple[float, set[str]]] = {}
    for row in rows:
        if row["role"] != "merger" or _is_machine_author(row["engineer"]):
            continue
        if row["sha"] not in events:
            weight = min(1.0, SWEEP_CAP_FILES / max(1, row["nfiles"])) * _decay(row["ts"], now)
            events[row["sha"]] = (weight, set())
        events[row["sha"]][1].add(_norm_person(row["engineer"]))
    counts: dict[str, float] = defaultdict(float)
    for weight, people in events.values():
        for person in people:
            counts[person] += weight
    return sum(weight for weight, _ in events.values()), dict(counts)


def _merge_weights(rows: list, now: float, global_distribution: tuple) -> dict[str, float]:
    """General merging is weak evidence. Area concentration or independent
    authorship/review restores its weight. Never discard the underlying rows."""
    global_total, global_counts = global_distribution
    local_total, local_counts = _merge_distribution(rows, now)
    # A merge is not independent corroboration of itself. Count each person
    # once per non-merge change even if they authored and reviewed it.
    merge_shas = {r["sha"] for r in rows if r["role"] == "merger"}
    independent: dict[str, tuple[float, set[str]]] = {}
    for row in rows:
        if (row["sha"] in merge_shas or row["role"] not in ("author", *APPROVAL_ROLES)
                or _is_machine_author(row["engineer"])):
            continue
        if row["sha"] not in independent:
            weight = min(1.0, SWEEP_CAP_FILES / max(1, row["nfiles"])) * _decay(row["ts"], now)
            independent[row["sha"]] = (weight, set())
        independent[row["sha"]][1].add(_norm_person(row["engineer"]))
    independent_total = sum(w for w, _ in independent.values())
    participation: dict[str, float] = defaultdict(float)
    for weight, people in independent.values():
        for person in people:
            participation[person] += weight
    factors = {}
    for person, count in local_counts.items():
        local_share = count / local_total if local_total else 0.0
        global_share = global_counts.get(person, 0.0) / global_total if global_total else 0.0
        specialization = max(0.0, local_share - global_share) / max(1e-9, 1.0 - global_share)
        corroboration = participation[person] / independent_total if independent_total else 0.0
        support = min(1.0, max(specialization, corroboration / MERGE_CORROBORATION_SHARE))
        factors[person] = MERGE_BASE_WEIGHT + (1.0 - MERGE_BASE_WEIGHT) * support
    return factors


def _scope_aggregate(store: Graph, repo: str, scope: str, now: float) -> dict:
    """The requester-independent part of _scope_signals, read and decoded
    once per repo generation: the shares, counts, roles, names and last
    activity per person, plus per change its weight, its authors and the
    people who approved it, which is what affinity needs."""
    global_distribution = store.memo(
        repo, "merge_distribution", (now,), lambda: store.changes_under(repo, ""),
        lambda all_rows: _merge_distribution(all_rows, now))
    # A write in another area changes specialization even if this scope's
    # rows do not change (including writes from another DB connection).
    merge_key = (global_distribution[0], tuple(sorted(global_distribution[1].items())))

    def build(rows):
        merge_weights = _merge_weights(rows, now, global_distribution)
        by_sha: dict[str, list] = defaultdict(list)
        for r in rows:
            by_sha[r["sha"]].append(r)
        approval: dict[str, float] = defaultdict(float)
        merge_approval: dict[str, float] = defaultdict(float)
        author: dict[str, float] = defaultdict(float)
        app_n: dict[str, set] = defaultdict(set)
        auth_n: dict[str, set] = defaultdict(set)
        roles: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        last: dict[str, str] = {}
        display: dict[str, str] = {}
        emails: dict[str, str] = {}
        # A GitHub noreply address names its login outright: the join
        # between a CODEOWNERS handle and the name commits carry, before
        # GitHub is synced. Measured on home-assistant/core, where most
        # code owners commit under a full name their handle does not spell.
        logins: dict[str, str] = {}
        emails_seen = {}
        norm: dict[str, str] = {}
        changes: list[tuple[str, float, list[tuple[str, str]], dict[str, float]]] = []
        total = merge_total = 0.0
        for sha, group in by_sha.items():
            head = group[0]
            w = _change_weight(head, now)
            merge_event = any(r["role"] == "merger" for r in group)
            if merge_event:
                merge_total += w
            else:
                total += w
            authors = [(r["engineer"], r["email"]) for r in group if r["role"] == "author"]
            approvers: dict[str, float] = {}
            for r in group:
                name = r["engineer"]
                if name not in norm:
                    # Rows written before record_change kept machines out.
                    norm[name] = "" if _is_machine_author(name) else _norm_person(name)
                key = norm[name]
                if not key:
                    continue
                display.setdefault(key, name)
                if r["email"] and key not in emails:
                    emails[key] = r["email"].lower()
                noreply = _NOREPLY_LOGIN.match(r["email"] or "")
                if noreply:
                    logins.setdefault(noreply.group(1).lower(), key)
                if r["role"] == "author":
                    author[key] += w
                    auth_n[key].add(sha)
                elif r["role"] in APPROVAL_ROLES:
                    factor = merge_weights.get(key, MERGE_BASE_WEIGHT) if r["role"] == "merger" else 1.0
                    # Multiple approval roles on one change are one signal.
                    previous = approvers.get(key, 0.0)
                    approvers[key] = max(previous, factor)
                    target = merge_approval if merge_event else approval
                    target[key] += w * (approvers[key] - previous)
                    app_n[key].add(sha)
                else:
                    continue
                roles[key][r["role"]] += 1
                if r["ts"] > last.get(key, ""):
                    last[key] = r["ts"]
            changes.append((sha, w, authors, approvers))
        # Commits and their merge commits often describe the same work.
        # Normalize those evidence streams separately: retaining a merge
        # must not dilute authorship/review shares or count an approval twice.
        # Use the stronger approval stream, with merge weights already applied.
        total = total or merge_total
        if merge_total:
            for key, weight in merge_approval.items():
                approval[key] = max(approval[key], weight / merge_total * total)
        return {"approval": approval, "author": author, "roles": roles, "last": last, "display": display,
                "merge_weights": merge_weights, "logins": logins,
                "emails": emails, "total": total, "n_changes": len(by_sha), "changes": changes,
                "app_n": {k: len(v) for k, v in app_n.items()}, "auth_n": {k: len(v) for k, v in auth_n.items()}}
    return store.memo(repo, "scope", (scope, now, merge_key), lambda: store.changes_under(repo, scope), build)


def _scope_signals(store: Graph, repo: str, scope: str, now: float,
                   req: tuple[set[str], set[str]] | None = None) -> dict:
    """Per person under a prefix: the recency-weighted share of changes
    they authored and the share they reviewed, accepted, or merged; the
    distinct change counts behind those shares; last activity. With a
    requester named, also the share of the requester's own changes in
    scope that each person approved (affinity), computed here on top of
    the cached aggregate and never cached across requesters."""
    agg = _scope_aggregate(store, repo, scope, now)
    aff: dict[str, float] = defaultdict(float)
    aff_n: dict[str, set] = defaultdict(set)
    req_total = 0.0
    req_n = 0
    if req:
        asked: dict[tuple[str, str], bool] = {}
        for sha, w, authors, approvers in agg["changes"]:
            for who in authors:
                if who not in asked:
                    asked[who] = _is_requester(who[0], who[1], req)
            if not any(asked[who] for who in authors):
                continue
            req_total += w
            req_n += 1
            for key in approvers:
                aff[key] += w * approvers[key]
                aff_n[key].add(sha)
    # Fresh views of the shared tables: the caller indexes them as
    # defaultdicts, which would write missing keys into the cache.
    return {"approval": defaultdict(float, agg["approval"]), "author": defaultdict(float, agg["author"]),
            "merge_weights": agg["merge_weights"],
            "aff": aff, "roles": defaultdict(lambda: defaultdict(int), agg["roles"]), "last": agg["last"],
            "display": agg["display"], "emails": agg["emails"], "logins": agg.get("logins", {}),
            "total": agg["total"],
            "n_changes": agg["n_changes"], "app_n": agg["app_n"], "auth_n": agg["auth_n"],
            "aff_n": {k: len(v) for k, v in aff_n.items()}, "req_total": req_total, "req_n": req_n}


def _active_since(store: Graph, repo: str) -> dict[str, str]:
    return store.memo(repo, "active", (), lambda: sorted(store.last_activity(repo).items()),
                      lambda rows: {_norm_person(k): v for k, v in rows})


def _months_ago(ts: str, now: float) -> int:
    try:
        t = datetime.datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return 0
    return int(max(0.0, now - t) / (30 * 86400))


def _pct(x: float) -> str:
    return f"{round(100 * x)}%"


def _role_summary(roles: dict[str, int]) -> str:
    names = {"reviewed-by": "Reviewed-by", "acked-by": "Acked-by", "helped-by": "Helped-by",
             "approved-by": "Approved-by", "committer": "as committer", "merger": "as merger",
             "merged-by": "merged the pull request"}
    parts = [f"{names[r]} on {c}" for r, c in sorted(roles.items(), key=lambda kv: -kv[1])
             if r in names and c >= 1]
    return ", ".join(parts[:3])


REPO_WIDE_MAJORITY = 0.50
REPO_WIDE_MIN_CHANGES = 10


def _repo_wide(store: Graph, repo: str, now: float,
               req: tuple[set[str], set[str]] | None = None) -> tuple[str, list[str], float] | None:
    """A question that names no area, in a repository one person clearly
    carries: routed to them with the evidence saying exactly that. In a
    repository nobody dominates, the honest answer is that Raven does
    not know."""
    sig = _scope_signals(store, repo, "", now, req)
    if sig["n_changes"] < REPO_WIDE_MIN_CHANGES or sig["total"] <= 0:
        return None
    last_active = _active_since(store, repo)
    approved = sum(sig["approval"].values()) / sig["total"]
    best, best_share = "", 0.0
    for key in set(sig["author"]) | set(sig["approval"]):
        ts = last_active.get(key, "")
        if now and ts and _months_ago(ts, now) * 30 > INACTIVE_DAYS:
            continue
        if _is_requester(sig["display"].get(key, key), sig["emails"].get(key, ""), req):
            continue
        # Authorship carries a repository only where nobody else approves
        # the work; where reviews are recorded, only an approval majority does.
        share = sig["approval"][key] / sig["total"]
        if approved < 0.25:
            share = max(share, sig["author"][key] / sig["total"])
        if share > best_share:
            best, best_share = key, share
    if not best or best_share < REPO_WIDE_MAJORITY:
        return None
    name = sig["display"][best]
    kind = "authored" if sig["author"][best] >= sig["approval"][best] else "reviewed or accepted"
    n = sig["auth_n"].get(best, 0) if kind == "authored" else sig["app_n"].get(best, 0)
    line = (f"{kind} {n} of the last {sig['n_changes']} changes repo-wide ({_pct(best_share)} recency weighted); "
            f"the question names no specific area, so this is a repo-wide call")
    return (store.resolve_engineer(name), [line], round(_W["author"] * best_share, 3))


def _specificity(scope: str) -> tuple[int, int]:
    """How narrow a path scope is: deeper first, then longer. Used to let
    the narrowest rule that covers a file be the one that owns it."""
    scope = (scope or "").strip().strip("/")
    return (scope.count("/"), len(scope))


def _authority_matches(store: Graph, repo: str, hits: list[PathHit], question: str, context: str,
                       category: str, path: str = "", also_paths: list[str] | None = None) -> list[dict]:
    """The verified authority rows that cover this decision: repo-wide
    rows, path rows whose pattern matches a resolved path (or the file
    the agent named, tree or no tree), and category rows whose category
    the question falls under. Each match names a person (a team's
    members each), the role, the weight it carries and the evidence line
    that says so."""
    from .scopes import decision_scopes, primary_scopes
    rows = store.authority_rows(repo)
    if not rows:
        return []
    scopes = decision_scopes(question, context, category)
    # A category row carries its full weight for what the decision is
    # about. One it only mentions makes the holder a candidate, never the
    # verified owner: the owner may answer, so a word must not decide who
    # that is. Measured live: a security question that said "docs" went to
    # whoever decides docs.
    about = set(primary_scopes(question, category))
    probes = [(h.path, h.is_dir) for h in hits]
    # Every path the decision names, not only the first: measured live on
    # eb9d22d, a decision about connectionpool.py and util/retry.py went
    # to the team listed for the first file, past the person the map says
    # decides the second.
    for named in [path, *(also_paths or [])]:
        named = (named or "").strip().lstrip("/")
        if named and named != "unknown" and all(p != named for p, _ in probes):
            probes.append((named, named.endswith("/")))
    # Which path rows match which file, and how specific each one is.
    # CODEOWNERS' own rule is that the narrowest matching pattern owns the
    # file: a team listed for pkg/ does not also decide for the directory
    # inside it that another team is listed for.
    hit_paths: dict[str, list[str]] = {}
    for r in rows:
        if r["scope_kind"] != "path":
            continue
        try:
            rx = _pattern_re(r["scope"], "codeowners")
        except re.error:
            continue
        for p, is_dir in probes:
            probe = p.rstrip("/")
            if rx.match(probe) or (is_dir and rx.match(probe + "/x")):
                hit_paths.setdefault(r["id"], []).append(p)
    # Narrowest per role: a narrower rule for another role does not
    # outrank this one. Someone who must approve one file does not decide
    # it. Measured live on eb9d22d: Theo must approve util/retry.py, Mira
    # decides util/*, and five of seven questions about retry.py went to
    # Theo, with Mira's rule read as outranked.
    narrowest: dict[tuple[str, str], tuple[int, int]] = {}
    for rid, paths in hit_paths.items():
        row = next(r for r in rows if r["id"] == rid)
        for p in paths:
            key = (p, row["role"])
            narrowest[key] = max(narrowest.get(key, (0, 0)), _specificity(row["scope"]))

    out: list[dict] = []
    for r in rows:
        why, outranked = "", False
        if r["scope_kind"] == "repo":
            why = "repository-wide"
        elif r["scope_kind"] == "path":
            matched = hit_paths.get(r["id"], [])
            own = [p for p in matched if _specificity(r["scope"]) >= narrowest[(p, r["role"])]]
            if own:
                why = f"for {r['scope']} (matches {own[0]})"
            elif matched:
                # A rule for the directory above. The person knows the
                # area, and does not outrank whoever the narrower rule
                # names for this file.
                why = f"for {r['scope']} (matches {matched[0]}, where a more specific rule for the same role applies)"
                outranked = True
        elif r["scope_kind"] == "category" and r["scope"] in about:
            why = f"for {r['scope']} decisions (this one speaks of {r['scope']})"
        elif r["scope_kind"] == "category" and r["scope"] in scopes:
            why = f"for {r['scope']} decisions (this one only mentions {r['scope']})"
            outranked = True
        if not why:
            continue
        people = [r["person"]] if r["person"] else store.team_members_by_handle((r["team"] or {}).get("handle", "")) \
            if r["team"] else []
        if r["team"] and not people:
            people = [{"id": m, "name": store.get_person(m)["name"]} for m in r["team"].get("members", [])
                      if store.get_person(m)]
        for person in people:
            if not person:
                continue
            strong = r["role"] in ("decides", "approves") and r["accepted"] and not outranked
            weight = _W["authority"] if strong else _W["knows"]
            if r["team"]:
                weight *= 0.9
            verb = {"decides": "decides", "approves": "must approve", "knows": "knows"}[r["role"]]
            line = f"verified: {person['name']} {verb} {why}"
            if r["team"]:
                line += f", as a member of {r['team']['name']}"
            line += f" ({r['source']}" + (f", asserted by {r['asserted_by']}" if r["asserted_by"] else "")
            if not r["accepted"]:
                line += ", not yet accepted"
            line += ")"
            if r.get("note"):
                line += f": {r['note'][:120]}"
            where = ""
            if r["scope_kind"] == "path":
                matched = hit_paths.get(r["id"], [])
                where = next((p for p in matched if _specificity(r["scope"]) >= narrowest[(p, r["role"])]),
                             matched[0] if matched else "")
            out.append({"key": _norm_person(person["name"]), "name": person["name"], "role": r["role"],
                        "weight": weight, "line": line, "strong": strong, "id": r["id"], "path": where,
                        "topic": r["scope_kind"] == "category" and r["scope"] in about and r["scope"],
                        "primary": r["scope_kind"] == "path" and any(
                            p.rstrip("/") == (path or "").strip().strip("/")
                            and _specificity(r["scope"]) >= narrowest[(p, r["role"])]
                            for p in hit_paths.get(r["id"], [])),
                        "scope_key": (r["role"], why)})
    # A team expanded into its members gives every one of them the same
    # authority for the same area, and the winner's evidence then reads
    # like a finding about that person. On Grafana one area had 143 people
    # recorded as deciding it, all within 0.02 of each other, and which
    # one came first was very nearly arbitrary. Say how many share it, so
    # a person reading the evidence treats the name as a placement and
    # hands it on rather than assuming Raven knows something.
    shared: dict = {}
    for m in out:
        shared[m["scope_key"]] = shared.get(m["scope_key"], 0) + 1
    for m in out:
        n = shared[m["scope_key"]]
        if n >= SHARED_AUTHORITY_MANY:
            m["line"] += (f"; {n} people are recorded as deciding for that same area, so this names one of "
                          "them on their own history here rather than a decider the map picks out")
        m.pop("scope_key", None)
    return out


def signal_route(store: Graph, repo: str, question: str, context: str = "", path: str = "",
                 notes: list[str] | None = None, requester: str = "",
                 hints: list[str] | None = None,
                 hits: list[PathHit] | None = None, category: str = "",
                 also_paths: list[str] | None = None) -> list[tuple[str, list[str], float]]:
    """`hits`, when given, are the paths the caller already resolved for
    this question; otherwise they are resolved here. When what GitHub
    told Raven about the repository is stale or missing, the route says
    so: on the winner's evidence and in the notes."""
    ranked = _signal_route(store, repo, question, context=context, path=path, notes=notes, requester=requester,
                           hints=hints, hits=hits, category=category, also_paths=also_paths)
    from .github import sync_note
    note = sync_note(store, repo)
    if note:
        if notes is not None:
            for_operator = sync_note(store, repo, operator=True)
            if for_operator not in notes:
                notes.append(for_operator)
        if ranked:
            name, evidence, score = ranked[0]
            ranked[0] = (name, list(evidence) + [note], score)
    return ranked


def _signal_route(store: Graph, repo: str, question: str, context: str = "", path: str = "",
                  notes: list[str] | None = None, requester: str = "",
                  hints: list[str] | None = None,
                  hits: list[PathHit] | None = None, category: str = "",
                  also_paths: list[str] | None = None) -> list[tuple[str, list[str], float]]:
    from .config import load
    from .ingest import fetch_blame, fetch_changes, fetch_listings, fetch_tree
    live = load().live_retrieval
    reasons = notes if notes is not None else []
    req = requester_keys(requester, store, repo)
    if live:
        fetch_tree(store, repo)
        fetch_listings(store, repo)
    now = _now(store, repo)
    if hits is None:
        hits = resolve_paths(store, repo, question, context, path, hints=hints, also_paths=also_paths)
    hits = hits[:3]
    verified = _authority_matches(store, repo, hits, question, context, category, path, also_paths)
    # Answer-derived weak rows duplicate contact history without its source
    # scope or outcomes. Only the scoped contact matcher may reuse that
    # learning; treating this broad hint as independent evidence bypasses
    # conflicts, declines, opt-outs and freshness. Keep the historical rows,
    # explicit maps and declared referrals intact. Legacy notes are not a
    # structured provenance link and must not be parsed to invent one.
    answer_hints = {a['id'] for a in store.authority_rows(repo)
                    if a['source'] == 'answer' and a['role'] == 'knows'}
    if any(a['id'] in answer_hints for a in verified):
        reasons.append('answer-derived weak routing hints require applicable contact history; '
                       'unlinked legacy provenance is not inferred')
        verified = [a for a in verified if a['id'] not in answer_hints]
    if not hits and not verified:
        wide = _repo_wide(store, repo, now, req)
        if wide is not None:
            return [wide]
        reasons.append("no path in the tree matches the question, its context, or an indexed record on the subject")
        return []
    if live:
        fetch_changes(store, repo, [h.path if h.is_dir else (h.path.rsplit("/", 1)[0] + "/" if "/" in h.path else h.path)
                                    for h in hits])
        now = _now(store, repo)
    blame = fetch_blame(store, repo, [h.path for h in hits if not h.is_dir])
    last_active = _active_since(store, repo)
    repo_sig = _scope_signals(store, repo, "", now, req) if req else None
    # Hits share their upper levels; each level is read once per route.
    sigs: dict[str, dict] = {}
    listed_at: dict[str, list[dict]] = {}
    req_name = ""
    if req:
        req_name = next((n for n in (repo_sig or {}).get("display", {}).values()
                         if _is_requester(n, "", req)), "") or next(iter(req[0]), "") or next(iter(req[1]), "")
    scored: dict[str, Candidate] = {}
    display: dict[str, str] = {}
    scope_notes: list[str] = []
    hit_weight_total = 0.0
    # Without history the levels stay empty, so only listings, blame and
    # the authority map can speak; the loop below handles that.
    peers = set(_scope_aggregate(store, repo, "", now)["app_n"])
    teams_seen: list[tuple[str, str]] = []
    catch_all_seen: list[tuple[str, str]] = []
    listed_seen: dict[str, str] = {}
    requester_seen: set[str] = set()
    user_rows = store.db.execute(
        "SELECT engineer, path_prefix FROM ownership WHERE repo=? AND source='user' AND valid_to IS NULL",
        (repo,)).fetchall()

    for hit in hits:
        # The signals at the named path and at the levels above it, the
        # nearer the louder. A level with no history at all is skipped;
        # thin history counts proportionally at its own level.
        levels: list[tuple[str, dict, float]] = []
        above = 0
        for level in _levels(hit):
            if level not in sigs:
                sigs[level] = _scope_signals(store, repo, level, now, req)
            sig = sigs[level]
            if not sig["n_changes"]:
                continue
            if sig["n_changes"] >= MIN_SCOPE_CHANGES:
                lw = LEVEL_WEIGHTS[above]
                above += 1
            else:
                lw = THIN_LEVEL_WEIGHT
            levels.append((level, sig, lw))
            if above >= len(LEVEL_WEIGHTS):
                break
        listed = listed_at[hit.path] = listed_for(store, repo, hit.path)
        file_blame = blame.get(hit.path, {}) if not hit.is_dir else {}
        if not levels and not listed and not file_blame:
            scope_notes.append(f"no history and no listing for {hit.path}")
            continue
        hit_weight_total += hit.weight
        wsum = sum(lw for _l, _s, lw in levels) or 1.0
        listed_by: dict[str, list[dict]] = defaultdict(list)
        logins: dict[str, str] = {}
        emails_seen = {}
        for _level, sig, _lw in levels:
            display.update({k: v for k, v in sig["display"].items() if k not in display})
            for who, addresses in sig["emails"].items():
                emails_seen.setdefault(who, set()).update(addresses)
            for login, who in sig.get("logins", {}).items():
                logins.setdefault(login, who)
        for entry in listed:
            if entry["role"] == "team" or "/" in entry["person"]:
                # A team the organization has told Raven about is its
                # members, each a listed owner; an unknown team is not a
                # person to ask, and the evidence says so.
                members = store.team_members_by_handle(entry["person"])
                if not members:
                    teams_seen.append((entry["person"], entry["pattern"]))
                    continue
                for member in members:
                    mkey = _norm_person(member["name"])
                    listed_by[mkey].append({**entry, "person": member["name"], "role": "owner",
                                            "team": entry["person"]})
                    display.setdefault(mkey, member["name"])
                    listed_seen.setdefault(mkey, member["name"])
                continue
            if entry["catch_all"]:
                catch_all_seen.append((entry["person"], entry["pattern"]))
                continue
            key = _resolve_handle(entry["person"], display, logins, emails_seen)
            listed_by[key].append(entry)
            display.setdefault(key, entry["person"])
            listed_seen.setdefault(key, display[key])
        blame_by: dict[str, tuple[str, str, float]] = {}
        for name, (bname, bemail, share) in file_blame.items():
            key = _norm_person(name)
            # Line ownership counts for people the history knows at all;
            # a name blame alone remembers may have left years ago.
            if key in last_active:
                blame_by[key] = (bname, bemail, share)
                display.setdefault(key, bname)
        keys: set[str] = set(listed_by) | set(blame_by)
        for _level, sig, _lw in levels:
            keys |= set(sig["approval"]) | set(sig["author"])
        for key in keys:
            who = display.get(key, key)
            if _is_machine_author(who):
                continue
            email = next((sig["emails"].get(key, "") for _l, sig, _w in levels if sig["emails"].get(key)), "")
            if req and _is_requester(who, email, req):
                requester_seen.add(who)
                continue
            blended = 0.0
            strongest = 0.0
            best: tuple | None = None
            for level, sig, lw in levels:
                total_auth = sig["total"] or 1.0
                thin = min(1.0, (sig["n_changes"] + 1) / (MIN_SCOPE_CHANGES + 1))
                app_share = min(1.0, sig["approval"][key] / total_auth)
                auth_share = sig["author"][key] / total_auth
                aff_share = sig["aff"][key] / sig["req_total"] if sig["req_total"] > 0 else 0.0
                aff_full = min(1.0, sig["req_n"] / AFFINITY_FULL_CHANGES)
                s_i = (_W["approval"] * app_share + (_W["author_peer"] if key in peers else _W["author"]) * auth_share) * thin \
                    + _W["affinity"] * aff_share * aff_full
                blended += lw * s_i
                strongest = max(strongest, min(1.0, lw) * s_i)
                if best is None or s_i > best[0]:
                    best = (s_i, level, sig, app_share, auth_share, aff_share, thin)
            # Judged by the level that supports them best; the other
            # levels can only add, so breadth never dilutes a clear owner.
            s = max(strongest, blended / wsum)
            lines: list[str] = []
            for entry in listed_by.get(key, []):
                s += _W["maintainer"] if entry["role"] in ("maintainer", "owner") else _W["reviewer"]
                label = "MAINTAINERS" if entry["kind"] == "maintainers" else "CODEOWNERS"
                role = "" if entry["role"] in ("maintainer", "owner") else " as designated reviewer"
                if entry.get("team"):
                    lines.append(f"CODEOWNERS lists the team @{entry['team']} for {entry['pattern']}; {who} is a member")
                    continue
                lines.append(f"{label} lists {who}{role} for {entry['pattern']}"
                             + (f" ({entry['section']})" if entry["section"] and entry["kind"] == "maintainers" else ""))
            app_share = auth_share = aff_share = thin = 0.0
            chosen = hit.path
            if best is not None:
                _s, chosen, sig, app_share, auth_share, aff_share, thin = best
                if app_share > 0:
                    lines.append(f"reviewed or accepted {sig['app_n'][key]} of the last {sig['n_changes']} changes under "
                                 f"{chosen} ({_pct(app_share)} recency weighted; {_role_summary(sig['roles'][key])})")
                    if sig["roles"][key].get("merger") and sig["merge_weights"].get(key, 1.0) < 1.0:
                        lines.append(f"merge activity counts at {_pct(sig['merge_weights'][key])} weight here; "
                                     "general integration alone is weak area evidence")
                    if sig["roles"][key].get("merger"):
                        lines.append("approval uses the stronger of review and weighted merge shares; "
                                     "merge commits do not dilute the authorship history")
                if auth_share > 0:
                    lines.append(f"authored {sig['auth_n'][key]} of the last {sig['n_changes']} changes under {chosen} "
                                 f"({_pct(auth_share)} recency weighted)")
                if aff_share > 0:
                    lines.append(f"reviewed or accepted {sig['aff_n'][key]} of {req_name}'s last {sig['req_n']} "
                                 f"changes under {chosen}")
            if key in blame_by:
                # Line ownership beyond what recent authorship already
                # credits: the long memory of who built this file.
                bshare = blame_by[key][2]
                recent_auth = next((sig["author"][key] / (sig["total"] or 1.0) for level, sig, _lw in levels
                                    if level == hit.path), 0.0)
                s += _W["blame"] * max(0.0, bshare - recent_auth)
                lines.append(f"wrote {_pct(bshare)} of the human-written lines of {hit.path} (git blame, bots excluded)")
            # Who usually reviews this requester, anywhere in the repository,
            # tips the balance between people who already hold signal here.
            if repo_sig is not None and repo_sig["req_total"] > 0 and repo_sig["aff"].get(key, 0.0) > 0 \
                    and (app_share > 0 or auth_share > 0 or key in listed_by or key in blame_by):
                g = repo_sig["aff"][key] / repo_sig["req_total"]
                s += _W["affinity_repo"] * g * min(1.0, repo_sig["req_n"] / AFFINITY_FULL_CHANGES)
                if aff_share <= 0:
                    lines.append(f"reviewed or accepted {repo_sig['aff_n'][key]} of {req_name}'s last "
                                 f"{repo_sig['req_n']} changes across the repository")
            if s <= 0:
                continue
            if hit.weight < 0.6 and chosen:
                # The area came from words alone, not from a path the
                # agent named: say so, so a wrong area is caught early.
                lines.append(f"area matched from words alone ({hit.why}); confirm the path before relying on it")
            cand = scored.setdefault(key, Candidate(name=who))
            cand.per_hit.append(hit.weight * s)
            cand.approval = max(cand.approval, app_share * thin)
            cand.author = max(cand.author, auth_share * thin)
            if s >= cand.best:
                cand.best = s
                cand.lines = lines
                cand.scope = chosen
                if best is not None and app_share > 0:
                    roles = best[2]["roles"].get(key, {})
                    cand.merge_led = roles.get("merger", 0) * 2 >= max(1, best[2]["app_n"].get(key, 0))
            for _level, sig, _lw in levels:
                cand.last_ts = max(cand.last_ts, sig["last"].get(key, ""))
    if hit_weight_total <= 0 and not verified:
        reasons.append("; ".join(scope_notes) if scope_notes else "no history under the matched paths")
        if teams_seen:
            t, pat = teams_seen[0]
            reasons.append(f"CODEOWNERS lists the team @{t} for {pat}, but a team is not a person to ask")
        return []
    # Each person is judged by the path that supports them best; other
    # paths add a little, so hits that disagree do not dilute the answer.
    # Weak area evidence needs strong people evidence: the score is
    # scaled by how firmly the question named its area, so a lone word
    # that happens to be a filename cannot route a question to a listed
    # maintainer of some other subsystem.
    confidence = max(0.4, min(1.0, max((h.weight for h in hits), default=0.4)))
    for cand in scored.values():
        best_hit = max(cand.per_hit) if cand.per_hit else 0.0
        cand.score = (best_hit + 0.25 * (sum(cand.per_hit) - best_hit)) * confidence
    # What the organization verified outranks what git suggests: a person
    # or team the authority map says decides or approves this scope is
    # the owner, whatever the history says, and is routed to even when
    # they asked (the decision is theirs to make). Someone it says merely
    # knows the scope is a candidate.
    # A person's authority here is the strongest one they hold for this
    # decision, not the sum of them: being named by a directory's rule
    # and by the rule for the directory inside it is one authority, not
    # two, and someone on four teams is not four times the owner. The
    # other rows stay in the evidence.
    best_match: dict[str, dict] = {}
    role_rank = {"decides": 2, "approves": 1, "knows": 0}
    for match in verified:
        current = best_match.get(match["key"])
        if current is None or (match["weight"], role_rank[match["role"]]) > (current["weight"],
                                                                             role_rank[current["role"]]):
            best_match[match["key"]] = match
    for key, match in best_match.items():
        also = [m["line"] for m in verified if m["key"] == key and m is not match][:2]
        cand = scored.setdefault(key, Candidate(name=match["name"]))
        cand.score += match["weight"]
        cand.verified = max(cand.verified, match["weight"])
        cand.lines = [match["line"], *also] + cand.lines
        if match["strong"]:
            cand.verified_role = match["role"]
            cand.scope = cand.scope or match.get("path") or (hits[0].path if hits else "")
            cand.per_hit = cand.per_hit or [0.0]
        cand.topic = next((m["topic"] for m in verified
                           if m["key"] == key and m["topic"] and m["strong"] and m["role"] == "decides"), "")
        cand.primary = any(m["primary"] and m["strong"] and m["role"] == "decides"
                           for m in verified if m["key"] == key)
    # You told Raven: a recorded routing answer for a matching path wins;
    # a row recorded with no path is repo-wide.
    for r in user_rows:
        prefix = r["path_prefix"]
        if prefix and not any(h.path.startswith(prefix) for h in hits):
            continue
        key = _norm_person(r["engineer"])
        cand = scored.setdefault(key, Candidate(name=r["engineer"]))
        cand.score += _W["user"]
        cand.lines = ["you told Raven this owner directly"] + cand.lines

    # Inactive people are never routed to; say so when one would have won.
    # A verified authority is never inactive: the organization says they
    # decide, whatever their git history.
    inactive: list[Candidate] = []
    for key in list(scored):
        ts = last_active.get(key) or scored[key].last_ts
        if now and ts and _months_ago(ts, now) * 30 > INACTIVE_DAYS and not scored[key].verified:
            inactive.append(scored.pop(key))
    if not scored:
        if requester_seen and not inactive:
            reasons.append(f"the only person with history here is the requester ({', '.join(sorted(requester_seen))}); "
                           f"Raven does not route a question back to its requester")
        else:
            reasons.append("the people with history here have all been inactive for over a year"
                           if inactive else "no person holds any signal under the matched paths")
        return []
    ranked = sorted(scored.values(), key=lambda c: (-c.score, c.verified_role != "decides", c.name))
    winner = ranked[0]
    # A listed person who is not active here loses to someone holding the live majority.
    live_share = {c.name: max(c.approval, c.author) for c in ranked}
    override = ""
    # The person who decides is asked; the one who must approve signs.
    # Two responsibilities, not one ranking: whoever else's history adds
    # to an approver's score, the map says the decision is somebody
    # else's, and required approvers are added as signers on their own.
    if winner.verified_role == "approves":
        decider = next((c for c in ranked[1:] if c.verified_role == "decides"), None)
        if decider is not None:
            override = (f"note: {winner.name} must approve here and signs as a required approver; "
                        f"{decider.name} decides it, so Raven asks {decider.name}")
            ranked.remove(decider)
            ranked.insert(0, decider)
            winner = decider
    # Secondary paths still contribute candidates and required signers,
    # but do not displace the primary path's verified decider.
    if not winner.primary:
        primary = next((c for c in ranked[1:] if c.primary), None)
        if primary is not None:
            note = (f"note: {primary.name} decides the primary path {path}; "
                    "other affected paths retain their approval requirements")
            override = f"{override}; {note}" if override else note
            ranked.remove(primary)
            ranked.insert(0, primary)
            winner = primary
    # Someone who decides what the decision is about outranks someone who
    # decides the files it lands in: a security call on a runtime file is
    # the security owner's. Measured live on 5e967e4: "May Retry-After
    # diagnostics contain bearer credentials?", category security, went to
    # the person who decides util/*, and the security decider was not even
    # among the signers.
    if winner.verified and not winner.topic:
        topical = next((c for c in ranked[1:] if c.topic), None)
        if topical is not None:
            note = (f"note: {topical.name} decides {topical.topic} decisions, which this one is about; "
                    f"{winner.name} {'decides' if winner.verified_role == 'decides' else 'holds authority for'} "
                    f"the files it lands in, so Raven asks {topical.name}")
            override = f"{override}; {note}" if override else note
            ranked.remove(topical)
            ranked.insert(0, topical)
            winner = topical
    if not winner.verified and any(ln.startswith(("MAINTAINERS lists", "CODEOWNERS lists")) for ln in winner.lines):
        if live_share[winner.name] < 0.25:
            challenger = next((c for c in ranked[1:] if live_share[c.name] >= LIVE_MAJORITY), None)
            if challenger is not None:
                label = "CODEOWNERS" if any("CODEOWNERS" in ln for ln in winner.lines) else "MAINTAINERS"
                override = (f"note: {label} lists {winner.name} here, but {challenger.name} holds the live "
                            f"majority ({_pct(live_share[challenger.name])} of the recent changes under "
                            f"{challenger.scope}), so Raven routes to {challenger.name}; {winner.name} still "
                            f"holds the {label} approval")
                ranked.remove(challenger)
                ranked.insert(0, challenger)
                winner = challenger
    # The mirror case: the leader is not listed and leads on merging, which
    # someone who merges across the whole repository does in every area,
    # while CODEOWNERS or MAINTAINERS names someone active here. The listed
    # person is asked. Measured on home-assistant/core: the person who
    # merges four in five of the repository's changes led eight of ten
    # integrations over the code owners who write them.
    listed = lambda c: any(ln.startswith(("MAINTAINERS lists", "CODEOWNERS lists")) for ln in c.lines)
    if not override and not winner.verified and winner.merge_led and not listed(winner):
        merged_total, merged_by = store.memo(repo, "merge_distribution", (now,), lambda: store.changes_under(repo, ""),
                                             lambda all_rows: _merge_distribution(all_rows, now))
        broad = merged_by.get(_norm_person(winner.name), 0.0) / merged_total if merged_total else 0.0
        # Gatekeeping, not ownership: they merge here about as much as they
        # merge everywhere. Someone who merges one area far more than the
        # rest (an area maintainer) keeps it.
        here_total, here_by = _merge_distribution(store.changes_under(repo, winner.scope), now)
        here = here_by.get(_norm_person(winner.name), 0.0) / here_total if here_total else 0.0
        specialized = max(0.0, here - broad) / max(1e-9, 1.0 - broad) >= GATEKEEPER_SPECIALIZATION
        active = [c for c in ranked[1:] if listed(c) and not c.verified and live_share[c.name] >= LISTED_ACTIVE_SHARE]
        if broad >= BROAD_MERGER_SHARE and not specialized and active:
            pick = active[0]
            label = "CODEOWNERS" if any("CODEOWNERS" in ln for ln in pick.lines) else "MAINTAINERS"
            override = (f"note: {winner.name} leads here on merging, and merges {_pct(broad)} of the repository's "
                        f"changes; {label} lists {pick.name}, who holds {_pct(live_share[pick.name])} of the recent "
                        f"changes under {pick.scope}, so Raven routes to {pick.name}")
            ranked.remove(pick)
            ranked.insert(0, pick)
            winner = pick
    # Where reviews are spread across a few regulars, the one who reviews
    # the most of an area is still the person to ask: an approval leader
    # with a third of the recent changes routes below the general floor.
    approval_leader = (winner.approval >= APPROVAL_LEADER_SHARE and confidence >= 0.8
                       and winner.score >= MIN_SCORE * 0.7)
    if winner.score < MIN_SCORE and not approval_leader and not winner.verified:
        if confidence < 0.8:
            reasons.append(f"the question only weakly points at {winner.scope} ({hits[0].why}); the strongest "
                           f"signal there is {winner.name} with {_pct(max(winner.approval, winner.author))} of "
                           f"the recent changes, not enough to route on")
        else:
            reasons.append(f"nobody holds a clear share under {winner.scope}: the strongest signal is {winner.name} "
                           f"with {_pct(max(winner.approval, winner.author))} of the recent changes")
        return []
    lines = list(winner.lines)
    if override:
        lines.append(override)
    elif listed_seen and _norm_person(winner.name) not in listed_seen \
            and not any(ln.startswith(("MAINTAINERS lists", "CODEOWNERS lists")) for ln in winner.lines):
        names = ", ".join(sorted(set(listed_seen.values())))
        label = "MAINTAINERS" if any(item["kind"] == "maintainers" for hit in hits
                                     for item in listed_at[hit.path]) else "CODEOWNERS"
        if winner.verified:
            lines.append(f"note: {label} lists {names} here; the organization's authority map names {winner.name}, "
                         f"so Raven routes to {winner.name}; {names} still holds the {label} listing")
        else:
            lines.append(f"note: {label} lists {names} here, but live activity points at {winner.name}, "
                         f"so Raven routes to {winner.name}; {names} still holds the {label} approval")
    if len(ranked) > 1 and winner.score - ranked[1].score <= CO_OWNER_MARGIN and ranked[1].scope == winner.scope \
            and max(ranked[1].approval, ranked[1].author) > 0:
        ru = ranked[1]
        lines.append(f"note: shared ownership, {ru.name} holds {_pct(max(ru.approval, ru.author))} of the recent "
                     f"changes under {ru.scope} (vs {_pct(max(winner.approval, winner.author))} for {winner.name}), "
                     f"loop them in if this is contentious")
    for c in inactive[:1]:
        lines.append(f"note: {c.name} holds history under {c.scope} but has been inactive for "
                     f"{_months_ago(last_active.get(_norm_person(c.name)) or c.last_ts, now)} months, so Raven "
                     f"did not route to them")
    for team, pat in teams_seen[:1]:
        lines.append(f"note: CODEOWNERS lists the team @{team} for {pat}; {winner.name} is the person active there")
    for person, pat in catch_all_seen[:1]:
        if _norm_person(person) != _norm_person(winner.name):
            lines.append(f"note: {person} is listed project-wide ({pat}), not for this area")
    for who in sorted(requester_seen)[:1]:
        lines.append(f"note: {who} asked; they hold history here too, and Raven never routes a question back "
                     f"to its requester")
    if hits:
        hit = next((h for h in hits if h.path.startswith(winner.scope) or winner.scope.startswith(h.path)), hits[0])
        lines.append(f"area: {hit.why}")
    elif path and path != "unknown":
        lines.append(f"area: the agent is working in {path.lstrip('/')}")
    else:
        lines.append("area: the question names no path; routed on the decision's category")
    out = [(store.resolve_engineer(winner.name), lines, round(winner.score, 3))]
    for c in ranked[1:6]:
        out.append((store.resolve_engineer(c.name), c.lines[:3], round(c.score, 3)))
    return out
