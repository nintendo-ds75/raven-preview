"""The page a Slack message links to: one task, opened by the person it
was sent to, with or without an account.

A message to a person carries a link of their own. The link names the
person, the task and the decision they were asked about; it is a
random token stored only as its hash, it expires, and it can be
revoked. Opening it shows who asked for the task and what they asked
for, the decision waiting on this person, who else was contacted and
what they decided, and what has happened since. The person can answer
from it, on the same standing they would have in Slack: the link proves
who they are, and the permission check (bridge/authz.py) still decides
whether they may answer. A link never carries an administrator's
override, and it opens nothing but its own task.

The person can also add a note: context for the coding agent, which
reads it on the task's tree, attributed to them and never a signed
answer. They can withdraw a note they added, and the agent is told.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from datetime import datetime, timezone

from .store import Invalid, field

LINK_DAYS = 14


MODES = ("off", "static")


def mode(graph) -> str:
    """What a message links to, set by an administrator: `off`, no task
    link (messages read as they did before the page), or `static`, the
    page with the task's context, the answer form and a note box. Static
    is the default: it needs nothing configured. Any other stored value,
    such as an `agent` saved before that mode was taken out, reads as
    static."""
    value = graph.get_setting("brief_mode") or "static"
    return value if value in MODES else "static"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def migrate(db) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS brief_links (
            id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, person_id TEXT NOT NULL,
            run_id TEXT NOT NULL, decision_id TEXT NOT NULL DEFAULT '', notification_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '', expires_at INTEGER NOT NULL,
            revoked_at TEXT NOT NULL DEFAULT '', last_used_at TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS brief_links_run ON brief_links(run_id, person_id);
    """)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------- links ----------------

def link_days(graph) -> int:
    try:
        days = int(graph.get_setting("brief_link_days") or LINK_DAYS)
    except ValueError:
        days = LINK_DAYS
    return max(1, min(days, 90))


def mint(graph, person_id: str, run_id: str, decision_id: str = "", notification_id: str = "") -> str:
    """A new link for one person to one task: the token, shown once.
    The caller runs inside a transaction."""
    if not person_id or not run_id:
        raise Invalid("A task link names a person and a task")
    token = "rvn_" + secrets.token_urlsafe(24)
    graph.db.execute(
        "INSERT INTO brief_links(id, token_hash, person_id, run_id, decision_id, notification_id, created_at, "
        "expires_at) VALUES(?,?,?,?,?,?,?,?)",
        (secrets.token_hex(8), _hash(token), person_id, run_id, decision_id or "", notification_id or "", now_iso(),
         int(time.time()) + link_days(graph) * 86400))
    return token


def url_for(base_url: str, token: str) -> str:
    """The page's address. The token rides in the fragment, which the
    browser never sends: it stays out of server logs and Referer
    headers, and the page sends it as a header instead."""
    return f"{base_url.rstrip('/')}/brief#{token}"


def revoke(graph, link_id: str) -> None:
    graph.db.execute("UPDATE brief_links SET revoked_at=? WHERE id=? AND revoked_at=''", (now_iso(), link_id))


def mint_own(graph, person_id: str, run_id: str, decision_id: str = "") -> str:
    """A link a signed-in person makes for themselves, from the app. Only
    the hash of a token is kept, so an earlier one cannot be handed back;
    instead the one made before is revoked, and a person holds one such
    link per task however often they open the page. Every click minted
    another link that worked for two weeks. Links that came with a
    message are left alone. The caller runs inside a transaction."""
    graph.db.execute("UPDATE brief_links SET revoked_at=? WHERE person_id=? AND run_id=? AND notification_id='' "
                     "AND revoked_at=''", (now_iso(), person_id, run_id))
    return mint(graph, person_id, run_id, decision_id)


RESEND_PER_LINK_HOURS = 1
RESEND_PER_PERSON_DAY = 3


def resend(store, token: str) -> None:
    """A person whose link expired asks for a new one. The new link goes
    to the person the old one named, in their own Slack DM, never back to
    whoever is holding the old one, so asking proves nothing and gives
    nothing away; the caller says the same thing whatever happened, so
    the answer does not tell anyone whether a token was ever a link. A
    revoked link stays revoked, a link that still works needs no new one,
    and a person gets one new link per old link an hour and a few a day.
    Measured: a person with no account whose link had expired reached a
    page that told them to ask "whoever messaged you", which was Raven."""
    graph = store.graph
    token = (token or "").strip()
    if not token.startswith("rvn_") or len(token) > 100:
        return
    row = graph.db.execute("SELECT * FROM brief_links WHERE token_hash=?", (_hash(token),)).fetchone()
    if row is None or row["revoked_at"] or int(row["expires_at"]) > time.time() or mode(graph) == "off":
        return
    person = graph.get_person(row["person_id"])
    delivery = getattr(store, "delivery", None)
    if person is None or not person.get("active", 1) or delivery is None or not delivery.enabled \
            or not delivery.base_url:
        return
    destination, _pid, _note, kind = delivery._destination(person["name"])
    if kind != "dm":
        # A link signs in as its person: it goes where only they read it.
        return
    def since(hours):
        return datetime.fromtimestamp(time.time() - hours * 3600, timezone.utc).isoformat()
    mine = []
    for r in graph.db.execute("SELECT detail, created_at FROM events WHERE kind='brief_link_resent' AND created_at > ?",
                              (since(24),)):
        try:
            d = json.loads(r["detail"])
        except ValueError:
            continue
        if d.get("person_id") == person["id"]:
            mine.append((r["created_at"], d))
    if len(mine) >= RESEND_PER_PERSON_DAY or any(d.get("link_id") == row["id"] and at > since(RESEND_PER_LINK_HOURS)
                                                 for at, d in mine):
        return
    run = graph.db.execute("SELECT title, repo FROM runs WHERE id=?", (row["run_id"],)).fetchone()
    if run is None:
        return
    with graph.transaction():
        fresh = mint(graph, person["id"], row["run_id"], row["decision_id"], row["notification_id"])
        graph.append_event("brief_link_resent", {"task_id": row["run_id"], "person_id": person["id"],
                                                 "link_id": row["id"], "by": person["name"]})
    text = "\n".join([f"*A new link to your task, {person['name']}.*",
                      f"Task: {run['title']}" + (f" ({run['repo']})" if run["repo"] else ""),
                      f"<{url_for(delivery.base_url, fresh)}|Open the task> (your own link, no account needed)",
                      "You asked for this from a task link that had expired. If you did not, you can ignore it."])
    try:
        channel = destination
        if channel.startswith(("U", "W")):
            channel = delivery.transport.open_dm(channel)
        delivery.transport.post_message(channel, text, [{"type": "section", "text": {"type": "mrkdwn", "text": text}}])
    except Exception as error:
        with graph.transaction():
            graph.append_event("notification_failed", {"task_id": row["run_id"], "kind": "brief_link",
                                                       "to": person["name"], "error": str(error)[:300]})


class Link:
    """A link that checked out: who opened it, on which task, and the
    actor they act as."""

    def __init__(self, row, person):
        self.id = row["id"]
        self.run_id = row["run_id"]
        self.decision_id = row["decision_id"] or ""
        self.notification_id = row["notification_id"] or ""
        self.expires_at = int(row["expires_at"])
        self.person = person

    @property
    def asked(self) -> str:
        """The decision Raven messaged this person about, or "". Only a
        link that came with a message was asked: a link a signed-in
        person makes for themselves names a decision to look at, and
        must not let them take one nobody gave them."""
        return self.decision_id if self.notification_id else ""

    @property
    def actor(self):
        """The person, on the standing a Slack reply has. An admin's
        link acts as a member: overriding someone else's decision needs
        a signed-in admin who says so."""
        from .authz import Actor
        role = self.person.get("role") or "member"
        return Actor(id=self.person["id"], name=self.person["name"],
                     role=role if role in ("viewer", "member") else "member", kind="link")


def resolve(graph, token: str) -> Link | None:
    """The link this token is, or None: unknown, revoked, expired, or
    its person is no longer active."""
    token = (token or "").strip()
    if not token.startswith("rvn_") or len(token) > 100:
        return None
    row = graph.db.execute("SELECT * FROM brief_links WHERE token_hash=?", (_hash(token),)).fetchone()
    if row is None or row["revoked_at"] or int(row["expires_at"]) <= time.time():
        return None
    person = graph.get_person(row["person_id"])
    if person is None or not person.get("active", 1):
        return None
    with graph.transaction():
        graph.db.execute("UPDATE brief_links SET last_used_at=? WHERE id=?", (now_iso(), row["id"]))
    return Link(row, person)


# ---------------- what the page shows ----------------

def _flatten(nodes):
    out = []
    for n in nodes:
        out.append(n)
        out.extend(_flatten(n.get("children") or []))
    return out


def _person_card(graph, name: str) -> dict:
    """What the page says about a person named on the task: name, team
    and handles, never an email address or a credential."""
    person = graph.find_person(name) if name else None
    if person is None:
        return {"name": name, "known": False}
    card = {"name": person["name"], "known": True, "team": person.get("team") or "",
            "github": person.get("github_login") or "", "role": person.get("role") or "member"}
    teams = [t["name"] for t in graph.teams() if person["id"] in (t.get("members") or [])]
    if teams:
        card["teams"] = teams
    return card


# What a write path records when nobody gave a reason. They are markers,
# not reasoning: shown under "Why:" they read as the person's own words.
_NO_REASON = frozenset(("answered from the task page", "corrected from the task page", "no additional rationale "
                        "supplied", "answered in slack", "corrected in slack", "corrected at sign-off"))


def _reason(text: str) -> str:
    """A rationale as the page shows it: "" for a placeholder."""
    text = (text or "").strip()
    return "" if text.lower() in _NO_REASON else text


def _node_summary(n: dict, actor, graph, store, asked: str = "") -> dict:
    from . import authz
    status = ("needs_review" if n["needs_review"] else "signed" if n["authorized"]
              else "waiting" if n["blocking"] else n["status"])
    out = {k: n.get(k) for k in ("node_id", "question", "answer", "rationale", "owner", "answered_by", "signed_by",
                                 "required_signers", "signatures", "authorized", "blocking", "needs_review",
                                 "review_reason", "path", "kind", "depth", "origin", "partial", "options")}
    out["rationale"] = _reason(out.get("rationale"))
    out["state"] = status
    out["raw_status"] = n["status"]
    out["can_act"] = False
    if n["status"] not in ("duplicate", "suggested", "adopted"):
        try:
            decision = store.get_decision(n["node_id"])
        except Invalid:
            decision = None
        if decision is not None:
            action = "answer" if decision["status"] == "pending" else "sign"
            basis, why = authz.basis_for(graph, actor, decision, action)
            if not decision.get("owner_id") and decision["status"] == "pending":
                # An answer needs an owner. The person Raven asked takes
                # it, as act() does; anyone else would be refused, so the
                # page does not offer them the form.
                basis, why = (("asked", "") if n["node_id"] == asked and actor.role != "viewer"
                              else ("", "nobody owns this decision yet"))
            out["can_act"] = bool(basis)
            out["why_not"] = why if not basis else ""
            out["action"] = action
            out["updated_at"] = decision["updated_at"]
            # Who the viewer handed it on to: the list offered "Answer
            # this" on it, and the card it opened said it waits on them.
            out["handed_to"] = _handed_to(graph, decision, actor.name)
    return out


_APPENDED = ("Paths: ", "Options: ")
_NO_CONTEXT = "a node on the canvas"


def _own_context(context: str) -> tuple[str, list[str]]:
    """The context as the agent wrote it, and the paths it is about.

    canvas.add_node writes a node's paths and options onto the end of its
    context, so routing and a plain-text Slack message carry them. The
    page shows both already, the options as buttons and the paths as
    "Touches", so on the page those lines only repeat. A node given no
    context at all stores a placeholder, which is not context either."""
    lines = (context or "").split("\n")
    paths: list[str] = []
    while lines and lines[-1].startswith(_APPENDED):
        line = lines.pop()
        if line.startswith("Paths: "):
            paths = [p.strip() for p in line[len("Paths: "):].split(", ") if p.strip()]
    text = "\n".join(lines).strip()
    return ("" if text == _NO_CONTEXT else text), paths


def _same(a: str, b: str) -> bool:
    return bool(a) and (a or "").strip().lower() == (b or "").strip().lower()


def _plain(text: str) -> str:
    """Evidence words for a person: no hex ids, and the product's name as
    the page says it."""
    text = re.sub(r"\bdecision [0-9a-f]{12}\b", "an earlier decision", text or "")
    return re.sub(r"\bBridge\b", "Raven", text).strip()


_DECISION_REF = re.compile(r"\bdecision ([0-9a-f]{12})\b")
# A record as the ladder cites it: "<kind> <ref>[ [status]]: <title>".
_RECORD_REF = re.compile(r"\b([a-z][a-z_-]*) (\S+?)(?: \[[^\]]*\])?: ")
_RECORD_KINDS = {"pr": "Pull request", "commit": "Commit", "ticket": "Ticket", "doc": "Document", "docs": "Document",
                 "slack": "Slack thread", "jira": "Jira issue", "issue": "Issue", "adr": "Decision record"}


def _found(store, d: dict, viewer: str) -> list[dict]:
    """What Raven found, for the page: each earlier decision and record
    the evidence cites, read from the store and said in plain words, and
    any conflict between two of them.

    The evidence string is the audit trail, and a Slack message carries
    a cut of it. Measured on prometheus/prometheus: the page showed that
    cut, with the ladder's "assumption: 'policy' is not a low-stakes
    category", a decision id, "similarity 0.38", Slack's underscores as
    text, and "[cut here: the inbox has the rest]" to a person with no
    inbox. Nothing is copied from it here but what it cites; a search
    that found nothing says nothing."""
    from .delivery import _conflicts
    evidence = d.get("evidence") or ""
    graph = store.graph
    out: list[dict] = []
    for text in _conflicts(evidence):
        out.append({"kind": "conflict", "lead": "Two sources Raven holds disagree:", "question": "",
                    "quote": _plain(text), "note": ""})
    # A decision the ladder read as how the owner decides: after a hand-on
    # the owner is someone else, and "a related question" hid whose
    # judgment it was.
    how = set(re.findall(r"how they decide:.*?\bdecision ([0-9a-f]{12})\b", evidence))
    seen: set = set()
    for did in _DECISION_REF.findall(evidence):
        if did in seen or did == d["id"]:
            continue
        seen.add(did)
        prior = graph.db.execute("SELECT d.run_id, d.question, d.answer, d.answered_by, d.signoff, d.status, "
                                 "o.name AS owner_name FROM decisions d LEFT JOIN owners o ON o.id = d.owner_id "
                                 "WHERE d.id=?", (did,)).fetchone()
        if prior is None:
            continue
        where = "on this task" if prior["run_id"] == d["run_id"] else "on another task"
        who = prior["answered_by"] or ""
        if prior["answer"] and who:
            if did in how and (not _same(who, d.get("owner_name") or "")
                               or not _bears_on(d.get("question") or "", prior["question"], prior["answer"])):
                # "How they decide" was read for the owner Raven asked; once
                # it is handed on it describes somebody else's judgment. And
                # a match under the prediction floor is not similar: on
                # prometheus/prometheus, watcher memory per tenant was shown
                # as "a similar question" to rejecting out-of-order samples.
                continue
            signed = prior["signoff"] in ("signed", "rule")
            name = "you" if _same(who, viewer) else who
            out.append({"kind": "decision", "question": prior["question"], "quote": prior["answer"],
                        "lead": (f"Earlier, {name} answered a question {where} that may bear on this:" if did in how
                                 else f"Earlier, {name} answered a related question {where}:"),
                        "note": _signed_note(signed)})
        elif prior["status"] == "pending":
            out.append({"kind": "decision", "question": prior["question"], "quote": "",
                        "lead": f"A question still open {where} asks something similar:",
                        "note": f"Waiting on {prior['owner_name']}." if prior["owner_name"] else ""})
    for kind, ref in _RECORD_REF.findall(evidence):
        if (kind, ref) in seen:
            continue
        row = graph.db.execute("SELECT kind, ref, title, body, author, status FROM intents WHERE kind=? AND ref=?",
                               (kind, ref)).fetchone()
        if row is None:
            continue
        seen.add((kind, ref))
        author, status = row["author"] or "", row["status"] or ""
        out.append({"kind": "record", "question": "", "quote": row["title"] or "",
                    "lead": f"{_RECORD_KINDS.get(kind, kind.capitalize())} {ref}"
                            + (f" by {author}" if author else "") + ":",
                    "note": status[:1].upper() + status[1:] + "." if status else ""})
    if _open(d):
        hit = _precedent(store, d, seen)
        if hit is not None:
            who = hit.get("answered_by") or hit.get("signed_by") or "someone"
            out.append({"kind": "decision", "question": hit["question"], "quote": hit["answer"],
                        "lead": f"Earlier, {'you' if _same(who, viewer) else who} answered a question on another "
                                "task that may bear on this:",
                        "note": _signed_note(True)})
    return out


def _signed_note(signed: bool) -> str:
    return ("Signed for that question." if signed else "Not signed.") + " Context here, not approval."


def _open(d: dict) -> bool:
    """The decision still waits for a person's answer or signature."""
    return d.get("status") == "pending" or (d.get("signoff") == "required" and not d.get("authorized"))


PRECEDENT_FLOOR = 0.25
PRECEDENT_SHARED = 2


def _precedent(store, d: dict, seen: set) -> dict | None:
    """The signed answer on another task that bears most on this question,
    found by a search of the workspace's memory for its question.

    The evidence string is what the ladder found when the question was
    asked, against the floor it uses for predicting an answer. Measured
    on prometheus/prometheus: Priya was asked whether remote-write
    should drop excess samples, their page said nothing, and the signed
    tsdb answer "Never drop samples silently" was one search away. A
    precedent is not a prediction, so the floor here is lower; a hit must
    also share two words with the question, so one common word is not a
    match. Same-task decisions are under Decisions already."""
    question = d.get("question") or ""
    try:
        rows = store.graph.memory_search(question, limit=6, repo=d.get("repo") or "", min_score=PRECEDENT_FLOOR)
    except Exception:
        return None
    for r in rows:
        if not r.get("signed") or r.get("run_id") == d.get("run_id") or r.get("id") in seen or not r.get("answer"):
            continue
        if _bears_on(question, r.get("question") or "", r.get("answer") or ""):
            seen.add(r["id"])
            return r
    return None


_WORD = re.compile(r"[a-z0-9][a-z0-9'_-]*")
_STOP = frozenset("the a an and or but of to in on for is are was were be been it this that with as at by we our "
                  "i you they them their should would could will can do does did not no yes so if then than".split())


def _content_words(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 2}


_SUFFIX = re.compile(r"(?:ing|ed|es|s|ly)$")


def _stem(word: str) -> str:
    """Enough of a word to match its other forms: "dropped" and "drop",
    "samples" and "sample". Not a stemmer; a match between two short texts."""
    if len(word) > 4:
        word = _SUFFIX.sub("", word)
    if len(word) > 3 and word[-1] == word[-2]:
        word = word[:-1]
    return word


def _bears_on(question: str, other_question: str, other_answer: str) -> bool:
    """An earlier question and answer share two content words with this
    question, in any of their forms: one common word is not a match."""
    asked = {_stem(w) for w in _content_words(question)}
    theirs = {_stem(w) for w in _content_words(f"{other_question} {other_answer}")}
    return len(asked & theirs) >= PRECEDENT_SHARED


# Where a line of routing evidence starts. The evidence is joined with
# "; ", and a note may hold "; " of its own, so a fragment that does not
# start a line belongs to the one before it. Splitting on every "; " cut
# the CODEOWNERS note in three and the page showed its first third.
_LINE_START = re.compile(r"^(?:[a-z][a-z -]*:\s|authored |reviewed |wrote |merge |approval |CODEOWNERS |MAINTAINERS |"
                         r"GitHub |handed on |once you answer|historical )")


def _evidence_lines(text: str) -> list[str]:
    out: list[str] = []
    for part in (text or "").split("; "):
        part = part.strip()
        if not part:
            continue
        if out and not _LINE_START.match(part):
            out[-1] += "; " + part
        else:
            out.append(part)
    return out


def _learned_scope(text: str) -> str:
    """"decisions on tsdb/wlog/* in acme/x" as a person reads it."""
    parts = []
    for item in text.split(", "):
        item = re.sub(r" in \S+/\S+$", "", item.strip())
        m = re.match(r"decisions on (\S+)$", item)
        if m:
            parts.append(f"questions under {m.group(1).rstrip('*')}")
        elif item == "every decision":
            parts.append("every question in this repository")
        else:
            parts.append(re.sub(r"\bdecisions\b", "questions", item))
    return " and ".join(p for p in parts if p)


def _listed(graph, d: dict) -> list[dict]:
    """Who CODEOWNERS and MAINTAINERS name for the decision's file, each
    person once, joined to the Raven person they are when the workspace
    has one. MAINTAINERS.md names people and CODEOWNERS their logins, so
    "Ada Brooks" and "@abrooks" are one entry; a team, a project-wide
    listing and an exclusion are not people to hand anything to."""
    from .identity import Person, norm_handle, same_person
    from .signals import listed_for
    path = (d.get("path") or "").lstrip("/")
    if not path or path == "unknown":
        return []
    try:
        entries = listed_for(graph, d.get("repo") or "", path)
    except Exception:
        return []
    people = [p for p in graph.people() if p.get("active", 1)]
    out: list[dict] = []
    for e in entries:
        raw = " ".join((e.get("person") or "").split())
        if not raw or e.get("catch_all") or e.get("role") in ("team", "exclude") or "/" in raw:
            continue
        label = "MAINTAINERS" if e["kind"] == "maintainers" else "CODEOWNERS"
        where = f"{label} for {e.get('section') or e['pattern']}" if label == "MAINTAINERS" else \
            f"{label} for {e['pattern']}"
        handle = norm_handle(raw) if " " not in raw else ""
        probe = Person(handle=handle) if handle else Person(name=raw, email=e.get("email") or "")
        person = next((p for p in people if handle and norm_handle(p.get("github_login") or "") == handle), None) \
            or next((p for p in people if same_person(probe, graph._person_identity(p))), None)
        hit = next((x for x in out if (person is not None and x["person"] is not None
                                       and x["person"]["id"] == person["id"])
                    or (person is None and x["person"] is None and any(same_person(probe, q) for q in x["probes"]))),
                   None)
        if hit is None:
            hit = {"person": person, "probes": [], "handle": "", "name": "", "where": []}
            out.append(hit)
        hit["probes"].append(probe)
        if handle:
            hit["handle"] = hit["handle"] or handle
        else:
            hit["name"] = hit["name"] or raw
        if where not in hit["where"]:
            hit["where"].append(where)
    for x in out:
        x["label"] = (f"{x['name']} (@{x['handle']})" if x["name"] and x["handle"]
                      else x["name"] or "@" + x["handle"])
    return out


def _quoted(text: str) -> str:
    """A quote closing a sentence: “...” with the sentence's own period
    outside it only when the quote does not end one already."""
    text = (text or "").strip()
    return f"“{text}”" + ("" if text[-1:] in ".!?…" else ".")


def _why(graph, link: Link, d: dict) -> dict | None:
    """Why the decision went to its owner: one plain sentence first, and
    the routing evidence behind a toggle, every line of it whole.

    Measured on prometheus/prometheus: the page listed the first four
    fragments of the evidence ("verified: ... (config)", recency-weighted
    shares, "note: CODEOWNERS lists lfranco, kmarsh, praman,
    abrooks here"), cut before the line that said why the map chose
    them, and never said that @praman was Priya. The sentence
    reads the authority line and the listing for the file, and joins a
    listed handle to the GitHub login the workspace has for the owner."""
    owner = d.get("owner_name") or ""
    lines = _evidence_lines(d.get("owner_evidence") or "")
    reason = d.get("routing_reason") or ""
    if not owner or not (lines or reason):
        return None
    yours = _same(owner, link.person["name"])
    you, You = ("you", "You") if yours else (owner, owner)
    are = "you are" if yours else f"{owner} is"
    said: list[str] = []
    m = re.match(r"Reassigned to .+? by (.+?) \((.+)\)$", reason)
    if m:
        # The evidence still describes whoever had it before.
        return {"heading": "Why this came to you" if yours else f"Why Raven asked {owner}",
                "summary": f"{m.group(1)} assigned this to {you} ({m.group(2)}).", "details": []}
    details = []
    for line in lines:
        hand = re.match(r'handed on by (.+?)(?:: "(.*)")?$', line)
        learn = re.match(r"once you answer, (?:Bridge|Raven) routes (.+) to you first$", line)
        if hand:
            # On the page of the person who handed it on, it is theirs to
            # say in the first person: "Rafael Ortega handed this on" read
            # as someone else's act on Rafael's own page.
            by = "You" if _same(hand.group(1), link.person["name"]) else hand.group(1)
            said.append(f"{by} handed this on to {you}" + (f": {_quoted(hand.group(2))}" if hand.group(2) else "."))
        elif learn:
            scope = _learned_scope(learn.group(1))
            said.append(f"If {'you answer' if yours else owner + ' answers'}, Raven asks "
                        f"{'you' if yours else 'them'} first about later {scope}." if d.get("status") == "pending"
                        else f"Raven now asks {you} first about later {scope}.")
        elif not line.startswith(("area:", "GitHub not synced")):
            details.append(line)
    if not said:
        verified = next((re.match(r"verified: .+? (decides|must approve|knows) for (\S+(?: decisions)?)", ln)
                         for ln in details if ln.startswith(f"verified: {owner} ") and "(referral" not in ln), None)
        # A route an earlier hand-on taught: one person's word, accepted
        # when the other answered. Routing writes it as a verified line
        # whose source is the referral; the page called it the ownership map.
        referred = next((re.match(r"(?:verified|referred): .+? (?:decides|must approve|knows) for (\S+(?: decisions)?).*?"
                                  r"\(referral(?:, asserted by ([^,)]+))?", ln)
                         for ln in details if ln.startswith((f"verified: {owner} ", f"referred: {owner} "))
                         and "(referral" in ln), None)
        person = graph.find_person(owner)
        listed = []
        if person is not None:
            for x in _listed(graph, d):
                if x["person"] is not None and x["person"]["id"] == person["id"]:
                    listed += [w for w in x["where"] if w not in listed]
        if referred and not verified:
            scope = referred.group(1).rstrip(",;")
            scope = (_learned_scope(scope) if scope.endswith(" decisions") else _learned_scope(f"decisions on {scope}"))
            by = referred.group(2) or "someone"
            by = "you" if _same(by, link.person["name"]) else by
            said.append(f"{by[:1].upper() + by[1:]} handed {you} an earlier question here, so Raven asks {you} first "
                        f"about {scope}. That is a hand-on, not Raven's ownership map"
                        + (f"; {are} also in {' and '.join(listed)}" if listed else "") + ".")
        elif verified:
            role = {"decides": "deciding", "must approve": "approving", "knows": "knowing"}[verified.group(1)]
            said.append(f"Raven's ownership map lists {you} as {role} {verified.group(2).rstrip(',;')}"
                        + (f"; {are} also in {' and '.join(listed)}" if listed else "") + ".")
        elif listed:
            said.append(f"{You} {'are' if yours else 'is'} in {' and '.join(listed)}.")
        else:
            history = next((ln for ln in details if ln.startswith(("authored ", "reviewed ", "wrote "))), "")
            if history:
                said.append(f"Raven chose {you} from the repository's history: {you} "
                            + re.sub(r"\s*\([^)]*\)$", "", history) + ".")
            elif reason.startswith("Routed from the ownership graph") or not reason:
                said.append(f"Raven routed this to {you} from its ownership map and the repository's history.")
            else:
                said.append(_plain(reason.split(":", 1)[0]).rstrip(".") + ".")
    clean = []
    for line in details:
        hand_on = re.match(r"(?:verified|referred): (.+?) \(referral(?:, asserted by ([^,)]+))?(, not yet accepted)?\)(.*)$",
                           line)
        if hand_on:
            line = (f"{hand_on.group(1)}, from a hand-on by {hand_on.group(2) or 'someone'}"
                    + (" they have not answered yet" if hand_on.group(3) else "") + hand_on.group(4))
        line = re.sub(r"^(?:verified|note): ", "", line)
        line = _plain(line.replace(" (config)", ""))
        clean.append(line[:1].upper() + line[1:])
    return {"heading": "Why this came to you" if yours else f"Why Raven asked {owner}",
            "summary": " ".join(said), "details": clean}


def _email_hint(email: str) -> str:
    """The address the person will sign in with, enough of it to know it
    as theirs: a link can be forwarded, so the page never shows it whole."""
    local, _, domain = (email or "").partition("@")
    if not local or not domain:
        return ""
    return local[:2 if len(local) > 3 else 1] + "…@" + domain


def _notes(graph, run_id: str, viewer_id: str = "", everything: bool = False) -> list[dict]:
    """The task's notes, as canvas.task_notes reads them, with where each
    came from. A note the viewer added from the page can be withdrawn;
    the notice that tells the agent so is for the agent, and the page
    marks the note itself instead."""
    out = []
    for r in graph.db.execute("SELECT id, detail, created_at FROM events WHERE run_id=? AND kind='task_note' "
                              "ORDER BY id", (run_id,)):
        try:
            d = json.loads(r["detail"])
        except ValueError:
            continue
        out.append({"id": r["id"], "by": d.get("by", ""), "text": d.get("text", ""), "at": r["created_at"],
                    "source": d.get("source") or "", "withdraws": d.get("withdraws"),
                    "mine": bool(viewer_id) and d.get("person_id") == viewer_id and d.get("source") == "page"})
    if everything:
        return out
    gone = {n["withdraws"] for n in out if n["source"] == "withdrawn"}
    shown = []
    for n in out:
        if n["source"] == "withdrawn":
            continue
        n["withdrawn"] = n["id"] in gone
        n.pop("withdraws", None)
        shown.append(n)
    return shown


def _focus(store, link: Link, nodes: list[dict], decision_id: str = "") -> dict | None:
    """The decision this person was asked about, or another on the task
    they chose to open, with what a Slack message tells them: the brief,
    the context, the options, what Raven found, why it came to them and
    how they decided before. A decision opened from the list was shown
    with all of that emptied, so "Answer this" lost the context the card
    above it had."""
    decision_id = decision_id or link.decision_id
    if not decision_id:
        return None
    try:
        d = store.get_decision(decision_id)
    except Invalid:
        return None
    if d["run_id"] != link.run_id:
        # The page opens nothing but its own task.
        return None
    from .llm import drop_absence
    try:
        options = json.loads(d.get("options") or "[]")
    except ValueError:
        options = []
    from . import authz
    graph = store.graph
    node = next((n for n in nodes if n["node_id"] == d["id"]), {})
    context, paths = _own_context(d.get("context") or "")
    path = d.get("path") or ""
    if not paths and path and path != "unknown":
        paths = [path]
    # Handing on, as `not me @person` does in Slack, on the same standing.
    can_refer = (_open(d) and d["status"] in ("pending", "resolved", "partial", "assumed", "proposed")
                 and bool(authz.basis_for(graph, link.actor, d, "refer")[0]))
    return {"node_id": d["id"], "question": d["question"], "context": context, "paths": paths,
            "source_revalidation": d.get("source_revalidation"),
            "source_anchors": d.get("source_anchors", []), "work_item_association": d.get("work_item_association"),
            "approval_scope_text": d["approval_scope_text"], "replacement_ends_rule": bool(d.get("reusable")),
            "approval_scope": d["approval_scope"], "approval_scope_labels": d["approval_scope_labels"],
            "brief": drop_absence(d.get("brief") or ""), "options": options, "status": d["status"],
            "prediction": d.get("prediction") or "", "found": _found(store, d, link.person["name"]),
            "owner": d.get("owner_name") or "", "routing_reason": d.get("routing_reason") or "",
            "why": _why(graph, link, d),
            "answer": d.get("answer") or "", "rationale": _reason(d.get("rationale")),
            "answered_by": d.get("answered_by") or "", "signoff": d.get("signoff") or "",
            "path": d.get("path") or "", "updated_at": d["updated_at"],
            "can_act": node.get("can_act", False), "action": node.get("action", "answer"),
            "why_not": node.get("why_not", ""), "asked": d["id"] == link.decision_id,
            "can_refer": can_refer, "handon": _handon_people(graph, d, link.person) if can_refer else [],
            "handon_unknown": _handon_unknown(graph, d) if can_refer else [],
            "handon_keeps": _handon_keeps(store, d, link) if can_refer else "",
            "handed_to": _handed_to(graph, d, link.person["name"])}


def _handon_keeps(store, d: dict, link: Link) -> str:
    """What a hand-on from the page leaves alone, said beside its button:
    the route a hand-on in the app would teach. From a link a hand-on is
    for this question only. A link can be forwarded, and a hand-on from
    one taught Raven that the person named decides tsdb/wlog/* for the
    whole workspace, over the administrator's map; the form said nothing
    of it before it was sent."""
    plan = store.handon_scopes(d, link.actor)
    default = plan["default"]
    if default["scope_kind"] == "none":
        return ""
    if default["scope_kind"] == "path":
        return f"later questions under {default['scope'].rstrip('*')}"
    if default["scope_kind"] == "category":
        return f"later {default['scope']} questions"
    return "later questions in this repository"


def _handed_to(graph, d: dict, viewer: str) -> str:
    """Who this person handed the decision on to, while it is still theirs
    to hold: "" if they never did, or it came back to them."""
    if _same(d.get("owner_name") or "", viewer):
        return ""
    to = ""
    for r in graph.db.execute("SELECT detail FROM events WHERE decision_id=? AND kind='owner_changed' ORDER BY id",
                              (d["id"],)):
        try:
            e = json.loads(r["detail"])
        except ValueError:
            continue
        if e.get("referral") and _same(e.get("by") or "", viewer):
            to = e.get("to") or ""
    return to


HANDON_SHOWN = 8
_ROLE_WORDS = {"decides": "decides", "approves": "approves", "knows": "knows"}


def _handed_by(graph, d: dict, viewer: str) -> str:
    """Who handed this decision to the viewer last, or ""."""
    by = ""
    for r in graph.db.execute("SELECT detail FROM events WHERE decision_id=? AND kind='owner_changed' ORDER BY id",
                              (d["id"],)):
        try:
            e = json.loads(r["detail"])
        except ValueError:
            continue
        if e.get("referral"):
            by = (e.get("by") or "") if _same(e.get("to") or "", viewer) else by
    return by


def _handon_unknown(graph, d: dict) -> list[dict]:
    """The people CODEOWNERS or MAINTAINERS name for the decision's file
    whom the workspace has no person for: shown in the picker, marked, so
    the page does not read as if nobody else is listed. A hand-on goes to
    a person Raven can message, so they cannot be picked."""
    return [{"name": x["label"], "why": x["where"][0]} for x in _listed(graph, d) if x["person"] is None]


def _handon_people(graph, d: dict, viewer: dict) -> list[dict]:
    """Whom the page suggests handing the decision to: the people the
    authority map names for its path or topic and the CODEOWNERS and
    MAINTAINERS listings for its file first, then the rest of the map for
    the repository, each with the line that names them. Never the person
    holding the link or the current owner. Whoever handed it to them is
    marked and listed last: Tomas handed the question to Rafael, and the
    picker offered Tomas first with nothing to say so. Anyone else is
    named by hand."""
    from .scopes import primary_scopes
    from .signals import _pattern_re
    viewer_id = viewer["id"]
    repo = d.get("repo") or ""
    path = (d.get("path") or "").lstrip("/")
    if path == "unknown":
        path = ""
    owner = (d.get("owner_name") or "").strip().lower()
    topics = set(primary_scopes(d.get("question") or "", d.get("category") or ""))
    found: dict[str, dict] = {}

    def add(person, why, covers):
        if not person or not person.get("active", 1) or person["id"] == viewer_id \
                or person["name"].strip().lower() == owner:
            return
        hit = found.get(person["id"])
        if hit is None:
            found[person["id"]] = {"id": person["id"], "name": person["name"], "why": why, "covers": covers}
        elif covers and not hit["covers"]:
            hit.update(why=why, covers=True)

    for r in graph.authority_rows(repo):
        if not r.get("accepted", 1) or r["role"] not in _ROLE_WORDS:
            continue
        if r["scope_kind"] == "path":
            try:
                covers = bool(path) and bool(_pattern_re(r["scope"], "codeowners").match(path))
            except re.error:
                covers = False
            scope = r["scope"]
        elif r["scope_kind"] == "category":
            covers, scope = r["scope"] in topics, f"{r['scope']} questions"
        else:
            covers, scope = False, "the whole repository"
        people = [r["person"]] if r.get("person") else [graph.get_person(pid) for pid in
                                                          (r.get("team") or {}).get("members", [])]
        for person in people:
            add(person, f"{_ROLE_WORDS[r['role']]} {scope}", covers)
    for x in _listed(graph, d):
        if x["person"] is not None:
            add(x["person"], "in " + " and ".join(x["where"]), True)
    back = _handed_by(graph, d, viewer["name"])
    for x in found.values():
        x["handed_you"] = _same(x["name"], back)
    ranked = sorted(found.values(), key=lambda x: (x["handed_you"], not x["covers"], x["name"].lower()))
    return ranked[:HANDON_SHOWN]


def decided_by(n: dict) -> str:
    """Who decided a node: the person whose answer it is, the one who gave
    it and signed it (or gave it and waits on co-signers). An answer the
    agent or memory supplied, signed by someone else, was decided by
    nobody on the task: the signer signed it. web/task.js counts the same
    way. The page said "Decided" where the app said "Signed" for the
    same answer: the app counted a person's answer only on kind
    'answer', and an answered node keeps the kind it was asked with."""
    who = n.get("answered_by") or ""
    if not who:
        return ""
    return who if n.get("kind") == "answer" or any(_same(s, who) for s in n.get("signatures") or []) else ""


def _contacts(graph, run_id: str, nodes: list[dict]) -> list[dict]:
    """Everyone the task has reached, in the order it reached them: what
    they were asked, whether the message went out, and what they
    decided, handed on or still owe."""
    by_name: dict[str, dict] = {}

    def entry(name):
        key = name.strip().lower()
        if key not in by_name:
            by_name[key] = {"name": name, "asked": [], "decided": [], "handed_on": [], "waiting_on": [],
                            "messages": 0, "delivery_failed": False, "first_at": ""}
        return by_name[key]

    questions = {n["node_id"]: n["question"] for n in nodes}
    for r in graph.db.execute("SELECT decision_id, kind, person_name, state, created_at, sent_at FROM notifications "
                              "WHERE run_id=? ORDER BY created_at", (run_id,)):
        if not r["person_name"]:
            continue
        e = entry(r["person_name"])
        e["first_at"] = e["first_at"] or r["created_at"]
        if r["state"] == "sent":
            e["messages"] += 1
        elif r["state"] == "failed":
            e["delivery_failed"] = True
        q = questions.get(r["decision_id"])
        if q and q not in e["asked"]:
            e["asked"].append(q)
    for n in nodes:
        if n["status"] in ("duplicate", "suggested", "adopted"):
            continue
        author = decided_by(n)
        if author:
            entry(author)["decided"].append({"question": n["question"], "answer": n["answer"],
                                             "rationale": _reason(n["rationale"]), "node_id": n["node_id"]})
        for signer in n["signatures"]:
            if not _same(signer, author):
                entry(signer)["decided"].append({"question": n["question"], "answer": n["answer"],
                                                 "signed": True, "node_id": n["node_id"]})
        if n["blocking"]:
            for name in [n["owner"]] + list(n["required_signers"]):
                if name and name.lower() not in {s.lower() for s in n["signatures"]}:
                    e = entry(name)
                    if n["question"] not in e["waiting_on"]:
                        e["waiting_on"].append(n["question"])
    for r in graph.db.execute("SELECT detail, created_at FROM events WHERE run_id=? AND kind='owner_changed' ORDER BY id",
                              (run_id,)):
        try:
            d = json.loads(r["detail"])
        except ValueError:
            continue
        if d.get("referral") and d.get("by"):
            entry(d["by"])["handed_on"].append({"to": d.get("to", ""), "question": questions.get(d.get("decision_id"), ""),
                                                "at": r["created_at"]})
    return sorted(by_name.values(), key=lambda e: e["first_at"] or "~")


HISTORY_LABELS = {
    "task_started": "Task started", "owner_approved": "Decision recorded", "answer_corrected": "Answer corrected",
    "signoff": "Signed off", "owner_changed": "Handed on", "task_note": "Context added",
    "followup_added": "Follow-up question added", "node_added": "Coding agent raised a decision",
    "node_settled": "Settled by the coding agent",
    "run_updated": "Status changed", "task_finished": "Task finished",
}


def _history(graph, run_id: str, nodes: list[dict], limit: int = 80) -> list[dict]:
    questions = {n["node_id"]: n["question"] for n in nodes}
    out = []
    marks = ",".join("?" for _ in HISTORY_LABELS)
    for r in graph.db.execute(f"SELECT kind, decision_id, detail, created_at FROM events WHERE run_id=? AND kind IN ({marks}) "
                              "ORDER BY id DESC LIMIT ?", (run_id, *HISTORY_LABELS, limit)):
        try:
            d = json.loads(r["detail"]) if (r["detail"] or "").startswith("{") else {"text": r["detail"]}
        except ValueError:
            d = {"text": r["detail"]}
        # An answer's event names its signer as `actor`; without it the
        # history said a decision was recorded but not by whom.
        by = d.get("by") or d.get("answered_by") or d.get("actor_name") or d.get("actor") or d.get("requester") or ""
        text = d.get("text") or d.get("answer") or d.get("reason") or d.get("question") or ""
        if r["kind"] == "owner_changed" and d.get("to"):
            text = f"to {d['to']}" + (f" (by {by})" if by else "")
            by = ""
        out.append({"kind": r["kind"], "label": HISTORY_LABELS[r["kind"]], "at": r["created_at"], "by": str(by)[:100],
                    "text": str(text)[:600], "question": questions.get(r["decision_id"] or "", "")})
    return out


def overview(store, link: Link, workspace: str = "", has_account: bool = False, sign_in: dict | None = None,
             focus_id: str = "") -> dict:
    """Everything the page shows, read for the person the link names.
    `sign_in` says how this workspace signs people in ({"enabled",
    "github"}), so the page offers an account only where one can be made
    from it: never with sign-in off, never to an administrator, and not
    once an administrator has turned it off. `focus_id` is a decision on
    this task the person opened from the list; one on another task opens
    the decision the link names instead."""
    from .canvas import get_tree
    graph = store.graph
    sign_in = sign_in or {}
    can_create = (bool(sign_in.get("enabled")) and not has_account and graph.get_setting("brief_signup", "1") != "0"
                  and (link.person.get("role") or "member") != "admin")
    tree = get_tree(store, link.run_id)
    flat = _flatten(tree["nodes"])
    actor = link.actor
    nodes = [_node_summary(n, actor, graph, store, link.asked) for n in flat]
    focus = (_focus(store, link, nodes, focus_id) if focus_id else None) or _focus(store, link, nodes)
    run = graph.db.execute("SELECT * FROM runs WHERE id=?", (link.run_id,)).fetchone()
    keys = run.keys()
    counted = [n for n in flat if n["status"] not in ("duplicate", "suggested", "adopted")]
    return {
        "workspace": workspace,
        "viewer": {"name": link.person["name"], "role": actor.role, "has_account": has_account,
                   "has_email": bool(link.person.get("email")), "email_hint": _email_hint(link.person.get("email")),
                   "can_create_account": can_create, "github_sign_in": can_create and bool(sign_in.get("github")),
                   "sign_in": bool(sign_in.get("enabled")),
                   "link_expires_at": datetime.fromtimestamp(link.expires_at, timezone.utc).isoformat()},
        "task": {"id": tree["task_id"], "title": tree["title"], "goal": tree["goal"], "repo": tree["repo"],
                 "status": tree["status"], "verdict": tree["verdict"], "verdict_why": tree["verdict_why"],
                 "facts": tree["facts"], "next": tree["next"],
                 "created_at": run["created_at"] if "created_at" in keys else "",
                 "updated_at": run["updated_at"],
                 "agent": run["agent"] or ""},
        "requester": _person_card(graph, tree["requester"]),
        "focus": focus,
        # The link names a decision of its own, which the page can go back to.
        "asked_focus": bool(link.decision_id),
        "nodes": nodes,
        "progress": {"total": len(counted), "signed": sum(1 for n in counted if n["authorized"]),
                     "waiting": sum(1 for n in counted if n["blocking"])},
        "contacts": _contacts(graph, link.run_id, flat),
        "notes": _notes(graph, link.run_id, link.person["id"]),
        "history": _history(graph, link.run_id, flat),
    }


# ---------------- acting from the page ----------------

# What the person is told after they act, in their words: the coding
# agent reads the task's tree, which is Raven's word, not theirs.
SEEN = "The coding agent sees it the next time it checks this task."

def act(store, link: Link, data) -> dict:
    """The person answers, signs or corrects a decision on this task,
    exactly as a Slack reply would: the revision they read is named,
    and the permission check decides."""
    from . import canvas
    graph = store.graph
    decision_id = field(data, "decision_id", limit=100)
    decision = store.get_decision(decision_id)
    if decision["run_id"] != link.run_id:
        raise Invalid("Decision not found on this task")
    expected = field(data, "expected_updated_at", limit=60)
    answer = str(data.get("answer") or "").strip()[:12000]
    rationale = str(data.get("rationale") or "").strip()[:4000]
    actor = link.actor
    name = link.person["name"]
    # The task link permits only this task's decision, on this person's
    # standing. The same complete revision checks used by the inbox still run.
    reviewed = {k: data[k] for k in ('source_evidence', 'source_decision_pins') if k in data}
    if decision["status"] == "pending":
        if not answer:
            raise Invalid("Write the decision you are making")
        if not decision["owner_id"] and decision_id == link.asked and actor.role != "viewer":
            # Asked directly and nobody else owns it: like a Slack reply
            # in the thread, the person asked takes it. The revision they
            # read is checked before anything changes, and taking it is a
            # new revision, which the answer then names. Without that,
            # every answer here was refused as stale and the person was
            # left owning a decision they could not answer.
            from .store import check_revision
            with graph.transaction():
                rev = "SELECT updated_at FROM decisions WHERE id=?"
                check_revision({"expected_updated_at": expected}, graph.db.execute(rev, (decision_id,)).fetchone())
                graph.update_decision(decision_id, owner=name)
                expected = graph.db.execute(rev, (decision_id,)).fetchone()["updated_at"]
        # A reason only when the person gave one: "answered from the task
        # page" showed under Why in Decisions and Memory as if it were
        # theirs. Without one the store records that none was given.
        store.answer(decision_id, {"answer": answer, **({"rationale": rationale} if rationale else {}),
                                   "expected_updated_at": expected, "signed_by": name, "source": f"link: {name}", **reviewed},
                     actor=actor)
        notice = f"Recorded as {name}'s answer. {SEEN}"
    elif answer:
        canvas.sign_off(store, decision_id, {"by": name, "answer": answer,
                                             **({"rationale": rationale} if rationale else {}),
                                             "expected_updated_at": expected, **reviewed}, actor=actor)
        notice = f"Corrected and signed by {name}. {SEEN}"
    else:
        canvas.sign_off(store, decision_id, {"by": name, "expected_updated_at": expected, **reviewed}, actor=actor)
        notice = f"Signed off by {name}. {SEEN}"
    return {"notice": notice, "decision_id": decision_id}


def refer(store, link: Link, data) -> dict:
    """The person hands the decision on from the page, as `not me @person`
    does in Slack: the same store.refer, the revision they read named,
    the permission check deciding, their reason kept as the hand-on note.
    The DM told people to reply `not me`, and the page, the one surface
    some of them open, could only record an answer."""
    graph = store.graph
    decision_id = field(data, "decision_id", limit=100)
    decision = store.get_decision(decision_id)
    if decision["run_id"] != link.run_id:
        raise Invalid("Decision not found on this task")
    expected = field(data, "expected_updated_at", limit=60)
    who = " ".join(str(data.get("person") or "").split())[:200]
    if not who:
        raise Invalid("Name the person to hand it to")
    person = graph.get_person(who) or graph.find_person(who.lstrip("@"))
    if person is None or not person.get("active", 1):
        raise Invalid(_unknown_person(graph, decision, who))
    if person["id"] == link.person["id"]:
        raise Invalid("Hand it to someone else, or answer it yourself")
    reason = " ".join(str(data.get("note") or "").split())[:300]
    # For this question only: a link teaches Raven no route. Whoever holds
    # it (and a link can be forwarded) would otherwise decide who Raven
    # asks first about a whole directory, for everyone in the workspace.
    # Teaching a route is for the app, signed in, where the scope is shown.
    store.refer(decision_id, {"person": person["id"], "by": link.person["name"], "expected_updated_at": expected,
                              "note": reason, "scope_kind": "none"}, actor=link.actor)
    notice = f"Handed to {person['name']}, for this question only. It now waits on them."
    return {"notice": notice, "decision_id": decision_id, "to": person["name"]}


def _unknown_person(graph, decision: dict, who: str) -> str:
    """Why a name typed into "Someone else" was refused, in terms of what
    was typed. Measured: "kmarsh", a GitHub login CODEOWNERS lists for
    /tsdb, was told to give a full name, email or GitHub login."""
    from .identity import Person, norm_handle, same_person
    handle = norm_handle(who) if " " not in who.strip() and "@" not in who.lstrip("@") else ""
    probe = Person(handle=handle) if handle else Person(name=who)
    for x in _listed(graph, decision):
        if x["person"] is None and (any(same_person(probe, q) for q in x["probes"])
                                    or (handle and handle == x["handle"])):
            return (f"{who} is in {x['where'][0]} but has no Raven person yet. An admin can add them, or pick "
                    "someone in the list.")
    return f"Raven has no person called {who}. Pick someone in the list, or ask a Raven admin to add them."


def add_note(store, link: Link, data) -> dict:
    """A note from the page: canvas.add_note's note, on the link's
    standing, with the person it came from, so they can withdraw it."""
    from . import authz, canvas
    canvas._task(store, link.run_id)
    text = field(data, "text", limit=4000)
    authz.check(store.graph, link.actor, {"owner_id": "", "repo": ""}, "note")
    with store.graph.transaction():
        store.graph.append_event("task_note", {"task_id": link.run_id, "by": link.person["name"],
                                               "person_id": link.person["id"], "source": "page", "text": text})
    return {"task_id": link.run_id, "notes": canvas.task_notes(store, link.run_id)}


def withdraw_note(store, link: Link, note_id) -> dict:
    """The person takes back a note they added from the page. The note
    stays in the record and a second one tells the coding agent it was
    withdrawn; a note went straight to the agent and could not be taken
    back."""
    graph = store.graph
    try:
        note_id = int(note_id)
    except (TypeError, ValueError):
        raise Invalid("Name the note to withdraw") from None
    row = graph.db.execute("SELECT detail FROM events WHERE id=? AND run_id=? AND kind='task_note'",
                           (note_id, link.run_id)).fetchone()
    detail = {}
    if row is not None:
        try:
            detail = json.loads(row["detail"])
        except ValueError:
            detail = {}
    if detail.get("person_id") != link.person["id"] or detail.get("source") != "page":
        raise Invalid("Only the person who added a note can withdraw it")
    if any(n.get("withdraws") == note_id for n in _notes(graph, link.run_id, everything=True)):
        return {"note_id": note_id, "withdrawn": True}
    with graph.transaction():
        graph.append_event("task_note", {"task_id": link.run_id, "by": link.person["name"],
                                         "person_id": link.person["id"], "source": "withdrawn", "withdraws": note_id,
                                         "text": "Withdrawn by its author: " + str(detail.get("text") or "")[:600]})
    return {"note_id": note_id, "withdrawn": True}
