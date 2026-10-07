"""The resolution ladder: memory, records, assumption, human. Ported.

For each question of a task, in order:
  a. memory: an earlier signed or evidence-resolved decision matches (hybrid
     of hashed-embedding cosine and stemmed overlap, recency-decayed, with
     newest-wins conflict handling and numeric-clash escalation).
  a2/a3. ownership introspection and accountability questions resolve from
     the ownership graph (blame, CODEOWNERS, reviews reconciled).
  b. records: merged PRs, commits, tickets in the graph (IDF lexical rung,
     ref lookup, novelty and focus gates, superseded-by-newer and
     contested-by-newer checks); then the model-as-selector sweep with query
     expansion, grounded single-record composition, and joint composition
     across records and memories.
  c. assumption: low-stakes categories get a precedent-following default.
  d. human: dedupe against open twins, then route to the accountable owner.

Outcomes carry a kind: evidence (resolved or partial from a record or a
direct memory hit), prediction (assumed defaults, proposals from unratified
tickets, semantic reuse of a neighbouring answer), or new (routed for
judgment). With no model backend the ladder runs its deterministic rungs
only; nothing is fabricated.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace

from . import llm as llm_mod
from .config import Config
from .graph import Graph, applicability_status, iso_to_ts, parse_facts, rule_conditions, rule_status, ts_to_iso
from .resolve import resolve_paths
from .routing import coordinator_route, route_ranked

RECORD_MIN_TERMS = 3
RANKED_KEEP = 5
MEMORY_HALF_LIFE_DAYS = 180.0
MEMORY_FRESH_DAYS = 90.0
DIRECT_MEMORY_MIN = 0.85
NEAR_MEMORY_MIN = 0.60
# How far below the floor a candidate is still worth showing the model
# when nothing cleared it. Low enough to reach a paraphrase that shares
# few words with the original, high enough that the model is not asked
# about noise.
RERANK_MIN = 0.30
CONFLICT_MARGIN = 0.05


def _excerpt(text, limit: int, rest: str) -> str:
    """Stored text quoted inside another: spacing folded, whole when it
    fits, else cut at a word and marked. Never cut inside a word."""
    return llm_mod.clip_marked(" ".join(str(text or "").split()), limit, rest)


OPEN = "pending"


def _recency(updated_at: float) -> float:
    import math
    import time as _time
    age_days = max(0.0, (_time.time() - updated_at) / 86400.0)
    decayed = max(0.0, age_days - MEMORY_FRESH_DAYS)
    return math.exp(-decayed * math.log(2) / MEMORY_HALF_LIFE_DAYS)


@dataclass
class RunResult:
    task_id: str
    title: str
    resolved: list[dict] = field(default_factory=list)
    proposed: list[dict] = field(default_factory=list)
    assumed: list[dict] = field(default_factory=list)
    open: list[dict] = field(default_factory=list)
    # The rows created by this run while they were still being built;
    # published when the run is done, so no reader sees a half-built one.
    drafts: list[str] = field(default_factory=list)

    @property
    def counts(self) -> str:
        whole = [r for r in self.resolved if not r.get("partial")]
        partial = [r for r in self.resolved if r.get("partial")]
        parts = [f"{len(whole)} resolved", f"{len(partial)} partial"]
        if self.proposed:
            parts.append(f"{len(self.proposed)} proposed")
        parts += [f"{len(self.assumed)} assumed", f"{len(self.open)} open"]
        return " · ".join(parts)

    @property
    def decision_ids(self) -> list[str]:
        out = []
        for bucket in (self.resolved, self.proposed, self.assumed, self.open):
            for item in bucket:
                if item.get("id") and item["id"] not in out:
                    out.append(item["id"])
        return out


_FILLER = {"amid", "lately", "currently", "actually", "right", "today",
           "still", "really", "going", "exactly", "supposed", "officially",
           "anymore", "these", "those", "there", "where", "should", "would",
           "could", "about", "since", "after", "before", "already", "suppos"}

_UNRESOLVED_RE = re.compile(
    r"gathering|investigat|logged for|need a |need to |needs |should we"
    r"|feature request|requested|due (before|by|next)|complain"
    r"|before deciding|still waiting|exploring|no decision|would be nice"
    r"|evaluating|considering|proposed option|proposal|weighing"
    r"|reports? (of|that|occasional|intermittent)")

_DECIDED_RE = re.compile(
    r"decided|approved|going with|switched to|revised|raised (to|the)"
    r"|lowered (to|the)|capped|no longer|now (requires?|uses?|blocks?"
    r"|excluded?|hard)|adopted|standardized|set to|chose|resolved as"
    r"|closed as|working as intended|are excluded|is excluded")

_REVERSAL_RE = re.compile(
    r"\brevert(?:s|ed|ing)?\b|reverses|reversing|supersedes|superseding|replaces the|overturns"
    r"|no longer (the|a hard|blocked|applies)|instead of the (previous|old)"
    r"|as of this change|this replaces|rolls back|reinstates|we now allow"
    r"|previously we|used to (be|require)|is retired|retired\b|narrows")

_GAP_RE = re.compile(
    r"do(?:es)? not (\w+ly )?(specify|state|say|define|address|indicate"
    r"|establish|cover|describe|mention|detail|call out|set out|prescribe)"
    r"|leaves? [^.]{0,40}?(open|unspecified|unanswered|undefined)"
    r"|is silent on|no guidance on|not addressed (in|by) the record"
    r"|contains? no information|no information (about|on)"
    r"|neither record (\w+ly )?(specifies|states|says|defines|addresses|covers"
    r"|mentions|establishes|settles|distinguishes|ties)"
    r"|no record (specifies|states|says|defines|addresses|covers|mentions|settles)"
    r"|not (stated|specified|addressed|covered|settled|distinguished) (in|by) (any|either|the) record"
    # Measured live: "none of the records show ... or state that ...",
    # "(not included here)" and "X is supported, but whether ..." read as a
    # whole answer.
    r"|none of the records (\w+ )?(show|shows|state|states|say|says|specify|specifies|mention|mentions"
    r"|establish|establishes|cover|covers|address|addresses|settle|settles)"
    r"|not included here|(is|are) supported, but whether")

# A question about history itself: why something was dropped, what was
# once proposed. Only such a question is composed from records that were
# not adopted.
_HISTORY_Q_RE = re.compile(
    r"\b(?:why (?:was|were|did)\b|ever (?:proposed|considered|tried)|(?:was|were) (?:\w+ ){0,4}?"
    r"(?:proposed|rejected|cancell?ed|dropped|withdrawn|superseded)|history|historically|previously|used to"
    r"|originally|in the past)", re.IGNORECASE)

_RECENCY_Q_RE = re.compile(
    r"\b(today|currently|still|now|in effect|as of|reopened|since then"
    r"|latest|up to date|already been|has (it|that|this|the .{1,30}) been)\b")

_PENDING_RE = re.compile(
    r"\b(?:is |are )?to be (?:updated|implemented|added|revised|migrated"
    r"|rolled out|configured)\b|\bwill (?:be|need to be) (?:updated"
    r"|implemented|added|revised)\b|\bstill needs? to be\b|\bremains? to be\b")


def _states_a_gap(text: str) -> bool:
    return bool(_GAP_RE.search(text.lower()))


_CONTRAST_RE = re.compile(r"\b(?:rather than|instead of|as opposed to)\b")
_CONTRAST_STOP = {"the", "and", "for", "this", "that", "but", "their", "its", "our", "your",
                  "same", "one", "now", "one", "they", "them", "which", "who", "what"}


def _contrast_terms(*questions: str) -> list[str]:
    """The distinguishing condition a question introduces with 'rather
    than', 'instead of', or 'as opposed to': the words just before the
    phrase name the case asked about (month-to-month rather than annual).
    Numbers are left out, because a threshold is meant to be applied to
    an amount the record never names."""
    out: list[str] = []
    for question in questions:
        q = question.lower()
        for m in _CONTRAST_RE.finditer(q):
            before = q[:m.start()]
            clause = re.split(r"[,;:.?]|\b(?:is|are|was|were|be|being|on|with|has|have|had|of|to|for|in|at|by|under|from)\b", before)[-1]
            for t in re.findall(r"[a-z]+", clause)[-4:]:
                s = llm_mod.stem(t)
                if len(s) > 2 and s not in _CONTRAST_STOP and s not in _ASK_MACHINERY and s not in out:
                    out.append(s)
    return out


def _unmet_contrast(questions: list[str], texts: list[str]) -> list[str]:
    """Contrast terms of the question that none of the texts mention. An
    answer to the other side of the contrast must not be applied to this
    side: the rule for annual contracts says nothing about monthly ones."""
    terms = _contrast_terms(*questions)
    if not terms:
        return []
    have = {llm_mod.stem(t) for t in re.findall(r"[a-z]+", " ".join(texts).lower())}

    def met(term: str) -> bool:
        # month also matches monthly; the light stemmer keeps them apart.
        return any(h == term or (len(min(h, term, key=len)) >= 4 and (h.startswith(term) or term.startswith(h)))
                   for h in have)

    return [t for t in terms if not met(t)]


def _row_text(row) -> str:
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    return " ".join(str(row[k] or "") for k in ("title", "body", "question", "answer", "rationale", "context")
                    if k in keys)


_NUM_UNIT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(mm|cm|km|m|s|sec|seconds|min|minutes|h|hr|hours"
    r"|days?|%|percent|deg(?:rees)?|kts?|knots)\b")


# A figure a text names only to deny or replace it: "19 days, not 11 days",
# "instead of 11 days", "no longer 90 days", "down from 30 seconds".
_FORMER_RE = re.compile(r"(?:\b(?:not|no longer|instead of|rather than|replac(?:es|ed|ing)|in place of|previously|"
                        r"formerly|used to be|superseding)\s+(?:[\w-]+\s+){0,2}|\b(?:down from|up from|from)\s+)$")


def _figures(text: str) -> tuple[dict, dict]:
    """The figures a text states, by unit, as (asserted, denied)."""
    asserted: dict = {}
    denied: dict = {}
    low = (text or "").lower()
    for m in _NUM_UNIT_RE.finditer(low):
        n, u = m.group(1), m.group(2)
        u = {"sec": "s", "seconds": "s", "minutes": "min", "hr": "h",
             "hours": "h", "percent": "%", "knots": "kt", "kts": "kt",
             "days": "day", "degrees": "deg"}.get(u, u)
        clause = re.split(r"[.;!?]\s|,\s", low[max(0, m.start() - 40):m.start()])[-1]
        (denied if _FORMER_RE.search(clause) else asserted).setdefault(u, set()).add(float(n))
    return asserted, denied


def _numeric_clash(a: str, b: str) -> bool:
    """Do two texts state different concrete figures for the same unit?
    Only what each asserts counts, and one denying what the other asserts
    is a clash. Measured live on 5e967e4: "retain for 19 days, not 11
    days" shared the 11 with a signed "11 days" and read as agreeing."""
    aa, ad = _figures(a)
    ba, bd = _figures(b)
    for u, vals in aa.items():
        if (vals & bd.get(u, set())) or (u in ba and not (vals & ba[u])):
            return True
    return any(vals & ad.get(u, set()) for u, vals in ba.items())


_ENTITY_STOP = {"i", "the", "a", "an", "this", "that", "these", "those", "we", "our", "it", "its", "if", "when",
                "should", "do", "does", "is", "are", "was", "were", "can", "could", "would", "will", "may",
                "customer", "customers", "account", "accounts", "user", "users", "team", "teams", "paths",
                "options", "context", "note", "notes", "see", "also", "and", "or", "but", "for", "with",
                "from", "into", "not", "yes", "no"}
_SCOPE_LINE_RE = re.compile(r"^(?:paths|options)\s*:", re.IGNORECASE)
# API, APIs, URL, TLS, HTTPS: vocabulary, not the name of a customer or a release.
_ACRONYM_RE = re.compile(r"[A-Z]{2,5}s?")


def _scope_entities(text: str) -> set[str]:
    """The names a context turns on: capitalized words that are not the
    first word of their sentence, identifiers with digits or separators,
    references, quoted strings, emails. Two contexts naming different
    entities are two decisions (customer Alpha and customer Beta), even
    when the question text is identical."""
    out: set[str] = set()
    for line in (text or "").splitlines():
        if _SCOPE_LINE_RE.match(line.strip()):
            continue
        for sentence in re.split(r"[.;:?!\n]+", line):
            words = sentence.split()
            for i, raw in enumerate(words):
                w = raw.strip("\"'()[]{},")
                if not w:
                    continue
                low = w.lower()
                if re.fullmatch(r"\d+", w):
                    # A line number, a count, a year: not a name. A dotted
                    # number (a version) still is.
                    continue
                if re.search(r"\d", w) and len(w) >= 3:
                    out.add(low)
                elif "@" in w or "#" in w or "_" in w or ("-" in w and len(w) > 4 and w != low):
                    out.add(low)
                elif i > 0 and w[0].isupper() and len(w) > 1 and low not in _ENTITY_STOP \
                        and not _ACRONYM_RE.fullmatch(w):
                    out.add(low)
        for quoted in re.findall(r"[\"“]([^\"”]{2,60})[\"”]", line):
            out.add(quoted.strip().lower())
    return out


def _facts_of(store: Graph, decision_id: str) -> dict[str, str]:
    row = store.db.execute("SELECT facts FROM decisions WHERE id=?", (decision_id,)).fetchone()
    return parse_facts(row["facts"]) if row is not None and row["facts"] else {}


def _scope_difference(question: str, context: str, path: str, cand, facts: dict | None = None,
                      their_facts: dict | None = None, repo: str = "") -> tuple[str, bool]:
    """Why another decision with the same question may not be this one,
    and whether that is known or only suspected. Known: they are about
    different files, or both state a fact (customer=..., release=...)
    with different values. Suspected: their contexts name different
    things (customer Alpha, customer Beta) and no stated fact settles it.
    Facts both state alike settle it the other way. A name the other
    decision also uses, or the repository's own, is no difference.
    Measured live: a follow-up about the same release and file was
    called another scope because one context said "follow-up" and
    "urllib3" and the other "273" and "APIs". Empty when nothing says
    they differ."""
    mine = (path or "").strip().lstrip("/")
    theirs = (cand.path or "").strip().lstrip("/")
    if mine and theirs and mine != "unknown" and theirs != "unknown" and mine != theirs \
            and not mine.startswith(theirs.rstrip("/") + "/") and not theirs.startswith(mine.rstrip("/") + "/"):
        # A different file is a different scope when the answer was declared
        # for its files, or when a question is about its file. A question
        # about neither is not scoped by the file it happened to be asked
        # from. Measured live on eb9d22d: "What documentation must ship for a
        # new Retry feature?" answered from docs/user-guide.rst read as
        # another scope for the same question asked from changelog/.
        spec = getattr(cand, "applicability", None) or {}
        declared = bool(spec.get("paths"))
        # A signed policy explicitly scoped by facts can be recorded in
        # one file and prescribe work in another. The destination naming
        # its own path does not narrow the source's declared scope.
        fact_scoped = bool(spec.get("requires")) and cand.authorized and applicability_status(cand, path, facts)[0]
        if declared or _names_file(cand.question, theirs) or (_names_file(question, mine) and not fact_scoped):
            return f"this one is about {mine}, decision {cand.id} about {theirs}", True
    facts = {k.lower(): v for k, v in (facts or {}).items()}
    their_facts = {k.lower(): v for k, v in (their_facts or {}).items()}
    shared = [k for k in facts if k in their_facts]
    clash = [k for k in shared if facts[k].strip().lower() != their_facts[k].strip().lower()]
    if clash:
        k = clash[0]
        return f"this one states {k}={facts[k]}, decision {cand.id} states {k}={their_facts[k]}", True
    if shared:
        return "", False
    repo_words = {w for w in re.split(r"[/\s]+", (repo or "").lower()) if w}
    text_a = f"{question} {context}".lower()
    text_b = f"{cand.question} {cand.context or ''} {cand.answer or ''}".lower()
    ents_a = {e for e in _scope_entities(context) - _scope_entities(question)
              if e not in repo_words and e not in text_b}
    ents_b = {e for e in _scope_entities(cand.context or "") - _scope_entities(cand.question)
              if e not in repo_words and e not in text_a}
    if ents_a and ents_b:
        return (f"this one names {', '.join(sorted(ents_a)[:3])}, decision {cand.id} names "
                f"{', '.join(sorted(ents_b)[:3])}"), False
    return "", False


def _names_file(question: str, path: str) -> bool:
    """Whether a question is about the file it was asked from: it names
    the path, or the file's own name."""
    path = (path or "").strip().strip("/").lower()
    low = (question or "").lower()
    if not path or path == "unknown":
        return False
    base = path.rsplit("/", 1)[-1]
    return path in low or ("." in base and base in low)


def _memory_namespace_difference(store: Graph, task_id: str, source) -> str:
    """Explain incompatible recorded context for a new answer reuse only.

    Associations are neither supporting premises nor declared rule scope.
    Compare explicit canonical namespaces; missing context establishes no
    conflict. Do not attribute a later-added task link to an older answer.
    """
    from .context_memory import citation

    def namespaces(task, through=None):
        rows = store.db.execute('''SELECT record_id,source_version_id,recorded_at
            FROM task_source_anchors
            WHERE task_id=? AND role IN ('work_item','context')''', (task,))
        found = {}
        for row in rows:
            if through is not None and iso_to_ts(row['recorded_at']) > through:
                continue
            # Native Git -> GitHub aliases retain the backing row's original
            # identity. Compare the exact version selected by the task, not
            # that initial identity or a newer head's canonical namespace.
            pinned = citation(store.db, row['record_id'], row['source_version_id'])
            if not pinned:
                continue
            provider, namespace = pinned['provider'], pinned['namespace']
            if not provider or not namespace or provider == 'legacy' or namespace == 'legacy':
                continue
            found.setdefault(provider, set()).add(namespace)
        return found

    current = namespaces(task_id)
    previous = namespaces(source.task_id, source.updated_at)
    conflicts = sorted(p for p in current.keys() & previous.keys()
                       if current[p] != previous[p])
    if not conflicts:
        return ''
    here = ', '.join(f'{p}:{n}' for p in conflicts for n in sorted(current[p]))
    there = ', '.join(f'{p}:{n}' for p in conflicts for n in sorted(previous[p]))
    return (f'namespace_conflict: this task is linked to {here}; decision {source.id} '
            f'was associated with {there}')


def _scope_conflict(question: str, context: str, path: str, cand, facts: dict | None = None,
                    their_facts: dict | None = None, repo: str = "") -> str:
    """Why an open decision with the same question is not this decision,
    known or suspected (see `_scope_difference`). Empty when the scopes
    are compatible."""
    return _scope_difference(question, context, path, cand, facts, their_facts, repo)[0]


def _open_twin(store: Graph, cfg, client, question: str, emb: list[float], repo: str,
               exclude: str = "", context: str = "", path: str = "", related: list | None = None,
               facts: dict | None = None, skip: set | None = None):
    """An already-open decision this question duplicates, if any. Above 0.80
    hybrid similarity the twin is deterministic; the 0.45 to 0.80 band gets
    one same-decision model check when a backend is on. A candidate whose
    scope differs (other files, other named entities in its context) is
    never a twin: it lands in `related` instead, as the same question in
    another scope."""
    qt = set(_meaningful_terms(question))
    q_refs = set(_cited_refs(question))
    scored: list = []
    for s, cand in store.similar_open(emb, top_k=12, min_score=0.35, repo=repo, query=question):
        if cand.id == exclude or (skip and cand.id in skip):
            continue
        # A question bound to a managed provider call can be a twin like
        # any other: the answer that settles the canonical decision is
        # delivered to every call waiting on it or on its duplicates.
        if q_refs - set(_cited_refs(cand.question)):
            continue
        ct = set(_meaningful_terms(cand.question))
        ov = len(ct & qt) / min(len(ct), len(qt)) if ct and qt else 0.0
        hybrid = max(s, ov)
        why, known = (_scope_difference(question, context, path, cand, facts, _facts_of(store, cand.id), repo)
                      if hybrid >= 0.45 else ("", False))
        if why:
            if related is not None:
                related.append((cand, why, known))
            continue
        scored.append((hybrid, cand))
    scored.sort(key=lambda x: -x[0])
    if scored and scored[0][0] >= 0.80:
        return scored[0][1]
    nears = [cand for h, cand in scored[:2] if h >= 0.45]
    if cfg is None or client is None or not cfg.deep_retrieval:
        return None
    for near in nears:
        try:
            verdict = client.complete_json("same_decision", llm_mod.SAME_DECISION_SYSTEM,
                                           f"Q1: {question}\nQ2: {near.question}")
            if isinstance(verdict, dict) and verdict.get("same") is True:
                return near
        except llm_mod.LLMError:
            break
    return None


def _close_open_twins(store: Graph, source_id: str, question: str, repo: str,
                      answer: str, answered_by: str, source: str = "memory",
                      context: str = "", path: str = "") -> int:
    """Resolving a question also closes its still-open twins from other
    runs; only the deterministic 0.80 bar matches, never a model call.
    The twin takes the answer as evidence derived from the source
    decision: it still wants its own owner's sign-off, and a correction
    of the source reaches it."""
    emb = llm_mod.embed(question)
    src = store.get_decision(source_id, exact=True) if source_id else None
    revision = ts_to_iso(src.updated_at) if src is not None and src.updated_at else ""
    # The answer reaches only questions inside its own scope: the facts it
    # was given under, and the boundary its answerer declared (required and
    # excluded facts, paths, expiry). Measured live on eb9d22d: correcting
    # an answer declared for customer=acme resolved the same question asked
    # for customer=globex and for an unnamed customer.
    facts = _facts_of(store, source_id) if source_id else {}
    # Every boundary on the way here counts: this decision's own, and those
    # of the answers it was taken from (a question resolved from memory
    # carries the remembered answer's boundary, not a declaration of its own).
    bounds, node, seen = [], src, set()
    while node is not None and node.id not in seen and len(seen) < 5:
        seen.add(node.id)
        if node.applicability:
            bounds.append(node)
        node = store.get_decision(node.source_id, exact=True) if getattr(node, "source_id", "") else None
    unfit: set[str] = set()
    closed = 0
    while closed < 3:
        twin = _open_twin(store, None, None, question, emb, repo, exclude=source_id, context=context, path=path,
                          facts=facts, skip=unfit)
        if twin is None:
            break
        twin_facts = _facts_of(store, twin.id)
        if any(not applicability_status(spec, twin.path or "", twin_facts)[0] for spec in bounds):
            unfit.add(twin.id)
            continue
        source_decision = store.get_decision(source_id, exact=True)
        store.publish_evidence(
            twin.id, [_as_record(source_decision)] if source_decision else [], status="resolved", source=source, answer=answer, answered_by=answered_by,
            kind="evidence", signoff="required", source_id=source_id, source_revision=revision,
            evidence=(f"settled by the same answer as decision {source_id} "
                      f"(\"{_word_trunc(question, 60)}\"); this question was open in an earlier run"
                      + ("" if source == "human" else "; no human has signed this")))
        store.add_link(twin.id, source_id, "derived", "took its answer from this decision")
        store.append_event("twin_closed", {"task_id": twin.task_id, "decision_id": twin.id,
                                           "settled_by": source_id})
        closed += 1
    return closed


def close_open_twins(store: Graph, source_id: str, question: str, repo: str,
                     answer: str, answered_by: str, *, expected_revision: str = "") -> int:
    """Public entry used by the inbox after a human signs an answer."""
    # The caller's answer was captured before its commit. A later human
    # correction may have won before this post-commit work starts; never
    # publish that old answer with the newer source's revision. This entry
    # uses only deterministic local retrieval, with no model/provider call.
    with store.transaction():
        src = store.get_decision(source_id, exact=True)
        if (src is None or not src.authorized or src.superseded_by or src.answer != answer
                or (expected_revision and ts_to_iso(src.updated_at) != expected_revision)):
            return 0
        return _close_open_twins(store, source_id, question, repo, answer, answered_by, source="human",
                                 context=src.context, path=src.path)


_RETRO_MACHINERY = {"reason", "given", "decid", "who", "why", "made",
                    "call", "happen", "motiv", "was", "chose", "choos",
                    "explain", "stated", "behind"}
_TIME_MACHINERY = {"today", "currently", "actual", "actually", "effect",
                   "still", "latest", "already", "since", "reopen", "reopened", "current"}
_ASK_MACHINERY = _RETRO_MACHINERY | _TIME_MACHINERY | {
    "whether", "assume", "assumes", "assum", "claim", "claims",
    "say", "says", "said", "suppose", "supposed", "exact", "exactly",
    "versus", "inside", "without"}

_MISFIT_RE = re.compile(r"\b(?:which|what)\b[^?]*\b(?:release|version|milestone)\b")
_FIT_RES = {
    "rollout": re.compile(r"\b(?:roll ?out|ship|deploy|launch|stag(?:e|ed|ing)|migrat|flag)"),
    "ux": re.compile(r"\b(?:screen|command|surface|page|ui|button|cli|flow|menu|prompt)"),
    "compat": re.compile(r"\b(?:behavio|compat|existing|break|legacy|old|current)"),
}


def _canned_misfit(question: str, category: str = "") -> bool:
    q = question.lower()
    if _MISFIT_RE.search(q):
        return True
    fit = _FIT_RES.get(category)
    return bool(fit is not None and not fit.search(q))


_PATH_RE = re.compile(r"\b[\w.-]+/[\w./-]*\w\.\w{1,4}\b")


def _invented_paths(answer: str, *sources: str) -> list[str]:
    haystack = " ".join(sources).lower()
    return [p for p in dict.fromkeys(_PATH_RE.findall(answer)) if p.lower() not in haystack]


def _record_contradicting(store: Graph, cfg: Config, repo: str, question: str, answer: str):
    """A settled record that covers this question but says something else."""
    terms = _meaningful_terms(question)
    if len(terms) < 3:
        return None
    dfs = store.term_dfs(terms, repo=repo)
    focus = sorted((t for t in terms if dfs.get(t, 0) > 0), key=lambda t: dfs[t])[:3]
    if len(focus) < 2:
        return None
    ans_terms = set(_meaningful_terms(answer))
    if not ans_terms:
        return None
    for m in store.intents_matching(terms, limit=4, repo=repo):
        text = (m["title"] + " " + m["body"]).lower()
        if sum(1 for t in focus if t in text) < 2:
            continue
        if _disagrees(cfg, question, answer, m):
            return m
    return None


def _disagrees(cfg: Config, question: str, answer: str, record) -> bool:
    if not cfg.semantic_retrieval:
        return False
    try:
        verdict = llm_mod.Client(cfg).complete(
            "conflict", llm_mod.CONFLICT_SYSTEM,
            f"Question: {question}\n\nAnswer on file: {answer}\n\n"
            f"Record {record['kind']} {_ref(record)}: {record['title']}\n{record['body'][:800]}",
            max_tokens=16).strip().upper()
    except llm_mod.LLMError:
        return False
    return verdict.startswith("CONFLICT")


def _is_pure_refusal(text: str) -> bool:
    """Whether a composed text only says what the sources do not say:
    every clause states a gap, or the first does and nothing is decided."""
    body = text.strip()
    if not _states_a_gap(body):
        return False
    clauses = [c for c in re.split(r"(?<=[.!?])\s+|;\s*|,\s*(?:but|however|although|while)\s+", body) if c.strip()]
    if clauses and all(_GAP_RE.search(c.lower()) for c in clauses):
        return True
    first = body.split(".")[0].lower()
    return bool(_GAP_RE.search(first)) and not _DECIDED_RE.search(body.lower())


def _superseded_by_newer(store: Graph, repo: str, chosen) -> object | None:
    """Return a newer record that revokes `chosen`, or None."""
    try:
        chosen_ref, chosen_ts = chosen["ref"], _parse_ts(chosen["created_at"])
    except (IndexError, KeyError, TypeError):
        return None
    focus = _meaningful_terms(chosen["title"])
    if len(focus) < 2:
        return None
    best, best_ts = None, chosen_ts
    for m in store.intents_matching(focus, limit=8, repo=repo):
        if m["ref"] == chosen_ref or not _is_settled(m):
            continue
        ts = _parse_ts(m["created_at"])
        if ts <= best_ts:
            continue
        text = (m["title"] + " " + m["body"]).lower()
        if sum(1 for t in focus if t in text) < 2:
            continue
        if not _REVERSAL_RE.search(text):
            continue
        best, best_ts = m, ts
    return best


def _contested_by_newer(store: Graph, repo: str, chosen) -> object | None:
    """Return the newest OPEN or symptom ticket that contests `chosen`."""
    try:
        chosen_ref, chosen_ts = chosen["ref"], _parse_ts(chosen["created_at"])
    except (IndexError, KeyError, TypeError):
        return None
    focus = _meaningful_terms(chosen["title"])
    if len(focus) < 2:
        return None
    best, best_ts = None, chosen_ts
    for m in store.intents_matching(focus, limit=8, repo=repo):
        # A cancelled ticket on the same subject contests nothing.
        if m["ref"] == chosen_ref or m["kind"] not in ("ticket", "jira", "issue") or _void(m):
            continue
        ts = _parse_ts(m["created_at"])
        if ts <= best_ts:
            continue
        text = (m["title"] + " " + m["body"]).lower()
        if sum(1 for t in focus if t in text) < 2:
            continue
        if not (_non_decision(text) or not _is_settled(m)):
            continue
        best, best_ts = m, ts
    return best


_VOID_RE = re.compile(
    r"\b(?:cancel+ed|won'?t (?:do|fix|implement)|wont ?(?:do|fix)|rejected|declined|obsolete|superseded"
    r"|withdrawn|abandoned|duplicate|invalid|not (?:planned|doing|adopted)|dropped|discarded|deprecated|retired)\b",
    re.IGNORECASE)
_VOID_TITLE_RE = re.compile(
    r"^\s*[\[(]?\s*(?:obsolete|superseded|cancel+ed|withdrawn|deprecated|rejected|historical)\b", re.IGNORECASE)
_VOID_BODY_RE = re.compile(
    r"^\s*resolution\s*:\s*(?P<r>[^\n]{1,40})$|\b(?P<s>superseded|replaced|obsoleted) by\b",
    re.IGNORECASE | re.MULTILINE)


def _field(row, key: str) -> str:
    try:
        return str(row[key] or "")
    except (IndexError, KeyError):
        return ""


def _ref(row) -> str:
    return _field(row, 'display_ref') or _field(row, 'ref')


def _evidence_role(row, role):
    return {**dict(row), '_evidence_role': role}


def _void(row) -> str:
    """Why a record states something that was not adopted, or empty: its
    status (cancelled, won't do, rejected, obsolete, superseded), a title
    that says so ("[OBSOLETE] ..."), or a Jira resolution line or a
    "superseded by" in its opening. A record like that is history: what
    was once proposed or once held, never what stands. Measured live: a
    composed answer read a cancelled Jira proposal as the current policy."""
    status = _field(row, "status").strip()
    if status and _VOID_RE.search(status):
        return status
    title = _field(row, "title")
    marker = _VOID_TITLE_RE.match(title)
    if marker:
        return marker.group(0).strip(" [(").lower()
    for m in _VOID_BODY_RE.finditer(_field(row, "body")[:600]):
        if m.group("s"):
            return f"{m.group('s').lower()} by a later record"
        if _VOID_RE.search(m.group("r") or ""):
            return m.group("r").strip()
    return ""


# A record a question or its context names by its key: a tracker key
# (NET-102) or a pull request number (#1234).
_RECORD_KEY_RE = re.compile(r"(?<![\w/-])([A-Z][A-Z0-9]{1,9}-\d{1,7})\b|(?<![\w&])#(\d{1,7})\b")


def named_records(graph: Graph, repo: str, *texts: str) -> list[dict]:
    """The records Raven holds that these texts name by key, each with
    its status as the record gives it and, for one that was never adopted
    (cancelled, rejected, superseded), why it is history. Stated in the
    owner's message as facts rather than left to a brief or the evidence.
    Measured live on 63eb671: a question asked "as proposed in NET-102",
    and nothing in the message said NET-102 was cancelled."""
    keys: list[str] = []
    for text in texts:
        for m in _RECORD_KEY_RE.finditer(text or ""):
            key = m.group(1) or m.group(2)
            if key not in keys:
                keys.append(key)
    if not keys:
        return []
    from .store import repo_key
    scope = graph.resolve_repo(repo_key(repo)) if repo else ""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in graph.intents_by_ref(keys[:8], scope):
        kind, ref = _field(row, "kind"), _ref(row)
        if (kind, ref) in seen or kind in ("commit", "review"):
            continue
        seen.add((kind, ref))
        out.append({"ref": ref if not ref.isdigit() else f"#{ref}", "kind": kind,
                    "status": _field(row, "status").strip(), "title": _field(row, "title").strip(),
                    "void": _void(row)})
    order = {k if not k.isdigit() else f"#{k}": i for i, k in enumerate(keys)}
    return sorted(out, key=lambda r: order.get(r["ref"], 99))[:4]


def records_line(records: list[dict]) -> str:
    """The named records as one line for a person: key, status, title,
    and whether it is history."""
    parts = []
    for r in records:
        head = r["ref"] + (f" [{r['status']}]" if r.get("status") else "")
        title = f" \u201c{r['title']}\u201d" if r.get("title") else ""
        void = r.get("void") or ""
        tail = ((": not in force" + (f" ({void})" if void.lower() != (r.get("status") or "").lower() else "")
                 + ", so it is history, not current policy") if void else "")
        parts.append(head + title + tail)
    return "; ".join(parts)


def _is_settled(row) -> bool:
    """Eligible settled evidence, for every source kind; never approval.

    Explicit false and nonterminal/unknown states override optimistic defaults.
    """
    from .record_state import resolved_value, state_settled
    if _void(row) or not state_settled(_field(row, "status")):
        return False
    try:
        return resolved_value(row["resolved"])
    except (IndexError, KeyError):
        return True
    except ValueError:
        return False


def _ticket_state(row) -> str:
    """A record's status as the reader should see it, for any kind: a
    Jira issue posted as kind "jira" has a status too."""
    status = _field(row, "status").strip()
    return f" [{status}]" if status else ""


def _non_decision(text: str) -> bool:
    return bool(_UNRESOLVED_RE.search(text)) and not _DECIDED_RE.search(text)


def _parse_ts(created_at) -> float:
    return iso_to_ts(created_at)


def _norm_name(name: str) -> str:
    return (name or "").strip().lstrip("@").split()[0].lower() if (name or "").strip() else ""


def _memory_body(item) -> str:
    """What a memory item establishes: its answer plus the recorded
    rationale, which is where the why of a signed decision lives."""
    body = item.answer or ""
    if item.rationale:
        body += f"\nRationale: {item.rationale}"
    return body


def _as_record(item) -> dict:
    """A memory decision viewed as a record, for joint composition: kind
    'decision', its id as the reference, the signer as author."""
    return {"kind": "decision", "ref": item.id, "title": item.question,
            "decision_source_id": item.id, "decision_source_revision": ts_to_iso(item.updated_at),
            "body": _memory_body(item), "author": item.answered_by or "",
            "created_at": ts_to_iso(item.updated_at) if item.updated_at else "",
            "status": "", "resolved": 1}


def _selector_pick(cfg: Config, question: str, cands: dict[str, tuple[str, object]]):
    """One selector call over labeled candidates. Returns the picked
    (kind, item) or None; the model only ever selects from what it is
    shown."""
    import time as _time
    if not cands:
        return None
    lines = []
    for key, (kind, item) in cands.items():
        if kind == "memory":
            age = int(max(0, (_time.time() - item.updated_at) / 86400))
            label = (f"signed answer, {age}d old, by {item.answered_by}"
                     if item.source in ("human", "memory") and item.answered_by
                     else f"earlier resolution from {item.source or 'a record'}, {age}d old, unsigned")
            lines.append(f"{key} [{label}] Q: {item.question} A: {_memory_body(item)[:400]}")
        else:
            text = (item["title"] + " " + item["body"]).lower()
            void = _void(item)
            if void:
                status = f" [{void}, NOT ADOPTED: history of what was proposed or once held, not a decision]"
            elif not _is_settled(item):
                status = f" [{item['status'] or 'not done'}, STILL OPEN: this records a question, not a decision]"
            elif item["kind"] == "ticket" and _non_decision(text):
                status = " [status only]"
            else:
                status = _ticket_state(item)
            by = item["author"] or "unknown"
            lines.append(f"{key} [{item['kind']} {_ref(item)}, {item['created_at'] or 'undated'}, "
                         f"by/assignee {by}]{status} {item['title']}: {item['body'][:300]}")
    try:
        raw = llm_mod.Client(cfg).complete_json(
            "select", llm_mod.SELECTOR_SYSTEM,
            f"Question: {question}\n\nCandidates:\n" + "\n".join(lines))
    except llm_mod.LLMError:
        return None
    if not isinstance(raw, dict):
        return None
    return cands.get(str(raw.get("pick", "none")).strip().lower())


# The longest composed answer Raven will store. Composition is asked for
# one or two sentences (two to four across records); past these it is not
# an answer to sign, and it is never cut to fit.
COMPOSED_MAX = 1500
COMPOSED_JOINT_MAX = 2400


# A statement or open point about where the source came from rather than
# what it decided. Measured live on 63eb671: "Is record 5a1dba95d9eb a real
# reference or assumed" made a correct reuse of a signed rule partial.
_PROVENANCE_Q_RE = re.compile(r"\b(?:source|record|reference|citation|decision|ticket|author)\b[^.?]{0,80}\b(?:real|"
                              r"exist|genuine|assumed|authentic|from (?:another|which|whom)|written by|wrote|"
                              r"signed by|came from)\b|^(?:what|which|who) is the (?:source|author)\b|"
                              r"^(?:this|the) (?:rule|answer|policy|decision) (?:comes|came|is) from\b", re.IGNORECASE)
_SAYS_MISSING_RE = re.compile(r"\b(?:is|are) not (?:stated|established|specified|said|covered)\b|"
                              r"\bdoes not (?:establish|state|say|specify)\b|\bdo not (?:establish|state|say)\b",
                              re.IGNORECASE)


# Words that carry no concrete detail of their own in a prediction.
_PLAIN_WORDS = {"use", "uses", "using", "apply", "applies", "applied", "follow", "follows", "following", "followed",
                "choose", "chose", "chosen", "same", "keep", "keeps", "set", "make", "makes", "also", "like", "per",
                "pattern", "earlier", "answer", "answers", "decision", "decisions", "prior", "previous", "likely",
                "probably", "would", "should", "could", "consistent", "consistently", "similar", "similarly", "here",
                "case", "cases", "new", "this", "that", "these", "those", "their", "them", "they", "with", "from",
                "for", "and", "the", "its", "was", "were", "has", "have", "had", "been", "into", "onto", "over",
                "under", "both", "each", "every", "all", "any", "only", "just", "too", "than", "then", "there"}


def _novel_words(claim: str, *texts: str) -> list[str]:
    """The words of a claim that none of the texts contain, stemmed, less
    the ones that carry no concrete detail."""
    token = r"[a-z0-9_]+(?:\.[a-z0-9_]+)*"
    have = {llm_mod.stem(w) for t in texts for w in re.findall(token, (t or "").lower())}
    return [w for w in re.findall(token, (claim or "").lower())
            if len(w) > 2 and w not in _PLAIN_WORDS and llm_mod.stem(w) not in have]


def _given(context: str = "", facts: dict | None = None, also: str = "") -> str:
    """What the question's own context and stated facts establish, for the
    support check: an answer may rely on them."""
    parts = []
    if (context or "").strip():
        parts.append("Context: " + " ".join(context.split())[:1500])
    if facts:
        parts.append("Stated facts: " + ", ".join(f"{k}={v}" for k, v in sorted(facts.items())))
    if also:
        parts.append(also)
    return "\n".join(parts)


def _supported(cfg: Config, question: str, source: str, text: str, what: str = "the record",
               given: str = "", prediction: bool = False) -> str | None:
    """The composed answer with what its source does not establish split
    out and named, so it reads as answered in part; None when the source
    supports none of it; the text unchanged when the check cannot run.
    What the question, its context and stated facts say, and who wrote
    the source, are given: measured live on 63eb671, a question that
    called timeout_slack a numeric option got an answer saying that was
    not established, and a signed rule's reuse asked whether the decision
    it came from was real."""
    if not cfg.semantic_retrieval or not (text or "").strip():
        return text
    checked = llm_mod.check_support(cfg, question, source, text, given, prediction=prediction)
    if checked is None:
        return text
    inferred = [str(c.get("claim") or "").strip() for c in checked["claims"]
                if isinstance(c, dict) and str(c.get("support") or "").strip().lower() == "inferred"]
    inferred = [c for c in inferred if c]
    if prediction and inferred and not _novel_words(text, source, question, given):
        # A prediction made only of what their answers, the question and the
        # given already contain carries their pattern over and invents
        # nothing. Measured live: "Use retry_normal_total" for enterprise
        # customers, from their answer for normal customers, was sometimes
        # called invented. One that brings in a new word keeps the verdicts.
        inferred = []
    # One open point per statement taken out, and only those; when the
    # model wrote more or fewer, the statements themselves are named.
    # Measured live on 63eb671: a who-and-why answer came back with two new
    # questions about motives nobody asked. Provenance is never open.
    open_points = [" ".join(str(x).split()).rstrip("?.") for x in (checked.get("open") or []) if str(x).strip()]
    if len(open_points) == len(inferred):
        pairs = [(c, o) for c, o in zip(inferred, open_points)
                 if not (_PROVENANCE_Q_RE.search(c) or _PROVENANCE_Q_RE.search(o))]
    else:
        pairs = [(c, c.rstrip(".")) for c in inferred if not _PROVENANCE_Q_RE.search(c)]
    if not pairs:
        return text
    kept = " ".join(str(checked.get("answer") or "").split())
    # What the source leaves open is named once, below: a statement of what
    # is missing the check wrote itself is not kept, the answer's own are.
    original = " ".join((text or "").split()).lower()
    kept = " ".join(x for x in re.split(r"(?<=[.!?])\s+", kept)
                    if x and (not _SAYS_MISSING_RE.search(x) or x.lower().rstrip(".") in original))
    if not kept or llm_mod.looks_meta(kept):
        return None
    named = [o for _, o in pairs]
    verb = "do not" if what.endswith("s") else "does not"
    return f"{kept.rstrip()} {what[0].upper() + what[1:]} {verb} establish: {'; '.join(named[:4])}."


def _compose_answer(cfg: Config, question: str, citation: str, body: str, author: str = "",
                    given: str = "") -> str | None:
    """Grounded composition. None when the composer reports the record
    does not answer. Without a backend, retrieval alone cannot establish
    that a record body answers the question, so it remains context."""
    if not cfg.semantic_retrieval:
        return None
    try:
        text = llm_mod.Client(cfg).complete(
            "compose", llm_mod.COMPOSER_SYSTEM,
            f"Question: {question}\n\nRecord ({citation}, author/assignee: {author or 'unknown'}):\n{body}").strip()
        # A refusal, not an answer that says what it leaves open. Measured
        # live on 63eb671: any answer containing "the records do not" was
        # thrown away, and a supported partial answer with it.
        if "RECORD DOES NOT ANSWER" in text or "RECORDS DO NOT ANSWER" in text or _is_pure_refusal(text):
            return None
        if not text or llm_mod.looks_meta(text):
            return None
        # Whole or not at all. Measured live: answers were cut at 700
        # characters, mid-sentence, and stored as the answer; a cut can drop
        # the very condition that makes an answer right. One that runs this
        # long has stopped being the one or two sentences asked for.
        if len(text) > COMPOSED_MAX:
            return None
        return _supported(cfg, question, f"({citation}, author/assignee: {author or 'unknown'})\n{body}", text,
                          "the earlier answer" if citation.startswith("earlier answer") else "the record", given)
    except llm_mod.LLMError:
        return None


ASSUME_GAP = "NO PATTERN"

_GAP_ASSUME_RE = re.compile(
    r"no assumption|cannot be (?:derived|inferred)|can't be (?:derived"
    r"|inferred)|does(?:n't| not) set a pattern|(?:cannot|can't) infer|assumption gap")


def _compose_assumption(cfg: Config, question: str, citation: str, body: str) -> str:
    if not cfg.semantic_retrieval:
        return ""
    try:
        text = llm_mod.Client(cfg).complete(
            "assume", llm_mod.ASSUME_COMPOSER_SYSTEM,
            f"Question: {question}\n\nPrecedent ({citation}):\n{body}").strip()
        if text.upper().startswith(ASSUME_GAP) or _GAP_ASSUME_RE.search(text.lower()):
            return ASSUME_GAP
        if not text or llm_mod.looks_meta(text) or len(text) > 420:
            return ""
        return text
    except llm_mod.LLMError:
        return ""


_OWNERSHIP_Q_RE = re.compile(
    r"\b(?:blame|code ?owners?)\b[^?]*\b(?:share|percent|%|largest|hold"
    r"|holds|split|belong)|\b(?:share|percent|%|largest|holds?|split)"
    r"\b[^?]*\b(?:blame|code ?owners?)\b")
_OWNERSHIP_PROPOSAL_RE = re.compile(r"\bshould\b|\bupdat|\bchang|\badd(?:ing)?\b|\bremov|\brewrit")
_OWNERSHIP_STATUS_RE = re.compile(
    r"\bticket|assign|closed|sign(?:ed)? ?off|reviewer|open ticket|status|fix|nuisance|whose call\b")
_ROUTING_Q_RE = re.compile(
    r"\bwho(?:m|se)?\b[^?]*\bshould\b[^?]*\b(?:route|own|owns|decide|"
    r"decides|approve|approves|sign|signs|handle|handles|escalat)"
    r"|\bshould\b[^?]*\broute\b[^?]*\bto\b|\bwhose\s+call\b|\bwho\s+should\s+(?:bridge|raven)\b")
_ACCOUNTABILITY_Q_RE = re.compile(
    r"\bwho(?:'?s| is| are| holds?| has)\b[^?]*\b(?:accountable|responsib|the call|on the hook)"
    r"|\bwhose\b[^?]*\b(?:call|responsib|accountab)"
    r"|\bwho\b[^?]*\bowns\b"
    # The present tense of the same question, which is how an agent
    # usually puts it. The past tense ("who decided", "who approved") is
    # retrospective and belongs to the decision it asks about, so only
    # the present forms are here.
    r"|\bwho\b[^?]*\b(?:decides|approves|signs?\s+off|has\s+the\s+final\s+say)\b"
    r"|\bwho\b[^?]*\b(?:do|should)\s+(?:i|we)\s+(?:ask|talk\s+to|check\s+with)\b"
    r"|\bwho\s+to\s+ask\b"
    r"|\baccountable\s+(?:owner|human|person|party|engineer)\b"
    r"|\bwho\b[^?]*\b(?:most active|go-?to|primary|lead)\s+reviewer\b"
    r"|\bwho\b[^?]*\breviews\s+(?:the\s+)?(?:most|these)\b"
    r"|\bcan\b[^?]*\bapprove\b[^?]*\balone\b")
_STATUS_CLAUSE_RE = re.compile(
    r"\bhas\b[^?]*\bbeen\b|\bhave\b[^?]*\bbeen\b|\bcleaned up\b"
    r"|\bis it (?:done|complete|resolved|closed|fixed|ready)\b"
    r"|\b(?:been )?completed?\b|\bshipped\b|\brolled out\b"
    r"|\bsigned off yet\b|\bstill open\b|\bclosed yet\b")


def _ownership_answer(store: Graph, repo: str, question: str) -> str:
    qlow = question.lower()
    qterms = {llm_mod.stem(t) for t in re.findall(r"[a-z0-9]+", qlow) if len(t) > 2}
    all_rows = list(store.db.execute("SELECT * FROM ownership WHERE repo=? AND valid_to IS NULL", (repo,)))
    if not all_rows:
        return ""

    def _p_tokens(prefix: str) -> list[str]:
        return [llm_mod.stem(t) for t in re.split(r"[/_.\-]", prefix.lower()) if len(t) > 2]

    named = [r for r in all_rows if r["path_prefix"] not in ("", "*")
             and (r["path_prefix"].strip("/").lower() in qlow
                  or any(t in qterms for t in _p_tokens(r["path_prefix"])))]
    verbatim = [r for r in named if r["path_prefix"].strip("/").lower() in qlow]
    rows = verbatim or named
    if not rows:
        return ""
    rows.sort(key=lambda r: -(r["weight"] or 0))
    parts, seen = [], set()
    for r in rows[:12]:
        key = (r["engineer"], r["source"], r["path_prefix"])
        if key in seen:
            continue
        seen.add(key)
        where = r["path_prefix"] or "repo-wide"
        if r["source"] == "blame":
            parts.append(f"{r['engineer']}: {round((r['weight'] or 0) * 100)}% blame under {where}")
        elif r["source"] == "blame_recent":
            parts.append(f"{r['engineer']}: {round((r['weight'] or 0) * 100)}% recent blame under {where}")
        elif r["source"] == "codeowners":
            parts.append(f"{r['engineer']}: CODEOWNERS-listed for {where}")
        elif r["source"] == "review":
            parts.append(f"{r['engineer']}: {round((r['weight'] or 0) * 100)}% of reviews")
        elif r["source"] == "user":
            parts.append(f"{r['engineer']}: recorded owner for {where} (told to Raven directly)")
    if not parts:
        return ""
    scope = rows[0]["path_prefix"] or "the repo"
    return f"From the indexed ownership graph for {scope}: " + "; ".join(parts[:8]) + "."


_FOLLOWUP_REF_RE = re.compile(
    r"\byour (?:first |previous |earlier |own |latest )?answer"
    r"|\byou (?:just |previously |earlier )?(?:said|cited|answered|claimed|stated|recommended|gave|named)"
    r"|\bthe answer (?:above|you|bridge|raven)|\b(?:bridge|raven)(?:'s)? (?:answer|claim)"
    r"|\b(?:that|this|the) same\b|\bthe above\b|\bif so\b|\bgiven (?:that|this|your)\b"
    r"|\b(?:does|is|was|would|will) (?:that|it|this)\b"
    r"|\bwhat if\b|\binstead\b|\bthat (?:pause|rule|change|policy|decision|exception|ceiling|cap)\b"
    r"|\bsame (?:customer|rule|rounding)\b|\bstill in effect\b")


def _contextualize(cfg: Config, question: str, recent: list) -> str:
    if not recent:
        return ""
    ctx = "\n".join(f"- Q: {p.question[:200]} A: {(p.answer or '')[:300]}" for p in recent)
    try:
        raw = llm_mod.Client(cfg).complete_json(
            "followup", llm_mod.FOLLOWUP_CONTEXT_SYSTEM,
            f"Raven's recent answers, newest first:\n{ctx}\n\nNew question: {question}")
    except llm_mod.LLMError:
        return ""
    if not isinstance(raw, dict) or not raw.get("needs_context"):
        return ""
    rewritten = str(raw.get("question", "")).strip()
    if (not rewritten or rewritten == question or len(rewritten) > 600 or llm_mod.looks_meta(rewritten)):
        return ""
    return rewritten


def _expand_query(cfg: Config, question: str) -> list[str]:
    try:
        raw = llm_mod.Client(cfg).complete_json("expand", llm_mod.EXPAND_SYSTEM, f"Question: {question}")
    except llm_mod.LLMError:
        return []
    if isinstance(raw, dict):
        raw = raw.get("phrases", [])
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for p in raw[:8]:
        p = str(p).strip()
        if p and len(p) < 60 and p not in out:
            out.append(p)
    return out


def _compose_joint(cfg: Config, question: str, rows: list, given: str = ""):
    """Grounded composition across several records (and memories viewed as
    records) at once. Returns (answer, citation, partial) or None; partial
    is true when the answer leaves part of the question open."""
    rows = [r for r in rows if r is not None]
    left_out: list[str] = []
    if not _HISTORY_Q_RE.search(question):
        # What was not adopted is history, left out unless the question
        # asks about history. Measured live: given a cancelled proposal
        # marked NOT ADOPTED, the composer still reversed which option it
        # had rejected, and the owner was asked to sign that.
        left_out = [f"{r['kind']} {_ref(r)}{_ticket_state(r)}" for r in rows if _void(r)]
        rows = [r for r in rows if not _void(r)]
    rows = rows[:4]
    if len(rows) < 2 or not cfg.semantic_retrieval:
        return None
    if _ROUTING_Q_RE.search(question.lower()):
        return None
    labeled, cits = [], []
    for i, r in enumerate(rows, 1):
        cit = f"{r['kind']} {_ref(r)}{_ticket_state(r)}"
        cits.append(cit)
        labeled.append(
            f"r{i} ({cit}, author/assignee {r['author'] or 'unknown'}, {r['created_at'] or 'undated'}"
            + (f", NOT ADOPTED ({_void(r)}): history only" if _void(r) else "" if _is_settled(r) else ", STILL OPEN")
            + f"): {r['title']}\n{(r['body'] or '')[:900]}")
    try:
        text = llm_mod.Client(cfg).complete(
            "compose_joint", llm_mod.JOINT_COMPOSER_SYSTEM + "\nBefore COVERAGE, output USED_SOURCES: r1,r2 listing exactly the input records actually used. Omit rejected or irrelevant records.",
            f"Question: {question}\n\nRecords:\n" + "\n\n".join(labeled)).strip()
    except llm_mod.LLMError:
        return None
    if (not text or "RECORDS DO NOT ANSWER" in text or "RECORD DOES NOT ANSWER" in text
            or _is_pure_refusal(re.sub(r"\s*COVERAGE:\s*(?:FULL|PARTIAL)\W*$", "", text, flags=re.IGNORECASE))
            or llm_mod.looks_meta(text)):
        return None
    # The composer says whether the records answer every part. Measured
    # live: an answer that qualified its own conclusion was stored as
    # resolved.
    coverage = re.search(r"\s*COVERAGE:\s*(FULL|PARTIAL)\W*$", text, re.IGNORECASE)
    partial = False
    if coverage:
        partial = coverage.group(1).upper() == "PARTIAL"
        text = text[:coverage.start()].rstrip()
    if not text:
        return None
    used = re.search(r"(?:^|\n)USED_SOURCES:\s*([r0-9, ]+)\s*(?:$|\n)", text)
    if not used:
        return None
    if not re.fullmatch(r'r[1-9]\d*(?:\s*,\s*r[1-9]\d*)*', used.group(1).strip()):
        return None
    labels = re.findall(r"r(\d+)", used.group(1))
    if not labels or any(not 1 <= int(n) <= len(rows) for n in labels):
        return None
    selected = sorted({int(n) - 1 for n in labels})
    text = (text[:used.start()] + text[used.end():]).strip()
    if _is_pure_refusal(text):
        return None
    rows = [rows[i] for i in selected]
    cits = [cits[i] for i in selected]
    labeled = [labeled[i] for i in selected]
    text = re.sub(r"\s*\((?:r\d)(?:,\s*r\d)*\)", "", text)
    text = re.sub(r"\s*\([^()]*\br\d\b[^()]*\)", "", text)
    text = re.sub(r"\s*\b(?:and |in |per |from )?r\d\b", "", text)
    # Whole or not at all, as for one record (it was cut at 800).
    if len(text) > COMPOSED_JOINT_MAX:
        return None
    # Measured live on 5e967e4: a metric label and a rollout region were
    # asked, the records named the label only, and the composition proposed
    # a primary region none of them names.
    text = _supported(cfg, question, "\n\n".join(labeled), text, "the records", given)
    if text is None:
        return None
    cited = "; ".join(cits) + (f"; left out, not adopted: {', '.join(left_out)}" if left_out else "")
    return text, cited, partial or _states_a_gap(text), rows


_CHANGE_VERBS = {"add", "remov", "remove", "fix", "use", "make", "drop", "move", "updat", "update", "chang", "change",
                 "set", "get", "allow", "avoid", "keep", "enabl", "enable", "disabl", "disable", "support",
                 "implement", "handl", "handle", "introduc", "introduce", "convert", "replac", "replace", "renam",
                 "rename", "refactor", "clean", "cleanup", "simplifi", "simplify", "check", "improv", "improve",
                 "expos", "expose", "extract", "factor", "split", "merg", "merge", "new", "old", "also", "only"}


def _background_terms(question: str) -> set[str]:
    """Words that name where a change is, not what it decides: the
    components of any path or filename in the question, and the verbs
    every change record uses."""
    out: set[str] = set(_CHANGE_VERBS)
    for tok in re.findall(r"[A-Za-z0-9_./-]*[/.][A-Za-z0-9_./-]*", question or ""):
        for piece in re.split(r"[/._-]", tok.lower()):
            if len(piece) > 2:
                out.add(llm_mod.stem(piece))
                out.add(piece)
    return out


# An action request, not a mention of an API, undo entry or rollback log.
# This only limits what retrieved evidence can authorize; never retrieval.
_REVERSAL_REQUEST_RE = re.compile(
    r"^\s*(?:(?:should|shall|can|could|may|must)\s+(?:we|i)\s+"
    r"|do\s+(?:we|i)\s+need\s+to\s+|please\s+)?"
    r"(?:revert|undo|roll\s?back|back\s+out)\s+\S", re.IGNORECASE)
_PROPOSAL_Q_RE = re.compile(r"^\s*(?:should|shall|can|could|may|do we|is it (?:ok|fine|safe)|ok to)\b")
_IDENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[_.][A-Za-z0-9]+)+|[A-Za-z]+\d+[A-Za-z0-9]*|[A-Z]{2,}[A-Z0-9]*(?=\b)")
_FILE_EXT_RE = re.compile(r"\.(?:c|h|cc|cpp|hpp|inc|py|js|ts|tsx|jsx|rs|go|java|kt|rb|md|rst|json|ya?ml|toml|txt|sh|nix|mjs|cjs)$",
                          re.IGNORECASE)


def _question_identifiers(text: str, display: bool = False) -> list[str]:
    """The identifiers a question names (FEAT_NMI, PSTATE.ALLINT, HCR_EL2,
    mark_vs_dirty): a record that decided the question mentions them.
    Paths and filenames are not identifiers here; a record names a file
    its own way."""
    out: list[str] = []
    for tok in _IDENT_RE.findall(text or ""):
        if "/" in tok or _FILE_EXT_RE.search(tok) or len(tok) < 4 or tok.upper() == tok and len(tok) < 4:
            continue
        key = tok if display else tok.lower()
        if key not in out:
            out.append(key)
    return out[:6]


def _meaningful_terms(text: str) -> list[str]:
    stop = {"the", "and", "for", "this", "that", "does", "with", "from", "are",
            "what", "which", "when", "how", "who", "into", "get", "one"}
    out: list[str] = []
    for t in re.findall(r"[a-z0-9]+", text.lower()):
        if len(t) > 2 and t not in stop:
            s = llm_mod.stem(t)
            if s not in out:
                out.append(s)
    return out


def _word_trunc(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    cut = text[:n]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "..."


def _display_terms(text: str, stems: list[str]) -> list[str]:
    orig: dict[str, str] = {}
    for t in re.findall(r"[a-z0-9]+", text.lower()):
        orig.setdefault(llm_mod.stem(t), t)
    return [orig.get(s, s) for s in stems]


_REF_RE = re.compile(r"(?:#|\b(?:pr|pull request|issue)\s*#?)(\d{2,6})\b", re.IGNORECASE)
_REF_TOKEN_RE = re.compile(r"\b([A-Z][A-Za-z0-9]{1,9}-\d{2,6})\b")


def _cited_refs(text: str) -> list[str]:
    seen: list[str] = []
    for m in _REF_RE.findall(text):
        if m not in seen:
            seen.append(m)
    for m in _REF_TOKEN_RE.findall(text):
        if m not in seen:
            seen.append(m)
    return seen


def _ref_variants(refs: list[str]) -> list[str]:
    out: list[str] = []
    for r in refs:
        for v in (r, r.rsplit("-", 1)[-1] if "-" in r else r):
            if v and v not in out:
                out.append(v)
    return out


_RETRO_Q_RE = re.compile(r"\b(?:who|why)\b|\bdecided\b|\breason\b|\bwhat was\b", re.IGNORECASE)

_ATTRIBUTION_Q_RE = re.compile(
    r"\bwho\b[^?]*\b(?:decided|approved|signed|authored|made)\b"
    r"|\b(?:why|reason)\b[^?]*\bdecision\b", re.IGNORECASE)
_DECISION_ID_RE = re.compile(r"\b[0-9a-f]{12}\b", re.IGNORECASE)


def _historical_decision(store: Graph, question: str, repo: str):
    """Return a recorded human answer, or an explicit-ID lookup failure.

    Attribution is a read of a specific record, not permission to apply
    that policy to new work. Never substitute another record for an ID.
    Natural queries rank the answer and rationale as well as the title.
    """
    if not _ATTRIBUTION_Q_RE.search(question):
        return None, ""
    refs = list(dict.fromkeys(_DECISION_ID_RE.findall(question.lower())))
    if refs:
        if len(refs) != 1:
            return None, "Ask about one decision ID at a time; no other decision was substituted."
        item = store.get_decision(refs[0], exact=True)
        visible = store.db.execute("SELECT draft FROM decisions WHERE id=? AND repo=?", (refs[0], repo)).fetchone()
        if item is None or item.repo != repo or visible is None or visible["draft"]:
            return None, "The requested decision is not available in this repository; no other decision was substituted."
        if not item.authorized or not item.answered_by:
            return None, "The requested decision has no current authorized human answer; no other decision was substituted."
        return item, ""
    terms = set(_meaningful_terms(question)) - _ASK_MACHINERY - _RETRO_MACHINERY
    if len(terms) < 2:
        return None, ""
    ranked = []
    identifiers = re.findall(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b", question)
    for _, item in store.similar_answered(llm_mod.embed(question), top_k=64, min_score=0, repo=repo, query=question):
        if not item.authorized or not item.answered_by or item.source == "memory":
            continue
        body = item.question + " " + _memory_body(item)
        if any(not re.search(r"(?<!\w)" + re.escape(ident) + r"(?!\w)", body, re.IGNORECASE)
               for ident in identifiers):
            continue
        overlap = len(terms & set(_meaningful_terms(body))) / len(terms)
        if overlap >= 0.75 and not _unmet_contrast([question], [body]):
            ranked.append((overlap, item))
    ranked.sort(key=lambda pair: -pair[0])
    if ranked and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] > CONFLICT_MARGIN):
        return ranked[0][1], ""
    return None, ""


def guess_category(question: str) -> str:
    """A category for a question that arrives without one: retrospective
    who/why questions are data-source, everything else policy."""
    if _RETRO_Q_RE.search(question) and not _ROUTING_Q_RE.search(question.lower()) \
            and not _ACCOUNTABILITY_Q_RE.search(question.lower()):
        return "data-source"
    return "policy"


def _ranked_view(ranked: list[tuple[str, list[str], float]]) -> list[dict]:
    """The routing candidates as reported: the top few with their first
    evidence lines, so a caller sees the route that produced the owner."""
    return [{"owner": o, "evidence": list(ev[:3]), "score": round(s, 4)} for o, ev, s in ranked[:RANKED_KEEP]]


def _kind_for(status: str, source: str = "", mkind: str = "") -> str:
    if status in ("resolved", "partial"):
        return "prediction" if mkind == "semantic" else "evidence"
    if status in ("assumed", "proposed"):
        return "prediction"
    return "new"


def run_task(store: Graph, cfg: Config, title: str, repo: str = "",
             decisions: list[dict] | None = None,
             run_id: str | None = None, keep_draft: bool = False, scratch: bool = False) -> RunResult:
    """Run the ladder for one task. decisions: the decisions to resolve
    (dicts with question, category, and optionally context, path,
    options). run_id: an existing inbox run to attach the decisions to;
    otherwise a run is created. Each decision is written as a draft and
    published when it is resolved or routed; with keep_draft the caller
    publishes, once it has finished placing the node on its tree."""
    client = llm_mod.Client(cfg)
    task_id = run_id or store.create_task(title, repo=repo or "local")
    result = RunResult(task_id=task_id, title=title)
    decisions = [d for d in (decisions or []) if isinstance(d, dict) and d.get("question")]

    for d in decisions:
        question = d["question"].strip()
        category = str(d.get("category", "") or "").strip() or guess_category(question)
        d_context = str(d.get("context", "") or "")
        d_path = str(d.get("path", "") or "unknown")
        d_requester = str(d.get("requester", "") or "")
        # The decision category as the agent named it, for the authority
        # map; the ladder's own category (policy, definition) is not one.
        d_category = str(d.get("category", "") or "").strip()
        d_hints = [str(h) for h in (d.get("hints") or []) if str(h).strip()]
        # The other paths a decision names beside its first: the authority
        # map is read for every one of them.
        d_also = [str(p) for p in (d.get("also_paths") or []) if str(p).strip()]
        d_facts = parse_facts(d.get("facts"))
        asked_q = question
        if cfg.deep_retrieval and _FOLLOWUP_REF_RE.search(question.lower()):
            rewritten = _contextualize(cfg, question, store.recent_answered(repo=repo))
            if rewritten:
                question = rewritten
        emb = llm_mod.embed(question)
        # The row exists from here; it stays a draft, which no reader
        # sees, until it is routed (and, on the canvas, placed on the
        # tree). Nobody reads a decision that has no owner yet only
        # because the write is still in flight.
        did = store.add_decision(
            task_id, asked_q, category, OPEN,
            embedding=llm_mod.embed(asked_q) if asked_q != question else emb,
            options=[str(o).strip() for o in (d.get("options") or []) if str(o).strip()][:4],
            repo=repo, context=d_context, path=d_path, kind="new", draft=True)
        result.drafts.append(did)
        if scratch:
            store.db.execute("UPDATE decisions SET client_ref='__model_pass__' WHERE id=?", (did,))
        if question != asked_q:
            store.append_event("followup_context", {"task_id": task_id, "decision_id": did,
                                                    "asked": asked_q, "resolved_as": question})
        if d_facts:
            store.db.execute("UPDATE decisions SET facts=? WHERE id=?", (json.dumps(d_facts, sort_keys=True), did))
        historical, missing_history = _historical_decision(store, asked_q, repo)
        if historical is not None:
            answer = (f"{historical.answered_by} recorded the answer in decision {historical.id}:\n"
                      f"{historical.answer}\n\nRecorded rationale: "
                      f"{historical.rationale or 'No rationale was recorded.'}")
            evidence = f"historical attribution from decision {historical.id}; not authorization for new work"
            store.publish_evidence(did, [_as_record(historical)], status="resolved", source="memory", kind="evidence", answer=answer,
                                  answered_by=historical.answered_by, source_id=historical.id,
                                  source_revision=ts_to_iso(historical.updated_at), evidence=evidence)
            store.add_link(did, historical.id, "derived", "historical attribution of this recorded answer")
            store.append_event("resolve", {"task_id": task_id, "decision_id": did, "source": "memory",
                                           "cited": historical.id, "kind": "historical-attribution"})
            result.resolved.append({"id": did, "question": asked_q, "source": "memory", "answer": answer,
                                    "partial": False, "cited": historical.id})
            continue
        if missing_history:
            store.update_decision(did, evidence=missing_history)
            result.open.append({"id": did, "question": asked_q, "owner": None, "note": missing_history})
            continue
        why_open: list[str] = []
        # A signed answer and a settled record that disagree on a figure:
        # set when that is found, and then neither is served or proposed.
        conflict = None
        memory_used_rows = []

        # a. memory
        mem_pick = None
        reranked = ""
        accepted: list = []
        floor = NEAR_MEMORY_MIN
        # The asking machinery (who decided, what was the reason, still in
        # effect) is not subject matter: it must not dilute the overlap
        # with an answer about the same thing.
        qt = set(_meaningful_terms(question)) - _ASK_MACHINERY
        q_ref_res = [re.compile(r"(?<![A-Za-z0-9])" + re.escape(r) + r"(?![0-9])", re.IGNORECASE)
                     for r in _cited_refs(question)]
        fup = bool(q_ref_res or _FOLLOWUP_REF_RE.search(question.lower()))

        def _hybrid(sim: float, p) -> float:
            blob = (p.question + " " + (p.answer or "") if fup else p.question)
            pt = set(_meaningful_terms(blob))
            ov = (len(qt & pt) / min(len(qt), len(pt)) if qt and pt else 0.0)
            if q_ref_res and any(rx.search(blob) for rx in q_ref_res):
                ov = max(ov, 0.85)
            return max(sim, ov)

        raw = store.similar_answered(emb, top_k=8, min_score=0.25, repo=repo, query=question)
        if q_ref_res:
            have = {p.id for _, p in raw}
            for p in store.recent_answered(repo=repo, limit=12):
                if p.id not in have and any(rx.search(p.question + " " + (p.answer or "")) for rx in q_ref_res):
                    raw.append((0.3, p))
        # A retrospective question (who decided, what was the reason) asks
        # about a past decision, so its age is no reason to prefer another:
        # recency ranks what stands now, not what was decided then. Found
        # when a test's dates aged: an April decision decayed below an
        # August one on a question about the April decision.
        aged = (lambda p: 1.0) if guess_category(question) == "data-source" else (lambda p: _recency(p.updated_at))
        scored = sorted(((_hybrid(s, p) * aged(p), _hybrid(s, p), p) for s, p in raw),
                        key=lambda t: -t[0])
        accepted = [(eff, s, p) for eff, s, p in scored if eff >= floor]
        if accepted:
            eff, sim, past = accepted[0]
            pt = set(_meaningful_terms(past.question))

            def _same_decision(p2) -> bool:
                qt2 = set(_meaningful_terms(p2.question))
                if not pt or not qt2:
                    return False
                return len(pt & qt2) / min(len(pt), len(qt2)) >= 0.7

            near_even = {p.id for e2, _, p in accepted[1:] if eff - e2 <= CONFLICT_MARGIN}
            rivals = [p for _, _, p in scored
                      if p.id != past.id and p.answer != past.answer
                      and (p.id in near_even or _same_decision(p))]
            # A retrospective question (who decided, what was the reason)
            # asks about the decision it matched, so a newer answer on
            # the same subject does not replace it.
            if rivals and guess_category(question) != "data-source":
                pool = [past] + rivals
                pool.sort(key=lambda p: -p.updated_at)
                winner = pool[0]
                store.append_event("conflict_resolved", {
                    "task_id": task_id, "decision_id": did, "kept": winner.id,
                    "retired": [p.id for p in pool[1:]], "rule": "newest answer wins"})
                past = winner
            mem_pick = (eff, sim, past)
        elif scored:
            # The cheap score decides what is worth reading, not what is
            # worth reusing. Where nothing clears the floor, the near
            # misses are shown to the model rather than dropped unseen:
            # a paraphrase that shares few words with the original scores
            # below it, and so does a decision about a different column,
            # and the score alone cannot tell those apart. What comes
            # back enters as a near match, never as a direct hit, and
            # still faces the contrast and composition checks below.
            near = [(eff2, s2, p) for eff2, s2, p in scored if eff2 >= RERANK_MIN][:4]
            picked_id, picked_why = ("", "")
            if near and cfg.semantic_retrieval:
                picked_id, picked_why = llm_mod.rerank_memories(
                    cfg, question, [{"id": p.id, "score": eff2, "question": p.question, "answer": p.answer}
                                    for eff2, _s2, p in near])
            hit = next(((eff2, s2, p) for eff2, s2, p in near if p.id == picked_id), None)
            if hit is not None:
                mem_pick = (max(hit[0], floor), hit[1], hit[2])
                reranked = hit[2].id
                store.append_event("memory_reranked", {"task_id": task_id, "decision_id": did,
                                                       "memory": hit[2].id, "scored": round(hit[0], 2),
                                                       "why": picked_why})
            else:
                why_open.append(f"memory: best match scored {scored[0][0]:.2f}, under the {floor} floor"
                                + ("; read anyway, and it does not answer this" if near and cfg.semantic_retrieval
                                   else ""))
        else:
            why_open.append("memory: nothing similar answered before")

        if mem_pick is not None:
            applies, reason = applicability_status(mem_pick[2], d_path, d_facts)
            if not applies:
                why_open.append(f"memory: {reason}; earlier answer {mem_pick[2].id} is context, not an answer here: "
                                + _excerpt(mem_pick[2].answer, 240, "the earlier answer has the rest"))
                store.append_event("applicability_unmet", {"task_id": task_id, "decision_id": did,
                                                            "memory": mem_pick[2].id, "why": reason})
                mem_pick = None

        # A memory hit preempts the graph rungs only when it is itself an
        # answer about ownership; a nearby answer about the VALUE of a
        # setting must not swallow "whose call is it to change it".
        mem_about_ownership = mem_pick is not None and bool(
            _ACCOUNTABILITY_Q_RE.search(mem_pick[2].question.lower())
            or _ROUTING_Q_RE.search(mem_pick[2].question.lower())
            or _OWNERSHIP_Q_RE.search(mem_pick[2].question.lower()))

        # a2. ownership introspection
        if ((mem_pick is None or not mem_about_ownership) and repo and _OWNERSHIP_Q_RE.search(question.lower())
                and not _OWNERSHIP_PROPOSAL_RE.search(question.lower())
                and not _OWNERSHIP_STATUS_RE.search(question.lower())
                and not _cited_refs(question)):
            own_ans = _ownership_answer(store, repo, question)
            if own_ans:
                store.update_decision(did, status="resolved", source="record", answer=own_ans,
                                      kind="evidence",
                                      evidence="ownership graph (indexed blame and CODEOWNERS)")
                store.append_event("resolve", {"task_id": task_id, "decision_id": did, "source": "record",
                                               "cited": "ownership graph", "kind": "ownership-introspection"})
                result.resolved.append({"id": did, "question": question, "source": "record",
                                        "answer": own_ans, "partial": False, "cited": "ownership graph"})
                continue

        # a3. accountability
        if ((mem_pick is None or not mem_about_ownership) and repo
                and (_ROUTING_Q_RE.search(question.lower()) or _ACCOUNTABILITY_Q_RE.search(question.lower()))
                and not _STATUS_CLAUSE_RE.search(question.lower())
                # A proposal to change the ownership data itself falls
                # through; "whose call is it to change X" is still an
                # accountability question about X.
                and not (_OWNERSHIP_PROPOSAL_RE.search(question.lower())
                         and re.search(r"code ?owners?|ownership|\bowner\b", question.lower()))
                and not _cited_refs(question)):
            acc_notes: list[str] = []
            acc_ranked = route_ranked(store, repo, question,
                                      path=d_path if d_path != "unknown" else "", context=d_context,
                                      notes=acc_notes, requester=d_requester, hints=d_hints,
                                      category=d_category, also_paths=d_also, facts=d_facts,
                                      task_id=task_id, decision_id=did)
            acc = acc_ranked[0] if acc_ranked else None
            if acc is None and acc_notes:
                why_open.append("ownership: " + "; ".join(acc_notes))
            if acc is not None:
                a_owner, a_ev, a_score = acc
                approval = ""
                for ln in a_ev:
                    m_ap = re.search(r"CODEOWNERS lists ([^;]+?) here", ln)
                    if m_ap and _norm_name(m_ap.group(1)) != _norm_name(a_owner):
                        approval = (f" Sign-off per CODEOWNERS rests with {store.resolve_engineer(m_ap.group(1))}; "
                                    f"{a_owner} is the live author to consult.")
                        break
                answer = f"{a_owner} is the accountable owner. " + " ".join(a_ev) + approval
                store.update_decision(did, status="resolved", source="record", answer=answer,
                                      owner=a_owner, owner_evidence="; ".join(a_ev), kind="evidence",
                                      evidence="resolved from the ownership graph (blame, CODEOWNERS, "
                                               "and review signals reconciled)")
                store.append_event("resolve", {"task_id": task_id, "decision_id": did, "source": "record",
                                               "cited": "ownership graph", "kind": "accountability",
                                               "owner": a_owner})
                result.resolved.append({"id": did, "question": question, "source": "record",
                                        "answer": answer, "partial": False, "cited": "ownership graph",
                                        "ranked": _ranked_view(acc_ranked)})
                continue

        # b. records. A who-should-approve question the graph could not
        # route is not answered by a record that shares its words: it
        # goes to the human rung, which says Raven does not know.
        rec_best, rec_pool, retro_cand = None, [], None
        ref_rows: list = []
        matches: list = []
        routing_q = bool(_ROUTING_Q_RE.search(question.lower()) or _ACCOUNTABILITY_Q_RE.search(question.lower()))
        reversal_request = bool(_REVERSAL_REQUEST_RE.search(question))
        proposal_q = bool(_PROPOSAL_Q_RE.search(question.lower()))
        if not routing_q:
            terms = _meaningful_terms(question)
            required = max(RECORD_MIN_TERMS, min(4, (len(terms) + 1) // 2))
            if category == "data-source":
                required = 2
            live_tried, live_counts = False, {}
            ref_rows = store.intents_by_ref(_ref_variants(_cited_refs(question)), repo=repo)
            if ref_rows:
                rec_pool = [(99, r["created_at"] or "", r) for r in ref_rows]
                rec_best = ref_rows[0]
                matches = ref_rows
            while rec_best is None:
                dfs = store.term_dfs(terms, repo=repo)
                known = sorted((t for t in terms if dfs.get(t, 0) > 0), key=lambda t: dfs[t])
                known = [t for t in known if t not in _ASK_MACHINERY] or known
                focus = known[:3]
                focus_need = 2 if len(focus) >= 2 else len(focus)
                if category == "data-source" and focus:
                    focus = focus[:2]
                    focus_need = 1
                matches = store.intents_matching(terms, limit=5, repo=repo)
                passing = []
                max_ov, nd_skipped, focus_failed = 0, False, False
                idents = _question_identifiers(question)
                ident_failed, title_failed = False, False
                # Path components and change verbs are background in a code
                # repository; what is left is what the question is about.
                background = _background_terms(question)
                subject_terms = [t for t in known if t not in background]
                novel_subject = [t for t in terms if dfs.get(t, 0) == 0 and len(t) >= 4 and t not in _FILLER
                                 and t not in background and t not in _ASK_MACHINERY]
                for m in matches:
                    text = (m["title"] + " " + m["body"]).lower()
                    if m["kind"] == "ticket" and (_non_decision(text) or not _is_settled(m)):
                        nd_skipped = True
                        continue
                    overlap = sum(1 for t in terms if t in text)
                    covered = sum(1 for t in focus if t in text)
                    max_ov = max(max_ov, overlap)
                    if overlap >= required:
                        if covered < focus_need:
                            focus_failed = True
                            continue
                        # A record that decided this names what the question
                        # names: every identifier the question carries, and
                        # one of its rarest words in the record's own title.
                        # Shared area words (target, arm, add) are background
                        # in a code repository, not a match.
                        if idents and not all(i in text for i in idents):
                            ident_failed = True
                            continue
                        if category != "data-source" and m["kind"] != "decision" and proposal_q:
                            # A proposal (Should X do Y?) is settled only by a
                            # record whose own title names it: the rarer half
                            # of the question's known words. A record that
                            # merely touched the area decided nothing about it.
                            rare = subject_terms[:max(2, (len(subject_terms) + 1) // 2)]
                            title_low = m["title"].lower()
                            if len(rare) < 2:
                                # The question's distinctive words are unknown
                                # to every record: no record settled it.
                                if novel_subject:
                                    title_failed = True
                                    continue
                            elif sum(1 for t in rare if t in title_low) < max(2, (len(rare) + 1) // 2):
                                title_failed = True
                                continue
                        passing.append((overlap, m["created_at"] or "", m))
                if passing:
                    top = max(o for o, _, _ in passing)
                    rec_pool = [x for x in passing if x[0] >= top - 1]
                    rec_pool.sort(key=lambda x: x[1], reverse=True)
                    rec_best = rec_pool[0][2]
                if rec_best is not None:
                    repo_tok = llm_mod.stem(repo.lower()) if repo else ""
                    novel = [t for t in terms if len(t) >= 5 and t not in _FILLER and dfs.get(t, 0) == 0
                             and t != repo_tok and t not in _ASK_MACHINERY]
                    if len(novel) >= 2:
                        why_open.append("records: question introduces terms no record mentions "
                                        f"({', '.join(_display_terms(question, novel[:4]))})")
                        rec_best, rec_pool = None, []
                if rec_best is not None or live_tried or not cfg.live_retrieval:
                    break
                live_tried = True
                from . import ingest as ingest_mod
                hint = ""
                if repo:
                    hits = resolve_paths(store, repo, question, d_context, d_path if d_path != "unknown" else "")
                    hint = hits[0].path if hits else ""
                live_counts = ingest_mod.live_probe(store, repo, terms, hint,
                                                    refs=_cited_refs(question) or _cited_refs(title))
                store.append_event("live_probe", {"task_id": task_id, "decision_id": did, "hint": hint,
                                                  **live_counts})
                if not any(v for v in live_counts.values() if isinstance(v, int)):
                    break
            retro_cand = rec_best
            if retro_cand is None:
                r_dfs = store.term_dfs(terms, repo=repo)
                r_focus = [t for t in sorted((t for t in terms if r_dfs.get(t, 0) > 0), key=lambda t: r_dfs[t])
                           if t not in _ASK_MACHINERY and t not in _RETRO_MACHINERY][:2]
                for m in matches:
                    m_text = (m["title"] + " " + m["body"]).lower()
                    if _is_settled(m) and sum(1 for t in terms if t in m_text) >= 2 \
                            and (not r_focus or any(t in m_text for t in r_focus)):
                        retro_cand = m
                        break
            if rec_best is None:
                if not matches:
                    why_open.append("records: nothing shares this question's terms")
                elif focus_failed:
                    why_open.append("records: candidates matched background words but not the "
                                    f"question's focus terms ({', '.join(_display_terms(question, focus))})")
                elif ident_failed:
                    why_open.append("records: no record that shares this question's words mentions "
                                    f"{', '.join(_question_identifiers(question, display=True)[:3])}")
                elif title_failed:
                    if len(subject_terms) < 2 and novel_subject:
                        why_open.append("records: no record mentions "
                                        f"{', '.join(_display_terms(question, novel_subject[:3]))}")
                    else:
                        why_open.append("records: candidates share words in their bodies, but none names enough of "
                                        f"{', '.join(_display_terms(question, subject_terms[:3]))} in its title")
                elif nd_skipped and not passing:
                    why_open.append("records: only tickets describing unresolved problems matched, "
                                    "none record a decision")
                elif not passing:
                    why_open.append(f"records: best candidate shares {max_ov} terms, {required} needed")
                fetched = sum(v for k, v in live_counts.items() if k != "blame" and isinstance(v, int))
                if live_tried and fetched:
                    why_open.append(f"live: searched {live_counts.get('sources') or 'git history'} on "
                                    f"demand, {fetched} fresh items cached, none answers this")
                if live_tried and live_counts.get("failed"):
                    why_open.append(f"live: lookup FAILED for {live_counts['failed']}, so this search "
                                    "was incomplete")

        if reversal_request:
            why_open.append("intent: phrased as a request to reverse a change; "
                            "retrieved records are context, not approval for the reversal")
            context_rows = ref_rows or ([rec_best] if rec_best is not None else matches[:3])
            for row in context_rows[:3]:
                why_open.append(f"records: retrieved context {row['kind']} {_ref(row)}: "
                                f"{row['title']}; excerpt: {_excerpt(row['body'], 240, 'the record has the rest')}")

        # arbitration
        pick, pick_note, mem_lex = None, "", False
        if mem_pick is not None and rec_best is not None:
            if cfg.semantic_retrieval:
                choice = _selector_pick(cfg, question, {"m1": ("memory", mem_pick[2]), "r1": ("record", rec_best)})
                if choice is None:
                    why_open.append("semantic: neither the matched memory nor the matched record truly answers")
                else:
                    pick = choice
                    pick_note = "won cross-source arbitration"
                    mem_lex = choice[0] == "memory"
            else:
                if mem_pick[2].updated_at >= _parse_ts(rec_best["created_at"]):
                    pick, mem_lex = ("memory", mem_pick[2]), True
                else:
                    pick = ("record", rec_best)
                pick_note = "newest source won the cross-source tie"
        elif mem_pick is not None:
            if cfg.semantic_retrieval:
                cands: dict = {"m1": ("memory", mem_pick[2])}
                for i, (_, _, p) in enumerate(accepted[1:4], 2):
                    cands[f"m{i}"] = ("memory", p)
                sel_rows = list(ref_rows)
                seen_refs2 = {r["ref"] for r in sel_rows}
                for m in store.intents_matching(_meaningful_terms(question), limit=5, repo=repo):
                    if m["ref"] not in seen_refs2:
                        sel_rows.append(m)
                for i, m in enumerate(sel_rows[:7], 1):
                    cands[f"r{i}"] = ("record", m)
                choice = _selector_pick(cfg, question, cands)
                if choice is None:
                    why_open.append("semantic: neither the matched memory nor any record answers this "
                                    "question's actual ask")
                elif choice[0] == "memory":
                    pick, mem_lex = choice, True
                    if choice[1].id != mem_pick[2].id:
                        mem_pick = (mem_pick[0], mem_pick[1], choice[1])
                else:
                    pick = choice
                    pick_note = "a record outranked the matched memory"
            else:
                pick, mem_lex = ("memory", mem_pick[2]), True
        elif rec_best is not None:
            if cfg.semantic_retrieval:
                terms2 = _meaningful_terms(question)
                sel_rows2 = list(ref_rows)
                seen_refs3 = {r["ref"] for r in sel_rows2}
                for m in store.intents_matching(terms2, limit=6, repo=repo):
                    if m["ref"] not in seen_refs3:
                        sel_rows2.append(m)
                cands = {f"r{i}": ("record", m) for i, m in enumerate(sel_rows2[:8], 1)}
                choice = _selector_pick(cfg, question, cands)
                if choice is None:
                    why_open.append("records: the selector rejected every candidate as out of scope")
                else:
                    pick = choice
                    pick_note = "scope confirmed by the selector"
            else:
                pick = ("record", rec_best)
        sweep_mems: list = []
        if pick is None and cfg.deep_retrieval:
            expansion = _expand_query(cfg, question)
            sweep_rows = list(ref_rows)
            seen_sweep = {r["ref"] for r in sweep_rows}
            exp_emb = None
            exp_added = 0
            if expansion:
                exp_text = " ".join(expansion)
                exp_emb = llm_mod.embed(question + " " + exp_text)
                for m in store.intents_matching(_meaningful_terms(exp_text), limit=8, repo=repo):
                    if m["ref"] not in seen_sweep:
                        sweep_rows.append(m)
                        seen_sweep.add(m["ref"])
                        exp_added += 1
                # The expansion's vocabulary also reaches memory through
                # the stemmed hybrid search, not only the hashed cosine.
                for item in store.memory_search(question + " " + exp_text, limit=6, repo=repo, min_score=0.2):
                    p = store.get_decision(item["id"])
                    if p is not None and p.status != "pending":
                        sweep_mems.append(p)
            for item in store.memory_search(question, limit=6, repo=repo, min_score=0.2):
                p = store.get_decision(item["id"])
                if p is not None and p.status != "pending" and p.id not in {x.id for x in sweep_mems}:
                    sweep_mems.append(p)
            store.append_event("expand", {"task_id": task_id, "decision_id": did, "phrases": expansion,
                                          "added_candidates": exp_added})
            pick = _wide_select(store, cfg, question, emb, _meaningful_terms(question), repo=repo,
                                extra_rows=sweep_rows, extra_emb=exp_emb, extra_mems=sweep_mems)
            if pick is None:
                joint_rows = list(sweep_rows)
                seen_j = {r["ref"] for r in joint_rows}
                for m in store.intents_matching(_meaningful_terms(question), limit=6, repo=repo):
                    if m["ref"] not in seen_j:
                        joint_rows.append(m)
                        seen_j.add(m["ref"])
                for p in sweep_mems[:3]:
                    joint_rows.append(_as_record(p))
                unmet_j = _unmet_contrast([asked_q], [_row_text(r) for r in joint_rows])
                if unmet_j:
                    why_open.append(f"semantic: no candidate mentions {', '.join(unmet_j)}, "
                                    "the condition this question turns on")
                    joint = None
                elif reversal_request:
                    joint = None
                else:
                    joint = _compose_joint(cfg, question, joint_rows, given=_given(d_context, d_facts))
                if joint is not None:
                    j_answer, j_cited, *j_more = joint
                    j_partial = bool(j_more and j_more[0]) or _states_a_gap(j_answer)
                    used_rows = j_more[1] if len(j_more) > 1 else []
                    joint_open = any(not _is_settled(r) for r in used_rows)
                    store.publish_evidence(did, used_rows, status="proposed" if joint_open else "partial" if j_partial else "resolved", source="record",
                                          answer=j_answer, kind="prediction" if joint_open else "evidence",
                                          evidence=f"composed across records: {j_cited}"
                                          + ("; answers this only in part" if j_partial else ""))
                    store.append_event("resolve", {"task_id": task_id, "decision_id": did, "source": "record",
                                                   "cited": j_cited, "kind": "joint-composition"})
                    result.resolved.append({"id": did, "question": question, "source": "record",
                                            "answer": j_answer, "partial": j_partial, "cited": j_cited})
                    continue
                why_open.append("semantic: no candidate truly answers this, alone or jointly "
                                f"(expansion added {exp_added} candidates)")
            else:
                pick_note = "selected by the semantic rung"

        # Selection (including the model sweep) cannot turn a historical
        # record into approval to reverse it. Keep its citation for the owner.
        if reversal_request and pick is not None and pick[0] == "record":
            row = pick[1]
            why_open.append(f"records: selected {row['kind']} {_ref(row)}: {row['title']}; "
                            f"excerpt: {_excerpt(row['body'], 240, 'the record has the rest')}; "
                            "does not authorize the requested reversal")
            pick = None
        if reversal_request and pick is not None and pick[0] == "memory":
            previous = pick[1]
            if (previous.source == "record" or not previous.answered_by
                    or not _REVERSAL_REQUEST_RE.search(previous.question)):
                why_open.append(f"memory: decision {previous.id} is context, not a signed answer "
                                "to a reversal request")
                pick, mem_lex = None, False

        # Entailment gate on memory reuse. A neighbouring question's answer
        # is applied to THIS question through the grounded composer, never
        # served verbatim, unless the question asked is the same decision
        # word for word. A rewritten follow-up always composes: the
        # rewrite carries a prior answer's own words, which then match
        # that same memory as a false direct hit (a monthly-contract
        # question was answered with the annual rule, verbatim). When the
        # composer says the record does not answer, the ladder continues.
        if pick is not None and pick[0] == "memory":
            applies, reason = applicability_status(pick[1], d_path, d_facts)
            if not applies:
                why_open.append(f"memory: {reason}; earlier answer {pick[1].id} is context, not an answer here: "
                                + _excerpt(pick[1].answer, 240, "the earlier answer has the rest"))
                store.append_event("applicability_unmet", {"task_id": task_id, "decision_id": did,
                                                            "memory": pick[1].id, "why": reason})
                pick, mem_lex = None, False
        if pick is not None and pick[0] == "memory":
            unmet_m = _unmet_contrast([asked_q],
                                      [pick[1].question, _memory_body(pick[1]), pick[1].context or ""])
            if unmet_m:
                why_open.append(f"memory: decision {pick[1].id} never mentions {', '.join(unmet_m)}, "
                                "the condition this question turns on")
                store.append_event("contrast_unmet", {"task_id": task_id, "decision_id": did,
                                                      "memory": pick[1].id, "missing": unmet_m})
                pick, mem_lex = None, False
        verbatim_ok = (pick is not None and pick[0] == "memory" and mem_lex and mem_pick is not None
                       and question == asked_q and mem_pick[1] >= 0.95)
        joint_partial = False
        if pick is not None and pick[0] == "memory" and cfg.semantic_retrieval and not verbatim_ok:
            # A rule whose conditions this question meets is given: the
            # answer does not have to establish its own membership again.
            # Measured live on 63eb671: the rule was found to cover the
            # question while the composed answer doubted that it applied.
            rule_given = ""
            if pick[1].reusable:
                covered, _why = rule_status(pick[1], question, d_context, facts=d_facts)
                if covered:
                    rule_given = (f"The rule {pick[1].rule_by or pick[1].answered_by or 'its owner'} made of decision "
                                  f"{pick[1].id} covers this question: its conditions "
                                  f"({'; '.join(rule_conditions(pick[1].rule_conditions)) or 'none'}) are met here.")
            m_given = _given(d_context, d_facts, rule_given)
            grounded = _compose_answer(cfg, question, f"earlier answer {pick[1].id}",
                                       _memory_body(pick[1]), pick[1].answered_by, given=m_given)
            others = [p for _, s2, p in scored[1:6] if s2 >= 0.35 and p.id != pick[1].id][:3] if mem_lex else []
            if grounded is None or (_states_a_gap(grounded) and others):
                joint = _compose_joint(cfg, question, [_as_record(pick[1])] + [_as_record(p) for p in others],
                                       given=m_given) if others else None
                if joint is not None:
                    joint_partial = len(joint) > 2 and bool(joint[2])
                    memory_used_rows = joint[3] if len(joint) > 3 else []
                    primary = next((p for p in [pick[1]] + others if memory_used_rows and p.id == memory_used_rows[0].get('decision_source_id')), pick[1])
                    pick = ("memory", replace(primary, answer=joint[0]))
                    pick_note = (pick_note + "; " if pick_note else "") + f"composed with {joint[1]}"
                elif grounded is not None:
                    pick = ("memory", replace(pick[1], answer=grounded))
                else:
                    why_open.append(f"memory: decision {pick[1].id} is the closest answer but does not "
                                    "answer this question")
                    pick, mem_lex = None, False
            else:
                pick = ("memory", replace(pick[1], answer=grounded))

        # A signed answer and a settled record that state different figures
        # for the same thing are a conflict for a person, whichever one the
        # selector preferred. Measured live on eb9d22d: memory signed 11 days,
        # a newer record said 19, and the record was served as the answer.
        pre_rival = None
        if pick is not None and mem_pick is not None:
            if pick[0] == "record":
                mem = mem_pick[2]
                rival = pick[1] if applicability_status(mem, d_path, d_facts)[0] else None
            else:
                mem = store.get_decision(pick[1].id, exact=True) or pick[1]
                rival = _record_contradicting(store, cfg, repo, question, mem.answer)
                pre_rival = (rival,)
            if (mem.authorized and rival is not None and _is_settled(rival)
                    and _numeric_clash(mem.answer or "", _row_text(rival))):
                conflict = (mem, rival)
        if conflict is not None:
            mem, rival = conflict
            clash = f"{rival['kind']} {_ref(rival)}{_ticket_state(rival)}"
            why_open.append(f"conflict: decision {mem.id}, signed by {mem.answered_by or 'a person'}, says \""
                            f"{_excerpt(mem.answer, 200, 'the decision has the rest')}\", and {clash} says \""
                            f"{_excerpt(_row_text(rival), 200, 'the record has the rest')}\"; they state "
                            "different figures, so Raven serves neither and proposes neither: a person decides "
                            "which stands")
            store.append_event("memory_record_conflict", {"task_id": task_id, "decision_id": did,
                                                          "memory": mem.id, "record": clash, "escalated": True,
                                                          "served": "neither"})
            pick, mem_lex = None, False

        if pick is not None:
            kind, item = pick
            mem_conflict = ""
            mem_partial = False
            if kind == "memory":
                if mem_lex and mem_pick is not None:
                    eff, sim, _ = mem_pick
                    mkind = "direct" if eff >= DIRECT_MEMORY_MIN else "near match"
                    detail = f"{mkind}, decision {item.id}, similarity {sim:.2f}, decayed score {eff:.2f}"
                    if reranked and item.id == reranked:
                        # Say that the text score did not find this, so a
                        # person reading the evidence knows which part of
                        # it to disbelieve.
                        detail = (f"near match, decision {item.id}, below the text-score floor at {sim:.2f} and "
                                  "read as answering this anyway")
                    if pick_note and "composed with" in pick_note:
                        detail += "; " + pick_note.split("composed with", 1)[1].join(["composed with", ""])
                else:
                    mkind = "semantic"
                    detail = f"reused from decision {item.id}, {pick_note}, checked against this question"
                mem_partial = _states_a_gap(item.answer) or joint_partial
                rival = pre_rival[0] if pre_rival else _record_contradicting(store, cfg, repo, question, item.answer)
                if rival is not None and _numeric_clash(item.answer, (rival["title"] or "") + " " + (rival["body"] or "")):
                    # An unsettled record, or an unsigned answer: the figures
                    # still disagree, and the answer says so rather than one
                    # side quietly replacing the other.
                    clash = f"{rival['kind']} {_ref(rival)}{_ticket_state(rival)}"
                    mem_partial = True
                    item = replace(item, answer=(
                        f"CONFLICTED, do not act on either side alone: "
                        f"{item.answered_by or 'an earlier answer'} says: {item.answer} But {clash} states a "
                        f"different figure: \"{rival['title']}\". Confirm with the owner which stands."))
                    mem_conflict = f"{clash} states a different figure; surfaced in the answer itself"
                    store.append_event("memory_record_conflict", {"task_id": task_id, "decision_id": did,
                                                                  "memory": item.id, "record": clash,
                                                                  "surfaced": True})
                elif rival is not None and _is_settled(rival) and not item.authorized:
                    clash = f"{rival['kind']} {_ref(rival)}{_ticket_state(rival)}"
                    store.append_event("memory_record_conflict", {"task_id": task_id, "decision_id": did,
                                                                  "memory": item.id, "record": clash,
                                                                  "escalated": True})
                    kind, item = "record", rival
                elif rival is not None and _is_settled(rival):
                    clash = f"{rival['kind']} {_ref(rival)}{_ticket_state(rival)}"
                    mem_partial = True
                    item = replace(item, answer=(
                        f"CONFLICTED, do not act on either side alone: "
                        f"{item.answered_by or 'an earlier answer'} says: {item.answer} But {clash} states "
                        f"otherwise: \"{rival['title']}\". Neither is corroborated over the other; confirm "
                        f"with the owner which stands."))
                    mem_conflict = f"{clash} covers this too and does not agree; surfaced in the answer itself"
                    store.append_event("memory_record_conflict", {"task_id": task_id, "decision_id": did,
                                                                  "memory": item.id, "record": clash,
                                                                  "surfaced": True})
                elif rival is not None:
                    clash = f"{rival['kind']} {_ref(rival)}{_ticket_state(rival)}"
                    whose = "the signed answer" if item.authorized else "the earlier resolution"
                    mem_conflict = (f"{clash} covers this too and does not agree; the answer above is {whose}, "
                                    "check which should stand")
                    store.append_event("memory_record_conflict", {"task_id": task_id, "decision_id": did,
                                                                  "memory": item.id, "record": clash})
            if kind == "memory":
                # A person stands behind a signed row; the reuse is
                # evidence (still marked for sign-off here). An unsigned
                # row, one the agent settled without anyone signing, is a
                # prediction: shown with its provenance, never served as
                # evidence, and it closes no open question.
                signed = item.authorized
                origin = "memory" if signed else (item.source or "memory")
                revision = ts_to_iso(item.updated_at) if item.updated_at else ""
                # A signed answer given for another scope (another named
                # customer, other files) is a prediction for this one,
                # not evidence: the owner decides whether it carries over.
                # A reusable rule whose conditions hold is the one case
                # that resolves without a fresh signature: its owner said
                # so, for these conditions, until it expires.
                other_scope, known_scope = (_scope_difference(question, d_context, d_path, item, d_facts,
                                                              _facts_of(store, item.id), repo)
                                            if signed else ("", False))
                rule_ok, rule_why = (rule_status(item, question, d_context, facts=d_facts)
                                     if signed and item.reusable else (False, ""))
                if len(memory_used_rows) > 1 or mem_conflict:
                    rule_ok = False
                    rule_why = 'The answer combines or contrasts premises; it requires a fresh human sign-off'
                if rule_ok and other_scope and item.rule_scope != "any":
                    # Another customer, other files: the rule was made for
                    # its own scope and its owner did not say anywhere.
                    rule_ok, rule_why = False, (f"the rule from decision {item.id} applies in its own scope only, and "
                                                f"this {'is' if known_scope else 'may be'} another ({other_scope}); its "
                                                "owner did not make it apply anywhere")
                # Task/context links can explain why ordinary historical
                # evidence is not established here. They cannot narrow an
                # explicit rule whose existing declared conditions hold.
                namespace_scope = (_memory_namespace_difference(store, task_id, item)
                                   if signed and not rule_ok else '')
                auto_rules = store.get_setting("auto_rules") == "1"
                terms = [c for c in rule_conditions(item.rule_conditions)]
                covered = (f"the rule {item.rule_by or item.answered_by} made of decision {item.id}"
                           + (f" (conditions: {'; '.join(terms[:3])})" if terms else "")
                           + (f", until {item.rule_expires[:10]}" if item.rule_expires else ""))
                rule_note = ""
                attribution = (item.answered_by + " answered") if item.answered_by else (item.signed_by + " approved")
                if rule_ok and not auto_rules:
                    # The rule fits; the organization keeps every decision
                    # request-specific until it turns automatic rules on.
                    rule_ok = False
                    other_scope = ""
                    rule_why = ""
                    rule_note = (f"{covered} covers this; automatic rule authorization is off (settings.auto_rules), "
                                 "so a person signs it")
                    why_open.append("memory: " + rule_note)
                if rule_ok:
                    other_scope = ""
                    evidence = f"covered by {covered}; {attribution} it ({detail})"
                elif rule_why:
                    # The rule was nominated and did not fit: the node says
                    # so where the person reads it, not only in the log.
                    rule_note = rule_why + ("; this historical answer still requires fresh sign-off"
                                           if namespace_scope else "; the answer is evidence to sign, not a rule here")
                    why_open.append("memory: " + rule_note)
                if rule_ok:
                    pass
                elif signed and namespace_scope and not other_scope:
                    signed = False
                    evidence = (f"prediction: {attribution} in decision {item.id}; {namespace_scope}; "
                                f"historical context, not established evidence for this task ({detail}); "
                                "confirm applicability with the owner before acting on it")
                elif signed and other_scope:
                    signed = False
                    evidence = ((f"prediction: {attribution} this for another scope, decision "
                                 f"{item.id} ({other_scope}); it may not carry over" if known_scope else
                                 f"prediction: {attribution} a question like this in decision {item.id}, "
                                 f"which may be another scope ({other_scope}); Raven cannot tell whether it carries "
                                 "over")
                                + f" ({detail}); confirm with the owner before acting on it")
                elif signed:
                    evidence = f"{attribution} this ({detail})"
                else:
                    who = "settled by the agent" if item.source == "agent" else f"from {item.evidence or item.source}"
                    evidence = (f"prediction: reused from decision {item.id}, {who}, which no human has signed "
                                f"({detail}); confirm with the owner before acting on it")
                status = ("partial" if mem_partial else "resolved") if signed else "proposed"
                store.publish_evidence(
                    did, (memory_used_rows or [_as_record(item)]) + ([_evidence_role(rival, 'contradiction')] if mem_conflict and rival is not None else []),
                    status=status, source=origin, answer=item.answer,
                    answered_by=item.answered_by if signed else "",
                    prediction=None if signed else item.answer,
                    kind=_kind_for(status, origin, mkind) if signed else "prediction",
                    source_id=item.id, source_revision=revision,
                    evidence=evidence + (f"; conflict: {mem_conflict}" if mem_conflict else "")
                    + (f"; {pick_note}" if mem_lex and pick_note else "")
                    + (f"; {rule_note}" if rule_note else ""),
                    **({"signoff": "rule", "signed_by": item.rule_by or item.answered_by} if rule_ok and not mem_partial else {}))
                store.add_link(did, item.id, "derived", "took its answer from this decision")
                store.append_event("resolve" if signed else "proposed",
                                   {"task_id": task_id, "decision_id": did, "source": origin,
                                    "cited": item.id, "answered_by": item.answered_by if signed else "",
                                    "kind": mkind, "signed": signed})
                entry = {"id": did, "question": question, "source": origin,
                         "by": item.answered_by if signed else "", "answer": item.answer,
                         "conflict": mem_conflict, "partial": mem_partial,
                         "reused_from": item.id if mkind == "semantic" or not signed else "",
                         "reused_q": item.question if mkind == "semantic" or not signed else "",
                         "cited": item.evidence or f"earlier decision {item.id}"}
                if signed:
                    result.resolved.append(entry)
                    if not scratch:
                        _close_open_twins(store, did, question, repo, item.answer, item.answered_by, source=origin,
                                      context=d_context, path=d_path)
                else:
                    result.proposed.append({**entry, "state": "unsigned"})
                continue
            newer = _superseded_by_newer(store, repo, item)
            if newer is not None:
                store.append_event("superseded_record", {"task_id": task_id, "decision_id": did,
                                                         "stale": f"{item['kind']} {_ref(item)}",
                                                         "current": f"{newer['kind']} {_ref(newer)}"})
                item = newer
            citation = f"{item['kind']} {_ref(item)}{_ticket_state(item)}: {item['title']}"
            body = item["body"].strip() or item["title"].strip()
            if not cfg.semantic_retrieval:
                why_open.append(f"records: retrieved {citation} as context; no answerability check is available "
                                f"without a model. Excerpt: {_excerpt(body, 240, 'the record has the rest')}")
                if _RECENCY_Q_RE.search(question.lower()):
                    contest = _contested_by_newer(store, repo, item)
                    if contest is not None:
                        why_open.append(f"records: contested by newer {contest['kind']} {_ref(contest)}: "
                                        f"{contest['title']}; confirm what is in effect")
                    else:
                        why_open.append("records: no newer indexed record revisits this; the indexed set may be incomplete")
            unmet_r = _unmet_contrast([asked_q], [item["title"], body])
            void = _void(item)
            if void:
                why_open.append(f"records: {citation} was {void}: it records something that was not adopted, "
                                "so it is context, not the answer")
                answer = None
            elif reversal_request:
                # A memory/record conflict can replace the selected memory
                # after the earlier guard. That record is context too.
                why_open.append(f"records: {citation} is context, not reversal approval")
                answer = None
            elif unmet_r:
                why_open.append(f"records: {citation} never mentions {', '.join(unmet_r)}, "
                                "the condition this question turns on")
                answer = None
            else:
                answer = _compose_answer(cfg, question, citation, body, item["author"],
                                         given=_given(d_context, d_facts))
            if answer is not None:
                stray = _invented_paths(answer, body, item["title"], question)
                if stray:
                    answer += (f" (note: {', '.join(stray)} {'is' if len(stray) == 1 else 'are'} not named in "
                               f"the cited record; Raven added that, so check the file before trusting it)")
                    store.append_event("unsourced_path", {"task_id": task_id, "decision_id": did,
                                                          "paths": ", ".join(stray), "cited": citation})
            if answer is not None and _is_pure_refusal(answer):
                why_open.append(f"records: {citation} does not state an answer to this question")
                answer = None
            if not reversal_request and answer is None and category == "data-source" and _is_settled(item) \
                    and any(t in _RETRO_MACHINERY for t in _meaningful_terms(question)):
                pointer = (f"Not stated in the indexed record. The change itself is {citation}, by "
                           f"{item['author']}; the reasoning likely lives in that item's description or "
                           f"discussion, which Raven could not read from here.")
                store.publish_evidence(did, [_evidence_role(item, 'context')], status="partial", source="record", answer=pointer,
                                      answered_by=item["author"], kind="evidence",
                                      evidence=f"{citation}; states the change, not the reasoning")
                store.append_event("resolve", {"task_id": task_id, "decision_id": did, "source": "record",
                                               "cited": citation, "kind": "retro-pointer"})
                result.resolved.append({"id": did, "question": question, "source": "record",
                                        "answer": pointer, "partial": True, "cited": citation})
                continue
            if answer is None:
                joint_rows2, seen_j2 = [item], {item["ref"]}
                for r in ref_rows:
                    if r["ref"] not in seen_j2:
                        joint_rows2.append(r)
                        seen_j2.add(r["ref"])
                for _, _, r in rec_pool:
                    if r["ref"] not in seen_j2:
                        joint_rows2.append(r)
                        seen_j2.add(r["ref"])
                for m in store.intents_matching(_meaningful_terms(question), limit=6, repo=repo):
                    if m["ref"] not in seen_j2:
                        joint_rows2.append(m)
                        seen_j2.add(m["ref"])
                unmet_j2 = _unmet_contrast([asked_q], [_row_text(r) for r in joint_rows2])
                if reversal_request:
                    joint2 = None
                elif unmet_j2:
                    why_open.append(f"records: no candidate mentions {', '.join(unmet_j2)}, "
                                    "the condition this question turns on")
                    joint2 = None
                else:
                    joint2 = _compose_joint(cfg, question, joint_rows2, given=_given(d_context, d_facts))
                if joint2 is not None:
                    j_answer2, j_cited2, *j_more2 = joint2
                    j_partial2 = bool(j_more2 and j_more2[0]) or _states_a_gap(j_answer2)
                    used_rows = j_more2[1] if len(j_more2) > 1 else []
                    joint_open = any(not _is_settled(r) for r in used_rows)
                    store.publish_evidence(did, used_rows, status="proposed" if joint_open else "partial" if j_partial2 else "resolved", source="record",
                                          answer=j_answer2, kind="prediction" if joint_open else "evidence",
                                          evidence=f"composed across records: {j_cited2}"
                                          + ("; answers this only in part" if j_partial2 else ""))
                    store.append_event("resolve", {"task_id": task_id, "decision_id": did, "source": "record",
                                                   "cited": j_cited2, "kind": "joint-composition"})
                    result.resolved.append({"id": did, "question": question, "source": "record",
                                            "answer": j_answer2, "partial": j_partial2, "cited": j_cited2})
                    continue
                if cfg.semantic_retrieval:
                    why_open.append(f"records: {citation} was the best candidate but does not state the answer")
            elif not _is_settled(item):
                store.publish_evidence(did, [item], status="proposed", source="record", answer=answer,
                                      answered_by=item["author"], kind="prediction",
                                      evidence=f"{citation}; not ratified, the ticket is still {item['status'] or 'open'}")
                store.append_event("proposed", {"task_id": task_id, "decision_id": did, "cited": citation,
                                                "ticket_status": item["status"]})
                result.proposed.append({"id": did, "question": question, "source": "record", "answer": answer,
                                        "state": item["status"] or "open", "cited": citation})
                continue
            else:
                partial = _states_a_gap(answer)
                extra_ev = ""
                record_used_rows = [item]
                if _RECENCY_Q_RE.search(question.lower()):
                    contest = _contested_by_newer(store, repo, item)
                    if contest is not None:
                        record_used_rows.append(_evidence_role(contest, 'contradiction'))
                        c_ref = f"{contest['kind']} {_ref(contest)}{_ticket_state(contest)}"
                        answer += (f" (note: {c_ref}, filed after this change, reports the same subject as "
                                   f"unsettled: \"{contest['title']}\". Confirm the change still holds.)")
                        extra_ev = f"; contested by newer {c_ref}"
                        partial = True
                    else:
                        answer += " (no newer indexed record revisits this decision.)"
                    rec_text = (item["title"] + " " + item["body"]).lower()
                    pend = _PENDING_RE.search(rec_text)
                    if pend:
                        answer += (f" (the record also names follow-up work: \"...{pend.group(0)}...\", "
                                   f"which may not have landed.)")
                        extra_ev += "; record names pending follow-up work"
                        partial = True
                status = "partial" if partial else "resolved"
                store.publish_evidence(did, record_used_rows, status=status, source="record", answer=answer,
                                      answered_by=item["author"], kind="evidence",
                                      evidence=citation + ("; answers this only in part" if partial else "")
                                      + extra_ev + (f"; {pick_note}" if pick_note else ""))
                store.append_event("resolve", {"task_id": task_id, "decision_id": did, "source": "record",
                                               "cited": citation, "kind": pick_note or "lexical"})
                result.resolved.append({"id": did, "question": question, "source": "record",
                                        "answer": answer, "partial": partial, "cited": citation})
                if not partial:
                    if not scratch:
                        _close_open_twins(store, did, question, repo, answer, item["author"], source="record",
                                      context=d_context, path=d_path)
                continue

        # c. assumption
        if reversal_request:
            why_open.append("assumption: a reversal request requires a decision, not a default")
        elif conflict is not None:
            why_open.append("assumption: a conflict between a signed answer and a record is a person's call, "
                            "not a default")
        elif category in llm_mod.ASSUMABLE_CATEGORIES:
            precedent = store.db.execute(
                "SELECT count(*) c FROM intents" + (" WHERE repo=?" if repo else ""),
                (repo,) if repo else ()).fetchone()["c"]
            if precedent > 0:
                default = llm_mod._ASSUMED_DEFAULTS.get(category, "Follow the repo's existing precedent.")
                a_terms = _meaningful_terms(question)
                a_dfs = store.term_dfs(a_terms, repo=repo)
                a_focus = sorted((t for t in a_terms if a_dfs.get(t, 0) > 0), key=lambda t: a_dfs[t])[:3]
                need_focus = 2 if len(a_focus) >= 2 else len(a_focus)
                a_required = max(RECORD_MIN_TERMS, min(4, (len(a_terms) + 1) // 2))
                cited, assume_gap, chosen_m = "", False, None
                a_named = [m for m in ref_rows if _is_settled(m)]
                a_named_refs = {m["ref"] for m in a_named}
                a_cands = a_named + [
                    m for m in store.intents_matching(a_terms, limit=6, repo=repo)
                    if _is_settled(m) and m["ref"] not in a_named_refs
                    and sum(1 for t in a_terms if t in (m["title"] + " " + m["body"]).lower()) >= a_required]
                if cfg.semantic_retrieval and a_cands:
                    labeled = "\n".join(
                        f"r{i}: {m['kind']} {_ref(m)}{_ticket_state(m)}: {m['title']} | "
                        + " ".join((m["body"] or "").split())[:200]
                        for i, m in enumerate(a_cands, 1))
                    try:
                        verdict = client.complete_json("precedent", llm_mod.PRECEDENT_SYSTEM,
                                                       f"Question: {question}\n\nCandidates:\n{labeled}")
                        pick_s = str((verdict or {}).get("pick", "none"))
                        if pick_s.startswith("r") and pick_s[1:].isdigit():
                            idx = int(pick_s[1:]) - 1
                            if 0 <= idx < len(a_cands):
                                chosen_m = a_cands[idx]
                    except llm_mod.LLMError:
                        chosen_m = None
                else:
                    for m in a_cands:
                        n_text = (m["title"] + " " + m["body"]).lower()
                        if sum(1 for t in a_focus if t in n_text) >= need_focus:
                            chosen_m = m
                            break
                if chosen_m is not None:
                    m = chosen_m
                    newer = _superseded_by_newer(store, repo, m)
                    if newer is not None:
                        m = newer
                    cited = f"{m['kind']} {_ref(m)}{_ticket_state(m)}: {m['title']}"
                    composed = _compose_assumption(cfg, question, cited, m["body"])
                    if composed == ASSUME_GAP:
                        cited, assume_gap = "", True
                    elif composed:
                        # The record was read and produced this answer, so
                        # it is the precedent and says so.
                        default = composed + f" Nearest precedent here: {cited}."
                    else:
                        # Nothing read the record: it only shares words
                        # with the question. A canned default with a
                        # record stapled to it reads as though the team
                        # decided this, and a reviewer approved one that
                        # cited an unrelated change. The record stays in
                        # the evidence, out of the answer.
                        near, cited = cited, ""
                        why_open.append(f"assumption: {near} shares this question's terms but was not read "
                                        "into an answer, so it is not cited as precedent")
                if assume_gap:
                    why_open.append("assumption: the nearest precedent sets no pattern for this question, "
                                    "so none was made")
                elif not cited and ref_rows:
                    named = ", ".join(sorted({m["ref"] for m in ref_rows}))
                    why_open.append(f"assumption: the question itself cites {named}; a canned default cannot "
                                    "stand in for records it names, so none was made")
                elif not cited and _canned_misfit(question, category):
                    why_open.append(f"assumption: no relevant precedent, and the generic {category} default "
                                    "does not answer this specific question, so none was made")
                else:
                    if cited:
                        certainty = f"high certainty, closest precedent {cited}"
                    else:
                        certainty = ("no record in this repo clears the relevance bar for this question, so "
                                     "this is a plain default, not a pattern from your history")
                        default += (" (no relevant precedent in this repo; this is a default, not something "
                                    "your team has decided)")
                    store.publish_evidence(did, [m] if cited else [], status="assumed", source="assumption", answer=default,
                                          prediction=default, kind="prediction",
                                          evidence=f"low-stakes {category} decision, {certainty} "
                                                   f"({precedent} records indexed); confirm or correct in the inbox")
                    store.append_event("assume", {"task_id": task_id, "decision_id": did, "default": default,
                                                  "certainty": "high" if cited else "default-only",
                                                  "precedent_records": precedent, "cited": cited})
                    result.assumed.append({"id": did, "question": question, "default": default})
                    continue
            else:
                why_open.append("assumption: no records indexed to ground one")
        elif category:
            why_open.append(f"assumption: '{category}' is not a low-stakes category")

        # d. human
        if (not reversal_request and category == "data-source" and retro_cand is not None and _is_settled(retro_cand)
                and any(t in _RETRO_MACHINERY for t in _meaningful_terms(question))):
            item2 = retro_cand
            retro_used_rows = [_evidence_role(item2, 'context')]
            cit2 = f"{item2['kind']} {_ref(item2)}{_ticket_state(item2)}: {item2['title']}"
            answer2 = (f"Not stated in the indexed record. The closest recorded change is {cit2}, by "
                       f"{item2['author']}; the reasoning likely lives in that item's description or "
                       f"discussion, which Raven could not read from here.")
            if _RECENCY_Q_RE.search(question.lower()):
                contest2 = _contested_by_newer(store, repo, item2)
                if contest2 is not None:
                    retro_used_rows.append(_evidence_role(contest2, 'context'))
                    answer2 += (f" Newer {contest2['kind']} {_ref(contest2)}{_ticket_state(contest2)} "
                                f"revisits this: \"{contest2['title']}\".")
                else:
                    answer2 += " No newer indexed record revisits the decision."
            store.publish_evidence(did, retro_used_rows, status="partial", source="record", answer=answer2,
                                  answered_by=item2["author"], kind="evidence",
                                  evidence=cit2 + "; states the change, not the reasoning")
            store.append_event("resolve", {"task_id": task_id, "decision_id": did, "source": "record",
                                           "cited": cit2, "kind": "retro-pointer"})
            result.resolved.append({"id": did, "question": question, "source": "record", "answer": answer2,
                                    "partial": True, "cited": cit2})
            continue
        related_open: list = []
        twin = _open_twin(store, cfg, client, question, emb, repo, exclude=did,
                          context=d_context, path=d_path, related=related_open, facts=d_facts)
        for cand, why, known in related_open[:3]:
            # The same question is open in another scope: a relationship
            # to show, never one answer for two decisions.
            scope = "in another scope" if known else "maybe in another scope"
            store.add_link(did, cand.id, "related", f"same question, {'different' if known else 'possibly different'} "
                                                     f"scope: {why}")
            why_open.append(f"related: decision {cand.id} asks the same question {scope} ({why}); "
                            "one answer does not settle both")
        if twin is not None:
            store.update_decision(
                did, status="duplicate", superseded_by=twin.id, owner=twin.owner or "",
                owner_evidence=twin.owner_evidence or "", kind="new",
                answer=(f"Same open decision as {twin.id}, not re-asked"
                        + (f"; waiting on {twin.owner}." if twin.owner else ".")),
                evidence=(f"duplicate of open decision {twin.id}"
                          + (f"; {_excerpt(twin.evidence, 200, 'the open decision has the rest')}"
                             if twin.evidence else "")))
            store.add_link(did, twin.id, "duplicate", "the same open decision; one answer settles both")
            store.append_event("dedup_open", {"task_id": task_id, "decision_id": did, "twin": twin.id,
                                              "question": _word_trunc(question, 120)})
            result.open.append({
                "id": twin.id, "question": question, "owner": twin.owner or None, "duplicate_of": twin.id,
                "stub": did,
                "note": (f"already open as decision {twin.id} (asked earlier as: "
                         f"\"{_word_trunc(twin.question, 70)}\"), not opened again. One answer settles both.")})
            continue
        route_notes: list[str] = []
        ranked = route_ranked(store, repo, question,
                              path=d_path if d_path != "unknown" else "", context=d_context,
                              notes=route_notes, requester=d_requester, hints=d_hints,
                              category=d_category, also_paths=d_also, facts=d_facts,
                              task_id=task_id, decision_id=did) if repo else []
        owner_info = ranked[0] if ranked else None
        # The pilot mode: only what the organization verified routes on
        # its own; an inferred route goes to the coordinator with the
        # candidates named, so nobody is pinged on git history alone.
        inferred_only = owner_info is not None and not any(ln.startswith("verified:") for ln in owner_info[1])
        if owner_info is not None and inferred_only and store.get_setting("require_verified_route") == "1":
            fallback = coordinator_route(store, repo, ["an inferred route is not routed to without a verified owner"],
                                         ranked)
            if fallback is not None:
                owner_info = fallback
        elif owner_info is not None and inferred_only and store.find_person(owner_info[0]) is None:
            # The strongest signal is someone in the history Raven cannot
            # reach: not one of its people, so no message gets to them and
            # nobody can sign in as them. Measured live on 5e967e4: the only
            # verified owner had expired, the route went to a contributor
            # from git history, and the delivery failed.
            fallback = coordinator_route(store, repo, [f"{owner_info[0]} leads on git history but is not one of the "
                                                       "people in Raven, so nobody could ask them"], ranked)
            if fallback is not None:
                owner_info = fallback
        elif owner_info is None:
            fallback = coordinator_route(store, repo, route_notes)
            if fallback is not None:
                owner_info = fallback
        if owner_info is not None and category == "data-source" and any(
                t in ("who", "decid", "author", "made", "responsible") for t in _meaningful_terms(question)):
            o_owner, o_ev, o_score = owner_info
            owner_info = (o_owner, ["triage contact from ownership stats; this names who owns the area, NOT "
                                    "the person this question asks about, which the records did not establish",
                                    *o_ev], o_score)
        if owner_info is None:
            unknown_why = ("Raven does not know who owns this: " + "; ".join(route_notes)
                           if route_notes else "no routing signal cleared the bar")
            store.update_decision(did, status=OPEN, owner="", kind="new",
                                  owner_evidence=unknown_why,
                                  evidence="; ".join(why_open))
            store.append_event("route_unknown", {"task_id": task_id, "decision_id": did})
            result.open.append({"id": did, "question": question, "owner": None, "ranked": [],
                                "note": "Raven does not know who owns this from the graph."})
            continue
        owner, evidence, score = owner_info
        # How this owner has decided before: their own signed answers on
        # related decisions, as a prediction they confirm or correct.
        proposal_observations = {}
        proposal, proposal_from, proposal_why = (_how_they_decide(store, cfg, repo, question, d_context, emb, owner,
                                                                  path=d_path if d_path != "unknown" else "",
                                                                  facts=d_facts, observations=proposal_observations)
                                                 if conflict is None else ("", "", ""))
        with store.transaction():
            from .context_memory import _same_source_revision
            if proposal:
                for source_id, observed_revision in proposal_observations.items():
                    current = store.db.execute('SELECT updated_at,needs_review FROM decisions WHERE id=?', (source_id,)).fetchone()
                    if not current or current['needs_review'] or not _same_source_revision(observed_revision, current['updated_at']):
                        proposal, proposal_from, proposal_why = '', '', ''
                        break
                if proposal_from not in proposal_observations:
                    proposal, proposal_from, proposal_why = '', '', ''
            store.update_decision(did, status=OPEN, owner=owner, kind="prediction" if proposal else "new",
                                  owner_evidence="; ".join(evidence),
                                  evidence="; ".join(why_open + ([proposal_why] if proposal_why else [])),
                                  **({"prediction": proposal, "source_id": proposal_from,
                                      "source_revision": proposal_observations[proposal_from]} if proposal else {}))
            if proposal:
                store.add_link(did, proposal_from, "how-they-decide", "the owner's earlier answer the proposal rests on")
                store.append_event("proposal_composed", {"task_id": task_id, "decision_id": did, "owner": owner,
                                                         "from": proposal_from})
        store.append_event("ask_drafted", {"task_id": task_id, "decision_id": did, "owner": owner,
                                           "score": score})
        result.open.append({"id": did, "question": question, "owner": owner, "ranked": _ranked_view(ranked),
                            "routing": f"-> {owner}: " + "; ".join(evidence) if evidence else f"-> {owner}"})

    store.append_event("run_complete", {
        "task_id": task_id,
        "resolved": sum(1 for r in result.resolved if not r.get("partial")),
        "partial": sum(1 for r in result.resolved if r.get("partial")),
        "proposed": len(result.proposed), "assumed": len(result.assumed), "open": len(result.open)})
    if not keep_draft:
        publish_drafts(store, result.drafts)
    return result


def publish_drafts(store: Graph, ids: list[str]) -> None:
    """The nodes this run built are now decisions everyone can see."""
    if not ids:
        return
    with store.transaction():
        for did in ids:
            store.publish_draft(did)


_CHOICE_NUM_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:[a-z%]+\s+)?or\s+(\d+(?:\.\d+)?)\b", re.IGNORECASE)


def _offered_choices(question: str, context: str = "") -> list[str]:
    """The alternatives a question offers in so many words: figures joined
    by "or" ("11 or 13 events"), and the options the agent listed."""
    out = [n for pair in _CHOICE_NUM_RE.findall(question or "") for n in pair]
    for line in (context or "").splitlines():
        if line.lower().startswith("options:"):
            for option in line.split(":", 1)[1].split("|"):
                words = [w for w in re.findall(r"[a-z0-9_.]+", option.lower()) if len(w) > 2][:3]
                if words:
                    out.append(" ".join(words))
    return out


def _takes_a_choice(proposal: str, choices: list[str]) -> bool:
    """Whether a proposal names one of the offered alternatives (an
    option counts when its leading words are all in it). True when the
    question offers none."""
    if not choices:
        return True
    low = (proposal or "").lower()
    for choice in choices:
        words = choice.split()
        if (len(words) == 1 and re.search(rf"(?<![\d.]){re.escape(words[0])}(?![\d.])", low)) or \
                (len(words) > 1 and all(w in low for w in words)):
            return True
    return False


def _how_they_decide(store: Graph, cfg, repo: str, question: str, context: str, emb: list[float],
                     owner: str, path: str = "", facts: dict | None = None,
                     observations: dict | None = None) -> tuple[str, str, str]:
    """(prediction, source decision id, why): how the routed owner has
    decided related questions before, from their own signed answers
    only. The prediction is answer text the owner can confirm as is;
    the why says where it came from, how it relates to what it leans on
    (the same policy, or an analogy), and the scope that answer was given
    for when this request is outside it. With a backend the fast model
    writes the pattern in two sentences; without one the closest signed
    answer speaks for itself. Empty when the owner has no bearing
    answer. A prediction to confirm, never sign-off."""
    from .signals import _norm_person
    # A question about who decided or why is answered by the record of
    # that decision, not by a guess at how its owner would decide now.
    if set(_meaningful_terms(question)) & _RETRO_MACHINERY:
        return "", "", ""
    key = _norm_person(owner)
    priors: list[tuple[float, object, str]] = []
    for score, prior in store.similar_answered(emb, top_k=8, min_score=0.35, repo=repo, query=question):
        if not (prior.authorized and prior.answered_by and _norm_person(prior.answered_by) == key):
            continue
        applies, reason = applicability_status(prior, path, facts)
        scope = "" if applies else reason.replace(f"decision {prior.id} ", "", 1)
        # An answer its owner said does not hold here is not a basis for
        # proposing it here. Measured live on eb9d22d: an answer that
        # excluded customer=globex came back as the prediction for globex.
        if scope.startswith("excludes "):
            continue
        priors.append((score, prior, scope))
    if not priors:
        return "", "", ""
    priors.sort(key=lambda x: -x[0])
    best_score, best, best_scope = priors[0]
    if observations is not None:
        observations.update({prior.id: ts_to_iso(prior.updated_at) for _, prior, _ in priors})
    # The scope it was given for travels with the prediction, not only in
    # the evidence around it: measured live, out-of-scope predictions
    # repeated the answer assertively and left the evidence to explain.
    scope_note = (f"; prediction scope: decision {best.id}, which it leans on, {best_scope.replace('; ', ' and ')}, "
                  "so it is an analogy here, not the same decision" if best_scope else "")
    # A question that offers alternatives is answered by one of them.
    # Measured live on 5e967e4: "flush every 11 or 13 events?" got a
    # proposal about validating numeric options.
    choices = _offered_choices(question, context)
    if cfg is not None and cfg.semantic_retrieval:
        text, relation = llm_mod.compose_proposal(cfg, owner, question, context, [
            {"question": p.question, "answer": p.answer, "rationale": p.rationale, "scope": sc}
            for _s, p, sc in priors])
        if text and not _takes_a_choice(text, choices):
            return "", "", ""
        if text:
            # A concrete detail none of their answers states is taken out and
            # named as open, or the prediction is not made.
            source = "\n\n".join(f"(decision {p.id}, signed by {p.answered_by})\nQ: {p.question}\nA: {p.answer}"
                                  + (f"\nRationale: {p.rationale}" if p.rationale else "") for _s, p, _sc in priors)
            checked = _supported(cfg, question, source, text, "their earlier answers", _given(context, facts),
                                 prediction=True)
            if checked is None:
                return "", "", ""
            trimmed = checked != text
            text = checked
            how = ("the same policy as their signed answer" if relation == "same_policy" and not best_scope
                   and not trimmed else "by analogy with their signed answers")
            return text, best.id, (f"how they decide ({how}): composed from {len(priors)} of {owner}'s signed "
                                   f"answers (for example decision {best.id}); a prediction for {owner} to "
                                   "confirm" + ("; it leaves open what their earlier answers do not settle"
                                                if trimmed else "") + scope_note)
        return "", "", ""
    if not _takes_a_choice(best.answer, choices):
        return "", "", ""
    return best.answer, best.id, (f"how they decide: {owner} answered \"{_word_trunc(best.question, 80)}\" "
                                  f"(decision {best.id}, similarity {best_score:.2f})"
                                  + (f", because: {_word_trunc(best.rationale, 160)}" if best.rationale else "")
                                  + f"; a prediction for {owner} to confirm, not their answer here" + scope_note)


def _wide_select(store: Graph, cfg: Config, question: str, emb: list[float], terms: list[str], repo: str = "",
                 extra_rows: list | None = None, extra_emb: list[float] | None = None,
                 extra_mems: list | None = None):
    cands: dict[str, tuple[str, object]] = {}
    mems: list = store.anchored_answers(repo)
    for _, past in store.similar_answered(emb, top_k=6, min_score=0.02, repo=repo, query=question):
        if past.id not in {p.id for p in mems}:
            mems.append(past)
    seen_m = {p.id for p in mems}
    if extra_emb is not None:
        for _, p in store.similar_answered(extra_emb, top_k=4, min_score=0.02, repo=repo, query=question):
            if p.id not in seen_m:
                mems.append(p)
                seen_m.add(p.id)
    for p in (extra_mems or []):
        if p.id not in seen_m:
            mems.append(p)
            seen_m.add(p.id)
    for p in store.recent_answered(repo=repo, limit=3):
        if p.id not in seen_m and p.status != "partial":
            mems.append(p)
            seen_m.add(p.id)
    for i, past in enumerate(mems[:10], 1):
        cands[f"m{i}"] = ("memory", past)
    rows: list = list(extra_rows or [])
    seen = {r["ref"] for r in rows}
    for m in store.intents_matching(terms, limit=8, repo=repo):
        if m["ref"] not in seen:
            rows.append(m)
            seen.add(m["ref"])
    recent_rows = sorted((row for row, _ in store._intent_corpus(repo)[0]), key=lambda row: row["created_at"] or "", reverse=True)[:3]
    for m in recent_rows:
        if m["ref"] not in seen:
            rows.append(m)
            seen.add(m["ref"])
    for i, m in enumerate(rows[:14], 1):
        cands[f"r{i}"] = ("record", m)
    return _selector_pick(cfg, question, cands)


# ---------------- inbox integration ----------------

def ask(store, cfg: Config, run_id: str, question: str, context: str = "",
        path: str = "unknown", category: str = "", owner_id: str | None = None,
        requester: str = "", hints: list[str] | None = None, facts=None,
        keep_draft: bool = False, also_paths: list[str] | None = None, scratch: bool = False) -> dict:
    """Run one question through the ladder in front of the inbox. Returns
    the inbox's decision record with the ladder's outcome: kind evidence
    (resolved or partial, citation in evidence), prediction (assumed or
    proposed, to confirm), or new (pending, routed). A question that
    duplicates an already-pending decision returns that decision.
    `requester` names who the question is asked for; it is never routed
    back to them."""
    from .store import Invalid, repo_key
    graph = store.graph
    run = graph.get_task(run_id)
    if not run or run["status"] in ("completed", "abandoned"):
        raise Invalid("An active run is required")
    repo = graph.resolve_repo(repo_key(run["repo"]))
    with graph.source_scope(run_id):
        result = run_task(graph, cfg, run["title"], repo=repo, run_id=run_id, keep_draft=True, scratch=scratch,
                          decisions=[{"question": question, "category": category, "context": context,
                                      "path": path or "unknown", "requester": requester or "",
                                      "hints": list(hints or []), "facts": facts or {},
                                      "also_paths": list(also_paths or [])}])
    ids = result.decision_ids
    if not ids:
        publish_drafts(graph, result.drafts)
        raise Invalid("The ladder produced no decision")
    did = ids[0]
    dup = next((o for o in result.open if o.get("duplicate_of")), None)
    row = store.get_decision(did)
    # A duplicate returns its twin's row; the twin was routed in its own
    # run and is not routed, or asked for judgment, again here.
    if row["status"] == "pending" and dup is None:
        _inbox_route(store, graph, row, owner_id, run, repo,
                     suggest=not (cfg is not None and cfg.semantic_retrieval))
        row = store.get_decision(did)
    if not keep_draft:
        # The canvas keeps the node a draft until it has placed it on the
        # tree; every other caller publishes here.
        publish_drafts(graph, result.drafts)
    row["drafts"] = list(result.drafts)
    # The route that produced the owner, best first; absent when no rung
    # routed (a record or memory answered, or the question is a duplicate).
    routed = next((o for o in result.open + result.resolved if o.get("id") == did and "ranked" in o), None)
    if routed is not None:
        row["ranked"] = routed["ranked"]
    if dup:
        row["duplicate_stub"] = dup["stub"]
        row["note"] = dup["note"]
    return row


def _inbox_route(store, graph: Graph, row: dict, owner_id: str | None, run, repo: str, suggest: bool = True) -> None:
    """The inbox's own routing for a pending decision the graph could not
    route: an explicit owner, else the configured path patterns, plus a
    keyword suggestion from the same owner's prior answers."""
    import fnmatch
    from .store import Invalid, now
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        chosen, reason = row["owner_id"], row["routing_reason"]
        if owner_id:
            if not db.execute("SELECT 1 FROM owners WHERE id=?", (owner_id,)).fetchone():
                raise Invalid("Owner not found")
            chosen, reason = owner_id, "Explicitly selected for this request"
        elif chosen:
            # Whole, or cut at a word and marked. Measured live on eb9d22d: 26
            # stored reasons ended inside a word ("where a more sp", "26 mon").
            reason = "Routed from the ownership graph: " + _excerpt(row["owner_evidence"], 300,
                                                                    "the owner evidence has the rest")
        else:
            for owner in db.execute("SELECT * FROM owners ORDER BY created_at,id"):
                for pattern in (owner["patterns"] or "").split(","):
                    pattern = pattern.strip().lstrip("/")
                    if pattern and fnmatch.fnmatchcase((row["path"] or "").lstrip("/"), pattern):
                        chosen, reason = owner["id"], f"Path {row['path']} matches {pattern}"
        prediction, source_id, kind = row["prediction"], row["source_id"], row["kind"] or "new"
        source_revision = row.get("source_revision") or ""
        # A suggestion only when a prior answer from the same owner really
        # matches (the ladder's own memory floor), and never for a who or
        # why question, whose answer is a name and a reason, not a rule.
        # With a model, the ladder already read the owner's earlier answers
        # and proposed one only if it answers this; a keyword match is not
        # added on top. Measured live on 5e967e4: after the ladder found no
        # earlier answer that answers "flush every 11 or 13 events?", this
        # attached one about validating numeric options.
        if suggest and chosen and not prediction and (row["category"] or "") != "data-source":
            # Any signed decision in the org is evidence, whoever signed it;
            # the suggestion names its signer.
            choices = _offered_choices(row["question"], row["context"] or "")
            cands = [c for c in store.candidates(db, row["question"], None, repo=repo)
                     if c["similarity"] >= NEAR_MEMORY_MIN and not c.get("demoted_by")
                     and _takes_a_choice(c["answer"] or "", choices)]
            if cands:
                prediction, source_id, kind = cands[0]["answer"], cands[0]["id"], "prediction"
                # Pin only this fresh observation, on the same writer snapshot.
                # Do not manufacture missing revisions on older hints.
                observed = db.execute("SELECT updated_at FROM decisions WHERE id=?", (source_id,)).fetchone()
                source_revision = observed['updated_at'] if observed else ''
                # Same connection as the transaction: a write through the
                # graph's connection here would wait on our own lock.
                store.event(db, "prediction_suggested",
                            f"Unapproved suggestion from decision {cands[0]['id']}; hybrid similarity "
                            f"{cands[0]['similarity']}", row["id"], run["id"])
        db.execute("UPDATE decisions SET owner_id=?,routing_reason=?,prediction=?,source_id=?,source_revision=?,kind=?,updated_at=? WHERE id=?",
                   (chosen, reason if chosen else "No matching owner. Assign someone in the inbox.",
                    prediction, source_id, source_revision, kind, now(), row["id"]))
        db.execute("UPDATE runs SET status='needs_judgment',updated_at=? WHERE id=?", (now(), run["id"]))
        store.event(db, "judgment_requested", reason if chosen else "No matching owner. Assign someone in the inbox.",
                    row["id"], run["id"])
