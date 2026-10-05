"""Who may act on a decision: one check every transport asks before an
answer, a signature, a correction, a hand-on, an assignment or a rule
is recorded, whatever the request says about itself.

The actor is a person (or the local operator, or the bootstrap admin,
or an agent credential), the decision names its assigned owner and its
required signers, and the authority map names who decides or approves
its scope. Substantive approval (answering, signing, correcting, making
a rule) belongs to the assigned owner, a required signer, or a person
the map says decides or approves the scope. Routing (assigning or
handing on) belongs to those and to the coordinator. An administrator
can override, but only by saying so, and the record says so too. An
agent credential can do none of it: agents write nodes, people decide.
Every decision is compared by stable person id, never by display name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .store import Invalid

SUBSTANTIVE = ("answer", "sign", "correct", "rule")
ROUTING = ("assign", "refer")
ACTIONS = SUBSTANTIVE + ROUTING + ("followup", "note")


@dataclass
class Actor:
    """Who is acting: a person id (empty for the operator and the
    bootstrap admin), the name to record, their role, and how they
    proved it: session, token, agent, slack, bootstrap, operator."""
    id: str = ""
    name: str = ""
    role: str = "member"
    kind: str = "session"
    override: bool = False

    @classmethod
    def from_identity(cls, identity, override: bool = False) -> "Actor":
        return cls(id=identity.id or "", name=identity.name, role=identity.role, kind=identity.kind, override=override)

    @classmethod
    def person(cls, row: dict, kind: str = "slack") -> "Actor":
        return cls(id=row["id"], name=row["name"], role=row.get("role") or "member", kind=kind)


class Refused(Invalid):
    """The actor may not do this; the message says who may."""


def _scope_holders(graph, decision: dict, roles: tuple = ("decides", "approves")) -> list[dict]:
    """The people the authority map says decide or approve this
    decision's scope, by path, category or repository. A category row
    gives standing only for what the decision is about (its named
    category, else the categories its question names), never for a word
    its question or context happens to use: routing may suggest a
    person on a mention, approving needs the decision's own topic."""
    from .scopes import primary_scopes
    from .signals import _pattern_re
    import re
    repo = decision.get("repo") or ""
    rows = graph.authority_rows(repo)
    if not rows:
        return []
    scopes = set(primary_scopes(decision.get("question") or "", decision.get("category") or ""))
    path = (decision.get("path") or "").lstrip("/")
    try:
        paths = [str(x).lstrip("/") for x in json.loads(decision.get("scope_paths") or "[]") if x]
    except (ValueError, TypeError):
        paths = []
    if path and path != "unknown" and path not in paths:
        paths.insert(0, path)
    out: list[dict] = []
    for r in rows:
        if r["role"] not in roles or not r.get("accepted", 1):
            continue
        covers = False
        if r["scope_kind"] == "repo":
            covers = True
        elif r["scope_kind"] == "category":
            covers = r["scope"] in scopes
        elif r["scope_kind"] == "path" and paths:
            try:
                rx = _pattern_re(r["scope"], "codeowners")
                covers = any(rx.match(p) for p in paths)
            except re.error:
                covers = False
        if not covers:
            continue
        if r.get("person"):
            out.append(r["person"])
        elif r.get("team"):
            for pid in r["team"].get("members", []):
                p = graph.get_person(pid)
                if p is not None:
                    out.append(p)
    return out


def _owner_person_id(graph, decision: dict) -> str:
    """The stable id of the decision's assigned owner: the owner row's
    linked person, else the one person that exact name resolves to."""
    owner_id = decision.get("owner_id") or ""
    if not owner_id:
        return ""
    row = graph.db.execute("SELECT person_id, name FROM owners WHERE id=?", (owner_id,)).fetchone()
    if row is None:
        return ""
    if row["person_id"]:
        return row["person_id"]
    hits = [p for p in graph.people() if p["name"].strip().lower() == (row["name"] or "").strip().lower()]
    return hits[0]["id"] if len(hits) == 1 else ""


def _required_signer_ids(graph, decision: dict) -> set[str]:
    try:
        names = json.loads(decision.get("required_signers") or "[]")
    except (ValueError, TypeError):
        names = []
    out: set[str] = set()
    for name in names:
        hits = [p for p in graph.people() if p["name"].strip().lower() == str(name).strip().lower()]
        if len(hits) == 1:
            out.add(hits[0]["id"])
    return out


def basis_for(graph, actor: Actor, decision: dict, action: str) -> tuple[str, str]:
    """(basis, why_not): the standing on which the actor may take this
    action on this decision, or empty with the reason they may not.
    Bases: operator, admin-override, owner, required-signer, authority,
    coordinator."""
    if action not in ACTIONS:
        raise Invalid(f"unknown action {action!r}")
    if actor.kind == "operator":
        return "operator", ""
    if actor.kind == "agent" or actor.role == "agent":
        return "", ("an agent credential writes nodes and reads the tree; a person answers, signs, corrects, "
                    "hands on and makes rules")
    if actor.role == "viewer":
        return "", "a viewer reads; this needs the decision's owner, a required signer, or someone the authority map names"
    if actor.kind == "bootstrap":
        return ("admin-override", "") if actor.override or action in ROUTING + ("note",) else (
            "", "the bootstrap admin can route and administer; to decide for someone, pass override=true and it is recorded as an override")
    if action in ("note", "followup"):
        return "member", ""
    person_id = actor.id
    if not person_id:
        return "", "no person is signed in"
    if actor.kind == "slack" and action in ROUTING and not decision.get("owner_id"):
        # Only routing. The Slack delivery handler additionally requires
        # a reply to Raven's own message in the configured triage channel.
        return "slack-triage", ""
    owner_pid = _owner_person_id(graph, decision)
    if owner_pid and owner_pid == person_id:
        return "owner", ""
    if action in ("sign", "correct", "answer") and person_id in _required_signer_ids(graph, decision):
        return "required-signer", ""
    if any(p["id"] == person_id for p in _scope_holders(graph, decision)):
        return "authority", ""
    if action in ROUTING:
        coordinator = graph.coordinator(decision.get("repo") or "")
        if coordinator is not None and coordinator["id"] == person_id:
            return "coordinator", ""
    if actor.role == "admin":
        if actor.override:
            return "admin-override", ""
        return "", ("you are an admin, not this decision's owner; pass override=true to act on their behalf, "
                    "and the record will say so")
    owner_name = (decision.get("owner_name") or "").strip()
    who = f"its owner {owner_name}" if owner_name else "its owner (unassigned: the coordinator or an admin assigns it)"
    return "", f"this decision is {who}'s to {action}; you are not its owner, a required signer, or verified for its scope"


def check(graph, actor: Actor | None, decision: dict, action: str) -> str:
    """The basis, or a Refused error naming who may act. A missing actor
    is an internal caller that already checked (tests, the evaluation,
    the operator's own scripts) and gets the basis 'internal'."""
    if actor is None:
        return "internal"
    basis, why = basis_for(graph, actor, decision, action)
    if not basis:
        raise Refused(f"Not permitted: {why}")
    return basis
