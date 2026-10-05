"""Scoring one outcome against its labels, and the mechanism behind each
miss.

Labels: strong (reviewers, acks, the accepting committer, the merger) and
listing (MAINTAINERS or CODEOWNERS humans for the touched paths at T).
Related: everyone active on the touched directories before T.

Per outcome:
  hit1              routed owner is a strong label
  hit3              any of the top three candidates is a strong label
  alt               routed owner is a strong or listing label
  wrong_person      routed to someone with no relation to the paths or
                    their history (not labeled, not listed, not related)
  unknown_should    no owner although a label was knowable from pre-T data
  unknown_honest    no owner and no label was knowable
  evidence_honest   the evidence names a real signal (share, listing,
                    review history, referral) about a related path, never
                    a composite score or a stray token
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .identity import Person, is_machine, name_matches, parse_person, same_person

INACTIVE_DAYS = 365

_SIGNAL_RES = {
    "blame_share": re.compile(r"\d+% of the (?:last )?\d+ (?:recency-weighted )?commit touches under (\S+)"
                              r"|\d+% of the recency-weighted commit touches under (\S+)"
                              r"|authored \d+ of the last \d+ changes under (\S+)"),
    "listing": re.compile(r"(?:CODEOWNERS|MAINTAINERS) lists|listed (?:as|in) (?:CODEOWNERS|MAINTAINERS)"
                          r"|MAINTAINERS (?:names|entry)|designated reviewer"),
    "review_history": re.compile(r"reviewed or accepted \d+ of the last \d+ changes under (\S+)"
                                 r"|reviewed|Reviewed-by|acked|Acked-by|accepted|applied|landed|merged"
                                 r"|signed off|approv", re.I),
    "referral": re.compile(r"referr|you told Bridge|told Bridge", re.I),
    "activity": re.compile(r"authored|wrote|most active|commit(?:s|ted)? (?:to|under|in)", re.I),
}
_COMPOSITE_RE = re.compile(r"\bscore\b|strongest overall ownership signal|composite|weighted sum", re.I)
_REPO_WIDE_RE = re.compile(r"repo-wide|names no specific area|most active contributor overall", re.I)
_PATH_IN_EVIDENCE_RE = re.compile(r"under (\S+?)[,;)]?(?:\s|$)|lists? .+? for (\S+?)(?: \(|$|;)")


def _ts(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


def _related_person(name: str, related: list[dict]) -> dict | None:
    probe = parse_person(name)
    for r in related:
        if same_person(probe, Person(r["name"], r["email"])):
            return r
    return None


_GENERIC_COMPONENTS = {"include", "src", "lib", "tests", "test", "docs", "doc", "hw", "target", "drivers",
                       "arch", "scripts", "tools", "util", "common", "core", "misc", "modules", "services",
                       "gpu", "drm", "nixos", "pkgs", "deps"}


def _components(path: str) -> set[str]:
    out = set()
    for comp in path.strip("/").split("/"):
        c = comp.lower().rstrip("*")
        if "." in c and not c.startswith("."):
            c = c.rsplit(".", 1)[0]
        if len(c) >= 3 and c not in _GENERIC_COMPONENTS:
            out.add(c)
    return out


def _path_related(path: str, files: list[str], patterns: list[str]) -> bool:
    """A path in the evidence relates to the change when it is one of the
    touched files, an ancestor or descendant of one, covered by a listing
    pattern for one, or shares a specific (non-generic) component with
    one: include/gdbstub/ relates to gdbstub/gdbstub.c."""
    p = path.strip().strip("'\"").rstrip("/")
    if not p or p in ("*", "."):
        return False
    for f in files:
        if f == p or f.startswith(p + "/") or p.startswith(f.rsplit("/", 1)[0]) and "/" in f:
            return True
    for pat in patterns:
        core = pat.strip("/").rstrip("*").rstrip("/")
        if core and (p.startswith(core) or core.startswith(p)):
            return True
    mine = _components(p)
    if mine and any(mine & _components(f) for f in files):
        return True
    return False


def evidence_verdict(evidence: list[str], files: list[str], patterns: list[str]) -> tuple[bool, str]:
    """(honest, why). The first line is the route's stated reason."""
    if not evidence:
        return False, "no evidence line"
    head = evidence[0]
    signal = next((k for k, rx in _SIGNAL_RES.items() if rx.search(head)), "")
    if not signal:
        return False, "names no signal"
    if _COMPOSITE_RE.search(head):
        return False, "composite score"
    if _REPO_WIDE_RE.search(head):
        return False, "repo-wide fallback presented for a question about a specific area"
    for m in _PATH_IN_EVIDENCE_RE.finditer(head):
        path = next((g for g in m.groups() if g), "")
        if path and not _path_related(path, files, patterns):
            return False, f"names an unrelated path ({path})"
    return True, signal


def score(item: dict, outcome: dict) -> dict:
    strong = [Person(**p) for p in item["strong"]]
    listing = [Person(**p) for p in item["listing"]]
    related = item["related"]
    owner = outcome.get("owner") or ""
    ranked = outcome.get("ranked") or ([owner] if owner else [])
    files, patterns = item["files"], item.get("listing_patterns", [])

    knowable = any(_related_person(p.name or p.email, related) is not None for p in strong) \
        or any(p.matches(q) for p in strong for q in listing)
    hit_role = ""
    res = {"owner": owner, "hit1": False, "hit3": False, "alt": False, "wrong_person": False,
           "unknown_should": False, "unknown_honest": False, "team_handle": False, "bot": False,
           "evidence_honest": False, "evidence_why": "", "mechanism": "", "hit_role": "", "knowable": knowable,
           "error": outcome.get("error", "")}
    if outcome.get("error"):
        res["mechanism"] = "error"
        return res
    if not owner:
        res["unknown_should"] = knowable
        res["unknown_honest"] = not knowable
        res["mechanism"] = "unknown_should_know" if knowable else "unknown_honest"
        return res
    if "/" in owner and " " not in owner:
        res["team_handle"] = True
        res["mechanism"] = "team_handle"
        return res
    if is_machine(owner):
        res["bot"] = True
        res["wrong_person"] = True
        res["mechanism"] = "bot"
        return res
    hit = name_matches(owner, strong)
    res["hit1"] = hit is not None
    if hit is not None:
        for tag, names in item["label_sources"].items():
            if hit.label in names and tag not in ("MAINTAINERS", "CODEOWNERS"):
                hit_role = tag
                break
    res["hit_role"] = hit_role
    res["hit3"] = any(name_matches(n, strong) for n in ranked[:3])
    res["alt"] = res["hit1"] or name_matches(owner, listing) is not None
    honest, why = evidence_verdict(outcome.get("evidence") or [], files, patterns)
    res["evidence_honest"], res["evidence_why"] = honest, why
    rel = _related_person(owner, related)
    if not res["alt"] and rel is None:
        res["wrong_person"] = True
    if res["hit1"]:
        res["mechanism"] = "hit"
        return res
    res["mechanism"] = classify_miss(item, outcome, res, rel)
    return res


def classify_miss(item: dict, outcome: dict, res: dict, rel: dict | None) -> str:
    evidence = " ".join(outcome.get("evidence") or [])
    strong = [Person(**p) for p in item["strong"]]
    listing = [Person(**p) for p in item["listing"]]
    owner = outcome["owner"]
    t = _ts(item["ts"])
    if res["alt"]:
        listed = name_matches(owner, listing)
        if listed is not None and rel is not None and t is not None:
            last = _ts(rel["last_ts"])
            if last is not None and (t - last) > timedelta(days=INACTIVE_DAYS):
                return "stale_listing"
        return "listed_not_approver"
    if _REPO_WIDE_RE.search(evidence):
        return "repo_wide_fallback"
    for m in _PATH_IN_EVIDENCE_RE.finditer(evidence):
        path = next((g for g in m.groups() if g), "")
        if path and not _path_related(path, item["files"], item.get("listing_patterns", [])):
            return "wrong_area"
    if rel is None:
        return "no_relation"
    last = _ts(rel["last_ts"])
    if t is not None and last is not None and (t - last) > timedelta(days=INACTIVE_DAYS):
        return "inactive_person"
    roles = set(rel.get("roles", []))
    review_roles = {"reviewed-by", "acked-by", "helped-by", "committer", "approved-by", "merger"}
    if roles == {"author"} and any(_related_person(p.name or p.email, item["related"]) for p in strong):
        return "authorship_over_review"
    if roles & review_roles:
        return "plausible_reviewer_not_labeled"
    return "plausible_unlabeled"


def summarize(scored: list[dict]) -> dict:
    n = len(scored)
    if not n:
        return {"n": 0}
    keys = ("hit1", "hit3", "alt", "wrong_person", "unknown_should", "unknown_honest", "team_handle", "bot")
    out = {"n": n}
    for k in keys:
        out[k] = round(sum(1 for s in scored if s.get(k)) / n, 3)
    routed = [s for s in scored if s.get("owner") and not s.get("error")]
    out["routed"] = round(len(routed) / n, 3)
    # Reported apart: how often somebody is named at all (coverage), how
    # often the named person approved (precision among routed), how often
    # the named person is at least an acceptable contact (a label or a
    # listing), and how often the named person is one whose approval
    # counted (a review, an ack, an accepting committer, a merger).
    out["coverage"] = out["routed"]
    out["precision_routed"] = round(sum(1 for s in routed if s.get("hit1")) / len(routed), 3) if routed else 0.0
    out["acceptable_routed"] = round(sum(1 for s in routed if s.get("alt")) / len(routed), 3) if routed else 0.0
    out["authorized_signer"] = round(sum(1 for s in scored if s.get("hit1") and s.get("hit_role")) / n, 3)
    out["evidence_honest"] = round(sum(1 for s in routed if s.get("evidence_honest")) / len(routed), 3) if routed else 0.0
    out["errors"] = sum(1 for s in scored if s.get("error"))
    mech: dict[str, int] = {}
    for s in scored:
        m = s.get("mechanism") or ""
        if m and m != "hit":
            mech[m] = mech.get(m, 0) + 1
    out["mechanisms"] = dict(sorted(mech.items(), key=lambda kv: -kv[1]))
    roles: dict[str, int] = {}
    for s in scored:
        if s.get("hit1"):
            roles[s.get("hit_role") or "label"] = roles.get(s.get("hit_role") or "label", 0) + 1
    out["hit_roles"] = roles
    dishonest: dict[str, int] = {}
    for s in routed:
        if not s.get("evidence_honest"):
            why = s.get("evidence_why") or "?"
            why = re.sub(r"\(.*\)", "", why).strip()
            dishonest[why] = dishonest.get(why, 0) + 1
    out["evidence_issues"] = dict(sorted(dishonest.items(), key=lambda kv: -kv[1]))
    return out


STRATA = ("team_only_listing", "no_trailers", "aliases", "new_files", "stale_listing")


def stratify(item: dict) -> list[str]:
    """The strata an item falls in, from what it carries: a listing that
    names only teams; approval labels that come from the committer or the
    merger alone (no trailer); a label known by another name than the one
    history uses (an alias); files nobody touched before; a listing whose
    people were inactive for a year at T."""
    out: list[str] = []
    sources = item.get("label_sources") or {}
    if item.get("teams") and not item.get("listing"):
        out.append("team_only_listing")
    approval_tags = [k for k in sources if k not in ("MAINTAINERS", "CODEOWNERS") and not k.endswith(" teams")]
    if approval_tags and all(t in ("committer", "merger", "merged-by") for t in approval_tags):
        out.append("no_trailers")
    related = item.get("related") or []
    for p in item.get("strong") or []:
        rel = _related_person(p.get("email") or p.get("name") or "", related)
        if rel is not None and p.get("name") and rel.get("name") and rel["name"].strip().lower() != p["name"].strip().lower():
            out.append("aliases")
            break
    if not related:
        out.append("new_files")
    t = _ts(item.get("ts") or "")
    listing = item.get("listing") or []
    if listing and t is not None:
        active = False
        for p in listing:
            rel = _related_person(p.get("name") or p.get("email") or "", related)
            last = _ts(rel["last_ts"]) if rel else None
            if last is not None and (t - last) <= timedelta(days=INACTIVE_DAYS):
                active = True
                break
        if not active:
            out.append("stale_listing")
    return out


def summarize_strata(rows: list[dict], cond: str) -> dict:
    """The summary per stratum for one condition, over the rows whose
    item falls in it."""
    buckets: dict[str, list[dict]] = {k: [] for k in STRATA}
    for r in rows:
        if cond not in r.get("scores", {}):
            continue
        for k in stratify(r["item"]):
            buckets[k].append(r["scores"][cond])
    return {k: {"n": len(v), **{m: summarize(v)[m] for m in ("hit1", "alt", "wrong_person", "coverage", "precision_routed")}}
            for k, v in buckets.items() if v}
