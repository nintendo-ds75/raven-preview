"""The canvas: the decision tree a host agent writes to while it works.

The host agent breaks its task down; Raven never does. The agent calls
Raven at kickoff, before any node exists, so Raven sees the whole task
and not a question from the middle of it. Raven looks at the task with
what it knows on its own (the tree, MAINTAINERS and CODEOWNERS, git
history and blame, prior decisions, what is already pending) and says
whether it should be involved at all; most tasks are trivial and it
passes. From then on every decision the agent discovers becomes a node
under the node it grew from. Raven resolves what static context settles
(still marked for a person's sign-off), routes what needs novel judgment
to the person the signals name, and hands back what people answered
together with the follow-up questions they added for the next level,
because a corrected level n is also the right set of questions for
level n+1. Every write is idempotent by client reference or by question;
nothing here depends on the process staying alive: the tree is rows in
the same SQLite file the inbox reads.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
import re
from collections import defaultdict

from .graph import _DECISION_SELECT, _row_to_decision
from .store import Invalid, answer_hash, field, now, repo_key

MAX_PATHS = 20
MAX_OPTIONS = 8
MAX_ITEM = 500
PRIOR_DECISION_MIN = 0.35
# Words that mark a task as a call for the people who own the area, not
# a change an agent can make on its own.
_JUDGMENT_RE = re.compile(
    r"\b(remov(?:e|es|ed|ing|al)|deprecat\w*|breaks?|breaking|compat|compatibility|incompatible"
    r"|backwards?[- ]compatib\w*|migrat\w*|renam(?:e|es|ed|ing)|public api|defaults?|securit\w*"
    r"|billing|pric(?:e|es|ed|ing)|permissions?|retention|delet(?:e|es|ed|ing|ion)|drop(?:s|ped|ping)?|schema|licen[cs]\w*"
    r"|releases?|polic(?:y|ies)|contracts?|customers?|charg(?:e|es|ed|ing)|refunds?|privacy"
    r"|auth|authn|authz|authentication|authorization|rollout|rollback|revert(?:s|ed|ing)?|disabl(?:e|es|ed|ing)"
    r"|semantics|behaviou?r change|user[- ]facing"
    # "without changing existing retry behavior": what counts as a change
    # to existing behaviour is the owner's call.
    r"|chang(?:e|es|ed|ing) (?:\w+ ){0,3}behaviou?r"
    r"|(?:keep|preserve|retain) (?:\w+ ){0,3}(?:existing|current) (?:\w+ ){0,3}behaviou?r"
    # A lifetime, a limit or a credential is a number or a rule somebody
    # picks: how long, how many, who holds it, what happens to the ones
    # already issued.
    r"|expir\w*|lifetimes?|ttls?|credentials?|rate[- ]?limit\w*|quotas?"
    # A new switch an operator can see is always somebody's call: what
    # states it takes, what it ships as, and who turns it on.
    r"|feature[- ]?(?:toggle|flag|gate)s?|opt[- ]?in|opt[- ]?out)\b", re.IGNORECASE)
_QUESTION_RE = re.compile(r"\?|\bshould (?:we|i|the)\b|\bwhether\b|\bdecide\b", re.IGNORECASE)
# A narrow, explicit low-risk edit: the kind of change, and the task
# saying it changes no behaviour (or touching documentation only).
# Measured live on eb9d22d: "Fix a typo in docs/user-guide.rst. Keep it
# to one spelling or grammar correction with no behavioral change"
# engaged on a pending docs decision, a prior one and the word "release"
# in the task's facts; the agent asked an owner which typo to fix and
# waited four and a half minutes for a one-word edit.
_NARROW_KIND_RE = re.compile(r"\b(typos?|spelling|misspell\w*|grammar|grammatical|punctuation|whitespace"
                             r"|broken links?|dead links?)\b", re.IGNORECASE)
_NO_BEHAVIOUR_RE = re.compile(
    r"\bno (?:behaviou?r(?:al)?|functional|code|logic|runtime) changes?\b"
    r"|\bwithout (?:any )?(?:behaviou?r(?:al)?|functional) changes?\b"
    r"|\bwithout changing (?:any )?(?:behaviou?r|functionality|what it does)\b"
    r"|\b(?:docs?|documentation|comments?)[- ]only\b", re.IGNORECASE)
_DOC_PATH_RE = re.compile(r"(?:^|/)docs?/|\.(?:md|rst|txt|adoc)$|(?:^|/)(?:README|CHANGELOG|CONTRIBUTING)[^/]*$",
                          re.IGNORECASE)
# What a host appends as the task's facts ("facts: release=library-next")
# labels the task; it does not say the change touches a release.
_FACTS_CLAUSE_RE = re.compile(r"\bfacts?:\s*(?:[\w.-]+\s*=\s*[^\s,;]+[\s,;]*)+\.?", re.IGNORECASE)
# A host puts its own workflow instructions in the goal it passes. Those
# are addressed to the agent, not statements about the change, and a
# judgment word found in one says nothing about the task. Measured on
# Grafana: a brief Raven passed on engaged once the host appended
# boilerplate containing "authorization" and "release", which made the
# verdict evidence about the host rather than about the work.
_HOST_LABEL_RE = re.compile(
    r"^\W{0,4}(?:workflow|process|instructions?|rules?|guidelines?|reminders?|protocol|conventions?"
    r"|steps?|how to (?:work|proceed)|notes? (?:for|to) the agent)\b[^\n]*:\s*$", re.IGNORECASE)
# Raven's own tool names. A line naming one is the host telling the
# agent how to call Raven, whatever else is in it. ("MCP" is not here:
# a task can be about adding an MCP endpoint.)
_HOST_TOOL_RE = re.compile(r"\bbridge_(?:start_task|add_node|wait|get_tree|get_decision|finish_task|"
                           r"settle_node|ingest_repo|list_owners|search_decisions)\b")
_LIST_ITEM_RE = re.compile(r"^\W{0,4}(?:\d+[.)]|[-*•+])\s")
# What makes a sentence a statement about this change rather than prose:
# it names something in the repository.
_ANCHOR_RE = re.compile(
    r"`[^`]+`"
    r"|\b[\w.-]+/[\w./*-]+"
    r"|\b[\w-]+\.(?:go|py|ts|tsx|js|jsx|java|rb|rs|c|h|cc|cpp|cs|kt|swift|php|sql|ya?ml|json|md|proto|tf|toml)\b"
    r"|\b\w+(?:_\w+)+\b"
    r"|\b[a-z][a-z0-9]*(?:[A-Z][a-zA-Z0-9]*)+\b"
    r"|\b\w+\(\)")
_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]?")
# A sentence addressed to the agent about its own conduct. One of these
# that names nothing in the repository is house rules, not a statement
# about the change.
_AGENT_DIRECTIVE_RE = re.compile(
    r"^\W*(?:you\b|your\b|do not\b|don't\b|never\b|always\b|please\b|remember\b|ensure\b|make sure\b"
    r"|be sure\b|call\b|invoke\b|report\b|note that\b)"
    r"|\byou (?:are|must|should|will|can|may|need|have to)\b"
    # How the work goes through Raven is the host's process, not the change.
    # Measured live: "obtain needed policy decisions through Raven" in the
    # task the host passed was the rules' only reason to engage.
    r"|\b(?:through|via) (?:Bridge|Raven)\b", re.IGNORECASE)


def _items(key: str, raw, separator: str) -> list[str]:
    """A list of text items, or one string split on the separator; every
    item is text of at most MAX_ITEM characters, stripped."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = re.split(separator, raw)
    if not isinstance(raw, list):
        raise Invalid(f"{key} must be text or a list of text")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise Invalid(f"{key} items must be text")
        if len(item) > MAX_ITEM:
            raise Invalid(f"{key} items must be at most {MAX_ITEM} characters")
        out.append(item.strip())
    return out


def _paths(raw) -> list[str]:
    out: list[str] = []
    for p in _items("paths", raw, r"[,\s]+"):
        p = p.lstrip("/")
        if p and p not in out:
            out.append(p)
    return out[:MAX_PATHS]


def _marked(text: str, limit: int, rest: str) -> str:
    from .llm import clip_marked
    return clip_marked(text, limit, rest)


def _options(raw) -> list[str]:
    """The candidate answers: a JSON list, or text split on | or newlines.
    A comma or semicolon belongs to the option it is in. Measured live:
    "clamp after jitter: min(retry_after_max, delay + jitter), cap is never
    exceeded | ..." came apart at its commas, and the fragments past the
    fourth, a whole option among them, were dropped without a word."""
    if isinstance(raw, str) and raw.strip().startswith("["):
        try:
            raw = json.loads(raw)
        except ValueError:
            pass
    options = [o for o in _items("options", raw, r"\s*\|\s*|\n") if o]
    if len(options) > MAX_OPTIONS:
        raise Invalid(f"options: at most {MAX_OPTIONS}, {len(options)} given; merge or drop some, since the owner "
                      "reads every one")
    return options


def _text(data, key, limit=12000) -> str:
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise Invalid(f"{key} must be text")
    if len(value) > limit:
        raise Invalid(f"{key} must be at most {limit} characters")
    return value.strip()


def scope_key(context: str, paths: list[str]) -> str:
    """What identifies a decision beyond its question: the files it is
    about and the context it was asked with, normalized. Two nodes with
    one question and different scope keys are two decisions."""
    import hashlib
    ctx = " ".join((context or "").lower().split())
    body = "|".join(sorted(p.strip().lstrip("/") for p in (paths or []) if p.strip())) + "\n" + ctx
    return hashlib.sha256(body.encode()).hexdigest()[:20]


# ---------------- kickoff ----------------

def _corrects_the_repo(graph, row, repo: str) -> bool:
    """Whether a retry is naming the same task's repository properly
    rather than renaming the task. A key names one kickoff and does not
    rename it, but an identity that matched no graph at all was never a
    name for this task: it was a name for nothing. Measured on Grafana,
    a host passed its worktree path, Raven said so and told it to call
    again with the repository's own name, and this rule refused that
    call. Advice that the next rule forbids is worse than none."""
    known = graph.known_repos()
    if not known:
        return False
    return (graph.resolve_repo(repo_key(row["repo"])) not in known
            and graph.resolve_repo(repo_key(repo)) in known)


def _same_task(row, title: str, goal: str) -> bool:
    """Whether a kickoff under an existing client_key is that task. The
    title is the agent's own one-line wording and a fresh process words
    it again; the goal is the task as it was given, which a restart does
    not change. Measured live: a host killed mid-task came back with the
    same key and the same task, titled differently, and was refused."""
    if row["title"] == title:
        return True
    return bool(goal.strip()) and " ".join((row["goal"] or "").split()).lower() == " ".join(goal.split()).lower()


def _existing_task(store, client_key: str, title: str, repo: str, goal: str = "") -> dict | None:
    """The task a client key already names, as bridge_start_task returned
    it, or None. The same key with a different task is refused: a key
    names one kickoff, it does not rename it."""
    row = store.graph.db.execute("SELECT * FROM runs WHERE client_key=?", (client_key,)).fetchone()
    if row is None:
        return None
    if not _same_task(row, title, goal) or row["repo"] != repo:
        # The refusal says how to get the task back: telling an agent that
        # restarted to take a new key starts a second task beside the tree.
        raise Invalid(f"client_key {client_key!r} already names the task {row['title'][:80]!r} in {row['repo']}. "
                      "If this is that task, call again with that title or the goal as it was given; if it is a "
                      "different task, use a new key")
    return _task_as_started(store, row)


def start_task(store, cfg, data) -> dict:
    """Register a task at kickoff and say whether Raven should be
    involved. Returns the task id, the verdict (engage or pass) with its
    reason, and the discovery digest the verdict rests on. A client_key
    makes the kickoff idempotent: a retry with the same key returns the
    task it already started, and two racing kickoffs with one key make
    one task. Facts stated here hold for every node of the task."""
    from .graph import parse_facts
    facts = parse_facts(data.get("facts"))
    result = _start_task(store, cfg, data)
    task_id = result.get("task_id") or ""
    if facts and task_id:
        # Stated once for the task, so every node compares the same values.
        # Measured live: the host wrote release=library on one node and
        # the ticket's release=library-next on the next, and a signed
        # answer could not carry over.
        graph = store.graph
        from .store import check_not_abandoned
        with graph.transaction() as db:
            check_not_abandoned(db, task_id)
            row = db.execute("SELECT facts FROM runs WHERE id=?", (task_id,)).fetchone()
            current = _json_dict(row["facts"]) if row is not None else {}
            merged = {**current, **facts}
            if merged != current:
                graph.db.execute("UPDATE runs SET facts=? WHERE id=?", (json.dumps(merged), task_id))
        result["facts"] = merged
    return result


def _start_task(store, cfg, data) -> dict:
    resume = _text(data, "task_id", 100) or _text(data, "client_key", 200)
    if resume:
        existing = store.graph.get_task(resume)
        if existing is not None:
            requested_repo = _text(data, "repo", 300)
            if requested_repo and requested_repo != existing["repo"]:
                raise Invalid(f"This task_id already names repository {existing['repo']!r}; resuming cannot "
                              "replace its scope. Close the mistaken task with bridge_finish_task "
                              "(status=abandoned, reason=...), then omit task_id and use a new client_key "
                              "when starting the correctly scoped task.")
            return _task_as_started(store, existing)
    title = field(data, "title", limit=300)
    goal = _text(data, "goal")
    repo = field(data, "repo", "local", 300)
    agent = field(data, "agent", "Coding agent", 100)
    requester = _text(data, "requester", 200)
    client_key = _text(data, "client_key", 200)
    paths = _paths(data.get("paths"))
    graph = store.graph
    if client_key:
        row = graph.db.execute("SELECT * FROM runs WHERE client_key=?", (client_key,)).fetchone()
        if row is not None and row['status'] == 'abandoned':
            if row['repo'] != repo:
                raise Invalid("This client_key belongs to an abandoned task in a different repository. "
                              "Omit task_id and use a new client_key for the correctly scoped task.")
            return _task_as_started(store, row)
        if row is not None and _same_task(row, title, goal) and row["repo"] != repo \
                and _corrects_the_repo(graph, row, repo):
            # Same task, properly named at last: take the correction and
            # look again, since the first verdict read no repository.
            with graph.transaction():
                graph.db.execute("UPDATE runs SET repo=?, updated_at=? WHERE id=?", (repo, now(), row["id"]))
                graph.append_event("task_repo_corrected",
                                   {"task_id": row["id"], "was": row["repo"], "now": repo})
            return kickoff(store, cfg, row["id"], title, goal, repo, paths, requester)
        if row is not None and _same_task(row, title, goal) and row["repo"] == repo and paths \
                and (row["verdict"] or "") in ("unplaced", "pass"):
            # The same task, placed at last: the agent looked and names
            # the files. A verdict formed without them is judged again;
            # an engaged task is never passed by a second look.
            before = (row["paths"] or "").split()
            if set(paths) - set(before):
                with graph.transaction():
                    graph.append_event("task_placed", {"task_id": row["id"], "was": row["verdict"], "paths": paths})
                return kickoff(store, cfg, row["id"], title, goal or row["goal"] or "", repo,
                               list(dict.fromkeys(before + paths))[:MAX_PATHS], requester or row["requester"] or "")
        existing = _existing_task(store, client_key, title, repo, goal)
        if existing is not None:
            return existing
    run = store.add_run({"title": title, "agent": agent, "repo": repo})
    if client_key:
        import sqlite3
        try:
            with graph.transaction():
                graph.db.execute("UPDATE runs SET client_key=? WHERE id=?", (client_key, run["id"]))
        except sqlite3.IntegrityError:
            # Another kickoff with this key won the race: this run is
            # surplus, the task is theirs.
            with graph.transaction():
                graph.db.execute("DELETE FROM events WHERE run_id=?", (run["id"],))
                graph.db.execute("DELETE FROM runs WHERE id=?", (run["id"],))
            existing = _existing_task(store, client_key, title, repo, goal)
            if existing is not None:
                return existing
            raise Invalid("Kickoff raced another with the same client_key; retry")
    return kickoff(store, cfg, run["id"], title, goal, repo, paths, requester)


def kickoff(store, cfg, task_id: str, title: str, goal: str, repo: str, paths: list[str], requester: str) -> dict:
    """The kickoff of a task whose run row exists: discovery, the verdict,
    and the write of both onto the run. bridge_start_task ends here, and
    so does a task launched through managed execution, so every adapter
    starts on the same canvas."""
    graph = store.graph
    scope = graph.resolve_repo(repo_key(repo))
    from . import context_connectors
    context_result = None
    if context_connectors._connection(store, scope):
        context_result = context_connectors.search(store, {'repo': scope, 'query': (goal or title)[:2000], 'task_id': task_id})
    discovery = discover(store, cfg.without_models() if cfg else None, scope, title, goal, paths, requester)
    if context_result is not None:
        discovery['context_retrieval'] = {**context_result['external'],
            'source_ids': [r.get('record_id') for r in context_result['sources']],
            'related_decision_ids': [r['decision_id'] for r in context_result['related_decisions']]}
    narrow = narrow_edit(title, goal, paths or [a.get("path", "") for a in (discovery.get("areas") or [])])
    if narrow:
        discovery["narrow_edit"] = narrow
    verdict, why = triage(discovery, title, goal)
    discovery["verdict_source"] = "rules"
    # A repository name that matches nothing ingested is not the same as
    # a repository with nothing in it, and the two read identically: the
    # task resolves to no area, no prior decision is found, and the
    # verdict is a confident pass. Measured on Grafana, a host named its
    # own worktree directory and the sibling task passed on a decision
    # the same Raven had already answered. Say which it is.
    known = graph.known_repos()
    if scope and known and scope not in known:
        unknown = (f"Raven has no graph for {scope!r}: nothing in this verdict was read from a repository. "
                   f"It knows {', '.join(sorted(known)[:3])}"
                   + (f" and {len(known) - 3} more" if len(known) > 3 else "")
                   + ". Pass that name as `repo`, or ingest this checkout.")
        discovery["unknown_repo"] = scope
        why = f"{why}; {unknown}" if verdict == "pass" else why
    # An agent told to kick off before any work has no paths yet, and a
    # task whose words name no area leaves Raven nothing to judge. That
    # is not a pass. Measured on a real task ("Agent tokens currently
    # never expire. Add an expiry to them."), the pass sent the agent off
    # to settle the token lifetime alone, in an area with an owner on the
    # map; the agent itself had said the owner should decide.
    elif verdict == "pass" and not paths and not discovery.get("areas") and not discovery.get("people"):
        verdict = "unplaced"
        why = ("the task names no area Raven knows and no paths were given, so Raven cannot yet say whether "
               "anyone has to decide something: this is not a pass")
    # With a model key, the fast model reads the same digest and may
    # engage a task the rules passed. It may not do the reverse. The two
    # mistakes are not the same size: a task engaged for nothing costs an
    # agent one read, and a task passed wrongly is a decision that was
    # never put to anyone. Measured on a real repository, the model
    # downgraded two engage verdicts out of three to pass, calling a
    # schema migration and a feature-toggle removal routine, and the
    # agent then registered nothing (evals/real_oss).
    pending = uuid.uuid4().hex if cfg is not None and cfg.semantic_retrieval and not narrow else ''
    if pending:
        discovery['model_pending'] = pending
    # The run row exists since add_run; what the kickoff learned lands
    # on it in one write once the verdict is known.
    with graph.transaction():
        graph.db.execute("UPDATE runs SET goal=?, requester=?, paths=?, verdict=?, verdict_why=?, discovery=?, "
                         "updated_at=? WHERE id=?",
                         (goal, requester, " ".join(paths), verdict, why,
                          json.dumps(discovery, ensure_ascii=False), now(), task_id))
        graph.append_event("task_started", {"task_id": task_id, "verdict": verdict, "why": _clip(why, 300),
                                            "requester": requester, "paths": paths})
    if pending:
        _start_background(('task', task_id), _model_triage_pass, store, cfg, task_id, pending)
    proposed = candidates(discovery, title, goal)
    return {"model_pending": bool(pending), "task_id": task_id, "title": title, "repo": repo, "verdict": verdict, "why": why,
            "discovery": discovery, "candidates": proposed,
            "next": _next_for_verdict(verdict, discovery, proposed)}


def _deciders_inside(graph, repo: str, areas: list[str]) -> list[dict]:
    """The people the map says decide for a path inside one of these
    areas. Measured live on urllib3: the task mapped to src/urllib3/util/,
    the map named a decider for src/urllib3/util/retry.py, and the kickoff
    told the agent nobody was known and its questions would go unrouted;
    the first node it wrote went straight to that decider."""
    out: list[dict] = []
    for r in graph.deciders(repo):
        person = r.get("person") or {}
        scope = (r.get("scope") or "").lstrip("/")
        if r.get("scope_kind") != "path" or not person.get("name") or any(p["name"] == person["name"] for p in out):
            continue
        area = next((a for a in areas if a and scope.startswith(a.rstrip("/") + "/")), "")
        if area:
            out.append({"name": person["name"], "evidence": [f"{r['role']} for {scope}, inside {area}"],
                        "score": 0.5, "inside": scope})
    return out[:3]


def discover(store, cfg, repo: str, title: str, goal: str, paths: list[str], requester: str) -> dict:
    """What Raven knows about a task before any node exists: the areas
    the task names, who the signals point at, who is listed, the prior
    decisions that read like this task, and what is already pending."""
    from .routing import rank_for_decision
    from .signals import listed_for, signals_available
    from .resolve import resolve_paths
    graph = store.graph
    text = " ".join(t for t in (title, goal, " ".join(paths)) if t).strip()
    out: dict = {"areas": [], "people": [], "people_note": "", "listings": [], "prior_decisions": [],
                 "pending": [], "requester": requester, "repo": repo,
                 "known_repos": sorted(graph.known_repos())[:20], "known_repos_total": len(graph.known_repos())}
    if repo and (signals_available(graph, repo) or graph.authority_rows(repo)):
        hits = resolve_paths(graph, repo, text, "", paths[0] if paths else "")
        out["areas"] = [{"path": h.path, "why": h.why, "weight": round(h.weight, 2)} for h in hits[:5]]
        seen: set[tuple[str, str]] = set()
        for h in hits[:5]:
            for e in listed_for(graph, repo, h.path):
                if e["catch_all"] or (e["person"], e["pattern"]) in seen:
                    continue
                seen.add((e["person"], e["pattern"]))
                out["listings"].append({"person": e["person"], "pattern": e["pattern"], "role": e["role"],
                                        "kind": e["kind"], "section": e["section"]})
                if len(out["listings"]) >= 8:
                    break
        notes: list[str] = []
        ranked = rank_for_decision(graph, repo, "Who should approve this change: " + title,
                                   paths[:1], context=goal, notes=notes, requester=requester, hits=hits)
        out["people"] = [{"name": n, "evidence": list(ev[:3]), "score": s} for n, ev, s in ranked[:3]]
        if not out["people"] and out["areas"]:
            out["people"] = _deciders_inside(graph, repo, [a["path"] for a in out["areas"]])
        out["people_note"] = "; ".join(notes)
    if text:
        try:
            for c in graph.memory_search(text, limit=3, repo=repo):
                if c.get("similarity", 0) >= PRIOR_DECISION_MIN:
                    out["prior_decisions"].append({"id": c.get("id", ""), "question": c.get("question", ""),
                                                   "answer": _marked(c.get("answer") or "", 1200,
                                                                     "bridge_get_decision has the whole answer"),
                                                   "answered_by": c.get("answered_by") or "",
                                                   "similarity": round(float(c.get("similarity", 0)), 3)})
        except Exception:
            pass
    areas = [a["path"] for a in out["areas"]]
    if repo and (paths or areas):
        # The person this task's own nodes go to is asked anyway.
        top = {out["people"][0]["name"]} if out["people"] else set()
        other = _other_areas(graph, repo, paths or areas, top)
        if other:
            out["other_areas"] = other
    rows = graph.db.execute(
        "SELECT d.id, d.run_id, d.question, d.path, o.name AS owner_name FROM decisions d "
        "LEFT JOIN owners o ON o.id = d.owner_id WHERE d.status = 'pending' AND d.draft = 0 AND (? = '' OR d.repo = ?) "
        "ORDER BY d.created_at DESC LIMIT 300", (repo, repo)).fetchall()
    for d in rows:
        p = (d["path"] or "").lstrip("/")
        near = p and p != "unknown" and any(p.startswith(a) or a.startswith(p) for a in areas)
        # Only a pending decision on the same paths counts; with no area
        # resolved nothing is "on these paths".
        if near:
            out["pending"].append({"id": d["id"], "question": d["question"], "owner": d["owner_name"] or "",
                                   "path": d["path"], "task_id": d["run_id"]})
            if len(out["pending"]) >= 5:
                break
    return out


# How often a change to the task's files also touched an area somebody
# else decides, before that area is named at kickoff. A change to more
# files than CO_CHANGE_FILES (a reformat, a vendored update) says nothing
# about what usually changes together.
CO_CHANGE_SHARE = 0.3
CO_CHANGE_MIN = 3
CO_CHANGE_FILES = 50


def _other_areas(graph, repo: str, paths: list[str], top: set[str]) -> list[dict]:
    """The areas the map says somebody other than the task's own decider
    decides, that this change is likely to touch: one the task names, or
    one that changes to these files usually also touched, from the
    repository's own history. Measured live on 63eb671: the jitter task
    named a changelog fragment and never asked the person who decides
    changelog/*, because nothing at kickoff said changes to retry.py come
    with one."""
    from .graph import _pattern_covers
    from .llm import _TEST_PATH
    from .signals import _specificity
    given = {p.strip().strip("/"): p.strip() for p in paths if p and p.strip() and p.strip() != "unknown"}
    wanted = list(given)
    rows = [r for r in graph.authority_rows(repo)
            if r["scope_kind"] == "path" and r["role"] == "decides" and r["accepted"] and (r["person"] or r["team"])]
    if not wanted or not rows:
        return []
    narrowest: dict = {}

    def rule_for(path: str):
        # The narrowest rule for a file speaks for it.
        if path not in narrowest:
            matching = [r for r in rows if _pattern_covers(r["scope"], path)]
            narrowest[path] = max(matching, key=lambda r: _specificity(r["scope"])) if matching else None
        return narrowest[path]

    def decider(rule) -> str:
        return rule["person"]["name"] if rule["person"] else (rule["team"] or {}).get("name", "")

    out: dict = {}
    for p in wanted:
        rule = None if _TEST_PATH.search(p) else rule_for(p)
        if rule is not None and decider(rule) and decider(rule) not in top and rule["id"] not in out:
            out[rule["id"]] = {"scope": rule["scope"], "decider": decider(rule), "named": given[p]}
    marks = " OR ".join("cp.path=? OR cp.path LIKE ?" for _ in wanted)
    args = [x for p in wanted for x in (p, p + "/%")]
    shas = [r["sha"] for r in graph.db.execute(
        f"SELECT DISTINCT cp.sha, c.ts FROM change_paths cp JOIN changes c ON c.repo=cp.repo AND c.sha=cp.sha "
        f"WHERE cp.repo=? AND c.nfiles <= ? AND ({marks}) ORDER BY c.ts DESC LIMIT 60",
        [repo, CO_CHANGE_FILES, *args])]
    if len(shas) >= CO_CHANGE_MIN:
        touched: dict = {}
        marks = ",".join("?" for _ in shas)
        for r in graph.db.execute(f"SELECT sha, path FROM change_paths WHERE repo=? AND sha IN ({marks})",
                                  [repo, *shas]):
            path = r["path"]
            if _TEST_PATH.search(path) or any(path == p or path.startswith(p + "/") for p in wanted):
                continue
            rule = rule_for(path)
            if rule is not None:
                touched.setdefault(rule["id"], (rule, set()))[1].add(r["sha"])
        for rid, (rule, seen) in sorted(touched.items(), key=lambda kv: -len(kv[1][1])):
            name = decider(rule)
            if rid in out or not name or name in top or len(seen) / len(shas) < CO_CHANGE_SHARE:
                continue
            out[rid] = {"scope": rule["scope"], "decider": name, "changes": len(seen), "of": len(shas)}
    return list(out.values())[:3]


# The question a word in the task is a signal for. Each one is a
# template, filled from what discovery found, never invented from
# nothing: the agent decides which of them are real.
_CANDIDATE_QUESTIONS = {
    "compat": "Does the existing behaviour have to keep working for current users, and for how long?",
    "remove": "What happens to whoever depends on this today: is it removed outright, or deprecated first?",
    "default": "What should the default be, and does changing it change anything for existing installs?",
    "schema": "Is this a schema change that has to be migrated, and who signs the migration off?",
    "security": "Does this widen what anyone can reach, and who approves that?",
    "rollout": "Does this ship behind a flag, and who decides when it is turned on?",
    "toggle": "What states does this switch take, what does it ship as, and who decides when that changes?",
    "pricing": "What does this cost a customer, and who owns that call?",
    "api": "Is this a public interface, and do callers have to change with it?",
    "licence": "Does the licence of what this pulls in fit ours, and who says so?",
}
_WORD_TOPIC = (
    (("compat", "compatibility", "incompatible", "backward", "backwards", "behaviour change", "semantics",
      "user-facing", "breaks", "breaking", "existing behavior", "existing behaviour", "current behavior", "current behaviour"), "compat"),
    (("remov", "deprecat", "delet", "drop", "revert", "disabl"), "remove"),
    (("default", "defaults"), "default"),
    (("schema", "migrat"), "schema"),
    (("securit", "auth", "authn", "authz", "authentication", "authorization", "permission", "permissions",
      "privacy", "retention"), "security"),
    (("feature toggle", "feature-toggle", "featuretoggle", "feature flag", "feature-flag", "featureflag",
      "feature gate", "feature-gate", "featuregate", "opt in", "opt-in", "optin", "opt out", "opt-out",
      "optout"), "toggle"),
    (("rollout", "rollback", "release", "releases"), "rollout"),
    (("billing", "price", "prices", "priced", "pricing", "charge", "charges", "charging", "refund", "refunds", "customer", "customers",
      "contract", "contracts"), "pricing"),
    (("public api", "rename", "renames", "renamed", "renaming"), "api"),
    (("licen", "license", "licence"), "licence"),
)


def _topic(word: str) -> str:
    low = word.lower()
    for stems, topic in _WORD_TOPIC:
        if any(low.startswith(s) or s in low for s in stems):
            return topic
    return ""


def task_statement(goal: str) -> str:
    """The goal with the host's own workflow instructions taken out: a
    labelled block of them and the list it introduces, and any line that
    names a Raven tool. What is left is what the requester asked for,
    which is the only thing a verdict about the task may be read from."""
    kept: list[str] = []
    in_block = False
    for line in (goal or "").splitlines():
        stripped = line.strip()
        if _HOST_LABEL_RE.match(stripped):
            in_block = True
            continue
        if in_block and (not stripped or _LIST_ITEM_RE.match(stripped)):
            continue
        in_block = False
        if _HOST_TOOL_RE.search(line):
            continue
        kept.append(_FACTS_CLAUSE_RE.sub("", line))
    return "\n".join(kept).strip()


def narrow_edit(title: str, goal: str, paths: list[str]) -> str:
    """Why this task is a narrow, explicit low-risk edit, or "" when it
    is not one: a typo, spelling, grammar, punctuation, whitespace or
    link fix that says it changes no behaviour, or that touches only
    documentation, and poses no question. Anything less explicit keeps
    the conservative triage."""
    text = f"{title}\n{task_statement(goal)}"
    kind = _NARROW_KIND_RE.search(text)
    if not kind or _QUESTION_RE.search(text):
        return ""
    said = _NO_BEHAVIOUR_RE.search(text)
    docs = bool(paths) and all(_DOC_PATH_RE.search(p.strip()) for p in paths)
    if not (said or docs):
        return ""
    return (f"the task is a {kind.group(0).lower()} fix"
            + (f" and says \"{said.group(0)}\"" if said else " in documentation only"))


def _grounded(title: str, goal: str, areas: list[str]) -> list[str]:
    """The fragments of the task that say something about this change:
    the title the agent gave it, and the sentences of the requester's
    own words, less the ones that only tell the agent how to behave.
    A sentence that names a path, a file, an identifier or an area
    Raven knows is about the code however it is phrased; one that
    names none of them and is addressed to the agent ("you are
    authorized to…", "do not cut a release") is the host's house rules,
    and a verdict read off those is a verdict about the host."""
    out = [title.strip()] if (title or "").strip() else []
    for match in _SENTENCE_RE.finditer(task_statement(goal)):
        fragment = match.group(0).strip()
        if not fragment:
            continue
        # An area counts as an anchor only where its name is a path: a
        # bare one (MAINTAINERS, docs) is an ordinary English word too
        # often to be evidence that a sentence is about the code.
        anchored = bool(_ANCHOR_RE.search(fragment)) or any(
            ("/" in a or "." in a) and a.lower() in fragment.lower() for a in areas)
        if not anchored and _AGENT_DIRECTIVE_RE.search(fragment):
            continue
        out.append(fragment)
    return out


def _clip(text: str, width: int = 120) -> str:
    """Whole when it fits, else cut at a word and marked with an ellipsis;
    a single word longer than the width is the only thing cut inside."""
    flat = " ".join((text or "").split())
    if len(flat) <= width:
        return flat
    cut = flat[:width - 1]
    return (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip(",;:. ") + "…"


def judgment_signals(discovery: dict, title: str, goal: str) -> list[tuple[str, str]]:
    """Each judgment word the task speaks, with the fragment it came
    from, so the reason can quote its own evidence."""
    areas = [a.get("path", "") for a in (discovery.get("areas") or []) if a.get("path")]
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for fragment in _grounded(title, goal, areas):
        # A word that is the key of a stated fact ("release=library-next")
        # labels the task; it is not the task speaking of a release.
        found = {m.group(0).lower() for m in _JUDGMENT_RE.finditer(fragment)
                 if not fragment[m.end():].lstrip().startswith("=")}
        for word in sorted(found):
            # Three spellings of one word are one signal, not three.
            key = _topic(word) or word
            if key in seen:
                continue
            seen.add(key)
            out.append((word, fragment))
    return out


def candidates(discovery: dict, title: str, goal: str) -> list[dict]:
    """Decisions this task looks like it contains, from what Raven
    already found. Prompts for the agent, not decisions: it writes the
    ones that are real with bridge_add_node, and ignores the rest.

    Raven cannot see a question the agent never writes down, which a
    reviewer put as the largest remaining gap. This is what it can do
    about that without pretending to read the change: say which of its
    own signals usually mean somebody has to decide something."""
    out: list[dict] = []
    if discovery.get("narrow_edit"):
        # Nothing to prompt for: the edit is the agent's to make.
        return out
    area = (discovery.get("areas") or [{}])[0].get("path", "")
    people = discovery.get("people") or []
    who = people[0]["name"] if people else ""
    # What a model read out of the task, put there once at kickoff and
    # carried on the run's discovery so a later read costs nothing. It
    # goes first: it is specific to this task, where the templates below
    # are the shapes Raven's own signals stand for.
    for item in (discovery.get("named_decisions") or [])[:5]:
        out.append({"question": item["question"],
                    "why": f"the task says \"{_clip(item.get('from', ''), 100)}\""
                           + (f", and {area} is the area it lands in" if area else ""),
                    "paths": area, "owner": who, "source": "the task read closely"})
    seen: set[str] = set()
    for word, fragment in judgment_signals(discovery, title, goal):
        topic = _topic(word)
        if not topic or topic in seen or not (area or who):
            continue
        seen.add(topic)
        out.append({"question": _CANDIDATE_QUESTIONS[topic],
                    "why": f"the task says \"{word}\", in \"{_clip(fragment, 100)}\""
                           + (f", and {area} is the area it lands in" if area else ""),
                    "paths": area, "owner": who, "source": "the task's own words"})
    for prior in (discovery.get("prior_decisions") or [])[:2]:
        out.append({"question": f"Does the earlier answer still hold here? \"{_clip(prior['question'], 160)}\"",
                    "why": f"decision {prior['id']} answered that"
                           + (f", {prior['answered_by']}" if prior.get("answered_by") else ""),
                    "paths": area, "owner": prior.get("answered_by") or who, "source": "a prior decision"})
    listings = discovery.get("listings") or []
    patterns = sorted({e["pattern"] for e in listings})
    if len(patterns) > 1:
        out.append({"question": f"This change spans {patterns[0]} and {patterns[1]}: whose call is the part "
                                "where they meet?",
                    "why": "the areas it touches are listed to different people",
                    "paths": area, "owner": who, "source": "the listings"})
    # Another person's area this change names or is likely to touch keeps
    # its place: nothing else at kickoff names them.
    elsewhere = []
    for o in (discovery.get("other_areas") or [])[:2]:
        if o.get("named"):
            why = (f"the task names {o['named']}, and {o['decider']} decides {o['scope']}; write what the change "
                   f"settles there as a node for {o['decider']}")
        else:
            why = (f"{o['changes']} of the last {o['of']} changes to these files also touched {o['scope']}; write "
                   f"it as a node for {o['decider']} if this change will too")
        elsewhere.append({"question": f"What does this change add or name under {o['scope']}, which {o['decider']} "
                                      "decides?",
                          "why": why, "paths": o["scope"].rstrip("*"), "owner": o["decider"],
                          "source": "the task's own words" if o.get("named") else "the history"})
    return out[:6 - len(elsewhere)] + elsewhere


def triage(discovery: dict, title: str, goal: str) -> tuple[str, str]:
    """Engage only for a reason; pass otherwise. The reasons: the org
    already decided something close, a decision is already pending on
    these paths, the task spans areas owned by different people, the
    task speaks of a change that is a call for its owners, or the task
    itself poses a question."""
    reasons: list[str] = []
    # A narrow, explicit low-risk edit does not inherit the decisions made
    # or pending nearby, nor the ownership around it: they are about other
    # changes. A question the task itself poses still engages.
    narrow = discovery.get("narrow_edit") or ""
    prior = [] if narrow else (discovery.get("prior_decisions") or [])
    if prior:
        p = prior[0]
        reasons.append(f"the org already decided something close: \"{_clip(p['question'], 120)}\""
                       + (f" ({p['answered_by']})" if p.get("answered_by") else ""))
    pending = [] if narrow else (discovery.get("pending") or [])
    if pending:
        reasons.append(f"{len(pending)} decision{'s' if len(pending) > 1 else ''} already pending on these paths"
                       f" (for example \"{_clip(pending[0]['question'], 100)}\")")
    owners: dict[str, str] = {}
    by_pattern: dict[str, set[str]] = defaultdict(set)
    for e in discovery.get("listings") or []:
        if e["role"] in ("maintainer", "owner") and "/" not in e["person"]:
            owners.setdefault(e["person"], e["pattern"])
            by_pattern[e["pattern"]].add(e["person"])
    # Two areas with nobody in common: co-maintainers of one area are not
    # different owners.
    patterns = list(by_pattern.items())
    crossing = None if narrow else next(((a, b) for i, (a, sa) in enumerate(patterns) for b, sb in patterns[i + 1:]
                                          if not (sa & sb)), None)
    if crossing is not None:
        a, b = crossing
        reasons.append("the change spans areas owned by different people: "
                       + f"{', '.join(sorted(by_pattern[a])[:2])} ({a}) and {', '.join(sorted(by_pattern[b])[:2])} ({b})")
    people = discovery.get("people") or []
    signals = [] if narrow else judgment_signals(discovery, title, goal)
    words = [word for word, _ in signals]
    # Whether a change is a call for somebody is a property of the
    # change, not of whether Raven knows who to ask. Not knowing is a
    # gap in the map, and passing on it is how a compatibility question
    # gets decided by an agent because the repository never named a
    # person. The reason says which of the two it is.
    known = bool(people or owners)
    who = (people[0]["name"] if people else next(iter(owners), "")) if known else ""
    nobody = "; Raven does not know who owns this: it goes to the unrouted queue for an operator to place"
    # A task that resolves to no area at all is one Raven cannot reason
    # about; that is still a pass. An area it knows with nobody attached
    # is a gap in the map, not a reason to keep quiet.
    in_scope = known or bool(discovery.get("areas"))
    if words and in_scope:
        # Quote the words' own sentence: a reason that cannot be checked
        # against the task is not a reason somebody can act on.
        # A routing candidate may be only an author or a backup contact.
        # Recency and reachability do not establish decision authority.
        inside = people[0].get("inside", "") if people else ""
        reasons.append(f"the task speaks of {', '.join(words[:4])}, in \"{_clip(signals[0][1])}\", "
                       "which is a call for "
                       + ((f"{who}, who decides for {inside} in this area; name the files to route to them" if inside
                           else f"{who} as a first contact; ask them to confirm who decides or refer")
                          if known else "a person" + nobody))
    text = f"{title}\n{task_statement(goal)}"
    if _QUESTION_RE.search(text) and in_scope:
        reasons.append("the task itself poses a question a person has to answer"
                       + ("" if known or words else nobody))
    if reasons:
        return "engage", "; ".join(reasons)
    if narrow:
        return "pass", (f"{narrow}: nothing here calls for a person, and the decisions made or pending nearby are "
                        "about other changes; Raven passes, and the agent may still add a node if the edit turns "
                        "out to change behaviour")
    if people:
        why = (f"nothing here calls for a person: {people[0]['name']} is a first contact for the area and no "
               "prior decision, pending question, or policy word touches it")
    elif discovery.get("areas"):
        why = "nothing here calls for a person: no clear owner, prior decision, or pending question relates to it"
    else:
        why = "the task names no area Raven knows, and no prior decision or pending question relates to it"
    why += "; Raven passes, and the agent may still add a node if a real decision appears"
    if not (goal or "").strip():
        # A title is what the agent named the task, not what it was asked
        # to do. Measured on a real repository, the same task engages on
        # the requester's own wording and passes on the agent's one-line
        # title, so a pass formed from a title alone says so.
        why += ("; this verdict was formed from a one-line title with no goal: pass bridge_start_task the "
                "task as it was given to you and call it again if there is more to it than the title says")
    return "pass", why


def _next_for_verdict(verdict: str, discovery: dict, proposed: list[dict] | None = None) -> str:
    proposed = proposed or []
    ask = ""
    if discovery.get("unknown_repo"):
        # The most useful next step is not about the task at all: nothing
        # was read, so say that before anything else.
        return (f"call bridge_start_task again with the repository's own name as `repo` "
                f"(Raven has nothing under {discovery['unknown_repo']!r}); this verdict read no repository, "
                "so treat it as no answer rather than as a pass. Known repositories: "
                + ', '.join(discovery.get('known_repos') or [])
                + ". First close this mistaken task with bridge_finish_task(status=abandoned, reason=...). "
                  "Then omit task_id and use a new client_key when starting the correctly scoped task; "
                  "resuming the old task_id does not replace its repository.")
    if proposed:
        ask = (f"; Raven sees {len(proposed)} decision{'s' if len(proposed) > 1 else ''} this task may contain "
               "(candidates): write the ones that are real with bridge_add_node and ignore the rest")
    if verdict == "unplaced":
        return ("look before you change anything: once you know which files this task will change, call "
                "bridge_start_task again with the same title, repo and client_key and those files as `paths`, "
                "before your first edit; Raven answers engage or pass then" + ask)
    who = ", ".join(p["name"] for p in (discovery.get("people") or [])[:2])
    if verdict == "pass" and discovery.get("narrow_edit"):
        return ("proceed without Raven: make the edit yourself. If more than one place fits, pick the clearest "
                "and say which in checks; do not ask anyone to choose. Write a node only if the edit turns out to "
                "change behaviour or needs somebody's call, and call bridge_finish_task when done")
    if verdict == "pass":
        # Measured on a real task, the agent read "proceed without Raven"
        # as leave to pick the token lifetime and the fate of every live
        # token itself, in an area whose owner was on the map.
        return ("proceed without Raven; if you settle something the task left open (a default, a limit, a "
                "lifetime, what happens to existing data or users), write it as a node with bridge_add_node"
                + (f" for {who}, who own{'s' if ', ' not in who else ''} this area" if who else "")
                + ", and call bridge_finish_task when done" + ask)
    return ("write each decision you discover as a node with bridge_add_node (parent_id links it to the "
            "decision it grew from); Raven resolves what it can and routes the rest"
            + (f", most likely to {who}" if who else "") + ask
            + "; read bridge_get_tree before an irreversible step; a file you change in an area someone else "
              "decides needs a node for them, or a one-line reason at bridge_finish_task (uncovered)")


# ---------------- nodes ----------------

def _task(store, task_id: str):
    run = store.graph.get_task(task_id)
    if not run:
        raise Invalid("Task not found")
    return run


def _node(store, decision_id: str, task_id: str = "", what: str = "Decision"):
    """A decision by its exact id, and of the given task when one is
    named. Ids on the canvas are never prefixes: a parent_id of '1' must
    not bind to whichever node happens to start with 1."""
    row = store.graph.db.execute(_DECISION_SELECT + " WHERE d.id=?", (decision_id,)).fetchone()
    if row is None or (task_id and row["run_id"] != task_id):
        raise Invalid(f"{what} must name a node of this task" if task_id else f"{what} not found")
    return _row_to_decision(row)


def _unescaped(text: str) -> str:
    """A quote a host escaped twice. Measured live: Claude Code sent
    'like \\"0.5\\"' in its tool arguments and the owner's card showed
    the backslashes. A backslash before a double quote means nothing in
    prose; every other backslash is kept."""
    return text.replace('\\"', '"') if text else text


def add_node(store, cfg, data) -> dict:
    """One decision the agent discovered, as a node of its task's tree.
    Idempotent: the same client_ref, or the same question on the same
    tree, returns the existing node. The node runs through the ladder:
    resolved from memory or records (marked for sign-off), predicted from
    precedent (to confirm), routed to a person, or an honest unknown."""
    from .config import load
    from .ladder import ask
    task_id = field(data, "task_id", limit=100)
    question = _unescaped(field(data, "question", limit=2000))
    context = _unescaped(_text(data, "context"))
    parent_id = _text(data, "parent_id", 100)
    client_ref = _text(data, "client_ref", 200)
    adopt = _text(data, "adopt", 100)
    category = _text(data, "category", 40)
    depends_on = [d for d in _items("depends_on", data.get("depends_on"), r"[,\s]+") if d]
    from .graph import parse_facts
    facts = parse_facts(data.get("facts"))
    # An explicit owner, as the inbox form picks one; the ladder's own
    # route stands when this is empty.
    owner_id = _text(data, "owner_id", 100)
    paths = _paths(data.get("paths"))
    options = [_unescaped(o) for o in _options(data.get("options"))]
    graph = store.graph
    run = _task(store, task_id)
    if run["status"] in ("completed", "abandoned"):
        raise Invalid("This task is finished; start a new one with bridge_start_task")
    # The task's facts hold here unless this node states its own value.
    facts = {**_json_dict(run["facts"] if "facts" in run.keys() else ""), **facts}
    from .routing_memory import request_key, clarify, resolve_scope
    scope_request_key = request_key(question, client_ref, context, paths)
    requester = _text(data, "requester", 200) or (run["requester"] or "")
    if client_ref:
        row = graph.db.execute(
            "SELECT id FROM decisions WHERE run_id=? AND client_ref=? AND draft=0 ORDER BY created_at LIMIT 1",
            (task_id, client_ref)).fetchone()
        if row:
            resolve_scope(graph, task_id, scope_request_key)
            return node_view(store, row["id"], repeated=True)
    # Reject malformed relationships before reserving a ref or running the
    # ladder. A real host sent a client_ref as depends_on; validation after
    # publication exposed a node without its mandatory approver on retry.
    for dependency in depends_on:
        _node(store, dependency, task_id, "depends_on")
    if parent_id:
        _node(store, parent_id, task_id, "parent_id")
    if adopt:
        candidate = _node(store, adopt, task_id, "adopt")
        if candidate.status != "suggested":
            raise Invalid("adopt must name a follow-up question a person added to this task")
    # A retry of a node already on this tree must not become a new scope
    # question merely because the retry omitted its node-specific facts.
    for existing in graph.db.execute(
            "SELECT id,client_ref,scope_key FROM decisions WHERE run_id=? AND lower(question)=lower(?) "
            "AND draft=0 AND status NOT IN ('suggested','adopted') ORDER BY created_at", (task_id, question)):
        if client_ref and existing['client_ref'] and existing['client_ref'] != client_ref:
            continue
        if (not context and not paths) or existing['scope_key'] == scope_key(context, paths):
            resolve_scope(graph, task_id, scope_request_key)
            return node_view(store, existing['id'], repeated=True)
    if not owner_id:
        from .store import repo_key
        clarification = clarify(graph, task_id, graph.resolve_repo(repo_key(run['repo'])), question,
                                scope_request_key, path=paths[0] if paths else '', category=category, facts=facts)
        if clarification:
            return clarification
    if client_ref:
        # Nothing published under this ref. Take it before the ladder
        # runs, so a retry that overlaps this write waits for its node
        # instead of writing a second one.
        mine, existing = graph.claim_node_ref(task_id, client_ref)
        if existing:
            return node_view(store, existing, repeated=True)
        if not mine:
            held = _await_node_ref(graph, task_id, client_ref)
            if held:
                return node_view(store, held, repeated=True)
            raise Invalid(f"A node with client_ref {client_ref!r} is being written right now on this task. "
                          "Retry; it will come back as the node that write produced.")
    # The same question on this tree is the same node only in the same
    # scope: a bare retry (no context, no paths of its own) returns the
    # existing node; the same words about other files or another named
    # customer are another decision, linked to the first as related.
    # A node still being written is nobody's twin and nobody's relative:
    # it has no place on the tree yet to be the same place as.
    scope = scope_key(context, paths)
    related_ids: list[str] = []
    for row in graph.db.execute(
            "SELECT id, client_ref, scope_key FROM decisions WHERE run_id=? AND lower(question)=lower(?) "
            "AND draft=0 AND status NOT IN ('suggested', 'adopted') ORDER BY created_at",
            (task_id, question)).fetchall():
        if client_ref and row["client_ref"] and row["client_ref"] != client_ref:
            related_ids.append(row["id"])
            continue
        if (not context and not paths) or row["scope_key"] == scope:
            return node_view(store, row["id"], repeated=True)
        related_ids.append(row["id"])
    adopted = None
    if adopt:
        adopted = _node(store, adopt, task_id, "adopt")
        if adopted.status != "suggested":
            raise Invalid("adopt must name a follow-up question a person added to this task")
        parent_id = parent_id or adopted.parent_id
    depth = 0
    parent = None
    if parent_id:
        parent = _node(store, parent_id, task_id, "parent_id")
        depth = parent.depth + 1
    ctx = context
    # A fact the question words one way and the task states another: the
    # stated fact is the scope, and the owner reads both. Measured live on
    # 5e967e4: the question said release=library-next, the task's facts
    # said release=1.26.x, and the owner's brief followed the question.
    clashes = _fact_clashes(question, facts)
    if clashes:
        ctx = (ctx + "\n" if ctx else "") + "Facts: " + "; ".join(
            f"this task states {k}={stated}; the question's wording says {k}={said}, which is not this task's {k}"
            for k, said, stated in clashes)
    hints: list[str] = []
    # Every path this decision is about, worked out once and stored: its
    # own, else its parent's, else its task's. Routing weighs inherited
    # paths only when the question names no clear area; required
    # approvers and approval standing read all of them. Measured live: a
    # node that inherited or listed second the file a reviewer must
    # approve finished without that reviewer.
    scope_paths = list(dict.fromkeys(paths))
    if paths:
        ctx = (ctx + "\n" if ctx else "") + "Paths: " + ", ".join(paths)
    else:
        # A node with no paths of its own is about what its parent, and
        # failing that the task, is about: those paths count only when
        # the question itself names no clear area.
        task_paths = _paths(run["paths"] or "")
        parent_paths = _scope_paths_of(graph, parent) if parent is not None else []
        if parent_paths:
            hints = parent_paths
        elif task_paths:
            hints = task_paths
        scope_paths = list(dict.fromkeys(hints))
    if options:
        ctx = (ctx + "\n" if ctx else "") + "Options: " + " | ".join(options)
    effective = cfg if cfg is not None else load()
    from . import context_connectors
    if context_connectors._connection(store, run['repo']):
        context_connectors.search(store, {'repo': run['repo'], 'query': question[:2000], 'task_id': task_id})
    background = effective.semantic_retrieval
    first_pass = effective.without_models() if background else effective
    graph.expire_rules()
    # The node is built as a draft: routed, given its place on the tree,
    # its scope and sign-off written, and only then published. A reader
    # sees a finished node or no node, never one mid-flight.
    drafts: list[str] = []
    did = ""
    try:
        row = ask(store, first_pass, task_id, question, context=ctx or "a node on the canvas",
                  path=paths[0] if paths else "unknown", category=category, owner_id=owner_id or None,
                  requester=requester, hints=hints, facts=facts, keep_draft=True, also_paths=paths[1:])
        drafts = list(row.get("drafts") or [])
        # A duplicate comes back as its twin's row, which belongs to another
        # task; the node of this task is the stub, and its view names the
        # twin as duplicate_of.
        did = row.get("duplicate_stub") or row["id"]
        view = _place_node(store, graph, run, row, did, task_id, question, context, ctx, category, paths,
                           options, parent_id, client_ref, adopted, depth, scope, related_ids, depends_on,
                           first_pass, requester, hints, drafts, scope_paths=scope_paths, deferred=background)
    except Exception:
        # A node that could not be placed is still a decision somebody
        # asked: published, not hidden. The claim follows it, so a retry
        # returns the node this write did produce; with no node at all,
        # the client_ref goes back and the retry writes one.
        from .ladder import publish_drafts
        publish_drafts(graph, drafts)
        if client_ref:
            if did:
                graph.settle_node_ref(task_id, client_ref, did)
            else:
                graph.release_node_ref(task_id, client_ref)
        raise
    if client_ref:
        graph.settle_node_ref(task_id, client_ref, did)
    if background:
        _start_background(('node', did), _model_node_pass, store, effective, did, ctx, paths, hints, requester,
                          owner_id, context, view['updated_at'])
    resolve_scope(graph, task_id, scope_request_key)
    return view


CLAIM_WAIT = 20.0


def _await_node_ref(graph, task_id: str, client_ref: str, seconds: float = CLAIM_WAIT,
                    sleep=None, clock=None) -> str:
    """Another write holds this client_ref. Wait for the node it
    produces, rather than writing a second one for the same ref."""
    import time as _time
    sleep = sleep or _time.sleep
    clock = clock or _time.monotonic
    deadline = clock() + seconds
    while True:
        held = graph.claimed_node_ref(task_id, client_ref)
        if held:
            return held
        if clock() >= deadline:
            return ""
        sleep(0.05)


def _scope_paths_of(graph, node) -> list[str]:
    """The paths a node is about, as stored; its single path for a node
    written before scope paths were stored."""
    row = graph.db.execute("SELECT scope_paths, path FROM decisions WHERE id=?", (node.id,)).fetchone()
    if row is None:
        return []
    try:
        stored = [str(p) for p in json.loads(row["scope_paths"] or "[]") if p]
    except (ValueError, TypeError):
        stored = []
    if stored:
        return stored
    return [row["path"]] if row["path"] and row["path"] != "unknown" else []


def _approvers(graph, repo: str, question: str, ctx: str, category: str, scope_paths: list[str]) -> list[str]:
    """Everyone the authority map says must approve this decision: over
    every path it is about, every file its question names, its category
    and its repository. A team's members each."""
    from .signals import _authority_matches
    probes = list(scope_paths)
    try:
        from .resolve import explicit_hits, tree_for
        tree = tree_for(graph, repo)
        if tree.files:
            probes += [h.path for h in explicit_hits(tree, question).values() if h.weight >= 0.9]
    except Exception:
        pass
    out: list[str] = []
    for p in list(dict.fromkeys(probes)) or [""]:
        for m in _authority_matches(graph, repo, [], question, ctx, category, p):
            if m["role"] == "approves" and m["strong"] and m["name"] not in out:
                out.append(m["name"])
    return out


def _place_node(store, graph, run, row, did, task_id, question, context, ctx, category, paths,
                options, parent_id, client_ref, adopted, depth, scope, related_ids, depends_on,
                effective, requester, hints, drafts, scope_paths=(), deferred=False) -> dict:
    """The second half of add_node: the node is routed; give it its place
    on the tree, its scope, its signers and its notifications, then
    publish it. Split out so a failure anywhere in here still publishes
    the draft instead of leaving a decision nobody can see."""
    status = "duplicate" if did != row["id"] else row["status"]
    signoff = "required" if status in ("resolved", "partial", "assumed", "proposed") else ""
    if signoff and row.get("signoff") == "rule":
        # A reusable rule covered it: authorized by its owner's standing
        # declaration, no signature wanted here.
        signoff = "rule"
    # A node the record or memory settled still wants a person's sign-off,
    # so it names the person the signals point at, the same route a
    # pending node takes; the answer itself is unchanged.
    if signoff == "required" and not row.get("owner_name"):
        from .ladder import _ranked_view
        from .graph import parse_facts
        from .routing import route_ranked
        from .store import repo_key
        repo = graph.resolve_repo(repo_key(run["repo"]))
        if repo:
            signer = _route_signer(graph, run, question, ctx, paths, requester, hints, category,
                                   parse_facts(row.get("facts")), row.get('source_id'))
            if not signer:
                from .routing import coordinator_route
                fallback = coordinator_route(graph, repo, ["nobody is verified or clearly placed to sign this"])
                signer = [fallback] if fallback else []
            if signer:
                name, evidence, _score = signer[0]
                graph.update_decision(did, owner=name, owner_evidence="; ".join(["signs off"] + list(evidence)))
                row["ranked"] = _ranked_view(signer)
    # A pending node reaches its owner with a brief written from what
    # the agent said and what Raven found, when a model key is present.
    if status == "pending" and row.get("owner_name") and effective.semantic_retrieval:
        from .llm import compose_brief
        withheld: list[str] = []
        brief = compose_brief(effective, question, context, run["title"], options=options, why=withheld)
        if brief:
            graph.update_decision(did, brief=brief)
        elif withheld:
            # Not shown: the owner reads the agent's own words instead.
            graph.append_event("brief_withheld", {"task_id": task_id, "decision_id": did, "why": _clip(withheld[0], 400)})
    scope_paths = list(scope_paths or [])
    graph.db.execute(
        "UPDATE decisions SET parent_id=?, client_ref=?, depth=?, origin=?, options=?, scope_key=?, scope_paths=?, "
        "signoff=CASE WHEN signoff='' THEN ? ELSE signoff END WHERE id=?",
        (parent_id, client_ref, depth, "human" if adopted else "agent",
         json.dumps(options) if options else "", scope, json.dumps(scope_paths) if scope_paths else "",
         signoff, did))
    if deferred:
        graph.db.execute('UPDATE decisions SET model_pending=1 WHERE id=?', (did,))
    for rid in related_ids:
        graph.add_link(did, rid, "related", "the same question in another scope on this tree")
    # What this decision depends on: nodes of the same task it cannot be
    # acted on without; the tree shows both directions.
    for dep in depends_on[:MAX_PATHS]:
        if dep == did:
            continue
        _node(store, dep, task_id, "depends_on")
        graph.add_link(did, dep, "depends", "this decision depends on that one")
    # Who must sign: every person the authority map says must approve
    # this scope (a team's members each). One answer or signature is
    # not enough while any of them has not signed.
    if status != "duplicate":
        from .store import repo_key as _rk
        try:
            approvers = _approvers(graph, graph.resolve_repo(_rk(run["repo"])), question, ctx, category, scope_paths)
        except Exception as error:
            # Not knowing who must approve is never "nobody must": the node
            # waits for a person to look, and says why.
            approvers = []
            graph.db.execute("UPDATE decisions SET needs_review=1, review_reason=? WHERE id=?",
                             (f"Raven could not work out who must approve this ({type(error).__name__}); check the "
                              "authority map before relying on it", did))
        if approvers:
            graph.db.execute("UPDATE decisions SET required_signers=? WHERE id=?", (json.dumps(approvers), did))
        if approvers and signoff == "rule":
            # A rule speaks for the people who signed the decision it was
            # made from, and no one else. Measured live on 5e967e4: a rule
            # Mira made authorized a node on a file Theo must approve, and
            # the task finished without Theo.
            missing = _not_behind_rule(graph, row.get("source_id") or "", approvers)
            if missing:
                graph.db.execute(
                    "UPDATE decisions SET signoff='required', evidence=evidence || ? WHERE id=?",
                    (f"; the rule does not stand in for {', '.join(missing)}, who must approve this and did not "
                     f"sign decision {row.get('source_id')}, which the rule was made from", did))
                for name in missing:
                    if not deferred:
                        store.notify(did, "signoff", to=name)
    # Snapshot the complete published scope, including required signers.
    # The person it waits on hears about it: a question to answer, or an
    # answer to sign.
    if status == "pending" and not deferred:
        store.notify(did, "ask")
    elif signoff == "required" and not deferred:
        store.notify(did, "signoff")
    if adopted is not None:
        graph.db.execute("UPDATE decisions SET status='adopted', superseded_by=?, updated_at=? WHERE id=?",
                         (did, now(), adopted.id))
    graph.append_event("node_added", {"task_id": task_id, "decision_id": did, "parent_id": parent_id,
                                      "depth": depth, "client_ref": client_ref, "status": status,
                                      "adopted": adopted.id if adopted else ""})
    from .ladder import publish_drafts
    publish_drafts(graph, drafts)
    view = node_view(store, did)
    if "ranked" in row:
        view["ranked"] = row["ranked"]
    return view


def _fact_clashes(question: str, facts: dict) -> list[tuple[str, str, str]]:
    """The key=value facts a question names that the stated facts give
    another value, as (key, the question's value, the stated value)."""
    out = []
    for key, value in re.findall(r"\b([a-z][\w-]*)=([^\s,;)?]+)", question or "", re.IGNORECASE):
        stated = (facts or {}).get(key.lower())
        value = value.rstrip(".")
        if stated and " ".join(str(stated).split()).lower() != value.lower():
            out.append((key.lower(), value, str(stated)))
    return out


def _not_behind_rule(graph, source_id: str, approvers: list[str]) -> list[str]:
    """The required approvers who did not sign the decision a rule was
    made from."""
    row = graph.db.execute("SELECT signatures, signed_by FROM decisions WHERE id=?", (source_id,)).fetchone() \
        if source_id else None
    signed = set()
    if row is not None:
        signed = {str(x.get("by", "")).strip().lower() for x in _json_list(row["signatures"]) if isinstance(x, dict)}
        signed |= {n.strip().lower() for n in (row["signed_by"] or "").split(",") if n.strip()}
    return [a for a in approvers if a.strip().lower() not in signed]


def settle_node(store, data) -> dict:
    """The agent settles a node itself: the answer it acted on and why,
    recorded on the tree and marked for sign-off, so the whole decision
    record is visible to the people who own it."""
    task_id = field(data, "task_id", limit=100)
    node_id = field(data, "node_id", limit=100)
    answer = field(data, "answer")
    rationale = field(data, "rationale", "settled by the agent")
    graph = store.graph
    from .store import check_not_abandoned
    with graph.transaction() as db:
        check_not_abandoned(db, task_id)
        d = _node(store, node_id, task_id, "node_id")
        if d.status in ("approved", "duplicate", "adopted", "suggested", "withdrawn"):
            raise Invalid(f"a node that is {d.status} cannot be settled by the agent")
        if d.signoff in ("signed", "rule"):
            raise Invalid("a node a person signed is not re-settled by the agent; ask for a correction in the inbox")
        changed = bool(d.answer and d.answer.strip() != answer.strip())
        graph.update_decision(d.id, status="resolved", source="agent", answer=answer, rationale=rationale,
                              kind="agent", evidence="settled by the agent: " + _marked(
                                  rationale, 1200, "the rationale on the node has the rest"),
                              signoff="required", prediction=None, source_id=None, source_revision="",
                              needs_review=0, review_reason="")
        if changed:
            # A signature covers the text it was given for and nothing
            # else: a re-settled answer starts with none.
            db.execute("UPDATE decisions SET signatures='[]', signed_by='' WHERE id=?", (d.id,))
        pending = db.execute("SELECT 1 FROM decisions WHERE run_id=? AND status='pending'", (task_id,)).fetchone()
        if not pending:
            db.execute("UPDATE runs SET status=CASE WHEN status='completed' THEN status ELSE 'working' END,"
                       "updated_at=? WHERE id=?", (now(), task_id))
        graph.append_event("node_settled", {"task_id": task_id, "decision_id": d.id})
        if changed:
            graph.flag_dependents(d.id, f"the agent re-settled decision {d.id} with a different answer")
    if d.owner:
        store.notify(d.id, "signoff")
    return node_view(store, d.id)


_STATUS = {"approved": "answered", "resolved": "resolved", "partial": "resolved", "assumed": "predicted",
           "proposed": "predicted", "duplicate": "duplicate", "suggested": "suggested", "adopted": "adopted"}


def _status(d: dict) -> str:
    s = d["status"]
    if s == "pending":
        return "pending" if d.get("owner_name") else "unrouted"
    return _STATUS.get(s, s)


def _next_for_node(status: str, d: dict) -> str:
    if status == "withdrawn":
        return "This question was withdrawn when its mistaken task was closed."
    if d.get("model_pending"):
        return "Still reading the evidence in the background; use bridge_wait or bridge_get_tree. "
    if d.get("needs_review"):
        return ("needs review: " + (d.get("review_reason") or "an answer it was derived from was corrected")
                + "; do not act on it until a person confirms or corrects it in the inbox")
    if status == "answered":
        return ("act on the answer" + (f" from {d['answered_by']}" if d.get("answered_by") else "")
                + "; follow-up questions they added are children of this node in bridge_get_tree")
    if status in ("resolved", "predicted"):
        # What a person has done with the answer decides what the agent may
        # do, whatever the answer began as. Measured live: a prediction its
        # owner signed still said "a prediction, not a decision" here.
        required = _json_list(d.get("required_signers"))
        signed = _live_signatures(d)
        # Everyone it waits on, the owner and each required approver, not
        # the owner alone.
        waiting = [n for n in dict.fromkeys([d.get("owner_name") or ""] + required)
                   if n and n.lower() not in {x.lower() for x in signed}]
        signer = " and ".join(waiting) if waiting else (d.get("owner_name") or "the owner")
        began = ("" if status != "predicted" and d.get("kind") != "prediction"
                 else " (it began as a default Raven assumed)"
                 if d.get("status") == "assumed" else " (it began as a prediction from an earlier answer)")
        if (d.get("signoff") or "") == "required" and required and signed:
            remaining = [r for r in required if r.lower() not in {x.lower() for x in signed}]
            if remaining:
                return (f"signed by {', '.join(signed)}; still waiting on {', '.join(remaining)} (every required "
                        "approver signs): prepare on this answer, do not ship on it")
        if (d.get("signoff") or "") == "rule":
            return ("covered by a reusable rule" + (f" {d['signed_by']} made" if d.get("signed_by") else "")
                    + ": authorized, act on this answer; the evidence names the rule and its conditions")
        if (d.get("signoff") or "") in ("signed", "rule"):
            return ("signed" + (f" by {d['signed_by']}" if d.get("signed_by") else "")
                    + f": act on this answer{began}; follow-up questions are children of this node")
        if status == "predicted":
            return (f"a prediction, not a decision for this question: based on the source in its evidence, still needing confirmation here. Prepare "
                    f"on it, do not ship on it until {signer} signs it in the inbox; read bridge_get_tree before an "
                    "irreversible step")
        if (d.get("kind") or "") == "agent":
            return (f"your own answer is on the tree, unconfirmed, sign-off wanted from {signer}: prepare on it, "
                    "do not ship on it; a person may correct it, so read bridge_get_tree before an irreversible step")
        if d.get("status") == "partial":
            return (f"answered only in part: the evidence settles some of this and says what it leaves open. Do not "
                    f"fill the open part yourself: {signer} completes or signs it in the inbox (bridge_finish_task "
                    "is refused until then); read bridge_get_tree before an irreversible step")
        return (f"unconfirmed: evidence, not sign-off. Prepare on this answer, do not ship on it until {signer} "
                "signs it in the inbox (bridge_finish_task is refused until then); read bridge_get_tree before "
                "an irreversible step")
    if status == "pending":
        # Measured on a real task: told to "continue independent work", the
        # agent wrote the very choice it had asked about, its own way.
        return (f"waiting on {d.get('owner_name')}: do not settle this in code yourself; build only what does "
                "not depend on the answer, then bridge_wait (or read bridge_get_tree) before you write the part "
                "that does")
    if status == "unrouted":
        return "Raven does not know who owns this; a person assigns it in the inbox, or settle it yourself"
    if status == "duplicate":
        return f"the same question is already open as node {d.get('superseded_by')}; one answer settles both"
    if status == "suggested":
        if d.get("followup_required"):
            return ("a person added this question and marked it required: adopt it with "
                    "bridge_add_node(adopt=<node_id>); the task cannot finish until it is answered")
        return "a person added this question; adopt it with bridge_add_node(adopt=<node_id>) or leave it"
    return ""


def node_view(store, decision_id: str, repeated: bool = False) -> dict:
    """One node as the agent sees it, from its row (and, for a duplicate,
    the canonical decision it points at). A rule that expired since the
    last read stops authorizing before this says what is authorized."""
    store.graph.expire_rules()
    row = store.graph.db.execute(_DECISION_SELECT + " WHERE d.id=?", (decision_id,)).fetchone()
    if row is None:
        raise Invalid("Decision not found")
    canonical = None
    if row["status"] == "duplicate" and row["superseded_by"]:
        canonical = store.graph.db.execute(_DECISION_SELECT + " WHERE d.id=?", (row["superseded_by"],)).fetchone()
    view = _view(row, repeated, canonical)
    from . import context_memory as cm
    view['sources'] = cm.edges(store.graph.db, canonical['id'] if canonical else decision_id)
    view['source_reuse_requires_review'] = bool((canonical if canonical else row)['source_reuse_uncertain'])
    view['source_provenance'] = 'versioned' if view['sources'] and not view['source_reuse_requires_review'] else 'unknown'
    view['source_notice'] = cm.provenance_notice((canonical if canonical else row)['source'], view['sources'], (canonical if canonical else row)['source_reuse_uncertain'])
    view["related"] = _related(store.graph.links_for([decision_id]).get(decision_id, []))
    view["depends_on"] = [r["related_id"] for r in store.graph.db.execute(
        "SELECT related_id FROM decision_links WHERE kind='depends' AND decision_id=?", (decision_id,))]
    view["dependents"] = [r["decision_id"] for r in store.graph.db.execute(
        "SELECT decision_id FROM decision_links WHERE kind='depends' AND related_id=?", (decision_id,))]
    if row['parent_id']:
        parent = store.graph.db.execute(
            _DECISION_SELECT + ' WHERE d.id=? AND d.run_id=? AND d.draft=0',
            (row['parent_id'], row['run_id'])).fetchone()
        _with_parent_context(store, view, parent)
    return view


def _with_parent_context(store, view, parent, compact=False):
    if parent is not None:
        context = _decision_context(store, parent)
        # The whole tree already carries the parent's complete answer once.
        # Repeating it in every sibling multiplies response size by fan-out.
        view['parent'] = ({key: context[key] for key in (
            'node_id', 'status', 'authorized', 'needs_review', 'updated_at')} if compact else context)
        view['next'] += (f" Read parent node {parent['id']} (its current context is here or in bridge_get_tree) "
                         'before adding more questions; reconcile what its answer already covers. Parent context does not '
                         'authorize this child or transfer its owner\'s authority.')


def _related(links: list[dict]) -> list[dict]:
    return [ln for ln in links if ln["kind"] in ("related", "derived", "depends")]


def _view(row, repeated: bool = False, canonical=None) -> dict:
    """The node dict for one _DECISION_SELECT row: what node_view returns
    for a single node and what get_tree builds for every node of a task
    from one query. A duplicate reads through to its canonical decision:
    the answer, who gave it, whether it is signed, and what to do next
    come from there, so a stub never says "waiting" after the decision
    it points at was answered."""
    d = dict(row)
    status = _status(d)
    view = {"node_id": d["id"], "task_id": d["run_id"], "parent_id": d.get("parent_id") or "",
            "depth": int(d.get("depth") or 0), "client_ref": d.get("client_ref") or "",
            "origin": d.get("origin") or "agent", "question": d["question"], "status": status,
            "kind": d.get("kind") or "", "answer": d.get("answer") or "", "prediction": d.get("prediction") or "",
            "evidence": d.get("evidence") or "", "owner": d.get("owner_name") or "",
            "owner_evidence": d.get("owner_evidence") or "", "answered_by": d.get("answered_by") or "",
            "rationale": d.get("rationale") or "", "signoff": d.get("signoff") or "",
            "signed_by": d.get("signed_by") or "", "brief": d.get("brief") or "",
            "path": d.get("path") or "", "category": d.get("category") or "",
            "created_at": d["created_at"], "updated_at": d["updated_at"],
            "needs_review": bool(d.get("needs_review")), "review_reason": d.get("review_reason") or "",
            "authorized": _authorized(d), "blocking": _blocking(d),
            "duplicate_of": d.get("superseded_by") if status == "duplicate" else "",
            "reusable": bool(d.get("reusable")), "rule_conditions": d.get("rule_conditions") or "",
            "rule_expires": d.get("rule_expires") or "", "rule_scope": d.get("rule_scope") or "",
            "facts": _json_dict(d.get("facts")),
            "required_signers": _json_list(d.get("required_signers")),
            "signatures": _live_signatures(d),
            "historical_signatures": _signature_names(d) if d.get('needs_review') else [],
            "followup_required": bool(d.get("followup_required")),
            # An answer the evidence supports only in part reads "resolved"
            # like any other, and says so here. Measured live on eb9d22d:
            # stored as partial, shown to the agent as plain resolved.
            "partial": d.get("status") == "partial", "model_pending": bool(d.get("model_pending"))}
    try:
        view["options"] = json.loads(d.get("options") or "[]")
    except ValueError:
        view["options"] = []
    if repeated:
        view["repeated"] = True
    view["next"] = _next_for_node(status, d)
    if status == "duplicate" and canonical is not None:
        c = dict(canonical)
        c_status = _status(c)
        for key, col in (("answer", "answer"), ("answered_by", "answered_by"), ("signoff", "signoff"),
                         ("signed_by", "signed_by"), ("owner", "owner_name"), ("owner_evidence", "owner_evidence"),
                         ("rationale", "rationale"), ("kind", "kind")):
            view[key] = c.get(col) or ""
        view["needs_review"] = bool(c.get("needs_review"))
        view["review_reason"] = c.get("review_reason") or ""
        view["authorized"] = _authorized(c)
        view["blocking"] = _blocking(c)
        view["resolution"] = {"node_id": c["id"], "task_id": c["run_id"], "status": c_status,
                              "updated_at": c["updated_at"]}
        view["next"] = (f"same decision as node {c['id']} (task {c['run_id']}), which is {c_status}: "
                        + _next_for_node(c_status, c))
    return view


def _json_list(raw) -> list:
    try:
        value = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


def _json_dict(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _live_signatures(d: dict) -> list[str]:
    """The people whose signature covers the answer as it stands now: a
    signature is bound to the text it was given for, so a corrected or
    re-settled answer has none until people sign it again."""
    return [] if d.get('needs_review') else _signature_names(d)


def _signature_names(d: dict) -> list[str]:
    content = answer_hash(d.get("answer") or "")
    return [x.get("by", "") for x in _json_list(d.get("signatures"))
            if isinstance(x, dict) and x.get("by") and x.get("hash") == content]


def _authorized(d: dict) -> bool:
    return not d.get("needs_review") and (d.get("status") == "approved" or (d.get("signoff") or "") in ("signed", "rule"))


def _blocking(d: dict) -> bool:
    if d.get("model_pending") or d.get("status") == "pending" or d.get("needs_review"):
        return True
    if d.get("status") == "suggested":
        return bool(d.get("followup_required"))
    return d.get("status") in ("resolved", "partial", "assumed", "proposed") and not _authorized(d)


def get_tree(store, task_id: str) -> dict:
    """The whole canvas of one task: its verdict, every node nested under
    the node it grew from, the follow-up questions people added, and
    what to do next. The nodes come from one query; the discovery digest
    is not repeated here, bridge_start_task returned it."""
    graph = store.graph
    run = _task(store, task_id)
    # A rule that expired since the last read stops authorizing before
    # the tree says what is authorized.
    graph.expire_rules()
    graph.expire_drafts()
    # A correction committed while this response is being assembled was not
    # necessarily in the rows below. Never acknowledge it with an end-of-read
    # timestamp merely because rendering the old snapshot took longer.
    observed_revision = human_revisions(store, task_id)
    observed_at = now()
    rows = graph.db.execute(_DECISION_SELECT + " WHERE d.run_id=? AND d.draft=0 ORDER BY d.created_at, d.rowid",
                            (task_id,)).fetchall()
    recovered = False
    discovery = _json_dict(run['discovery'])
    if discovery.get('model_pending') and time.time() - __import__('datetime').datetime.fromisoformat(run['updated_at']).timestamp() > MODEL_PENDING_STALE:
        with _BACKGROUND_LOCK:
            active = any(key[:2] == ('task', task_id) for key in _BACKGROUND)
        if not active:
            discovery.pop('model_pending', None)
            discovery['model_error'] = 'Reading interrupted; the rules verdict still applies'
            with graph.transaction():
                graph.db.execute('UPDATE runs SET discovery=? WHERE id=?', (json.dumps(discovery), task_id))
                graph.append_event('task_triage_interrupted', {'task_id': task_id})
            run = _task(store, task_id)
    for item in rows:
        if item['model_pending'] and time.time() - __import__('datetime').datetime.fromisoformat(item['updated_at']).timestamp() > MODEL_PENDING_STALE:
            with _BACKGROUND_LOCK:
                active = any(key[:2] == ('node', item['id']) for key in _BACKGROUND)
            if not active:
                graph.db.execute('UPDATE decisions SET model_pending=0 WHERE id=?', (item['id'],))
                graph.append_event('model_read_interrupted', {'task_id': task_id, 'decision_id': item['id']})
                _notify_model_ready(store, store.get_decision(item['id']))
                recovered = True
    if recovered:
        rows = graph.db.execute(_DECISION_SELECT + " WHERE d.run_id=? AND d.draft=0 ORDER BY d.created_at, d.rowid", (task_id,)).fetchall()
    # Duplicates read through to their canonical decision; the ones on
    # this tree are already loaded, the ones on other trees cost one
    # more query, only when there are any.
    loaded = {r["id"]: r for r in rows}
    missing = [r["superseded_by"] for r in rows
               if r["status"] == "duplicate" and r["superseded_by"] and r["superseded_by"] not in loaded]
    if missing:
        marks = ",".join("?" for _ in missing)
        for r in graph.db.execute(_DECISION_SELECT + f" WHERE d.id IN ({marks})", missing).fetchall():
            loaded[r["id"]] = r
    nodes = [_view(r, canonical=loaded.get(r["superseded_by"]) if r["status"] == "duplicate" else None)
             for r in rows]
    from .context_connectors import blocked_decisions
    context_blocked = blocked_decisions(graph.db, run['repo'])
    for node in nodes:
        from . import context_memory as cm
        node['sources'] = cm.edges(graph.db, node.get('duplicate_of') or node['node_id'])
        source_row = loaded.get(node.get('duplicate_of') or node['node_id'])
        node['source_reuse_requires_review'] = bool(source_row and source_row['source_reuse_uncertain'])
        node['source_provenance'] = 'versioned' if node['sources'] and not node['source_reuse_requires_review'] else 'unknown'
        node['source_notice'] = cm.provenance_notice(source_row['source'] if source_row else '', node['sources'], bool(source_row and source_row['source_reuse_uncertain']))
        if (node.get('duplicate_of') or node['node_id']) in context_blocked:
            node['authorized'] = False
            node['blocking'] = True
            node['source_refresh_required'] = True
            node['next'] = 'External evidence needs a successful refresh before this answer can authorize work. Check bridge_connection_status.'
        parent = loaded.get(node['parent_id'])
        if parent is not None and parent['run_id'] == task_id and not parent['draft']:
            _with_parent_context(store, node, parent, compact=True)
    links = graph.links_for([n["node_id"] for n in nodes])
    depends_of: dict[str, list[str]] = defaultdict(list)
    marks = ",".join("?" for _ in nodes) or "''"
    for r in graph.db.execute("SELECT decision_id, related_id FROM decision_links WHERE kind='depends' AND decision_id IN "
                              f"({marks})", [n["node_id"] for n in nodes]):
        depends_of[r["decision_id"]].append(r["related_id"])
    for n in nodes:
        n["related"] = _related(links.get(n["node_id"], []))
        n["depends_on"] = depends_of.get(n["node_id"], [])
        n["dependents"] = [k for k, deps in depends_of.items() if n["node_id"] in deps]
    by_id = {n["node_id"]: n for n in nodes}
    # When each node was last answered or corrected by a person: a
    # sign-off or a re-route also touches updated_at, an answer does not
    # come from those.
    answered_at = {r["decision_id"]: r["at"] for r in graph.db.execute(
        "SELECT decision_id, max(created_at) AS at FROM events WHERE run_id=? "
        "AND kind IN ('owner_approved', 'answer_corrected') GROUP BY decision_id", (task_id,))}
    for n in nodes:
        parent = by_id.get(n["parent_id"])
        # A parent answered after this node was written: the answer may
        # change what this node should ask. Once a person answers this node
        # after that, their answer is the later word and nothing is left to
        # re-read. Measured live: three answers, children answered after
        # the parent, and the finished task still said to re-read two.
        # A follow-up the agent adopted lives on as the node that adopted it;
        # the placeholder has nothing left to re-read.
        parent_at = answered_at.get(parent["node_id"], "") if parent else ""
        n["parent_changed_after"] = bool(parent and parent["status"] == "answered" and n["status"] != "adopted"
                                         and parent_at > n["created_at"]
                                         and answered_at.get(n["node_id"], "") <= parent_at)
    children: dict[str, list[dict]] = defaultdict(list)
    for n in nodes:
        pid = n["parent_id"] if n["parent_id"] in by_id else ""
        children[pid].append(n)

    def nest(pid: str) -> list[dict]:
        return [{**n, "children": nest(n["node_id"])} for n in children.get(pid, [])]

    counts = defaultdict(int)
    for n in nodes:
        counts[n["status"]] += 1
        if n["status"] != "duplicate" and n["signoff"] == "required":
            counts["signoff_required"] += 1
        if n["needs_review"]:
            counts["needs_review"] += 1
        if n["blocking"]:
            counts["blocking"] += 1
        if n["parent_changed_after"]:
            counts["parent_changed_after"] += 1
    followups = [n for n in nodes if n["status"] == "suggested"]
    parts: list[str] = []
    if any(n.get('source_refresh_required') for n in nodes):
        parts.append('External evidence is awaiting refresh; earlier signatures do not authorize work while its current access or content is unverified')
    from .routing_memory import pending_scopes
    scope_clarifications = pending_scopes(graph, task_id)
    if scope_clarifications:
        counts['blocking'] += len(scope_clarifications)
        counts['needs_scope_clarification'] = len(scope_clarifications)
        parts.append(f"{len(scope_clarifications)} scope clarification(s) wait on the agent: confirm the missing "
                     "facts, then retry bridge_add_node with the same client_ref and explicit facts")
    waiting = [n for n in nodes if n["status"] == "pending"
               or (n["status"] == "duplicate" and n.get("resolution", {}).get("status") == "pending")]
    if waiting:
        who = sorted({n["owner"] for n in waiting if n["owner"]})
        parts.append(f"{len(waiting)} waiting on {', '.join(who) if who else 'nobody yet'}")
    if counts["unrouted"]:
        parts.append(f"{counts['unrouted']} with no owner (assign in the inbox or settle yourself)")
    if counts["needs_review"]:
        parts.append(f"{counts['needs_review']} need{'s' if counts['needs_review'] == 1 else ''} review: an "
                     "answer they were derived from was corrected; do not act on them")
    if followups:
        must = sum(1 for n in followups if n["followup_required"])
        parts.append(f"{len(followups)} follow-up question{'s' if len(followups) > 1 else ''} added by people, "
                     f"adopt with bridge_add_node(adopt=...)"
                     + (f" ({'all' if must == len(followups) else must} required: the task cannot finish until "
                        f"{'it is' if must == 1 else 'they are'} adopted and answered)" if must else ""))
    if counts["parent_changed_after"]:
        parts.append(f"{counts['parent_changed_after']} node{'s' if counts['parent_changed_after'] > 1 else ''} "
                     f"written before their parent was answered: re-read them")
    if counts["signoff_required"]:
        # Unsigned ones wait on their owner; ones a person signed wait on
        # each required approver who has not. Measured live: this named the
        # owner who had signed, not the reviewer it still waited on.
        wanted = [n for n in nodes if n["signoff"] == "required" and n["status"] != "duplicate"]
        unsigned = [n for n in wanted if not n["signatures"]]
        partly = [n for n in wanted if n["signatures"]]
        if unsigned:
            owners = sorted({n["owner"] for n in unsigned if n["owner"]})
            parts.append(f"{len(unsigned)} resolved without a person, sign-off wanted"
                         + (f" from {', '.join(owners)}" if owners else "")
                         + "; prepare on them, do not ship on them")
        if partly:
            missing = list(dict.fromkeys(r for n in partly for r in n["required_signers"]
                                         if r.lower() not in {x.lower() for x in n["signatures"]}))
            parts.append(f"{len(partly)} signed by one person, still waiting on "
                         f"{', '.join(missing) if missing else 'a required approver'}; prepare on them, do not "
                         "ship on them")
    if counts["blocking"] and run["status"] != "completed":
        parts.append("bridge_finish_task is refused until every node above is answered or signed"
                     + (", and every required follow-up adopted" if any(n["followup_required"] for n in followups)
                        else ""))
    blocked_deps = [n for n in nodes if any(by_id.get(dep, {}).get("blocking") for dep in n["depends_on"])]
    if blocked_deps:
        parts.append(f"{len(blocked_deps)} node{'s' if len(blocked_deps) > 1 else ''} depend on a decision that "
                     "still waits on a person")
    notes = task_notes(store, task_id)
    if notes:
        parts.append(f"{len(notes)} note{'s' if len(notes) > 1 else ''} from people on this task (notes)")
    review = _current_review(graph, task_id, _flatten(nest("")))
    if review is not None and review["status"] == "running":
        parts.append("the advisory diff review is still running; call bridge_wait to read its result, or repeat "
                     "bridge_finish_task with the same complete diff and checks to resume an interrupted review. "
                     "Once it finishes, repeat bridge_finish_task to refresh the saved proof")
    elif review is not None and review["status"] == "failed":
        parts.append("the advisory diff review failed; call bridge_finish_task again with the same complete "
                     "diff and checks to retry it before exporting the proof")
    if review is not None and review["status"] == "stale":
        ids = ", ".join(x["node_id"] for x in review["stale"][:6])
        parts.append(f"the diff reading on file (review {review['id']}) needs refreshing for "
                     f"{len(review['stale'])} decision{'s' if len(review['stale']) != 1 else ''} ({ids}): call "
                     "bridge_finish_task again with your current diff to read it against the questions and answers as they stand, "
                     "and do not report the change as following them until then")
    from . import context_memory as cm
    return {"source_anchors": cm.anchors(graph.db, task_id), "task_id": task_id, "title": run["title"], "goal": run["goal"] or "", "repo": run["repo"],
            "requester": run["requester"] or "", "facts": _json_dict(run["facts"] if "facts" in run.keys() else ""),
            "status": run["status"], "verdict": run["verdict"] or "",
            "verdict_why": run["verdict_why"] or "", "counts": dict(counts),
            "needs_review": bool(run["needs_review"]) if "needs_review" in run.keys() else False,
            "model_pending": bool(_json_dict(run["discovery"]).get("model_pending")) or any(n.get("model_pending") for n in nodes),
            "nodes": nest(""), "followups": followups, "observed_at": observed_at,
            "observed_revision": observed_revision, "notes": notes,
            "scope_clarifications": scope_clarifications,
            "next": "; ".join(parts) if parts else ("no node waits on anyone" if nodes else _nothing_yet(run)),
            **({"review": review} if review is not None else {})}


def _nothing_yet(run) -> str:
    """The tree is empty. If the kickoff saw decisions this task may
    contain, say so again here: this is where an agent looks before it
    calls the task done."""
    try:
        discovery = json.loads(run["discovery"] or "{}")
    except (ValueError, TypeError):
        discovery = {}
    proposed = candidates(discovery, run["title"] or "", run["goal"] or "")
    if not proposed:
        return "no node yet; add the decisions you discover"
    listed = "; ".join(f"{c['question']} ({c['why']})" for c in proposed[:3])
    return (f"no node yet. Raven sees {len(proposed)} it may contain, from its own signals: {listed}. "
            "Write the ones that are real with bridge_add_node; if none is, finish")


def task_notes(store, task_id: str, since: str = "") -> list[dict]:
    """What people added to the task itself: context the agent should
    read, in the order it was written."""
    out = []
    for r in store.graph.db.execute("SELECT detail, created_at FROM events WHERE run_id=? AND kind='task_note' "
                                    "AND created_at > ? ORDER BY id", (task_id, since)):
        try:
            d = json.loads(r["detail"])
        except ValueError:
            continue
        out.append({"by": d.get("by", ""), "text": d.get("text", ""), "at": r["created_at"]})
    return out


def add_note(store, task_id: str, data, actor=None) -> dict:
    """A person adds context to a running task: it lands on the tree for
    the agent (bridge_get_tree notes, bridge_wait returns it) and in the
    task's trace. Any person may; an agent may not."""
    from . import authz
    _task(store, task_id)
    text = field(data, "text", limit=4000)
    if actor is not None:
        authz.check(store.graph, actor, {"owner_id": "", "repo": ""}, "note")
    by = (actor.name if actor is not None and actor.id else field(data, "by", limit=100))[:100]
    with store.graph.transaction():
        store.graph.append_event("task_note", {"task_id": task_id, "by": by, "text": text})
    return {"task_id": task_id, "notes": task_notes(store, task_id)}


def trace(store, task_id: str) -> dict:
    """Everything that happened on a task, in order: the events (kickoff,
    nodes, resolutions, routes, answers, signatures, corrections, notes),
    the notifications that went out and how they fared, and every node's
    current standing. The audit trail of the decisions inside one task."""
    run = _task(store, task_id)
    graph = store.graph
    events = []
    for r in graph.db.execute("SELECT id, kind, decision_id, detail, created_at FROM events WHERE run_id=? ORDER BY id",
                              (task_id,)):
        try:
            detail = json.loads(r["detail"]) if (r["detail"] or "").startswith("{") else r["detail"]
        except ValueError:
            detail = r["detail"]
        events.append({"id": r["id"], "kind": r["kind"], "decision_id": r["decision_id"] or "", "detail": detail,
                       "at": r["created_at"]})
    notifications = [dict(r) for r in graph.db.execute(
        "SELECT id, decision_id, kind, channel, person_name, state, attempts, last_error, created_at, sent_at "
        "FROM notifications WHERE run_id=? ORDER BY created_at", (task_id,))]
    nodes = [{k: n[k] for k in ("node_id", "question", "status", "kind", "authorized", "blocking", "signoff", "signed_by",
                                 "answered_by", "required_signers", "signatures", "needs_review", "reusable")}
             for n in _flatten(get_tree(store, task_id)["nodes"])]
    from . import context_memory as cm
    return {"source_anchors": cm.anchors(graph.db, task_id), "task_id": task_id, "title": run["title"], "status": run["status"], "events": events,
            "notifications": notifications, "nodes": nodes, "notes": task_notes(store, task_id)}


CHECKS_KEPT = 2000


def _submitted_diff(data) -> str:
    """Validate transport integrity without repairing or normalizing a patch.

    Legacy advisory fragments remain accepted. Recognize actual patch
    headers narrowly, not every fragment starting with '+' or 'diff'. A
    source file without a final newline has a marker in a generated patch;
    the patch itself still ends in LF.
    """
    import hashlib

    _text(data, "diff", 60000)  # validate type/size, but do not use its stripped value
    diff = data.get("diff") or ""
    try:
        raw = diff.encode("utf-8")
    except UnicodeEncodeError:
        raise Invalid("diff must be valid UTF-8 text; reread the original patch without changing its bytes") from None
    if "diff_sha256" in data:
        expected = data["diff_sha256"]
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
            raise Invalid("diff_sha256 must be exactly 64 hexadecimal characters")
        if not diff.strip():
            raise Invalid("diff_sha256 requires a nonempty diff")
        if hashlib.sha256(raw).hexdigest() != expected.lower():
            raise Invalid("diff_sha256 does not match the submitted UTF-8 diff. Reread the complete patch "
                          "and submit its exact bytes, including the final newline, with the SHA-256 computed "
                          "from the original patch bytes. Nothing was finished and the diff was not read.")
    unified = re.search(r"(?m)^--- [^\n]+\n\+\+\+ [^\n]+\n@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", diff)
    git_patch = re.search(r"(?m)^diff --git [^\n]+\n(?:index [^\n]+|(?:old|new|new file|deleted file) "
                          r"mode \d+|similarity index \d+%|GIT binary patch)(?:\n|$)", diff)
    if (unified or git_patch) and not diff.endswith("\n"):
        raise Invalid("diff is missing its final newline; the submitted patch may have been truncated. "
                      "Reread the complete patch and preserve its exact bytes, including the final LF; "
                      "do not trim or reconstruct it. A source file without a final newline is represented "
                      "by the patch's 'No newline at end of file' marker. Nothing was finished and the diff "
                      "was not read.")
    return diff


def finish_task(store, data, sleep=None, stop=None) -> dict:
    """Close the task. The gate this passes is that every decision on the
    canvas was authorized by a person; it is not a statement that the
    change does what they authorized, and the answer says so. `checks`
    is what the agent ran (tests, build, lint) and is recorded verbatim
    as a claim, not as a result Raven verified. `sleep` is a caller's
    progress sleep: given one, the finish waits for the whole reading of
    the diff while it sends progress; without one it answers within
    FINISH_WAIT seconds and a reading still running is kept for later."""
    task_id = field(data, "task_id", limit=100)
    status = _text(data, "status", 20) or "completed"
    if status == 'abandoned':
        return abandon_task(store, task_id, _text(data, 'reason', 2000))
    if _task(store, task_id)['status'] == 'abandoned':
        raise Invalid('This task was abandoned; start a new one')
    if _json_dict(_task(store, task_id)['discovery']).get('model_pending'):
        return {'task_id': task_id, 'status': 'working', 'finished': False, 'model_pending': True,
                'next': 'Raven is still reading the task. Call bridge_wait, then check the tree before finishing.'}
    # What the agent ran is kept to its first CHECKS_KEPT characters and
    # the cut is said, rather than the finish refused. Measured live: a
    # host's first finish was rejected for a long test log and it had to
    # shorten its own claim and try again.
    checks = _text(data, "checks", 60000)
    if len(checks) > CHECKS_KEPT:
        checks = (checks[:CHECKS_KEPT].rstrip() + f" … [cut by Raven: {len(checks)} characters given, "
                  f"the first {CHECKS_KEPT} kept]")
    diff = _submitted_diff(data)  # reject damaged submissions before any finish/review/proof write
    # Files the diff changes whose decider was asked nothing on this task:
    # put to them, or said why not, before the task finishes. Measured live
    # on 63eb671: the finish named the changelog fragment the agent had
    # numbered itself, and the agent could no longer write the decision,
    # because the task was already finished.
    run = _task(store, task_id)
    uncovered = _uncovered(store, task_id, diff)
    discovery = _json_dict(run["discovery"] if "discovery" in run.keys() else "")
    if discovery.get("narrow_edit"):
        # A narrow edit settles nothing by what it is; the decider of the
        # file it touches is not asked about a typo.
        uncovered = []
    reasons = _justifications(_text(data, "uncovered", 4000), uncovered)
    open_ = [u for u in uncovered if u["path"] not in reasons]
    if open_ and status == "completed" and (run["verdict"] or "") in ("engage", "unplaced"):
        shown = "; ".join(f"{u['path']} ({u['decider']} decides {u['scope']})" for u in open_[:6])
        raise Invalid(
            f"Refused: the diff changes {len(open_)} file{'s' if len(open_) != 1 else ''} whose decider was asked "
            f"nothing on this task: {shown}"
            + (f" and {len(open_) - 6} more" if len(open_) > 6 else "")
            + ". For each one: if the change there settles something (a name, a number, a default, what users are "
              "told), write it as a node with bridge_add_node (paths=<the file>) so its decider answers, then "
              "finish; if it settles nothing, call bridge_finish_task again with uncovered set to one line per "
              "file, `<path>: why the change there settles nothing`. Nothing was finished and the diff was not read.")
    for u in uncovered:
        if u["path"] in reasons:
            u["reason"] = reasons[u["path"]]
    if reasons:
        store.graph.append_event("uncovered_justified", {"task_id": task_id, "reasons": reasons})
    result = store.update_run(task_id, {"status": status})
    tree = get_tree(store, task_id)
    nodes = _flatten(tree["nodes"])
    signed = [n for n in nodes if n.get("authorized")]
    if checks:
        store.graph.append_event("checks_reported", {"task_id": task_id, "checks": checks})
    review = _review(store, task_id, signed, diff, sleep, stop)
    follows = list(review.get("follows") or [])
    departed = [f for f in follows if f["verdict"] == "departs"]
    unclear = [f for f in follows if f["verdict"] == "unclear"]
    verified = ("what the agent says it ran: " + checks if checks
                else "the agent reported no checks; nothing here says the change was run at all")
    # The decisions it has no reading for are named: measured live, one
    # of six went unread, and the agent's summary said all six followed.
    read_ids = {f["node_id"] for f in follows}
    unread = [{"node_id": n["node_id"], "question": n["question"]} for n in signed
              if (n.get("answer") or "").strip() and n["node_id"] not in read_ids]
    if follows:
        # A model's reading, and it says so in the words a host repeats.
        # Measured live on eb9d22d: "6 are in it" became the host's "all six
        # followed", and an independent test found an expired budget that
        # still let a retry through.
        read = (f"A model read the diff you supplied against {len(follows)} of them: "
                f"{len(follows) - len(departed) - len(unclear)} read as following, with a line of the diff "
                f"behind each thing they require, {len(departed)} depart from what was signed, {len(unclear)} it "
                "could not tell from the diff alone")
        countered = [f["node_id"] for f in follows
                     if any((r.get("counterexample") or {}).get("located") for r in f.get("requirements") or [])]
        if countered:
            read += (f". It found a possible counterexample for {len(countered)} ({', '.join(countered)}): a path "
                     "through the change that does what the decision rules out. Check each one before you report "
                     "the change as following it")
        blind = [f["node_id"] for f in follows if f.get("unexamined")]
        if blind:
            read += (f". {len(blind)} of the readings depend on code the diff does not show ({', '.join(blind)}); "
                     "each lists it under unexamined")
        read += (". That is a model reading the diff, not a test run and not a check of the repository: it can "
                 "miss a path, it covers what was signed and nothing else in the change, and it authorizes nothing")
        if unread:
            read += (f". It has no reading for {len(unread)} ({', '.join(u['node_id'] for u in unread)}) and does not "
                     f"know whether the change follows {'them' if len(unread) > 1 else 'it'}; do not report "
                     f"{'them' if len(unread) > 1 else 'it'} as followed")
    elif review["status"] == "running":
        read = (f"A model is still reading the diff you supplied against {len(signed)} of them (review "
                f"{review['id']}); it takes longer than this call waits. The reading is kept when it is done: read "
                "it on bridge_get_tree (review), or call bridge_finish_task again with the same diff, and do not "
                "report the change as following what was signed until you have read it")
    elif review["status"] == "failed":
        read = ("Raven could not finish reading the diff you supplied (see the server log). It does not know "
                "whether the change follows them; call bridge_finish_task again with the same diff to read it again")
    elif diff.strip() and signed:
        # Asking for the diff the agent just passed would be wrong: what is
        # missing is a model to read it with.
        read = ("Raven did not read the diff you supplied: reading it takes a model, and none answered (set "
                "ANTHROPIC_API_KEY on the Raven server). It does not know whether the change follows them")
    elif diff.strip():
        # Measured live on 63eb671: the typo task passed its diff, signed
        # nothing, and was told to pass its diff.
        read = "No decision on this task was signed, so there was nothing to read the diff you supplied against"
    else:
        read = ("Raven did not read the diff and does not know whether it follows them; pass `diff` to "
                "bridge_finish_task and it will say what it can")
    if follows and review["status"] == "failed":
        read += (". The reading did not complete for every attempted decision; call bridge_finish_task again "
                 "with the same diff to retry the incomplete reading")
    # Files the diff changes whose decider was asked nothing on this task,
    # and the reason the agent gave for each it said settles nothing.
    unasked = [u for u in uncovered if not u.get("reason")]
    if unasked:
        named = "; ".join(f"{u['path']} ({u['decider']} decides {u['scope']})" for u in unasked[:5])
        read += (f". The diff changes {len(unasked)} file{'s' if len(unasked) != 1 else ''} whose decider was "
                 f"asked nothing on this task: {named}"
                 + (f" and {len(unasked) - 5} more" if len(unasked) > 5 else "")
                 + "; if the change there settles something, it was not put to them")
    said = [u for u in uncovered if u.get("reason")]
    if said:
        read += ". The agent says these changes settle nothing, so their deciders were not asked: " + "; ".join(
            f"{u['path']} ({u['decider']}): {_clip(u['reason'], 200)}" for u in said[:5])
    # A decision a standing rule covered was not signed on this task, and
    # the finish says so. Measured on the follow-up run: listed only with
    # its rule's maker as signer, the agent reported it as signed.
    ruled = [n for n in signed if n.get("signoff") == "rule"]
    proof = None
    if status == "completed" and diff.strip():
        from .proof import create as create_proof
        proof = create_proof(store, task_id, diff, checks=checks)
    # Starting or retrying the reader changes the next step. A person can
    # also correct an answer during the wait. Refresh only the guidance:
    # the submitted reading and saved proof keep their own snapshots, and
    # an internal tree read is not an agent acknowledgment of new answers.
    guidance = get_tree(store, task_id)
    guidance_review = guidance.get("review")
    return {**result, "counts": guidance["counts"], "next": guidance["next"],
            "guidance_snapshot": {
                "observed_at": guidance["observed_at"], "task_status": guidance["status"],
                "needs_review": guidance["needs_review"],
                "review": ({k: guidance_review[k] for k in ("id", "status", "read_status") if k in guidance_review}
                           if guidance_review else None),
                "description": "next and counts come from this later tree read. The finish result (status, "
                               "authorized, review and follows) describes the earlier signed-answer snapshot; "
                               "this summary does not acknowledge newer human answers. Read bridge_get_tree "
                               "to see them."},
            **({"proof": {"id": proof["id"], "diff_sha256": proof["payload"]["change"]["sha256"],
                          "export_tool": "bridge_export_proof"}} if proof else {}),
            "authorized": [{"node_id": n["node_id"], "question": n["question"],
                            "signed_by": n.get("signed_by") or n.get("answered_by") or "",
                            **({"by_rule": True} if n.get("signoff") == "rule" else {})} for n in signed],
            "checks": checks,
            "follows": follows,
            "unread": unread if follows else [],
            "uncovered": uncovered,
            **({"review": {k: review[k] for k in ("id", "status", "seconds") if review.get(k) is not None}}
               if review.get("id") else {}),
            "verified": False,
            "caveat": (f"{len(signed)} decision{'s' if len(signed) != 1 else ''} on this task "
                       f"{'were' if len(signed) != 1 else 'was'} authorized by a person"
                       + (f" ({len(ruled)} by a reusable rule made earlier, not signed on this task)" if ruled else "")
                       + f". {read}: {verified}. "
                       "Authorized is not verified; code review is still the gate on shipping.")}


def _conformance(store, task_id: str, signed: list[dict], diff: str) -> list[dict]:
    """For each authorized decision, whether the diff does what it says.

    A report and never a gate: the finish is not refused on a departure,
    because Raven gates authorization and has no standing to judge a
    change. What it is for is that a departure stops being invisible.
    Measured on Grafana, an owner corrected an answer to three states,
    the agent said its change matched, and the diff had one of them."""
    from .config import load as load_config
    from .llm import check_conformance
    cfg = load_config()
    if not diff.strip() or not signed:
        return []
    from concurrent.futures import ThreadPoolExecutor
    from .llm import focus_diff
    todo = [n for n in signed[:8] if (n.get("answer") or "").strip()]
    # Each decision reads the files it is about first.
    scoped = {}
    for n in todo:
        row = store.graph.db.execute("SELECT path, scope_paths FROM decisions WHERE id=?", (n["node_id"],)).fetchone()
        paths = [row["path"] or ""] + [str(p) for p in _json_list(row["scope_paths"])] if row is not None else []
        scoped[n["node_id"]] = focus_diff(diff, paths)

    def read_one(n):
        others = tuple((m.get("question") or "", m.get("answer") or "") for m in signed if m is not n)
        return check_conformance(cfg, n.get("question") or "", (n.get("answer") or "").strip(), scoped[n["node_id"]],
                                 others)
    # One read per decision, all at once: they share nothing but the diff.
    # Measured live: six decisions read one after another took about 44
    # seconds at the finish, and on 5e967e4 one reading took 208 seconds.
    with ThreadPoolExecutor(max_workers=min(8, len(todo) or 1)) as pool:
        reads = list(pool.map(read_one, todo))
    out = []
    for n, read in zip(todo, reads):
        if not read:
            continue
        # The requirements the verdict was worked out from, so a reviewer
        # has the evidence and not only its summary.
        out.append({"node_id": n["node_id"], "question": n.get("question") or "",
                    "verdict": read["verdict"], "why": read["why"],
                    "requirements": list(read.get("requirements") or []),
                    **({"incomplete": True} if read.get("incomplete") else {}),
                    **({"unexamined": list(read["unexamined"])} if read.get("unexamined") else {})})
    return out


def _justifications(text: str, uncovered: list[dict]) -> dict:
    """The agent's reason for each uncovered file it says settles nothing:
    one line per file naming it, or one line for the only one. A reason
    has to say something: a bare "ok" is not one."""
    lines = [ln.strip(" -*\t") for ln in (text or "").splitlines() if ln.strip()]
    out: dict = {}
    named = False
    for u in uncovered:
        path, base = u["path"], u["path"].rsplit("/", 1)[-1]
        # The line naming the whole path, else the one naming the file.
        line = next((ln for ln in lines if path in ln), None) or next(
            (ln for ln in lines if re.search(r"(?<![\w./-])" + re.escape(base) + r"(?![\w-])", ln)), None)
        if line is None:
            continue
        named = True
        head, sep, tail = line.partition(":")
        reason = tail.strip() if sep and (path in head or base in head) else line
        if len(reason) >= 12:
            out[path] = reason
    if not named and len(uncovered) == 1 and len(" ".join(lines)) >= 12:
        out[uncovered[0]["path"]] = " ".join(lines)
    return out


def _uncovered(store, task_id: str, diff: str) -> list[dict]:
    """The files a diff changes that the authority map says somebody
    decides, where no decision on this task went to that person or was
    about that area. Tests are left out. Measured live on 5e967e4: the
    agent wrote a user-guide section and picked a changelog fragment name,
    the docs decider was never asked, and nothing at the finish said so:
    a reading of the signed decisions cannot see a decision never made."""
    from .graph import _pattern_covers
    from .llm import _TEST_PATH
    from .signals import _specificity
    changed = list(dict.fromkeys(re.findall(r"(?m)^diff --git a/\S+ b/(\S+)", diff or "")
                                 or re.findall(r"(?m)^\+\+\+ b/(\S+)", diff or "")))
    changed = [p for p in changed if not _TEST_PATH.search(p)]
    if not changed:
        return []
    graph = store.graph
    run = _task(store, task_id)
    repo = graph.resolve_repo(repo_key(run["repo"]))
    rows = [r for r in graph.authority_rows(repo)
            if r["scope_kind"] == "path" and r["role"] == "decides" and r["accepted"] and (r["person"] or r["team"])]
    if not rows:
        return []
    nodes = graph.db.execute("SELECT d.path, d.scope_paths, o.name AS owner_name FROM decisions d "
                             "LEFT JOIN owners o ON o.id = d.owner_id WHERE d.run_id=? AND d.draft=0 "
                             "AND d.status NOT IN ('suggested', 'duplicate')", (task_id,)).fetchall()
    owners = {(n["owner_name"] or "").strip().lower() for n in nodes}
    about = [p for n in nodes for p in [n["path"] or "", *[str(x) for x in _json_list(n["scope_paths"])]]
             if p and p != "unknown"]
    out = []
    for path in changed:
        matching = [r for r in rows if _pattern_covers(r["scope"], path)]
        if not matching:
            continue
        rule = max(matching, key=lambda r: _specificity(r["scope"]))
        if rule["person"]:
            names = [rule["person"]["name"]]
        else:
            team = rule["team"] or {}
            names = [p["name"] for p in (graph.get_person(m) for m in team.get("members", [])) if p] \
                or [team.get("name", "")]
        if any(n.strip().lower() in owners for n in names if n):
            continue
        if any(_pattern_covers(rule["scope"], p) for p in about):
            continue
        out.append({"path": path, "decider": ", ".join(n for n in names if n), "scope": rule["scope"]})
    return out


# How long a finish without a progress channel waits for the reading of
# the diff before it answers without it: under the 60 seconds a host gives
# a tool call. With progress, a streamed finish waits for the whole of it.
FINISH_WAIT = 45.0
FINISH_WAIT_STREAMED = 900.0
_REVIEWS: dict = {}
_REVIEWS_LOCK = __import__("threading").Lock()


def _revisions(signed: list[dict]) -> dict:
    """The signed answers a reading is against: each decision's answer, by
    its hash."""
    return {n["node_id"]: answer_hash(n.get("answer") or "") for n in signed}


def _review_inputs(signed: list[dict]) -> dict:
    """Question/answer content supplied to the reader, including its other
    signed decisions. Signers and timestamps do not change those inputs.
    Keep their order too: the reader caps decisions and signed context."""
    import hashlib
    return {n["node_id"]: hashlib.sha256(json.dumps(
        [position, n.get("question") or "", n.get("answer") or ""], ensure_ascii=False,
        separators=(",", ":")).encode()).hexdigest() for position, n in enumerate(signed)}


def _review_key(task_id: str, diff: str, signed: list[dict]) -> tuple[str, str]:
    """One reading per task, diff and ordered signed question/answer inputs."""
    import hashlib
    diff_hash = hashlib.sha256(diff.encode()).hexdigest()[:16]
    inputs = json.dumps(_review_inputs(signed), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256("\n".join([task_id, diff_hash, inputs]).encode()).hexdigest()[:12], diff_hash


def _stored_review(graph, task_id: str, review_id: str | None = None) -> dict | None:
    """The latest reading kept for a task (or the one with this id): done
    or failed with what it found, or running."""
    for r in graph.db.execute("SELECT kind, detail, created_at FROM events WHERE run_id=? AND kind IN "
                              "('conformance_started', 'conformance_read') ORDER BY id DESC", (task_id,)):
        try:
            d = json.loads(r["detail"] or "{}")
        except ValueError:
            continue
        if not d.get("review_id") or (review_id is not None and d["review_id"] != review_id):
            continue
        read = {k: d[k] for k in ("revisions", "review_inputs") if isinstance(d.get(k), dict)}
        if r["kind"] == "conformance_read":
            return {"id": d["review_id"], "status": d.get("status") or "done", "follows": d.get("follows") or [],
                    "diff_hash": d.get("diff_hash") or "", "seconds": d.get("seconds"), "finished_at": r["created_at"],
                    **read}
        return {"id": d["review_id"], "status": "running", "follows": [], "diff_hash": d.get("diff_hash") or "",
                "started_at": r["created_at"], **read}
    return None


def _current_review(graph, task_id: str, nodes: list[dict]) -> dict | None:
    """The latest kept reading, as it stands against the tree now. A
    reading is about the signed questions and answers it read: when one
    changes, is no longer signed, or a decision was signed after it,
    the reading is stale and says which. Measured live on 63eb671: an
    owner reversed a signed answer after the task finished, re-signed it,
    and the tree went on showing the old reading as done and followed."""
    review = _stored_review(graph, task_id)
    if review is None:
        return None
    by_id = {n["node_id"]: n for n in nodes}
    signed = {n["node_id"]: n for n in nodes if n.get("authorized") and (n.get("answer") or "").strip()}
    stale: list[dict] = []

    def add(nid: str, why: str) -> None:
        n = by_id.get(nid) or {}
        stale.append({"node_id": nid, "question": n.get("question") or "", "why": why})
    read = review.get("revisions")
    if read is None:
        # Kept before readings recorded what they read: whatever a person
        # answered, corrected or signed after it.
        since = review.get("finished_at") or review.get("started_at") or ""
        touched = {r["decision_id"] for r in graph.db.execute(
            "SELECT DISTINCT decision_id FROM events WHERE run_id=? AND created_at > ? AND kind IN "
            "('owner_approved', 'answer_corrected', 'signoff', 'signature')", (task_id, since))}
        read = {f["node_id"]: None for f in review.get("follows") or []}
        for nid in read:
            if nid in touched:
                add(nid, "a person answered, corrected or signed it after this reading")
    else:
        for nid, revision in read.items():
            n = by_id.get(nid)
            if n is None:
                continue
            if nid not in signed:
                add(nid, "it is no longer signed: it was reopened, corrected or put in doubt after this reading")
            elif answer_hash(n.get("answer") or "") != revision:
                add(nid, "its signed answer changed after this reading")
    for nid in signed:
        if nid not in read:
            add(nid, "it was signed after this reading, which does not cover it")
    inputs = review.get("review_inputs")
    current_inputs = _review_inputs(list(signed.values()))
    changed = {x["node_id"] for x in stale}
    if inputs is None:
        # An answer-only cache cannot establish which question/context it
        # read. Preserve the old record, but require an explicit new finish.
        for nid in read:
            if nid not in changed:
                add(nid, "this older reading did not record its question and signed decision context")
    else:
        for nid in dict.fromkeys([*inputs, *current_inputs]):
            if nid not in changed and inputs.get(nid) != current_inputs.get(nid):
                add(nid, "its question or signed decision context changed after this reading")
    if stale:
        # Every reading receives the other signed questions and answers as
        # context, so a changed secondary decision also stales its findings.
        review = {**review, "read_status": review["status"], "status": "stale", "stale": stale,
                  "follows": [{**f, "stale": True} for f in review.get("follows") or []]}
    return review


def _review(store, task_id: str, signed: list[dict], diff: str, sleep=None, stop=None) -> dict:
    """The reading of the diff against what was signed: run once per task,
    diff and ordered signed question/answer inputs, and kept. A retried finish,
    or a host that timed out before the answer came, reads the kept one
    instead of starting it again. Measured live on 5e967e4: three finish
    calls timed out at 60 seconds while the reading ran, the hosts never
    saw the departure it found, and a retry would have read it all again."""
    import threading
    import time as _time
    if not diff.strip() or not signed:
        return {"status": "none", "follows": []}
    graph = store.graph
    rid, diff_hash = _review_key(task_id, diff, signed)
    with _REVIEWS_LOCK:
        stored = _stored_review(graph, task_id, rid)
        if stored is not None and stored["status"] == "done":
            return stored
        # A failed reading is evidence of that attempt, not a permanent
        # answer. The finish response tells the host to retry the same diff;
        # that explicit call starts one new attempt while preserving history.
        # Reads alone never retry, and concurrent finish calls share the thread.
        thread = _REVIEWS.get(rid)
        if thread is None:
            # Never started, or started by a process that has since gone.
            graph.append_event("conformance_started", {"task_id": task_id, "review_id": rid, "diff_hash": diff_hash,
                                                       "decisions": [n["node_id"] for n in signed],
                                                       "revisions": _revisions(signed),
                                                       "review_inputs": _review_inputs(signed)})
            thread = threading.Thread(target=_run_review, args=(store, task_id, rid, diff_hash, signed, diff),
                                      daemon=True, name=f"bridge-review-{rid}")
            _REVIEWS[rid] = thread
            thread.start()
    budget = min(FINISH_WAIT_STREAMED if sleep is not None else FINISH_WAIT, max(0.0, call_budget() - 5))
    started = _time.monotonic()
    while thread.is_alive() and _time.monotonic() - started < budget and not (stop is not None and stop.is_set()):
        if sleep is not None:
            try:
                sleep(1.0)
            except (BrokenPipeError, ConnectionResetError):
                # Losing the caller's progress stream ends only its wait.
                # Finish has already recorded completion; let it save the
                # exact submitted proof while the advisory reader continues.
                break
        else:
            thread.join(timeout=min(1.0, max(0.0, budget - (_time.monotonic() - started))))
    stored = _stored_review(graph, task_id, rid)
    if stored is not None and stored["status"] in ("done", "failed"):
        return stored
    return {"id": rid, "status": "running", "follows": [], "diff_hash": diff_hash}


def _run_review(store, task_id: str, rid: str, diff_hash: str, signed: list[dict], diff: str) -> None:
    import sys
    import time as _time
    started = _time.monotonic()
    status, follows = "done", []
    try:
        follows = _conformance(store, task_id, signed, diff)
        # The provider adapter returns no reading on an unavailable or
        # malformed response. With inference enabled that is an incomplete
        # attempt, not a successful empty result to cache forever. Preserve
        # any completed findings; an explicit finish retry can recover.
        from .config import load as load_config
        expected = {n["node_id"] for n in signed[:8] if (n.get("answer") or "").strip()}
        if load_config().semantic_retrieval and (expected - {r["node_id"] for r in follows}
                                                 or any(r.get('incomplete') for r in follows)):
            status = "failed"
            print(f"Raven: reading the diff for task {task_id} returned incomplete model results", file=sys.stderr)
    except Exception as error:
        status = "failed"
        print(f"Raven: reading the diff for task {task_id} failed: {type(error).__name__}: {error}", file=sys.stderr)
    try:
        store.graph.append_event("conformance_read", {
            "task_id": task_id, "review_id": rid, "diff_hash": diff_hash, "status": status,
            "revisions": _revisions(signed), "review_inputs": _review_inputs(signed),
            "seconds": round(_time.monotonic() - started, 1), "follows": follows,
            "read": [{"node": r["node_id"], "verdict": r["verdict"],
                      "counterexamples": sum(1 for q in r["requirements"]
                                             if (q.get("counterexample") or {}).get("located"))}
                     for r in follows]})
    finally:
        with _REVIEWS_LOCK:
            _REVIEWS.pop(rid, None)
        store.graph.close_thread()


# ---------------- waiting for people ----------------

WAIT_DEFAULT = 300.0
WAIT_CAP = 1800.0


def _flatten(nodes: list[dict]) -> list[dict]:
    out: list[dict] = []
    for n in nodes:
        out.append(n)
        out.extend(_flatten(n.get("children") or []))
    return out


def _watch_key(n: dict) -> tuple:
    """What a waiting agent cares about on one node: anything here
    changing means a person moved the decision (or the tree around it)."""
    res = n.get("resolution") or {}
    return (n["status"], n.get("signoff") or "", bool(n.get("needs_review")), bool(n.get("authorized")),
            bool(n.get("blocking")), n.get("answer") or "", n.get("answered_by") or "", n.get("owner") or "",
            n.get("signed_by") or "", bool(n.get("model_pending")), res.get("status") or "", bool(n.get("parent_changed_after")))


def _seconds(raw, default: float, cap: float) -> tuple[float, bool]:
    if raw in (None, ""):
        return default if default <= cap else cap, False
    try:
        value = float(str(raw).strip())
    except ValueError:
        raise Invalid("timeout must be a number of seconds")
    if not math.isfinite(value):
        raise Invalid("timeout must be a finite number of seconds")
    if value < 0:
        raise Invalid("timeout must not be negative")
    return (cap, True) if value > cap else (value, False)


# What a wait says when the server stops under it. Measured live on
# eb9d22d: Raven was restarted during a streamed wait, and the host heard
# nothing until its own idle limit cut the call 329 seconds later.
STOPPED_NEXT = ("Raven stopped while you waited, most likely a restart: nothing is lost (answers and signatures are "
                "stored). Call bridge_wait again in a few seconds with the same since, or read bridge_get_tree")


def wait(store, data, cap: float = WAIT_CAP, interval: float = 1.0, sleep=None, clock=None, stop=None) -> dict:
    """Block until a person moves the task, or the timeout: an answer, a
    signature, a correction, a hand-on, a follow-up question added, a
    node put in doubt. Returns what changed with what to do about each
    node; on timeout, which nodes still wait on whom. With node_id, only
    that node (and follow-ups under it) is watched. Nothing is lost by
    not waiting: an answer that lands meanwhile is on the tree when the
    agent reads it. Waiting reads the same rows the inbox writes, so it
    works across processes and survives a restart of either side.
    `stop` (a threading.Event) ends the wait early when the server is
    stopping, and the result says so rather than going quiet."""
    import time as _time
    sleep = sleep or _time.sleep
    clock = clock or _time.monotonic
    task_id = field(data, "task_id", limit=100)
    node_id = _text(data, "node_id", 100)
    since = _text(data, "since", 40)
    timeout, capped = _seconds(data.get("timeout"), WAIT_DEFAULT, cap)
    tree = get_tree(store, task_id)
    before = {n["node_id"]: n for n in _flatten(tree["nodes"])}
    if node_id and node_id not in before:
        raise Invalid(f"node_id {node_id!r} is not on task {task_id}")
    watched = [node_id] if node_id else [nid for nid, n in before.items() if n["blocking"]]
    result = {"task_id": task_id, "timed_out": False, "waited_seconds": 0.0, "changed": [], "waiting_on": [],
              "counts": tree["counts"], "timeout_applied": timeout, "observed_at": tree["observed_at"],
              "observed_revision": tree.get('observed_revision', {}),
              **({"review": tree["review"]} if tree.get("review") is not None else {})}
    if tree.get('scope_clarifications'):
        return {**result, 'scope_clarifications': tree['scope_clarifications'], 'next': tree['next']}
    if capped:
        result["notice"] = f"timeout capped at {cap:g} seconds on this connection; call again to keep waiting"
    changed: list[dict] = []
    result["notes"] = []
    if since:
        # What people did since the agent last looked (the observed_at of
        # its last bridge_get_tree or bridge_wait) is reported at once: an
        # answer that landed while the agent was busy is not waited for.
        # People leave events; the agent's own writes are not changes.
        marks = ",".join("?" for _ in PEOPLE_EVENTS)
        touched = {r["decision_id"] for r in graph_events(store, task_id, since, marks)}
        for nid, n in before.items():
            if nid not in touched or (node_id and nid != node_id and n.get("parent_id") != node_id):
                continue
            changed.append({**_change(store, n), "from": "", "new": n["created_at"] > since})
        result["notes"] = task_notes(store, task_id, since)
    # Required follow-ups are work for the agent, not unanswered requests
    # for a person. Return them even when the host omitted a since cursor.
    actionable = [n for n in before.values() if n["status"] == "suggested" and n.get("followup_required")
                  and (not node_id or n["node_id"] == node_id or n.get("parent_id") == node_id)]
    seen = {n["node_id"] for n in changed}
    changed.extend({**_change(store, n), "from": "", "new": False} for n in actionable if n["node_id"] not in seen)
    # A node-specific wait without a cursor asks for its answer. If that
    # answer landed before the call, return it rather than awaiting a
    # second human action that nobody is expected to take.
    if node_id and not since and not before[node_id]["blocking"] and node_id not in {n["node_id"] for n in changed}:
        changed.append({**_change(store, before[node_id]), "from": "", "new": False})
    notes_before = len(tree.get("notes") or [])
    review_pending = not node_id and (tree.get('review') or {}).get('status') == 'running'
    reading = (bool(_json_dict(_task(store, task_id)['discovery']).get('model_pending'))
               or any(n.get('model_pending') for n in before.values()) or review_pending)
    verdict_before = tree.get('verdict')
    if changed or result["notes"] or (not watched and not reading):
        result["changed"] = changed
        result["waiting_on"] = [{"node_id": n["node_id"], "question": n["question"], "status": n["status"],
                                 "owner": n["owner"]} for n in before.values() if n["blocking"]]
        if changed or result["notes"]:
            result["next"] = "read the changes above and act on each node's next; " + tree["next"]
        else:
            result["next"] = ("nothing on this task waits on a person; " + tree["next"]) if before else tree["next"]
        return result
    start = clock()
    while True:
        elapsed = clock() - start
        if elapsed >= timeout:
            result["timed_out"] = True
            break
        if stop is not None and stop.is_set():
            result["interrupted"] = "the Raven server is stopping"
            break
        sleep(max(0.0, min(interval, timeout - elapsed)))
        tree = get_tree(store, task_id)
        after = {n["node_id"]: n for n in _flatten(tree["nodes"])}
        reading_now = (bool(_json_dict(_task(store, task_id)['discovery']).get('model_pending'))
                       or any(n.get('model_pending') for n in after.values())
                       or (not node_id and (tree.get('review') or {}).get('status') == 'running'))
        if tree.get('verdict') != verdict_before or (reading and not reading_now):
            result['task'] = _task_as_started(store, _task(store, task_id))
        for nid, n in after.items():
            old = before.get(nid)
            if node_id and nid != node_id and n.get("parent_id") != node_id:
                continue
            if old is None:
                changed.append({**_change(store, n), "from": "", "new": True})
            elif _watch_key(old) != _watch_key(n):
                changed.append({**_change(store, n), "from": old["status"]})
        if len(tree.get("notes") or []) > notes_before:
            result["notes"] = (tree.get("notes") or [])[notes_before:]
        if changed or result["notes"] or result.get("task"):
            break
    result["waited_seconds"] = round(clock() - start, 2)
    result["changed"] = changed
    result["counts"] = tree["counts"]
    result["observed_at"] = tree["observed_at"]
    result['observed_revision'] = tree.get('observed_revision', {})
    if tree.get("review") is not None:
        result["review"] = tree["review"]
    still = [n for n in _flatten(tree["nodes"]) if n["blocking"] and (not node_id or n["node_id"] == node_id)]
    result["waiting_on"] = [{"node_id": n["node_id"], "question": n["question"], "status": n["status"],
                             "owner": n["owner"]} for n in still]
    if changed or result["notes"]:
        result["next"] = "read the changes above and act on each node's next; " + tree["next"]
    elif result.get("interrupted"):
        result["next"] = STOPPED_NEXT
    elif review_pending and (tree.get('review') or {}).get('status') != 'running':
        result["next"] = ("The advisory review finished. Read review, then call bridge_finish_task again with "
                          "the same complete diff and checks to refresh its saved proof. " + tree["next"])
    elif (tree.get('review') or {}).get('status') == 'running' or result.get('task'):
        result["next"] = tree["next"]
    else:
        who = sorted({n["owner"] for n in still if n["owner"]})
        result["next"] = (f"nothing changed in {result['waited_seconds']:g}s; {len(still)} node"
                          f"{'s' if len(still) != 1 else ''} still wait{'s' if len(still) == 1 else ''} on "
                          f"{', '.join(who) if who else 'nobody yet (unrouted: assign in the inbox or settle yourself)'}"
                          "; continue independent work, then wait again or read bridge_get_tree; an answer that "
                          "lands meanwhile is on the tree when you read it")
    return result


# The events people leave on a node; everything else on the log is the
# agent's own writes or Raven's bookkeeping.
PEOPLE_EVENTS = ("owner_approved", "answer_corrected", "signoff", "signature", "owner_changed", "prediction_withdrawn",
                 "dependent_flagged", "twin_closed", "followup_added", "reply_received", "rule_made", "rule_ended",
                 "question_reframed")


def human_revisions(store, task_id: str) -> dict:
    """Snapshot the append-only human-event log before reading decision rows.

    IDs alone are not a commit watermark on PostgreSQL: a transaction with a
    smaller allocated ID can commit later, including for the same decision.
    Its arrival changes the count even when the greatest ID stays unchanged.
    """
    marks = ','.join('?' for _ in PEOPLE_EVENTS)
    return {r['decision_id']: {'max_event_id': int(r['max_id']), 'event_count': int(r['event_count'])}
            for r in store.graph.db.execute(
                'SELECT decision_id, max(id) AS max_id, count(*) AS event_count FROM events '
                f'WHERE run_id=? AND decision_id IS NOT NULL AND kind IN ({marks}) GROUP BY decision_id',
                (task_id, *PEOPLE_EVENTS))}


def _receipt_covers(seen, revision) -> bool:
    return isinstance(seen, dict) and all(type(seen.get(key)) is int and seen[key] >= revision[key]
                                         for key in ('max_event_id', 'event_count'))


def note_agent_read(store, task_id: str, observed_at: str, observed_revision: dict,
                    full_node_ids: set[str]) -> bool:
    """Acknowledge only a complete set of unread, fully returned decisions.

    A whole-task wait may return no answers, or only the new changes while an
    older unread sibling is omitted. Neither its timestamp nor a client's
    `since` cursor is evidence that those omitted answers were read.
    """
    if not task_id or not observed_at or not isinstance(observed_revision, dict):
        return False
    with store.graph.transaction():
        run = _task(store, task_id)
        receipts = _json_dict(run['agent_read_events'])
        unread = {did: revision for did, revision in observed_revision.items()
                  if not _receipt_covers(receipts.get(did), revision)}
        if set(unread) - full_node_ids:
            return False
        receipts.update(unread)
        store.graph.db.execute(
            'UPDATE runs SET agent_read_events=?, agent_read_at=CASE WHEN agent_read_at < ? '
            'THEN ? ELSE agent_read_at END WHERE id=?',
            (json.dumps(receipts, sort_keys=True), observed_at, observed_at, task_id))
    return True


def unread_by_agent(store, task_id: str) -> list[dict]:
    """The nodes a person acted on after the agent last read the tree: an
    answer, a signature, a correction or a hand-on it has not seen.
    Measured on a real task, the owner answered 37 seconds after the
    question and the agent went on building its own guess on both counts
    it had asked about, reading the tree fifteen edits later. It happened
    to read before finishing; nothing made it, and with no model to read
    the diff the finish would have said only that the decision was
    authorized."""
    row = store.graph.db.execute("SELECT agent_read_events FROM runs WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return []
    receipts = _json_dict(row['agent_read_events'])
    out = []
    for did, revision in human_revisions(store, task_id).items():
        if _receipt_covers(receipts.get(did), revision):
            continue
        n = node_view(store, did)
        who = n.get("signed_by") or n.get("answered_by") or n.get("owner") or "a person"
        out.append({"node_id": n["node_id"], "question": n["question"], "by": who, "status": n["status"]})
    return out


def require_agent_read(store, data) -> None:
    """The same observed-answer gate for every authenticated agent finish path."""
    task_id = data.get('task_id', '')
    # Preserve the existing pending-authorization refusal before the unread
    # check, and permit an accidental unanswered task to be abandoned.
    if data.get('status') != 'abandoned' and not store.graph.blocking_nodes(task_id):
        unread = unread_by_agent(store, task_id)
        if unread:
            raise Invalid(unread_refusal(unread))


def unread_refusal(unread: list[dict]) -> str:
    listed = "; ".join(f"{u['by']} on \"{_clip(u['question'], 90)}\" ({u['node_id']})" for u in unread[:4])
    more = f" and {len(unread) - 4} more" if len(unread) > 4 else ""
    return ("bridge_finish_task refused: people acted on this task after you last read it, and you have not seen "
            f"what they said: {listed}{more}. Read bridge_get_tree, make your change follow each answer, then "
            "call bridge_finish_task again")


def graph_events(store, task_id: str, since: str, marks: str):
    return store.graph.db.execute(
        f"SELECT DISTINCT decision_id FROM events WHERE run_id=? AND created_at > ? AND kind IN ({marks}) "
        "AND decision_id IS NOT NULL", (task_id, since, *PEOPLE_EVENTS)).fetchall()


def _change(store, n: dict) -> dict:
    # A wait returns changed nodes without the rest of the tree. Include the
    # full current parent context here, not just the tree's compact reference.
    parent_context = {}
    if n.get('parent_id'):
        parent = store.graph.db.execute(
            _DECISION_SELECT + ' WHERE d.id=? AND d.run_id=? AND d.draft=0',
            (n['parent_id'], n['task_id'])).fetchone()
        if parent is not None:
            parent_context = {'parent': _decision_context(store, parent)}
    return {"node_id": n["node_id"], "question": n["question"], "to": n["status"], "authorized": n["authorized"],
            "blocking": n["blocking"], "needs_review": n["needs_review"], "answer": n["answer"],
            "answered_by": n["answered_by"], "signed_by": n.get("signed_by") or "", "owner": n["owner"],
            "rationale": n.get("rationale") or "", "next": n["next"],
            **parent_context}


# ---------------- what people add ----------------

def add_followups(store, cfg, decision_id: str, data, actor=None) -> dict:
    from .store import check_not_abandoned
    with store.graph.transaction() as db:
        decision = _node(store, decision_id)
        check_not_abandoned(db, decision.task_id, {"status": decision.status})
        return _add_followups(store, cfg, decision_id, data, actor=actor)


def _add_followups(store, cfg, decision_id: str, data, actor=None) -> dict:
    """A person answering a node also writes the questions the answer
    raises: the right level n+1. They land as suggested children of the
    node, for the agent to adopt. Any person may; an agent may not."""
    from . import authz
    graph = store.graph
    d = _node(store, decision_id)
    authz.check(graph, actor, store.get_decision(d.id), "followup")
    if actor is not None and actor.id:
        data = {**data, "by": actor.name}
    raw = data.get("questions")
    if isinstance(raw, str):
        raw = [ln for ln in raw.splitlines()]
    if not isinstance(raw, list):
        raise Invalid("questions must be a list of questions or one per line")
    questions = [str(q).strip() for q in raw if str(q).strip()]
    if not questions:
        raise Invalid("At least one question is required")
    # Refused, not cut or dropped: each one reaches the agent as written.
    if len(questions) > 10:
        raise Invalid(f"At most 10 follow-up questions at a time, {len(questions)} given")
    if any(len(q) > 2000 for q in questions):
        raise Invalid("A follow-up question must be at most 2000 characters")
    # Required: the task cannot finish until the agent adopts each one and
    # it is answered. Optional (the default): the agent may leave it.
    # Measured live: an owner's "before release, which option ..." was
    # left unadopted and the task finished.
    required = str(data.get("required") or "").strip().lower() in ("1", "true", "yes", "on")
    by = _text(data, "by", 100) or d.answered_by or d.owner or "a person"
    created: list[dict] = []
    for q in questions:
        exists = graph.db.execute(
            "SELECT id, status FROM decisions WHERE run_id=? AND lower(question)=lower(?) LIMIT 1",
            (d.task_id, q)).fetchone()
        if exists:
            if required and exists["status"] == "suggested":
                graph.db.execute("UPDATE decisions SET followup_required=1 WHERE id=?", (exists["id"],))
            created.append(node_view(store, exists["id"], repeated=True))
            continue
        nid = graph.add_decision(d.task_id, q, d.category or "", "suggested", source="human",
                                 repo=d.repo, context=f"added by {by} after answering: {_clip(d.question, 200)}",
                                 path=d.path or "unknown", kind="followup")
        graph.update_decision(nid, parent_id=d.id, depth=d.depth + 1, origin="human")
        if required:
            graph.db.execute("UPDATE decisions SET followup_required=1 WHERE id=?", (nid,))
        graph.append_event("followup_added", {"task_id": d.task_id, "decision_id": nid, "parent_id": d.id, "by": by,
                                              "required": required})
        created.append(node_view(store, nid))
    return {"nodes": created}


def sign_off(store, decision_id: str, data, actor=None, transaction_db=None) -> dict:
    """A person signs a node Raven or the agent resolved without them,
    or corrects it: a correction is a signed answer, recorded like any
    other, and it settles the twins of the question. The signature is
    bound to the revision the person reviewed (`expected_updated_at`)
    and to the text it covers; a node that moved on since is refused.
    The actor must be the node's owner, a required signer, verified for
    its scope, or an admin overriding."""
    from . import authz
    from .store import answer_hash, check_not_abandoned, check_revision
    graph = store.graph
    d = _node(store, decision_id)
    by = (actor.name if actor is not None and actor.id else field(data, "by", limit=100))[:100]
    answer = _text(data, "answer")
    if d.status in ("duplicate", "suggested", "adopted", "withdrawn"):
        raise Invalid(f"a node that is {d.status} is not signed off; "
                      + ("its twin carries the answer" if d.status == "duplicate" else "the agent adopts it first"))
    basis = authz.check(graph, actor, store.get_decision(d.id), "correct" if answer else "sign")
    if transaction_db is not None and not transaction_db.in_transaction:
        raise Invalid("Internal sign-off transaction must already be open")
    from contextlib import nullcontext
    if answer:
        # Ownership, the new answer and every invalidation are one write.
        # A read-back caller keeps its outer transaction and consent record.
        with (nullcontext(transaction_db) if transaction_db is not None else graph.transaction()) as db:
            current = db.execute("SELECT * FROM decisions WHERE id=?", (d.id,)).fetchone()
            check_not_abandoned(db, current["run_id"], current)
            check_revision(data, current, db=db)
            owner_id = graph.owner_id_for(by)
            db.execute("UPDATE decisions SET owner_id=? WHERE id=?", (owner_id, d.id))
            store.answer(d.id, {"answer": answer, "rationale": _text(data, "rationale") or "corrected at sign-off",
                                "applicability": data.get("applicability") or {},
                                "evidence_mode": data.get('evidence_mode', 'retain'),
                                **({'source_evidence': data['source_evidence']} if 'source_evidence' in data else {}),
                                'source_decision_pins': data.get('source_decision_pins', []),
                                "source": data.get("source"), "signed_by": by,
                                "expected_updated_at": current["updated_at"]}, actor=actor, transaction_db=db)
        view = node_view(store, d.id)
        remaining = [r for r in view["required_signers"] if r.lower() not in {x.lower() for x in view["signatures"]}]
        if remaining:
            view["notice"] = f"Corrected and signed by {by}; still waiting on {', '.join(remaining)}"
        return view
    if d.status in ("pending",):
        raise Invalid("A pending node needs an answer, not a sign-off")
    # One compare-and-write: the revision is checked, the signatures that
    # cover this exact text are counted, and the result written, inside
    # one transaction, so two signers or a signer racing a correction
    # serialize and neither counts a signature for other text.
    stamp = now()
    with (nullcontext(transaction_db) if transaction_db is not None else graph.transaction()) as db:
        current = db.execute("SELECT * FROM decisions WHERE id=?",
                                   (d.id,)).fetchone()
        check_not_abandoned(db, current["run_id"], current)
        check_revision(data, current, db=db)
        from . import context_memory as cm
        replacement_pins = data.get('source_evidence')
        if replacement_pins == [] and cm.edges(db, d.id):
            raise Invalid('A sign-off cannot discard source reliance; write an explicit independent replacement instead')
        cm.validate_decision_pins(db, data.get('source_decision_pins', []))
        cm.require_retained_premises(db, d.id, replacement_pins)
        cm.snapshot_decision(db, d.id, reason='before-signoff')
        legacy_reviewed = cm.review_legacy_reuse(db, d.id, replacement_pins, data.get('source_decision_pins', []), data.get('expected_updated_at'))
        reviewed_human_sources, human_sources_changed = cm.rebind_reviewed_human_sources(db, d.id, replacement_pins, data.get('source_decision_pins', []), data.get('expected_updated_at'))
        cm.revalidate_historical_context(db, d.id, replacement_pins, data.get('source_decision_pins', []))
        cm.check_current(db, d.id, pins=replacement_pins, reviewed_human_sources=reviewed_human_sources)
        source_reason = graph.source_review_reason(d.id, db=db, allow_root_review=True)
        if source_reason:
            raise Invalid("Cannot sign this answer: " + source_reason)
        if not (current["answer"] or "").strip():
            raise Invalid("There is no answer on this node to sign; answer it or correct it")
        content = answer_hash(current["answer"] or "")
        required = _json_list(current["required_signers"])
        signatures = [x for x in _json_list(current["signatures"]) if isinstance(x, dict) and x.get("hash") == content]
        old_pins = [cm.pin(e, e['role']) for e in cm.edges(db, d.id)]
        evidence_changed = legacy_reviewed or human_sources_changed or (replacement_pins is not None and sorted(map(cm.encoded, replacement_pins)) != sorted(map(cm.encoded, old_pins)))
        if evidence_changed:
            signatures = []  # old co-signatures did not cover these source versions
            db.execute("UPDATE decisions SET signoff='required',signed_revision='',signed_hash='',signed_by='' WHERE id=?", (d.id,))
        retired_rule = bool(evidence_changed and current['reusable'])
        if retired_rule:
            graph.retire_replaced_rule(d.id,
                'A current-source review replaces the previous standing grant; explicit regrant required.',
                by=by, basis=basis, db=db)
            # The new signature records the actual post-action scope.
            current = db.execute('SELECT * FROM decisions WHERE id=?', (d.id,)).fetchone()
        if by.lower() not in {x.get("by", "").lower() for x in signatures}:
            from .approval_scope import snapshot as approval_scope
            signatures.append({"by": by, "at": stamp, "revision": stamp, "hash": content,
                               "scope": approval_scope(current)})
        remaining = [r for r in required if r.lower() not in {x.get("by", "").lower() for x in signatures}]
        if remaining:
            # One of several required approvers: recorded, not yet authorized.
            db.execute("UPDATE decisions SET signatures=?, signed_by=?, updated_at=?, actor_id=?, actor_name=?, "
                             "actor_basis=? WHERE id=?",
                             (json.dumps(signatures), ", ".join(x["by"] for x in signatures), stamp,
                              actor.id if actor is not None else "", by, basis, d.id))
            graph.append_event("signature", {"task_id": d.task_id, "decision_id": d.id, "by": by, "remaining": remaining,
                                             "revision": stamp, "basis": basis,
                                             **authz.event_provenance(actor, data.get("source"))}, db=db)
        else:
            # A person's answer, now signed by everyone it needed, is a
            # recorded answer like any other, not something resolved from
            # evidence. A fully signed evidence proposal also leaves its
            # provisional state, while keeping its source and answer intact.
            db.execute("UPDATE decisions SET signoff='signed', signed_by=?, signed_revision=?, signed_hash=?, "
                             "signatures=?, needs_review=0, review_reason='', updated_at=?, actor_id=?, actor_name=?, "
                             "actor_basis=?, status=CASE WHEN source='human' AND kind='answer' THEN 'approved' "
                             "WHEN status='proposed' THEN 'resolved' ELSE status END WHERE id=?",
                             (by if len(signatures) < 2 else ", ".join(x["by"] for x in signatures), stamp,
                              content, json.dumps(signatures), stamp,
                              actor.id if actor is not None else "", by, basis, d.id))
            graph.append_event("signoff", {"task_id": d.task_id, "decision_id": d.id, "by": by, "corrected": False,
                                           "revision": stamp, "basis": basis,
                                           **authz.event_provenance(actor, data.get("source"))}, db=db)
        cm.snapshot_decision(db, d.id, pins=replacement_pins, reason='human-signoff')
        if not remaining:
            from .execution_store import record_answer
            record_answer(db, {"id": d.id, "run_id": d.task_id, "owner_name": by}, current["answer"] or "",
                          current["rationale"] or "", provenance=f"signed by {by}")
    def committed():
        if basis == 'owner':
            with graph.transaction():
                graph.learn_from_answer(d.id, by, store.get_decision(d.id).get('owner_evidence') or '')
        for name in remaining:
            store.notify(d.id, "signoff", to=name)
        if not remaining:
            try:
                from .ladder import close_open_twins
                close_open_twins(graph, d.id, d.question, d.repo or "", current["answer"] or "", by,
                                 expected_revision=stamp)
            except Exception:
                pass
    from .database import after_commit
    after_commit(db, committed)
    view = node_view(store, d.id)
    if remaining:
        view["notice"] = f"Signed by {by}; still waiting on {', '.join(remaining)}"
    return view


def _decision_context(store, row):
    """Current, attributed context only; never copy its standing to another node."""
    canonical = None
    if row['status'] == 'duplicate' and row['superseded_by']:
        canonical = store.graph.db.execute(
            _DECISION_SELECT + ' WHERE d.id=?', (row['superseded_by'],)).fetchone()
    view = _view(row, canonical=canonical)
    keys = ('node_id', 'parent_id', 'question', 'status', 'kind', 'partial', 'owner', 'answer', 'rationale',
            'evidence', 'answered_by', 'signed_by', 'signatures',
            'signoff', 'authorized', 'blocking', 'needs_review', 'review_reason', 'model_pending',
            'updated_at', 'facts', 'required_signers', 'reusable', 'rule_conditions', 'rule_scope', 'rule_expires')
    return {**{key: view[key] for key in keys}, 'context': row['context'] or '',
            'paths': _json_list(row['scope_paths']) or ([row['path']] if row['path'] else []),
            'applicability': _json_dict(row['applicability'])}


def _candidate_represented(graph, run, candidate, row):
    """Only an exact question in the same stated scope is already represented.

    Broader parents and semantic overlap are deliberately not suppressed: the
    host must read their answers and decide what remains to ask.
    """
    normalize = lambda text: ' '.join((text or '').casefold().split())
    if normalize(candidate['question']) != normalize(row['question']):
        return False
    if row['status'] in ('suggested', 'adopted', 'withdrawn') or row['superseded_by']:
        return False
    paths = set(_paths(candidate.get('paths')))
    node = _row_to_decision(row)
    if not paths or paths != set(_scope_paths_of(graph, node)):
        return False
    facts = _json_dict(run['facts'])
    if {k: normalize(str(v)) for k, v in facts.items()} != {
            k: normalize(str(v)) for k, v in _json_dict(row['facts']).items()}:
        return False
    # The key preserves the original context before generated Paths/Options
    # lines were appended. Do not strip user prose or infer scope equivalence:
    # another customer, environment or exception may be written in any language.
    # Uncertain or legacy scope stays a candidate for the host to reconcile.
    return row['scope_key'] in {scope_key('', list(paths)), scope_key(run['goal'], list(paths))}


def _task_as_started(store, row):
    # Kickoff discovery is a historical reading of the goal, not a fresh work
    # list. In the deep=1 humanize campaign, wait returned its two candidates
    # after a broad parent already asked both, and the host copied them into
    # children without first reading that parent's answer.
    store.graph.expire_rules()
    discovery = _json_dict(row['discovery'])
    proposed = candidates(discovery, row['title'], row['goal'] or '')
    nodes = store.graph.db.execute(
        _DECISION_SELECT + ' WHERE d.run_id=? AND d.draft=0 ORDER BY d.created_at, d.rowid',
        (row['id'],)).fetchall()
    represented, remaining = [], []
    for candidate in proposed:
        matches = [n['id'] for n in nodes if _candidate_represented(store.graph, row, candidate, n)]
        if matches:
            represented.append({**candidate, 'node_ids': matches})
        else:
            remaining.append(candidate)
    represented_questions = {c['question'] for c in represented}
    if 'named_decisions' in discovery:
        discovery['named_decisions'] = [c for c in discovery['named_decisions']
                                        if c.get('question') not in represented_questions]
    next_step = _next_for_verdict(row['verdict'], discovery, remaining)
    if nodes:
        next_step = ('Read existing_nodes and bridge_get_tree before adding more questions. Reconcile the '
                     'kickoff candidates with the questions and answers already on this task; they may overlap '
                     'or already be covered. Add only genuinely new or uncovered decisions in their actual '
                     'scope. Existing answers and referrals do not authorize a new node or transfer ownership. '
                     'Read bridge_get_tree before an irreversible step.')
    if row['status'] in ('abandoned', 'completed'):
        next_step = 'This task is closed; start a new task for new work.'
        if row['status'] == 'completed':
            next_step = ('This task is closed to new decision nodes. After the advisory review, call '
                         'bridge_finish_task on this same task with the same complete diff and checks to '
                         'refresh its proof, then bridge_export_proof. Start a new task only for new work.')
        remaining = []
        discovery['named_decisions'] = []
    return {'task_id': row['id'], 'title': row['title'], 'repo': row['repo'], 'status': row['status'],
            'verdict': row['verdict'], 'why': row['verdict_why'], 'discovery': discovery,
            'candidates': remaining, 'represented_candidates': represented,
            'existing_nodes': [_decision_context(store, n) for n in nodes[:20]],
            'existing_node_count': len(nodes), 'existing_nodes_complete': len(nodes) <= 20,
            'repeated': True, 'model_pending': bool(discovery.get('model_pending')), 'next': next_step}


def people_acted(store, task_id):
    participation = PEOPLE_EVENTS + ('task_note', 'interview_started')
    marks = ','.join('?' for _ in participation)
    return bool(store.graph.db.execute(f"SELECT 1 FROM events WHERE run_id=? AND kind IN ({marks}) LIMIT 1",
                                      (task_id, *participation)).fetchone())


def abandon_task(store, task_id, reason):
    if not reason.strip():
        raise Invalid('A reason is required to close a mistaken task')
    graph = store.graph
    _ = store.delivery  # Initialize the outbox before opening a transaction.
    with graph.transaction():
        run = _task(store, task_id)
        if run['status'] == 'abandoned':
            return {'task_id': task_id, 'status': 'abandoned', 'repeated': True}
        if run['status'] == 'completed' or people_acted(store, task_id):
            raise Invalid('A task people have acted on cannot be abandoned; preserve its decision history')
        rows = graph.db.execute('SELECT id FROM decisions WHERE run_id=?', (task_id,)).fetchall()
        for row in rows:
            graph.db.execute("UPDATE decisions SET status='withdrawn', signoff='', model_pending=0, updated_at=? WHERE id=?", (now(), row['id']))
            graph.append_event('node_withdrawn', {'task_id': task_id, 'decision_id': row['id'], 'reason': reason})
        graph.db.execute("UPDATE notifications SET state='superseded' WHERE run_id=? AND state IN ('queued','failed')", (task_id,))
        discovery = _json_dict(run['discovery']);discovery.pop('model_pending', None)
        graph.db.execute("UPDATE runs SET status='abandoned', discovery=?, updated_at=? WHERE id=?", (json.dumps(discovery), now(), task_id))
        graph.append_event('task_abandoned', {'task_id': task_id, 'reason': reason})
    return {'task_id': task_id, 'status': 'abandoned', 'reason': reason}


_BACKGROUND = {}
_BACKGROUND_LOCK = threading.Lock()
MODEL_PENDING_STALE = 600


def call_budget():
    try:
        return max(1.0, min(50.0, float(os.environ.get('BRIDGE_MCP_CALL_BUDGET', '50'))))
    except ValueError:
        return 50.0


def background_running():
    with _BACKGROUND_LOCK:
        return bool(_BACKGROUND)


def wait_for_background(timeout=25.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _BACKGROUND_LOCK:
            workers = list(_BACKGROUND.values())
        if not workers:
            return True
        for worker in workers:
            worker.join(max(0, min(0.1, deadline - time.monotonic())))
    return not background_running()


def _start_background(key, fn, *args):
    # A fresh discovery may supersede a read already in flight. Track both
    # until they exit; each writer still compares its persisted token/revision.
    key = (*key, uuid.uuid4().hex)
    def run():
        try:
            fn(*args)
        finally:
            with _BACKGROUND_LOCK:
                _BACKGROUND.pop(key, None)
            args[0].graph.close_thread()
    with _BACKGROUND_LOCK:
        worker = threading.Thread(target=run, daemon=True, name='raven-model-' + key[1])
        _BACKGROUND[key] = worker
        worker.start()


def _model_triage_pass(store, cfg, task_id, pending):
    graph = store.graph
    row = _task(store, task_id)
    title, goal = row['title'], row['goal'] or ''
    discovery = _json_dict(row['discovery']);narrow = bool(discovery.get('narrow_edit'))
    verdict, why = row['verdict'], row['verdict_why']
    try:
        if cfg is not None and cfg.semantic_retrieval and not narrow:
            from .llm import name_decisions
            named = name_decisions(cfg, title, task_statement(goal),
                                   [a.get("path", "") for a in (discovery.get("areas") or [])],
                                   [p.get("name", "") for p in (discovery.get("people") or [])])
            if named:
                discovery["named_decisions"] = named
        if cfg is not None and cfg.semantic_retrieval:
            from .llm import model_triage
            advised = model_triage(cfg, title, goal, discovery, verdict, why)
            if advised:
                discovery["verdict_rules"] = verdict
                discovery["verdict_rules_why"] = why
                discovery["verdict_source"] = "model"
                if advised["verdict"] == verdict:
                    if advised["why"]:
                        why = f"{why} (the model agrees: {advised['why']})"
                elif advised["verdict"] == "engage":
                    verdict, why = "engage", advised["why"] or why
                elif verdict == "unplaced":
                    # The model read the same digest with no area in it; its
                    # pass knows no more than the rules did.
                    discovery["verdict_model"] = advised["verdict"]
                    discovery["verdict_model_why"] = advised["why"]
                    discovery["verdict_source"] = "rules"
                else:
                    discovery["verdict_model"] = advised["verdict"]
                    discovery["verdict_model_why"] = advised["why"]
                    discovery["verdict_source"] = "rules"
                    why = f"{why} (the model would have passed this: {advised['why']}; Raven engages anyway)"

    except Exception as error:
        discovery['model_error'] = type(error).__name__
    finally:
        discovery.pop('model_pending', None)
        with graph.transaction():
            current = _task(store, task_id)
            if _json_dict(current['discovery']).get('model_pending') == pending and current['status'] != 'abandoned':
                graph.db.execute('UPDATE runs SET verdict=?, verdict_why=?, discovery=?, updated_at=? WHERE id=?',
                                 (verdict, why, json.dumps(discovery), now(), task_id))
                graph.append_event('task_triaged', {'task_id': task_id, 'verdict': verdict, 'why': why})


def _model_node_pass(store, cfg, did, ctx, paths, hints, requester, owner_id, original_context, scheduled_revision):
    from .ladder import ask
    from .llm import compose_brief
    graph = store.graph
    live = store.get_decision(did)
    run = _task(store, live['run_id'])
    scratch_ids = []
    graph._local.model_exclude = did
    try:
        # The revision is captured when inference is scheduled. A person
        # may answer before this thread even starts, so its startup snapshot
        # must never become permission to overwrite that human action.
        marks = ','.join('?' for _ in PEOPLE_EVENTS)
        acted_before_start = graph.db.execute(
            f"SELECT 1 FROM events WHERE decision_id=? AND kind IN ({marks}) LIMIT 1",
            (did, *PEOPLE_EVENTS)).fetchone()
        if (live['updated_at'] != scheduled_revision or not live.get('model_pending')
                or run['status'] == 'abandoned' or live['status'] == 'withdrawn' or acted_before_start):
            return
        result = ask(store, cfg, run['id'], live['question'], context=ctx or 'a node on the canvas',
                     path=paths[0] if paths else 'unknown', category=live.get('category') or '', owner_id=owner_id or None,
                     requester=requester, hints=hints, facts=_json_dict(live.get('facts')),
                     keep_draft=True, also_paths=paths[1:], scratch=True)
        scratch_ids = result.get('drafts') or []
        scratch = result.get('duplicate_stub') or result['id']
        outcome = dict(graph.db.execute('SELECT * FROM decisions WHERE id=?', (scratch,)).fetchone())
        if outcome['status'] in ('resolved','partial','assumed','proposed') and not outcome.get('signoff'):
            outcome['signoff'] = 'required'
        if not outcome.get('owner_id'):
            ranked = _route_signer(graph, run, live['question'], ctx, paths, requester, hints,
                                    live.get('category') or '', _json_dict(live.get('facts')), outcome.get('source_id'))
            if ranked:
                outcome['owner_id'] = graph.owner_id_for(ranked[0][0])
                outcome['owner_evidence'] = '; '.join(ranked[0][1])
        if outcome.get('signoff') == 'rule' and _not_behind_rule(graph, outcome.get('source_id') or '', _json_list(live.get('required_signers'))):
            outcome['signoff'] = 'required'
        if outcome['status'] == 'pending':
            withheld=[]
            outcome['brief'] = compose_brief(cfg, live['question'], original_context, run['title'],
                                               options=_json_list(live.get('options')), why=withheld) or ''
            if withheld:
                graph.append_event('brief_withheld', {'task_id': run['id'], 'decision_id': did, 'why': withheld[0]})
        with graph.transaction():
            current = dict(graph.db.execute('SELECT * FROM decisions WHERE id=?', (did,)).fetchone())
            if current['updated_at'] == scheduled_revision and current['model_pending'] and _task(store, run['id'])['status'] != 'abandoned':
                from . import context_memory as cm
                source_pins = [cm.pin(e, e['role']) for e in cm.edges(graph.db, scratch)]
                cm.validate(graph.db, did, source_pins)
                cm.validate_derivations(graph.db, scratch)
                fields = ['status','answer','rationale','source','source_id','evidence','kind','prediction','owner_id',
                          'owner_evidence','answered_by','signoff','signed_by','signed_hash','signed_revision','signatures','brief','superseded_by','source_reuse_state']
                fields = [f for f in fields if f in outcome]
                graph.db.execute('UPDATE decisions SET ' + ','.join(f+'=?' for f in fields) + ', updated_at=? WHERE id=?',
                                 (*[outcome[f] for f in fields], now(), did))
                graph.db.execute('INSERT OR IGNORE INTO decision_links(decision_id, related_id, kind, note, created_at, source_version_id) '
                                 'SELECT ?, related_id, kind, note, created_at, source_version_id FROM decision_links WHERE decision_id=?', (did, scratch))
                for event in graph.db.execute('SELECT id, detail FROM events WHERE decision_id=?', (scratch,)).fetchall():
                    detail = _json_dict(event['detail'])
                    if detail.get('decision_id') == scratch:
                        detail['decision_id'] = did
                    graph.db.execute('UPDATE events SET decision_id=?, detail=? WHERE id=?', (did, json.dumps(detail), event['id']))
                graph.append_event('model_read', {'task_id': run['id'], 'decision_id': did})
                cm.attach(graph.db, did, source_pins, replace=True)
    except Exception as error:
        graph.append_event('model_read_failed', {'task_id': run['id'], 'decision_id': did, 'error': type(error).__name__})
    finally:
        graph._local.model_exclude = ''
        with graph.transaction():
            for sid in scratch_ids:
                graph.db.execute('DELETE FROM decision_links WHERE decision_id=? OR related_id=?', (sid, sid))
                graph.db.execute('DELETE FROM events WHERE decision_id=?', (sid,))
                graph.db.execute('DELETE FROM decisions WHERE id=?', (sid,))
            graph.db.execute('UPDATE decisions SET model_pending=0 WHERE id=?', (did,))
        current = store.get_decision(did)
        acted = graph.db.execute("SELECT 1 FROM events WHERE decision_id=? AND kind IN (" + ','.join('?' for _ in PEOPLE_EVENTS) + ") AND created_at>=? LIMIT 1", (did, *PEOPLE_EVENTS, live['updated_at'])).fetchone()
        if not acted and (current['updated_at'] == live['updated_at'] or graph.db.execute("SELECT 1 FROM events WHERE decision_id=? AND kind='model_read'", (did,)).fetchone()):
            _notify_model_ready(store, current)


def _notify_model_ready(store, row):
    if row['status'] == 'pending':
        store.notify(row['id'], 'ask')
    elif row.get('signoff') == 'required':
        store.notify(row['id'], 'signoff')
        for name in _json_list(row.get('required_signers')):
            store.notify(row['id'], 'signoff', to=name)


def _route_signer(graph, run, question, ctx, paths, requester, hints, category, facts, source_id=''):
    from .routing import rank_for_decision, coordinator_route
    repo = graph.resolve_repo(repo_key(run['repo']))
    ranked = rank_for_decision(graph, repo, question, paths, context=ctx, requester=requester, hints=hints,
                               category=category, facts=facts)
    source = graph.get_decision(source_id) if source_id else None
    temporary = source and graph.db.execute(
        "SELECT 1 FROM events WHERE decision_id=? AND kind='route_learning_optout' LIMIT 1", (source.id,)).fetchone()
    source_signer = (source.answered_by or source.signed_by) if source and source.authorized and not temporary else ''
    from .graph import parse_facts, applicability_status
    from .ladder import _scope_difference
    scope_row = graph.db.execute('SELECT facts FROM decisions WHERE id=?', (source.id,)).fetchone() if source else None
    source_facts = parse_facts(scope_row['facts']) if scope_row else {}
    complete_scope = all(str((facts or {}).get(k, '')).lower() == v.lower() for k, v in source_facts.items())
    scope_difference = _scope_difference(question, ctx, paths[0] if paths else '', source, facts,
                                         source_facts, repo)[0] if source else ''
    person = graph.find_person(source_signer) if source_signer else None
    if (person and person['active'] and person['role'] != 'viewer' and complete_scope and not scope_difference
            and applicability_status(source, paths[0] if paths else '', facts)[0]):
        if not ranked or not any(e.startswith('verified:') for e in ranked[0][1]):
            ranked.insert(0, (source_signer, [f'signs off; {source_signer} signed the reused answer in decision {source.id}'], 2.0))
    if not ranked:
        fallback = coordinator_route(graph, repo, ['nobody is clearly placed to sign this'])
        ranked = [fallback] if fallback else []
    return ranked
