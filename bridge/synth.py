"""Sealed-key exams: premise to synthetic org to hard questions to strict
grading. Ported. Everything here is real inference on the operator's key;
load_case loads a case dict into the graph so the same ladder answers it.
"""

from __future__ import annotations

import json
import time

from . import llm as llm_mod
from .config import Config
from .graph import Graph, ts_to_iso

REPO = "synthco"

CASE_SYSTEM = (
    "You design adversarial test organizations for Bridge, a tool that "
    "resolves the hidden decisions inside software tasks from an org's "
    "records, memory, and ownership graph. Given a premise, invent a "
    "realistic engineering org for it. Return ONLY a JSON object, no prose, "
    "with exactly these fields:\n"
    "  engineers: 6 to 10 full names, diverse, no two sharing a first name\n"
    "  services: 3 to 5 short lowercase service names (valid directory "
    "names, premise-specific, never generic like 'backend')\n"
    "  blame: object mapping each service name to a list of [engineer, "
    "share] pairs, shares between 0 and 1, one clear primary owner per "
    "service (share 0.5 to 0.8), one or two minor contributors\n"
    "  codeowners: object mapping each service name to one engineer; "
    "exactly ONE service must list an engineer who is NOT its top blame "
    "owner (a deliberately stale CODEOWNERS entry)\n"
    "  reviews: object mapping a few engineers to review shares 0.05-0.35\n"
    "  prs: 12 to 18 merged PRs [{ref, title, author, body, age}], age in "
    "days 5-400. At least 4 must contain real recorded decisions with the "
    "reason; the rest are realistic noise. Bodies 1-3 sentences.\n"
    "  tickets: 6 to 10 tickets [{ref, title, assignee, body, age, status}] "
    "with premise-appropriate ref prefixes and a workflow status (Done, In "
    "Progress, To Do). At least 2 contain real decisions; at least 1 "
    "decision-bearing ticket must be stale (age over 350) with content that "
    "is now wrong; at least 1 must be an open proposal (To Do).\n"
    "  memory: 3 to 5 past signed answers [{q, answer, by, ageDays, "
    "superseded}]. Include one older than 600 days and two live answers to "
    "the SAME question that disagree.\n"
    "  traps: list of short strings naming every trap you planted and the "
    "correct behavior.\n"
    "Decision-bearing content must never quote the premise verbatim. No em "
    "dashes or en dashes anywhere."
)

PROF_SCALE = (
    "\nProduction scale for this org: 24 to 32 PRs, 12 to 16 tickets, 6 to "
    "8 memory entries. Layer the traps: include one decision chain of three "
    "records where the newest reverses an intermediate reversal (only the "
    "newest is correct), one pair of records that partially conflict where "
    "scope decides which applies, one stale memory contradicted by a newer "
    "record, and one service whose CODEOWNERS, top blame owner, and most "
    "frequent reviewer are three different people."
)

EXAM_SYSTEM = (
    "You are a professor writing a hard exam for a student system named "
    "Bridge, which resolves questions from an org's memory (signed past "
    "answers with time decay and newest-wins conflicts), records (merged "
    "PRs and tickets), safe assumptions (only categories rollout, compat, "
    "ux, with precedent), or by routing to the accountable human (blame "
    "beats stale CODEOWNERS). You have the org's full ground truth. Write "
    "the ANSWER KEY BEFORE the student ever sees the exam. Every question "
    "must be genuinely hard: heavily paraphrased, testing a planted trap, "
    "requiring the newest link of a decision chain, or scoped so a "
    "plausible neighbor answer is wrong. Return ONLY a JSON array of:\n"
    "  {question, category, key: {resolution, fact, owner, cited, rubric}}\n"
    "category: definition, policy, data-source, rollout, compat, ux, ops. "
    "key.resolution: memory | record | assumption | open. key.fact: the "
    "precise fact a full-credit answer must state (empty for open). "
    "key.owner: the correct human for open questions, else \"\". "
    "key.cited: the record ref a record resolution must cite, else \"\". "
    "key.rubric: one sentence stating what earns full credit and the "
    "classic wrong answer that earns zero. Cover the ladder. No em dashes "
    "or en dashes."
)

GRADE_SYSTEM = (
    "You are a professor grading one exam answer from the student system "
    "Bridge against your pre-written answer key. You also have the org's "
    "full ground truth. Grade strictly:\n"
    "  3: matches the key: right resolution rung AND states the key fact "
    "(or names the key owner for open questions), with a citation "
    "consistent with the key when one is specified.\n"
    "  2: right rung but the fact is incomplete, the citation is adjacent, "
    "or the owner is a defensible co-owner.\n"
    "  1: wrong rung but safe: escalated to a human instead of answering, "
    "or refused with a stated reason.\n"
    "  0: the classic wrong answer: states the reversed or stale fact, "
    "cites a record that does not support it, resolves what the key says "
    "must route, or routes to the trap owner.\n"
    "The student cites memory entries by internal decision id (a hex "
    "string); that is a legitimate reference into the org's memory store. "
    "Return ONLY JSON: {score, verdict, missing, comment, improvement} "
    "where verdict is pass (3) / partial (2) / safe_miss (1) / harmful "
    "(0). No em dashes or en dashes."
)


def _client(cfg: Config) -> llm_mod.Client:
    return llm_mod.Client(cfg)


def gen_case(cfg: Config, premise: str, complexity: int = 2) -> dict:
    hardness = {1: "moderate", 2: "high", 3: "brutal"}.get(int(complexity), "high")
    system = CASE_SYSTEM + (PROF_SCALE if int(complexity) >= 3 else "")
    raw = _client(cfg).complete_json(
        "gen_case", system,
        f"Premise: {premise.strip()}\nDifficulty: {hardness} (scale noise volume and trap subtlety accordingly).")
    if not isinstance(raw, dict):
        raise llm_mod.LLMError("case generation returned a non-object")
    memory = raw.pop("memory", [])
    traps = raw.pop("traps", [])
    raw.setdefault("engineers", [])
    raw["premise"] = premise.strip()
    return {"case": raw, "memory": memory, "traps": traps}


def gen_exam(cfg: Config, case: dict, n: int = 10) -> list[dict]:
    payload = json.dumps(case, separators=(",", ":"))[:28000]
    raw = _client(cfg).complete_json(
        "gen_exam", EXAM_SYSTEM,
        f"The org (ground truth):\n{payload}\n\nWrite {int(n)} exam questions with the full answer key.")
    if not isinstance(raw, list):
        raise llm_mod.LLMError("exam generation returned a non-array")
    out = []
    for item in raw:
        if isinstance(item, dict) and item.get("question"):
            item.setdefault("category", "policy")
            item.setdefault("key", {})
            out.append(item)
    return out[: int(n)]


def grade(cfg: Config, case: dict, item: dict, result: dict) -> dict:
    prompt = (
        f"Ground truth org:\n{json.dumps(case, separators=(',', ':'))[:24000]}\n\n"
        f"Exam question: {item.get('question')}\nCategory: {item.get('category')}\n"
        f"Answer key: {json.dumps(item.get('key', {}))}\n\n"
        f"Student Bridge's report:\n{json.dumps(result, indent=1)[:4000]}")
    raw = _client(cfg).complete_json("grade", GRADE_SYSTEM, prompt)
    if not isinstance(raw, dict) or "score" not in raw:
        raise llm_mod.LLMError("grader returned no score")
    raw["score"] = max(0, min(3, int(raw["score"])))
    return raw


CHANGES_PER_SERVICE = 20


def load_case(store: Graph, payload: dict, repo: str = REPO) -> dict:
    """Load a case dict (bench-built or generated) into the graph: PRs and
    tickets as intents linked to the services they mention, blame and
    reviews as synthetic changes in the shared people-to-paths structure
    (the same rows an ingested repository writes, so routing takes the
    same path), CODEOWNERS as listings, the ownership rows the
    introspection rung reads, and memory as signed decisions with their
    age preserved."""
    from datetime import datetime, timedelta, timezone

    def _ts(age_days) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=float(age_days or 0))).isoformat()

    c = payload.get("case", payload)
    services = sorted(set(c.get("blame") or {}) | set(c.get("codeowners") or {}))

    def _paths_named(*texts: str) -> list[str]:
        low = " ".join(texts).lower()
        return [s + "/" for s in services if s.lower() in low]

    for e in c.get("engineers", []):
        store.upsert_engineer(e)
    for pr in c.get("prs", []):
        store.upsert_intent(repo, "pr", str(pr["ref"]), pr.get("title", ""), pr.get("body", ""),
                            pr.get("author", ""), _ts(pr.get("age")))
        store.add_intent_paths(repo, "pr", str(pr["ref"]), _paths_named(pr.get("title", ""), pr.get("body", "")))
    for tk in c.get("tickets", []):
        status = str(tk.get("status") or "")
        resolved = status.lower() in ("", "done", "closed", "resolved", "shipped")
        store.upsert_intent(repo, "ticket", str(tk["ref"]), tk.get("title", ""), tk.get("body", ""),
                            tk.get("assignee", ""), _ts(tk.get("age")), status=status, resolved=resolved)
        store.add_intent_paths(repo, "ticket", str(tk["ref"]), _paths_named(tk.get("title", ""), tk.get("body", "")))
    # Blame shares become authored changes over the last half year, one
    # file per service, the authors interleaved so shares and not list
    # order decide recency; review shares become Reviewed-by roles spread
    # over everyone else's changes.
    store.set_source(repo, "git_now", _ts(0))
    changes: list[tuple[str, str, str, str]] = []
    for svc, rows in (c.get("blame") or {}).items():
        path = f"{svc}/{svc}.py"
        store.upsert_artifact(repo, path, CHANGES_PER_SERVICE)
        left = {}
        for eng, share in rows:
            store.set_ownership(repo, svc + "/", eng, "blame", float(share),
                                f"{round(float(share) * 100)}% of commit touches under {svc}/")
            left[eng] = round(float(share) * CHANGES_PER_SERVICE)
        j = 0
        while any(left.values()):
            for eng in left:
                if left[eng]:
                    left[eng] -= 1
                    changes.append((f"synth{len(changes):04d}", _ts(5 + 170 * j / CHANGES_PER_SERVICE), path, eng))
                    j += 1
    people: dict[str, list[tuple[str, str, str]]] = {sha: [(eng, "", "author")] for sha, _t, _p, eng in changes}
    offset = 0
    for eng, share in (c.get("reviews") or {}).items():
        if share:
            store.set_ownership(repo, "", eng, "review", float(share),
                                f"reviewed {round(float(share) * 100)}% of recent changes")
            others = [sha for sha, _t, _p, author in changes if author != eng]
            for i in range(round(float(share) * len(others))):
                people[others[(offset + i) % len(others)]].append((eng, "", "reviewed-by"))
            offset += round(float(share) * len(others))
    for sha, ts, path, eng in changes:
        store.add_change(repo, sha, ts, [path], people[sha])
    for i, (svc, owner) in enumerate((c.get("codeowners") or {}).items()):
        store.set_ownership(repo, svc + "/", "@" + owner.split()[0].lower(), "codeowners", 1.0,
                            f"CODEOWNERS lists {owner} for {svc}/")
        store.add_listing(repo, "codeowners", svc + "/", "@" + owner.split()[0].lower(), ord=i)
    run_id = store.create_task("seeded memory", agent="synth", repo=repo)
    now = time.time()
    for m in payload.get("memory", []):
        did = store.add_decision(run_id, m["q"], "policy", "approved", "human", m["answer"], m["by"],
                                 embedding=llm_mod.embed(m["q"]), repo=repo, kind="new")
        age = now - float(m.get("ageDays", 0)) * 86400
        store.db.execute("UPDATE decisions SET updated_at=?, created_at=?, superseded_by=? WHERE id=?",
                         (ts_to_iso(age), ts_to_iso(age), "retired" if m.get("superseded") else "", did))
    counts = store.counts()
    store.append_event("case_loaded", {"intents": counts["intents"], "ownership": counts["ownership"]})
    return counts
