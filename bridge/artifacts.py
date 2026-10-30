"""What a learned line points at.

Every line Raven keeps about a person, and every decision, is a node with a
pointer to the first-class thing it came from: the voice intro it was said
in, the decision call, the meeting transcript, the task an agent asked it
in. The node holds a one-line summary; ``resolve`` returns the whole thing
(the full transcript, with the line that was quoted marked) so anyone can
check what was actually said.

Pointers are ``kind:id``. Kinds: intro, call, meeting, decision.
"""
import json
import re

from .store import Invalid

LABELS = {"intro": "Voice intro", "call": "Decision call", "meeting": "Meeting transcript", "decision": "Decision"}


def _short(text, limit=140):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _person(store, person_id):
    p = store.graph.get_person(person_id) if person_id else None
    return {"id": p["id"], "name": p["name"]} if p else None


def _loads(raw, default):
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _mark(turns, quote):
    """Which turn the quoted words came from."""
    norm = lambda s: " ".join(re.findall(r"[a-z0-9']+", str(s or "").lower()))
    q = norm(quote)
    for t in turns:
        t["quoted"] = bool(q) and q in norm(t.get("text"))
    return turns


def _intro(store, ref_id):
    row = store.graph.db.execute("SELECT * FROM intro_interviews WHERE id=?", (ref_id,)).fetchone()
    if not row:
        raise Invalid("Voice intro not found")
    person = _person(store, row["person_id"])
    who = person["name"] if person else "Them"
    turns = []
    for t in _loads(row["turns"], []):
        if t.get("question"):
            turns.append({"who": "Raven", "text": t["question"]})
        if t.get("answer"):
            turns.append({"who": who, "text": t["answer"], "person_id": row["person_id"]})
    notes = _loads(row["summary"], {}).get("notes", [])
    learned = [n.get("text", "") for n in notes if n.get("text")]
    minutes = _minutes(row["started_at"], row["ended_at"] or row["saved_at"])
    return {"title": f"Voice intro with {who}", "at": row["saved_at"] or row["ended_at"] or row["created_at"],
            "people": [person] if person else [], "turns": turns, "learned": learned,
            "summary": f"{who} on a {minutes + ' ' if minutes else ''}voice intro: " + (
                _short(" ".join(learned[:2]), 160) if learned else f"{len([t for t in turns if t['who'] != 'Raven'])} answers")}


def _minutes(start, end):
    from datetime import datetime
    try:
        seconds = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except (TypeError, ValueError):
        return ""
    return f"{max(1, round(seconds / 60))}-minute" if seconds > 0 else ""


def _call(store, ref_id):
    row = store.graph.db.execute("SELECT * FROM interviews WHERE id=?", (ref_id,)).fetchone()
    if not row:
        raise Invalid("Call not found")
    person = _person(store, row["person_id"])
    who = person["name"] if person else "Them"
    decision = store.get_decision(row["decision_id"]) or {}
    turns = []
    for t in _loads(row["turns"], []):
        if t.get("prompt") or t.get("question"):
            turns.append({"who": "Raven", "text": t.get("prompt") or t.get("question")})
        if t.get("response") or t.get("answer"):
            turns.append({"who": who, "text": t.get("response") or t.get("answer"), "person_id": row["person_id"]})
    if not turns and row["transcript"]:
        turns = [{"who": who, "text": row["transcript"], "person_id": row["person_id"]}]
    return {"title": f"Call with {who}", "at": row["updated_at"], "people": [person] if person else [],
            "turns": turns, "question": decision.get("question", ""), "answer": row["answer"] or decision.get("answer", ""),
            "task_id": row["task_id"], "node_id": row["decision_id"],
            "summary": f"{who} on a call: " + _short(row["answer"] or decision.get("answer", ""), 150)}


def _decision(store, ref_id):
    d = store.get_decision(ref_id)
    if not d:
        raise Invalid("Decision not found")
    task = store.graph.get_task(d.get("run_id") or "")
    task = dict(task) if task else {}
    # Who made the call: whoever recorded or signed the answer, never just
    # whoever it was first routed to.
    who = d.get("actor_name") or d.get("answered_by") or d.get("signed_by") or ""
    person = store.graph.find_person(who) if who else None
    return {"title": task.get("title") or "Decision", "at": d.get("updated_at", ""),
            "people": [{"id": person["id"], "name": person["name"]}] if person else [],
            "question": d.get("question", ""), "answer": d.get("answer", ""), "why": d.get("rationale") or "",
            "signed_by": d.get("signed_by") or "", "task_id": d.get("run_id", ""), "node_id": d["id"], "turns": [],
            "summary": _decided_line(who, d.get("signed_by") or "") + _short(d.get("answer"), 150)}


def _decided_line(who, signer):
    if who and signer and signer.lower() != who.lower():
        return f"{who} answered and {signer} signed off: "
    return f"{who} decided: " if who else "Decided: "


def _meeting(store, ref_id):
    row = store.graph.db.execute("SELECT * FROM meeting_occurrences WHERE id=?", (ref_id,)).fetchone()
    if not row:
        raise Invalid("Meeting not found")
    turns = []
    for chunk in store.graph.db.execute(
            "SELECT c.record_id FROM meeting_transcript_chunks c WHERE c.meeting_id=? AND c.active=1 ORDER BY c.chunk_key",
            (ref_id,)).fetchall():
        try:
            read = store.get_record(chunk["record_id"], row["repo"])
        except Invalid:
            continue
        version = read["source"].get("source_version_id")
        body = next((v["snapshot"].get("body", "") for v in read["versions"] if v["id"] == version), "")
        for line in body.splitlines():
            m = re.match(r"^\s*(?:\[[^\]]*\]\s*)*([^:\[\]]{1,60}):\s+(.+)$", line)
            if m:
                person = store.graph.find_person(m.group(1).strip())
                turns.append({"who": m.group(1).strip(), "text": m.group(2).strip(),
                              **({"person_id": person["id"]} if person else {})})
            elif line.strip():
                turns.append({"who": "", "text": re.sub(r"^(?:\[[^\]]*\]\s*)+", "", line.strip())})
    learned = [dict(r) for r in store.graph.db.execute(
        "SELECT p.person_id, p.text FROM person_context p WHERE p.artifact=? AND p.removed_at=''", ("meeting:" + ref_id,))]
    people, seen = [], set()
    for t in turns + learned:
        pid = t.get("person_id")
        if pid and pid not in seen:
            seen.add(pid)
            people.append(_person(store, pid))
    return {"title": row["title"], "at": row["updated_at"], "people": [p for p in people if p], "turns": turns,
            "learned": [x["text"] for x in learned], "repo": row["repo"],
            "summary": _meeting_summary(store, ref_id) or (_short(learned[0]["text"], 160) if learned
                                                          else f"{len(turns)} lines of transcript")}


def _meeting_summary(store, ref_id):
    for r in store.graph.db.execute("SELECT detail FROM events WHERE kind='meeting_learned' ORDER BY id DESC LIMIT 50"):
        d = _loads(r["detail"], {})
        if d.get("meeting_id") == ref_id and d.get("summary"):
            return d["summary"]
    return ""


RESOLVERS = {"intro": _intro, "call": _call, "meeting": _meeting, "decision": _decision}


def resolve(store, kind, ref_id, quote=""):
    if kind not in RESOLVERS:
        raise Invalid("Unknown source kind")
    out = RESOLVERS[kind](store, str(ref_id)[:100])
    out.update(kind=kind, id=ref_id, ref=f"{kind}:{ref_id}", kind_label=LABELS[kind])
    _mark(out["turns"], quote)
    return out


def pointer(store, kind, ref_id):
    """The node's view of its source: what it is and a one-line summary."""
    try:
        full = resolve(store, kind, ref_id)
    except Invalid:
        return None
    return {k: full.get(k) for k in ("ref", "kind", "id", "kind_label", "title", "summary", "at")}


def call_for(store, decision_id):
    row = store.graph.db.execute("SELECT id FROM interviews WHERE decision_id=? AND status='confirmed' "
                                 "ORDER BY updated_at DESC LIMIT 1", (decision_id,)).fetchone()
    return row["id"] if row else ""
