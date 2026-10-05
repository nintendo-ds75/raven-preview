"""Baselines the routing bench compares Bridge against, each from the
same pre-T data an item carries: the first human CODEOWNERS or
MAINTAINERS lists for the touched paths; the person who most recently
reviewed, accepted or merged under the touched directories; a manual
map from path prefix to person (BRIDGE_BENCH_MANUAL_MAP, a JSON file).
Every baseline is scored with the metrics Bridge is scored with, so
the summary shows what routing on one signal alone would have done."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .identity import Person, is_machine, parse_person, same_person

REVIEW_ROLES = {"reviewed-by", "acked-by", "helped-by", "committer", "approved-by", "merger", "merged-by"}
NAMES = ("baseline-codeowners", "baseline-recent-reviewer", "baseline-manual-map")


def _outcome(condition: str, owner: str, evidence: list[str], ranked: list[str], question: str, context: str) -> dict:
    return {"condition": condition, "question": question, "context": context, "owner": owner,
            "evidence": evidence, "kind": "baseline", "status": "pending" if owner else "", "routing_reason": "",
            "answer": "", "ranked": ranked or ([owner] if owner else []),
            "ranked_evidence": [evidence] if owner else [], "error": "", "seconds": 0.0}


def codeowners_only(item: dict, question: str = "", context: str = "") -> dict:
    """The first human the listing names for the touched paths, in
    listing order; nobody when the listing names only teams or nobody."""
    listed = [p for p in (item.get("listing") or []) if p.get("name") and not is_machine(p["name"])]
    owner = listed[0]["name"] if listed else ""
    source = next((k for k in (item.get("label_sources") or {}) if k in ("MAINTAINERS", "CODEOWNERS")), "the listing")
    evidence = [f"{source} lists {owner} for the touched paths"] if owner else ["no human listed for the touched paths"]
    return _outcome("baseline-codeowners", owner, evidence, [p["name"] for p in listed[:3]], question, context)


def most_recent_reviewer(item: dict, question: str = "", context: str = "") -> dict:
    """Whoever reviewed, accepted or merged under the touched directories
    most recently before T, never the author."""
    author = parse_person(f"{(item.get('author') or {}).get('name', '')} <{(item.get('author') or {}).get('email', '')}>")
    cands = []
    for r in item.get("related") or []:
        if not (set(r.get("roles") or []) & REVIEW_ROLES) or is_machine(r.get("name", "")):
            continue
        if same_person(author, Person(r.get("name", ""), r.get("email", ""))):
            continue
        cands.append(r)
    cands.sort(key=lambda r: r.get("last_ts") or "", reverse=True)
    owner = cands[0]["name"] if cands else ""
    evidence = ([f"the most recent reviewer or acceptor in the touched directories ({(cands[0].get('last_ts') or '')[:10]})"]
                if owner else ["nobody reviewed in the touched directories before"])
    return _outcome("baseline-recent-reviewer", owner, evidence, [c["name"] for c in cands[:3]], question, context)


def manual_map(item: dict, mapping: dict[str, str], question: str = "", context: str = "") -> dict:
    """The longest path prefix the map names for any touched file."""
    best = ("", "")
    for f in item.get("files") or []:
        for prefix, person in mapping.items():
            p = prefix.strip("/")
            if (f == p or f.startswith(p + "/") or p == "") and len(p) >= len(best[0]) and person:
                best = (p, person)
    owner = best[1]
    evidence = [f"the manual map names {owner} for {best[0] or 'everything'}"] if owner else ["the manual map names nobody for the touched paths"]
    return _outcome("baseline-manual-map", owner, evidence, [owner] if owner else [], question, context)


def load_manual_map() -> dict[str, str]:
    path = os.environ.get("BRIDGE_BENCH_MANUAL_MAP", "").strip()
    if not path or not Path(path).exists():
        return {}
    try:
        data = json.loads(Path(path).read_text())
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def baseline_outcomes(item: dict, question: str = "", context: str = "", mapping: dict[str, str] | None = None) -> dict[str, dict]:
    out = {"baseline-codeowners": codeowners_only(item, question, context),
           "baseline-recent-reviewer": most_recent_reviewer(item, question, context)}
    mapping = load_manual_map() if mapping is None else mapping
    if mapping:
        out["baseline-manual-map"] = manual_map(item, mapping, question, context)
    return out
