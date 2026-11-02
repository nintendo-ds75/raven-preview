"""Where one person's attention goes: the page a signed-in person lands on.

Scoped to the viewer and to the tasks being built now. Three short lists:
what truly waits on them (and why it reached them rather than being
settled), what Raven settled on their behalf from calls they already made
(each traceable to that call, each correctable), and their calls that
changed recently (a correction they made, or someone replacing a call of
theirs), so they can see the system learn. Nothing here lists everything
Raven knows.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from . import authz
from .agent_names import display_name

LIVE_EXCLUDED = ("completed", "failed", "cancelled", "abandoned")
HIDDEN_STATUSES = ("duplicate", "suggested", "adopted", "withdrawn")
CHANGED_DAYS = 60

_SELECT = """SELECT d.*, o.name AS owner_name, r.title AS run_title, r.agent AS run_agent, r.requester AS run_requester,
             r.status AS run_status FROM decisions d LEFT JOIN owners o ON o.id=d.owner_id JOIN runs r ON r.id=d.run_id
             WHERE d.draft=0"""


def _options(raw):
    try:
        parsed = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [str(o) for o in parsed][:4] if isinstance(parsed, list) else []


def _clip(text, n):
    text = (text or "").strip()
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _area(evidence: str) -> str:
    """'you decide for pricing decisions' from the owner evidence the router
    wrote ('verified: Priya Shah decides for pricing decisions (...)')."""
    m = re.search(r"\b(decides|approves|knows)\s+for\s+([^(;]+?)(?:\s*\(|;|$)", evidence or "")
    if not m:
        return ""
    verb = {"decides": "decide", "approves": "approve", "knows": "know the most about"}[m.group(1)]
    return f"you {verb} {m.group(2).strip()}" if verb != "decide" else f"you decide for {m.group(2).strip()}"


def _agent(row) -> str:
    return display_name(row["run_agent"]) if row["run_agent"] else ""


def _same_person(row, name: str) -> bool:
    """Whether this call is the person's own: they answered it, made it a
    rule, or own it. Signing on someone's behalf does not make it theirs."""
    name = (name or "").strip().lower()
    return bool(name) and name in {(row[k] or "").strip().lower() for k in ("answered_by", "rule_by", "owner_name") if k in row.keys()}


def _gaps(row) -> list[str]:
    """What the source left open, Raven's own note: shown as such, never
    inside the quoted answer."""
    try:
        parsed = json.loads(row["gaps"]) if "gaps" in row.keys() and row["gaps"] else []
    except ValueError:
        return []
    return [str(g) for g in parsed if str(g).strip()] if isinstance(parsed, list) else []


def _source(db, source_id):
    if not source_id:
        return None
    return db.execute(_SELECT + " AND d.id=?", (source_id,)).fetchone()


def _call(row, when=""):
    """The earlier call, as a page shows it: who, when, what, where."""
    if row is None:
        return None
    return {"id": row["id"], "run_id": row["run_id"], "task": row["run_title"], "question": row["question"],
            "answer": row["answer"] or "", "by": row["answered_by"] or row["signed_by"] or row["owner_name"] or "",
            "when": when or row["created_at"], "answered_via": row["answered_via"] or "",
            "replaced_by": row["superseded_by"] or ""}


def _day(stamp) -> str:
    """A day as people say it ("Oct 28"), not an ISO date."""
    try:
        when = datetime.fromisoformat(str(stamp or "").replace("Z", "+00:00"))
    except ValueError:
        return ""
    return f"{when:%b} {when.day}"


# What someone said in a meeting that bears on the open call, as
# meeting_precedent.context_line writes it into the evidence.
_SAID_RE = re.compile(r"(?:^|; )meeting: (?P<who>[^;:]+?) said in (?P<where>[^:;]+): “(?P<quote>[^”]+)”")


def _said(evidence: str) -> str:
    m = _SAID_RE.search(evidence or "")
    return f"{m.group('who')} said in {m.group('where')}: “{m.group('quote')}”" if m else ""


def _why_reached(row, source, name="") -> str:
    if row["needs_review"]:
        return "An earlier call this leaned on was replaced since, so it needs another look."
    if row["kind"] == "prediction" and source is not None:
        who = source["answered_by"] or source["signed_by"] or "someone"
        whose = "your own" if _same_person(source, name) else f"{who}'s"
        when = _day(source["created_at"])
        return (f"The closest earlier call is {whose}{f', from {when}' if when else ''}, and Raven is not sure it "
                "carries over to this case, so it asks instead of applying it.")
    if row["kind"] == "prediction":
        return "Raven has a likely answer, but nothing on record settles it."
    if row["status"] != "pending" and (row["answer"] or "").strip():
        return "Raven found an answer on record, but nobody has signed it for this case."
    # What was said about it in a meeting, or what the records said instead
    # of an answer, is the context the person is asked against, never
    # Raven's guess. Measured on the Parcelwise dry run (Nov 2): "I still
    # need to land the rate" came to Priya as "Raven would apply this".
    said = _said(row["evidence"] or "")
    if said:
        return f"Nothing on record settles this; {said}."
    if _gaps(row):
        return "Nothing on record settles this."
    return "No earlier call covers this."


def _needs_item(db, row, task_rows, name=""):
    source = _source(db, row["source_id"])
    others = [r for r in task_rows if r["id"] != row["id"] and r["status"] not in HIDDEN_STATUSES]
    settled = [r for r in others if r["status"] == "approved" or (r["signoff"] in ("signed", "rule") and not r["needs_review"])]
    waiting = len(others) - len(settled)
    agent = display_name(row["run_agent"]) if row["run_agent"] else "The coding agent"
    # What the agent is doing now, from the run's host session and its last
    # wait, never a fixed phrase: on dry run 10 the card said the agent kept
    # building while it had written nothing and was waiting.
    from .host_sessions import agent_state
    meanwhile = {"stopped": f"{agent} has stopped and is waiting on this",
                 "waiting": f"{agent} is waiting on this"}.get(
        agent_state(db, row["run_id"]), f"{agent} keeps building the parts that don't depend on this")
    if settled:
        meanwhile += f"; {len(settled)} other call{'s' if len(settled) != 1 else ''} on this task {'are' if len(settled) != 1 else 'is'} already settled"
    if waiting:
        meanwhile += f", {waiting} still with other people" if settled else f"; {waiting} other call{'s' if waiting != 1 else ''} on this task still wait on other people"
    meanwhile += "."
    area = _area(row["owner_evidence"] or "")
    why = _why_reached(row, source, name)
    if area and not row["needs_review"]:
        why += f" {area[0].upper()}{area[1:]}."
    guess = (row["prediction"] or "").strip() if row["kind"] == "prediction" or not (row["answer"] or "").strip() else (row["answer"] or "").strip()
    return {
        "id": row["id"], "run_id": row["run_id"], "task": row["run_title"], "agent": _agent(row),
        "requester": row["run_requester"] or "", "question": row["question"], "context": _clip(row["context"], 420),
        "options": _options(row["options"]), "status": row["status"], "kind": row["kind"] or "",
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "why": why, "guess": guess, "gaps": _gaps(row),
        "guess_from": _call(source, row["source_revision"]) if source is not None else None,
        "review_reason": row["review_reason"] or "", "meanwhile": meanwhile,
        "review": _review(db, row["id"]),
    }


def _review(db, decision_id) -> dict:
    """The source versions an answer here binds to, as the task page sends them: a call
    that leaned on an earlier call needs them, or the answer is refused for review."""
    from . import context_memory as cm
    review = cm.source_revalidation(db, decision_id)
    return {"has_reliance": bool(review.get("has_reliance")), "available": bool(review.get("available")),
            "pins": review.get("pins") or [], "decision_pins": review.get("decision_pins") or []}


def _handled_item(db, row, source, call=None):
    return {
        "id": row["id"], "run_id": row["run_id"], "task": row["run_title"], "agent": _agent(row),
        "question": row["question"], "answer": row["answer"] or "", "fit_reason": row["fit_reason"] or "",
        "gaps": _gaps(row), "when": row["updated_at"], "updated_at": row["updated_at"],
        "call": call if call is not None else _call(source, row["source_revision"]),
    }


def _meeting_call(row):
    """What the person said in the meeting, as the call Raven used: the
    verbatim quote (the node's rationale), the meeting named in the fit
    reason, dated by the meeting."""
    fit = row["fit_reason"] or ""
    where = fit.split(":", 1)[0] if fit.startswith("From ") else ""
    return {"id": "", "run_id": "", "task": where[5:] if where.startswith("From ") else "a meeting",
            "question": "", "answer": row["rationale"] or "", "by": row["answered_by"] or "",
            "when": row["source_revision"] or row["created_at"], "answered_via": "meeting", "replaced_by": ""}


def overview(store, me) -> dict:
    """The viewer's attention, from the decisions of the tasks being built
    now and the calls that changed in the last {CHANGED_DAYS} days."""
    graph = store.graph
    db = graph.db
    name = (getattr(me, "name", "") or "").strip()
    person_id = getattr(me, "id", "") or ""
    actor = authz.Actor(id=person_id, name=name, role=getattr(me, "role", "member") or "member",
                        kind=getattr(me, "kind", "session") or "session")
    out = {"person": {"id": person_id, "name": name}, "needs_you": [], "handled": [], "changed": [],
           "counts": {"needs_you": 0, "handled": 0, "live_tasks": 0, "asked_elsewhere": 0}}
    if not name and not person_id:
        return out
    rows = db.execute(_SELECT + f" AND d.superseded_by='' AND r.status NOT IN {LIVE_EXCLUDED!r} "
                      "ORDER BY d.created_at").fetchall()
    rows = [r for r in rows if r["status"] not in HIDDEN_STATUSES]
    by_task: dict[str, list] = {}
    for r in rows:
        by_task.setdefault(r["run_id"], []).append(r)
    out["counts"]["live_tasks"] = len(by_task)
    for r in rows:
        waits = authz.waits_on_a_person(dict(r))
        if not waits:
            continue
        # Assigned to this person (the owner, or a signer it still needs),
        # by id when signed in: not everything they have the authority to
        # touch. The local operator (no id) sees the name they act as.
        mine = (any(authz.basis_for(graph, actor, dict(r), action)[0] in ("owner", "required-signer")
                    for action in ("answer", "sign"))
                if person_id and actor.kind != "operator" else _same_person(r, name))
        if mine:
            out["needs_you"].append(_needs_item(db, r, by_task[r["run_id"]], name))
        else:
            out["counts"]["asked_elsewhere"] += 1
    out["needs_you"].sort(key=lambda x: x["created_at"])
    for r in rows:
        if r["signoff"] not in ("signed", "rule") or r["status"] == "approved" or r["needs_review"]:
            continue
        if r["source"] == "meeting" and not r["source_id"]:
            # Settled from what this person said in a meeting: the call it
            # used is their words there, not an earlier decision.
            if _same_person(r, name):
                out["handled"].append(_handled_item(db, r, None, call=_meeting_call(r)))
            continue
        if not r["source_id"]:
            continue
        source = _source(db, r["source_id"])
        if source is None or not _same_person(source, name):
            continue
        out["handled"].append(_handled_item(db, r, source))
    out["handled"].sort(key=lambda x: x["when"], reverse=True)
    since = (datetime.now(timezone.utc) - timedelta(days=CHANGED_DAYS)).isoformat()
    changed = db.execute(_SELECT + " AND d.supersedes!='' AND d.updated_at>=? ORDER BY d.updated_at DESC LIMIT 40", (since,)).fetchall()
    seen = set()
    for new in changed:
        old = _source(db, new["supersedes"])
        if old is None or new["id"] in seen:
            continue
        i_corrected = _same_person(new, name)
        it_was_mine = _same_person(old, name)
        if not (i_corrected or it_was_mine):
            continue
        seen.add(new["id"])
        why = ""
        for event in db.execute("SELECT detail FROM events WHERE decision_id=? AND kind='precedent_corrected' ORDER BY id DESC LIMIT 1", (new["id"],)):
            try:
                why = json.loads(event["detail"]).get("why") or ""
            except ValueError:
                why = ""
        out["changed"].append({
            "id": new["id"], "run_id": new["run_id"], "task": new["run_title"], "when": new["updated_at"],
            "by": new["signed_by"] or new["answered_by"] or "", "mine": i_corrected, "was_mine": it_was_mine,
            "question": new["question"], "answer": new["answer"] or "", "why": why or new["rationale"] or "",
            "replaced": _call(old), "now_precedent": bool(new["reusable"]),
        })
    out["changed"] = out["changed"][:6]
    out["counts"]["needs_you"] = len(out["needs_you"])
    out["counts"]["handled"] = len(out["handled"])
    return out
