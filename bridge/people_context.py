"""One person's context: what Raven has learned about them, newest first.

Replaces the map. Instead of drawing people against area words, each
person gets one page: what they decide and know (the authority map), the
code they have written lately (git), what they told Raven (intro calls,
notes added by them or through an agent) and the decisions they answered.
It is a living list: everything on it carries who said it, where and when,
and the person (or an admin) can reword, drop or add a line.

The same reads back Ask Raven and the MCP tools, so an agent can ask
"who knows about rate cards and what did they say" and get these lines.
"""
from __future__ import annotations

import re
import secrets

from . import authz
from .store import Invalid, now

NOTE_LIMIT = 600
SOURCES_EDITABLE = {"intro chat", "added by them", "added by an admin", "via an agent", "from a call", "from a meeting"}


def _keys(person):
    keys = {person["name"].strip().lower()}
    for value in (person.get("email"), person.get("github_login"), *(person.get("aliases") or [])):
        if value:
            keys.add(str(value).strip().lower())
    return keys


def _person(store, person_id):
    person = store.graph.get_person(person_id)
    if not person:
        raise Invalid("Person not found")
    return person


def _may_edit(store, actor, person):
    """A person edits their own context; an admin (or the local operator)
    edits anyone's. Nobody else rewrites what someone said."""
    if actor is None:
        raise authz.Refused("Sign in to change what Raven knows")
    if actor.kind == "operator" or getattr(actor, "role", "") == "admin":
        return
    if actor.kind in ("session", "token") and actor.id == person["id"]:
        return
    raise authz.Refused(f"Only {person['name']} or an admin can change what Raven knows about them")


def _notes(store, person_id):
    rows = store.graph.db.execute(
        "SELECT id, topic, text, quote, question, source, interview_id, artifact, created_at FROM person_context "
        "WHERE person_id=? AND removed_at='' ORDER BY created_at DESC, seq", (person_id,)).fetchall()
    return [dict(r) for r in rows]


def _decided(store, person, limit=20):
    keys = sorted(_keys(person))
    marks = ",".join("?" * len(keys))
    rows = store.graph.db.execute(
        f"SELECT id, run_id, question, answer, rationale, answered_by, updated_at FROM decisions "
        f"WHERE status='approved' AND lower(coalesce(answered_by,'')) IN ({marks}) "
        f"ORDER BY updated_at DESC LIMIT ?", (*keys, limit)).fetchall()
    return [dict(r) for r in rows]


def _code(store, person, limit=6):
    keys = _keys(person)
    rows = [o for o in store.ownership(limit=500) if str(o.get("engineer", "")).lower() in keys
            and o.get("source") in ("blame", "blame_recent", "commits") and o.get("path_prefix")]
    rows.sort(key=lambda o: -(o.get("weight") or 0))
    seen, out = set(), []
    for o in rows:
        if o["path_prefix"] in seen:
            continue
        seen.add(o["path_prefix"])
        out.append({"path": o["path_prefix"], "repo": o.get("repo", ""), "share": round(o.get("weight") or 0, 2)})
        if len(out) >= limit:
            break
    return out


def directory(store, query=""):
    """Everyone, searchable by name, team, area or anything they said."""
    q = str(query or "").strip().lower()[:200]
    people = store.people()
    counts = {r["person_id"]: (r["n"], r["latest"]) for r in store.graph.db.execute(
        "SELECT person_id, COUNT(*) AS n, MAX(created_at) AS latest FROM person_context WHERE removed_at='' "
        "GROUP BY person_id").fetchall()}
    said = {}
    if q:
        for r in store.graph.db.execute("SELECT person_id, text FROM person_context WHERE removed_at=''").fetchall():
            if q in r["text"].lower():
                said.setdefault(r["person_id"], r["text"])
        for p in people:
            if p["id"] in said:
                continue
            for d in _decided(store, p, limit=40):
                text = f'{d["question"]} {d["answer"] or ""}'
                if q in text.lower():
                    said[p["id"]] = d["answer"] or d["question"]
                    break
    out = []
    for p in people:
        if not p.get("active", 1):
            continue
        areas = [a["scope"] for a in p.get("authority", []) if not a.get("ended_at")]
        hay = " ".join([p["name"], p.get("email") or "", p.get("github_login") or "", *p.get("teams", []), *areas]).lower()
        match = said.get(p["id"], "")
        if q and q not in hay and not match:
            continue
        n, latest = counts.get(p["id"], (0, ""))
        out.append({"id": p["id"], "name": p["name"], "teams": p.get("teams", []),
                    "decides": [a["scope"] for a in p.get("authority", []) if a.get("role") == "decides" and not a.get("ended_at")][:4],
                    "notes": n, "learned_at": latest or "", "match": match})
    out.sort(key=lambda r: (not r["match"], -(1 if r["notes"] else 0), r["name"].lower()))
    return {"people": out, "query": q}


def profile(store, person_id):
    """Everything Raven holds about one person, as one timeline."""
    person = _person(store, person_id)
    full = next((p for p in store.people() if p["id"] == person_id), person)
    authority = [a for a in full.get("authority", []) if not a.get("ended_at")]
    timeline = []
    from . import artifacts
    pointers = {}

    def point(ref):
        if ref and ref not in pointers:
            kind, _, ref_id = ref.partition(":")
            pointers[ref] = artifacts.pointer(store, kind, ref_id)
        return pointers.get(ref) if ref else None
    for n in _notes(store, person_id):
        ref = n.get("artifact") or ("intro:" + n["interview_id"] if n["interview_id"] else "")
        timeline.append({"kind": "said", "id": n["id"], "text": n["text"], "quote": n["quote"],
                         "question": n["question"], "topic": n["topic"], "source": n["source"],
                         "at": n["created_at"], "editable": n["source"] in SOURCES_EDITABLE, "pointer": point(ref)})
    for d in _decided(store, person):
        call = artifacts.call_for(store, d["id"])
        timeline.append({"kind": "decided", "id": d["id"], "text": d["answer"] or "", "question": d["question"],
                         "why": d["rationale"] or "", "task_id": d["run_id"],
                         "source": "on a call with Raven" if call else "answered an agent",
                         "at": d["updated_at"], "editable": False,
                         "pointer": point("call:" + call if call else "decision:" + d["id"])})
    timeline.sort(key=lambda t: t["at"] or "", reverse=True)
    from . import intro
    latest = store.graph.db.execute("SELECT status, saved_at FROM intro_interviews WHERE person_id=? "
                                    "ORDER BY created_at DESC LIMIT 1", (person_id,)).fetchone()
    return {"person": {"id": person["id"], "name": person["name"], "email": person.get("email") or "",
                       "github_login": person.get("github_login") or "", "teams": full.get("teams", [])},
            "decides": [{"scope": a["scope"], "note": a.get("note") or ""} for a in authority if a.get("role") == "decides"],
            "knows": [{"scope": a["scope"], "note": a.get("note") or ""} for a in authority if a.get("role") != "decides"],
            "code": _code(store, person),
            "timeline": timeline,
            "intro": {"status": latest["status"] if latest else "not_started", "saved_at": latest["saved_at"] if latest else ""},
            "topics": intro.TOPICS}


def remember(store, person_id, text, source="added by them", topic="other", quote="", question="", artifact=""):
    """Keep one line about a person. Returns the stored row."""
    from .intro import TOPICS
    person = _person(store, person_id)
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text or len(text) > NOTE_LIMIT:
        raise Invalid(f"A note must be 1 to {NOTE_LIMIT} characters")
    topic = topic if topic in TOPICS else "other"
    row = {"id": secrets.token_hex(10), "person_id": person_id, "topic": topic, "text": text,
           "quote": str(quote or "")[:NOTE_LIMIT], "question": str(question or "")[:400], "source": source,
           "artifact": str(artifact or "")[:120], "created_at": now()}
    with store.graph.transaction() as db:
        db.execute("INSERT INTO person_context(id, person_id, interview_id, topic, text, quote, question, source, seq, artifact, created_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (row["id"], person_id, "", topic, text, row["quote"], row["question"],
                                                   source, 0, row["artifact"], row["created_at"]))
        store.graph.append_event("context_added", {"person_id": person_id, "person_name": person["name"],
                                                   "note_id": row["id"], "source": source}, db=db)
    return row


def edit(store, person_id, data, actor=None):
    """add | edit | remove one line of a person's context."""
    person = _person(store, person_id)
    _may_edit(store, actor, person)
    action = data.get("action")
    if action == "add":
        own = actor is not None and actor.id == person_id
        remember(store, person_id, data.get("text"), "added by them" if own or actor.kind == "operator" else "added by an admin",
                 data.get("topic") or "other")
        return profile(store, person_id)
    note_id = str(data.get("id") or "")[:40]
    row = store.graph.db.execute("SELECT * FROM person_context WHERE id=? AND person_id=? AND removed_at=''",
                                 (note_id, person_id)).fetchone()
    if not row:
        raise Invalid("That note is gone or isn't this person's")
    with store.graph.transaction() as db:
        if action == "remove":
            db.execute("UPDATE person_context SET removed_at=? WHERE id=?", (now(), note_id))
        elif action == "edit":
            text = re.sub(r"\s+", " ", str(data.get("text") or "")).strip()
            if not text or len(text) > NOTE_LIMIT:
                raise Invalid(f"A note must be 1 to {NOTE_LIMIT} characters")
            # The quote it rests on stays as it was said.
            db.execute("UPDATE person_context SET text=? WHERE id=?", (text, note_id))
        else:
            raise Invalid("action must be add, edit or remove")
        store.graph.append_event("context_" + ("removed" if action == "remove" else "edited"),
                                 {"person_id": person_id, "note_id": note_id}, db=db)
    return profile(store, person_id)


_STOP = set("the a an and or but to of in on for with is are was were be been it this that what who how why when "
            "do does did we our you your they their i me my about from at by as should would could can".split())


def _terms(text):
    return {w[:-1] if w.endswith("s") and len(w) > 4 else w for w in re.findall(r"[a-z0-9]+", text.lower())
            if len(w) > 2 and w not in _STOP}


def relevant(store, question, limit=5):
    """What people told Raven that bears on a question: lines sharing its
    words, or about a person it names."""
    terms = _terms(question)
    people = {p["id"]: p for p in store.graph.people(active_only=False)}
    named = {pid for pid, p in people.items() if any(k and re.search(r"\b" + re.escape(k) + r"\b", question.lower())
                                                    for k in {p["name"].lower(), p["name"].split()[0].lower()})}
    scored = []
    for r in store.graph.db.execute("SELECT id, person_id, text, quote, source, interview_id, artifact, created_at FROM person_context "
                                    "WHERE removed_at=''").fetchall():
        overlap = len(terms & _terms(r["text"] + " " + (r["quote"] or "")))
        score = overlap + (2 if r["person_id"] in named else 0)
        if score >= 2 and r["person_id"] in people:
            scored.append((score, r["created_at"], {**dict(r), "person_name": people[r["person_id"]]["name"]}))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [s[2] for s in scored[:limit]]
