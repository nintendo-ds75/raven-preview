"""Near-duplicate questions on one task.

An agent working one task asks the same thing twice in different words:
"What per-use rate does Enterprise pay for Pilot?" and, twenty seconds
later, "How many cents does Enterprise pay per completed Pilot
response?". Both reached Dana as two calls. Measured on the Harborline
dry run (run 6): two such pairs on one canvas, four calls where there
were two questions.

The ladder's open-twin check (bridge/ladder.py, _open_twin) already folds
a question into an open decision of another task above 0.80 hybrid
similarity, and asks a model in the 0.45 to 0.80 band when one is on.
On the same canvas the pool of candidates is small and the context the
same, so this pass runs on what the ladder leaves open (a question the
record or memory settles is never folded), against the open nodes of
this task alone:

- the same words (the same term set, in any order) fold deterministically;
- a near match (0.40 or more) gets the one same-decision reading the
  ladder uses, synchronously, when a model is configured, so the agent
  hears "folded into node X" in the reply rather than after a background
  pass; without a model, or when the model is not sure, the question stays
  its own node and the person is asked (never merged on a guess);
- a candidate in another scope (other files, another named customer, a
  different stated fact, a different concrete figure) is never a twin,
  however close the words; one filed under another category folds only
  on the model's reading.

The fold is the ladder's own: the new node is a stub with status
'duplicate' and superseded_by the open node, linked as a duplicate, so
one answer settles both and the person is asked once.
"""
from __future__ import annotations

from . import llm as llm_mod

SURE = 0.80   # the ladder's deterministic bar
BAND = 0.40   # a same-task candidate this close is read by the model when one is on
NEARS = 2     # how many near candidates one model reading looks at


def _similarity(question: str, other: str, emb: list[float] | None = None) -> float:
    """The hybrid score the open-twin check uses: cosine of the local
    embeddings or stemmed term overlap over the shorter question,
    whichever is higher."""
    from .graph import _cosine
    from .ladder import _meaningful_terms
    qt, ct = set(_meaningful_terms(question)), set(_meaningful_terms(other))
    overlap = len(qt & ct) / min(len(qt), len(ct)) if qt and ct else 0.0
    cosine = _cosine(emb if emb is not None else llm_mod.embed(question), llm_mod.embed(other))
    return max(cosine, overlap)


def _jaccard(question: str, other: str) -> float:
    """Term overlap over both questions' terms together: 1.0 when they
    are the same words in another order, low when one merely contains
    the other's words among many of its own."""
    from .ladder import _meaningful_terms
    qt, ct = set(_meaningful_terms(question)), set(_meaningful_terms(other))
    return len(qt & ct) / len(qt | ct) if qt | ct else 0.0


def _open_nodes(graph, task_id: str, exclude: str = ""):
    rows = graph.db.execute(
        "SELECT id FROM decisions WHERE run_id=? AND status='pending' AND draft=0 AND superseded_by='' AND id!=? "
        "ORDER BY created_at", (task_id, exclude or "")).fetchall()
    out = []
    for r in rows:
        cand = graph.get_decision(r["id"], exact=True)
        if cand is not None:
            out.append(cand)
    return out


def same_task_twin(store, cfg, task_id: str, question: str, *, context: str = "", path: str = "",
                   category: str = "", facts: dict | None = None, repo: str = "", why: list | None = None,
                   exclude: str = ""):
    """The open node of this task that asks the same thing, or None. `why`
    collects one line per candidate considered, for the event log."""
    from .ladder import _cited_refs, _facts_of, _numeric_clash, _scope_difference
    graph = store.graph
    candidates = _open_nodes(graph, task_id, exclude)
    if not candidates:
        return None
    emb = llm_mod.embed(question)
    q_refs = set(_cited_refs(question))
    scored = []
    for cand in candidates:
        score = _similarity(question, cand.question, emb)
        if score < BAND:
            continue
        # On one canvas every question shares the task's words ("Pilot",
        # "charge", "per use"), so the shorter question's terms being all
        # in the longer one proves nothing. Measured live: "Which plans
        # are charged per use for Pilot?" folded into the allowance
        # question at 0.80. Words alone fold only the same term set.
        if score >= SURE and _jaccard(question, cand.question) < SURE:
            score = SURE - 0.01
        if q_refs - set(_cited_refs(cand.question)):
            _note(why, cand, score, "names a reference the other does not")
            continue
        if category and cand.category and category.strip().lower() != cand.category.strip().lower():
            # Another category: the same words fold only on the model's reading.
            score = min(score, SURE - 0.01)
        difference, _known = _scope_difference(question, context, path, cand, facts, _facts_of(graph, cand.id), repo)
        if difference:
            _note(why, cand, score, f"another scope: {difference}")
            continue
        if _numeric_clash(question, cand.question):
            _note(why, cand, score, "states a different figure")
            continue
        scored.append((score, cand))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    best_score, best = scored[0]
    if best_score >= SURE:
        _note(why, best, best_score, "the same words")
        return best
    if cfg is None or not cfg.deep_retrieval:
        for score, cand in scored[:NEARS]:
            _note(why, cand, score, "close, and no model is on to read whether it is the same decision; asked as its own node")
        return None
    client = llm_mod.Client(cfg)
    for score, cand in scored[:NEARS]:
        try:
            verdict = client.complete_json("same_decision", llm_mod.SAME_DECISION_SYSTEM,
                                           f"Q1: {question}\nQ2: {cand.question}")
        except llm_mod.LLMError:
            _note(why, cand, score, "the same-decision reading failed; asked as its own node")
            break
        if isinstance(verdict, dict) and verdict.get("same") is True:
            _note(why, cand, score, "the model read it as the same decision")
            return cand
        _note(why, cand, score, "the model read it as another decision")
    return None


def _note(why, cand, score, what):
    if why is not None:
        why.append(f"{cand.id} ({score:.2f}): {what}")


def fold_draft(store, row: dict, twin, *, why: list | None = None) -> dict:
    """The ladder left this question open: turn its draft into the stub
    of the open node that already asks it. Returns the open node's row
    the way ladder.ask returns a twin's (duplicate_stub names the stub,
    drafts holds it), so add_node places it like any other duplicate."""
    from .canvas import _DECISION_SELECT
    from .ladder import _excerpt, _word_trunc
    graph = store.graph
    did, task_id, question = row["id"], row["run_id"], row["question"]
    graph.update_decision(
        did, status="duplicate", superseded_by=twin.id, owner=twin.owner or "", kind="new",
        owner_evidence=twin.owner_evidence or "", routing_reason="same question already open on this task",
        answer=(f"Same open question as node {twin.id} on this task, not re-asked"
                + (f"; waiting on {twin.owner}." if twin.owner else ".")),
        evidence=(f"folded into open node {twin.id} (asked earlier as \"{_word_trunc(twin.question, 70)}\")"
                  + (f"; {_excerpt(twin.evidence, 200, 'the open node has the rest')}" if twin.evidence else "")))
    graph.add_link(did, twin.id, "duplicate", "the same open question on this task; one answer settles both")
    graph.append_event("dedup_open", {"task_id": task_id, "decision_id": did, "twin": twin.id, "same_task": True,
                                      "question": _word_trunc(question, 120), "why": list(why or [])[:6]})
    out = dict(graph.db.execute(_DECISION_SELECT + " WHERE d.id=?", (twin.id,)).fetchone())
    out["duplicate_stub"] = did
    out["drafts"] = list(row.get("drafts") or [did])
    out["note"] = (f"folded into node {twin.id}, already open on this task as \"{_word_trunc(twin.question, 70)}\". "
                   "One answer settles both; nobody is asked twice.")
    return out


def fold(store, task_id: str, question: str, twin, *, context: str = "", path: str = "", category: str = "",
         facts: dict | None = None, repo: str = "", why: list | None = None) -> dict:
    """Write the stub: a draft node of this task, status duplicate,
    pointing at the open node whose answer will settle both. Returns the
    open node's row the way ladder.ask returns a twin's: the row of the
    canonical decision, with duplicate_stub naming the stub and drafts
    holding it, so add_node places it like any other duplicate."""
    import json
    from .canvas import _DECISION_SELECT
    from .ladder import _excerpt, _word_trunc
    graph = store.graph
    did = graph.add_decision(task_id, question, category, "duplicate", repo=repo, context=context,
                             path=path or "unknown", kind="new", draft=True,
                             owner=twin.owner or "", owner_evidence=twin.owner_evidence or "",
                             answer=(f"Same open question as node {twin.id} on this task, not re-asked"
                                     + (f"; waiting on {twin.owner}." if twin.owner else ".")),
                             evidence=(f"folded into open node {twin.id} (asked earlier as \"{_word_trunc(twin.question, 70)}\")"
                                       + (f"; {_excerpt(twin.evidence, 200, 'the open node has the rest')}"
                                          if twin.evidence else "")),
                             embedding=llm_mod.embed(question), routing_reason="same question already open on this task")
    graph.db.execute("UPDATE decisions SET superseded_by=?, facts=? WHERE id=?",
                     (twin.id, json.dumps(facts) if facts else "", did))
    graph.add_link(did, twin.id, "duplicate", "the same open question on this task; one answer settles both")
    graph.append_event("dedup_open", {"task_id": task_id, "decision_id": did, "twin": twin.id, "same_task": True,
                                      "question": _word_trunc(question, 120), "why": list(why or [])[:6]})
    row = dict(graph.db.execute(_DECISION_SELECT + " WHERE d.id=?", (twin.id,)).fetchone())
    row["duplicate_stub"] = did
    row["drafts"] = [did]
    row["note"] = (f"folded into node {twin.id}, already open on this task as \"{_word_trunc(twin.question, 70)}\". "
                   "One answer settles both; nobody is asked twice.")
    return row
