"""The judgment log: what a reader needs to trust a call Raven settled.

A coding agent hits a judgment call; Raven settles it from a precedent (a
call the owning person already made) or routes it to that person. For the
settled ones a reader wants three things the row did not carry before:

* how the person's sign-off arrived (the browser, Slack, a Raven voice call,
  a meeting, a GitHub review): `answered_via`, recorded on every human
  answer and signature from the descriptive source label the transport
  already passes, never from client data alone;
* one line on why the precedent fits this case: `fit_reason`, written by
  the ladder when it settles from memory;
* one action that says "that's wrong, or doesn't apply here, here's why":
  `correct`, which records the person's answer as the signed answer of this
  decision through the ordinary answer path, retires the precedent it
  replaced (superseded_by) when the person says the old call was wrong
  everywhere, and makes the correction the standing precedent under their
  name, so the next similar question uses it.
"""
from __future__ import annotations

from .store import Invalid, check_revision, field, now

# The short tokens a page turns into words. Anything else is a label a
# transport invented later; the page shows it as it is.
CHANNELS = ("browser", "slack", "teams", "voice call", "call (typed)", "meeting", "github", "agent", "operator")


def received_via(db, source, actor=None) -> str:
    """How a human answer or signature reached Raven, from the descriptive
    source label the transport passes and the authenticated actor's kind.
    The label is not an identity claim; it only names the channel."""
    label = str(source or "").strip().lower()
    if label.startswith("slack"):
        return "slack"
    if label.startswith("teams"):
        return "teams"
    if label.startswith("confirmed interview"):
        interview_id = label.split(":", 1)[1].strip() if ":" in label else ""
        method = ""
        if interview_id:
            row = db.execute("SELECT capture_method FROM interviews WHERE id=?", (interview_id,)).fetchone()
            method = (row["capture_method"] if row is not None else "") or ""
        return "voice call" if method in ("browser-speech", "server-speech") else "call (typed)"
    if label.startswith("meeting"):
        return "meeting"
    if label.startswith("github"):
        return "github"
    if label.startswith(("inbox", "link", "page", "browser", "session")):
        return "browser"
    if label.startswith("local operator"):
        return "operator"
    kind = getattr(actor, "kind", "") if actor is not None else ""
    if kind in ("slack", "teams"):
        return kind
    if kind == "operator":
        return "operator"
    if kind == "agent":
        return "agent"
    if kind in ("session", "token", "bootstrap"):
        return "browser"
    return ""


def fit_reason(item, match_kind: str, rule_ok: bool, conditions=(), *, when: str = "", partial: bool = False,
               withheld: str = "") -> str:
    """One plain sentence on why a precedent fits the question it settled:
    how close the match was, whose call it was and when, and the standing
    its owner gave it. `item` is the memory row the ladder picked. With
    `partial` the answer settles only part of the question, so a person
    still signs it, rule or no rule: the reason says that, so it never
    reads "nobody needed to be asked" beside a sign-off that is required.
    `withheld` is why a standing rule was not applied ("a record
    disagrees", "the answer combines sources"): the sentence then says so,
    and never blames the owner for not making a rule of a row that is one.
    Measured on the Parcelwise dry run: three signed rules vetoed by a pull
    request read "Jordan did not make it a standing rule"."""
    who = item.answered_by or item.signed_by or "its owner"
    first = who.split()[0]
    if match_kind == "direct":
        how = f"The same question {who} already answered"
    elif match_kind == "semantic":
        how = f"The same situation, by meaning, that {who} answered"
    else:
        how = f"A close match to a question {who} answered"
    if when:
        how += f" on {when}"
    terms = [c for c in conditions if c]
    if rule_ok:
        scope = "anywhere" if item.rule_scope == "any" else "in this same area of the code"
        standing = f"{first} said that answer applies {scope}"
        if terms:
            standing += " when " + "; ".join(terms[:3])
        standing += (", but it settles this only in part, so a person still signs it here" if partial
                     else ", so nobody needed to be asked")
    elif withheld or getattr(item, "reusable", False):
        why = withheld or "it does not fit this question"
        standing = f"{first} made it a standing rule, but {why}, so a person still signs it here"
    else:
        standing = f"{first} did not make it a standing rule, so a person still signs it here"
    return f"{how}; {standing}."


def correct(store, decision_id: str, data, actor=None) -> dict:
    """A person says a settled call is wrong, or does not apply here, and
    gives the answer that does. The answer is recorded as this decision's
    signed answer (the ordinary answer path: revision check, standing
    check, history, dependents put in doubt). With scope 'everywhere' the
    precedent it was settled from is retired (superseded_by this decision)
    and the correction becomes the standing rule for the next similar
    question; with scope 'here' the old call stays for other cases and
    only this decision takes the new answer."""
    from . import authz
    graph = store.graph
    answer = field(data, "answer")
    why = field(data, "why") if data.get("why") else field(data, "rationale")
    scope = str(data.get("scope") or "everywhere").strip().lower()
    if scope not in ("everywhere", "here"):
        raise Invalid("scope must be 'everywhere' (the old call was wrong) or 'here' (it does not apply to this case)")
    current = store.get_decision(decision_id)
    if current.get("status") in ("duplicate", "suggested", "adopted", "withdrawn"):
        raise Invalid(f"a decision that is {current['status']} is not corrected here; its twin carries the answer")
    if not (current.get("answer") or "").strip():
        raise Invalid("Nothing is settled here yet: answer it instead of correcting it")
    check_revision(data, current)
    basis = authz.check(graph, actor, current, "correct")
    source_id = current.get("source_id") or ""
    prior = None
    if source_id:
        prior = graph.db.execute("SELECT d.*,o.name AS owner_name FROM decisions d LEFT JOIN owners o ON o.id=d.owner_id "
                                 "WHERE d.id=?", (source_id,)).fetchone()
    supersedes = source_id if scope == "everywhere" and prior is not None and not prior["superseded_by"] else ""
    by = (actor.name if actor is not None and actor.id else str(data.get("by") or current.get("owner_name") or ""))[:100]
    derived = bool(source_id) or current.get("source") in ("memory", "record")
    with graph.transaction() as db:
        row = db.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
        if row["updated_at"] != current["updated_at"]:
            raise Invalid("This decision changed while you were reviewing it. Reopen it before correcting it.")
        # The corrected decision is the signer's, as a correction at
        # sign-off is: by their id when a person is signed in.
        owner_id = (graph.owner_id_for_person(actor.id) if actor is not None and actor.id else None) \
            or (graph.owner_id_for(by) if by else None) or row["owner_id"]
        db.execute("UPDATE decisions SET owner_id=? WHERE id=?", (owner_id, decision_id))
        store.answer(decision_id, {
            "answer": answer, "rationale": why, "supersedes": supersedes,
            # The person says the precedent does not decide this: the
            # answer stands on its own, and the old derivation is history.
            "evidence_mode": "independent" if derived else "retain",
            "source": data.get("source") or (f"inbox: {by}" if by else None),
            "signed_by": by, "expected_updated_at": row["updated_at"]}, actor=actor, transaction_db=db)
        graph.append_event("precedent_corrected", {
            "task_id": row["run_id"], "decision_id": decision_id, "by": by, "basis": basis, "scope": scope,
            "replaced": supersedes, "replaced_owner": (prior["answered_by"] or prior["owner_name"] or "") if prior is not None else "",
            "why": why, "at": now()}, db=db)
    notice = f"Recorded as {by}'s answer." if by else "Recorded."
    if supersedes:
        notice += " The earlier call it was settled from is retired; the next similar question uses this one."
    elif scope == "everywhere" and prior is not None:
        notice += " The earlier call was already retired."
    elif scope == "here":
        notice += " The earlier call stays for other cases."
    # The correction is the standing precedent from here on: in the same
    # scope the old call held, when the old call said anywhere.
    if scope == "everywhere":
        try:
            fresh = store.get_decision(decision_id)
            wanted = "any" if prior is None or (prior["rule_scope"] or "") == "any" else "same"
            if fresh.get("signoff") == "signed" and (not fresh.get("reusable") or (fresh.get("rule_scope") or "") != wanted):
                store.make_rule(decision_id, {"by": by, "expected_updated_at": fresh["updated_at"], "scope": wanted}, actor=actor)
        except Invalid as error:
            notice += f" It stays request-specific: {error}"
    out = {"decision": store.get_decision(decision_id), "replaced": supersedes, "scope": scope, "notice": notice}
    return out

