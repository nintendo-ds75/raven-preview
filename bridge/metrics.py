"""What Raven saved, read from the ledger it already keeps.

Nothing here writes. Every number is derived from `decisions` and the
`events` table the inbox, the ladder and the canvas append to, over a
window of days, for the whole workspace and for each repository:

- proposal outcome: of the decisions Raven or the agent resolved
  without a person (a `resolve`, `proposed`, `assume` or `node_settled`
  event), how many a person signed unchanged, how many they corrected,
  how many a standing rule covered, and how many still wait;
- time to answer: an open question's creation to the first human
  answer (`owner_approved`, `answer_corrected`), median and p90;
- time to signature: a proposal's creation to the first human action
  on it (a sign-off, a signature or a correction), median and p90;
- re-ask rate: of the questions put to a person, how many had the same
  question text answered in the same repository inside the previous
  90 days, so the answer existed and was asked for again;
- reuse rate: decisions answered from memory (`resolve`/`proposed`
  with source `memory`), covered by a standing rule (signoff `rule`) or
  closed as the duplicate of an open twin;
- hand-on rate: of the decisions routed to a person, how many the
  first contact referred on (`owner_changed` with `referral`);
- agent wait avoided: for each reused decision, the time a person took
  to answer the decision it reused; when that is unknown, the window's
  median time to answer, counted as estimated;
- the same numbers per decision category (`by_category`), so the scope
  reviews an objective proposal opens (`objective_scope`) are read apart
  from the questions an agent asks on a task;
- work proposals: what became of the moves the objective head proposed,
  read from `objective_proposals` alone: how many were signed, corrected
  by a standing rule or dismissed, the precision that gives, how long a
  signature took, and the reasons people gave when they declined.
  Nothing in the proposal's own payload, confidence claims included, is
  read; only what a person did with it. A dismissed proposal whose
  source access is no longer current is counted but its title and
  reason are withheld, exactly as the Today page withholds the proposal.

bridge_wait durations are not persisted, so the wait avoided is the
human time the reused answer originally took, not a measured wait.
"""

import json
import re
from datetime import datetime, timedelta, timezone

from .store import Invalid, repo_key

PROPOSAL_EVENTS = ("resolve", "proposed", "assume", "node_settled")
ANSWER_EVENTS = ("owner_approved", "answer_corrected")
SIGN_EVENTS = ("signoff", "signature")
HUMAN_EVENTS = ANSWER_EVENTS + SIGN_EVENTS
TWIN_EVENTS = ("twin_closed", "dedup_open")
EVENT_KINDS = PROPOSAL_EVENTS + HUMAN_EVENTS + TWIN_EVENTS + ("owner_changed", "previous_answer")
# Nodes that were never a question for anyone: a follow-up a person
# suggested and the agent has not adopted, the suggestion once adopted,
# and nodes withdrawn with an abandoned task.
EXCLUDED_STATUSES = ("suggested", "adopted", "withdrawn")
REASK_DAYS = 90
DEFAULT_DAYS = 30
MAX_DAYS = 3650
OPEN_PROPOSAL_STATES = ("proposed", "review", "preparing", "review_error", "reconcile")
# A person signed these: `accepted`, and `superseded` once a later signed
# move on the same objective replaced it (the row keeps its acceptance).
SIGNED_PROPOSAL_STATES = ("accepted", "superseded")
WITHHELD_TITLE = "Proposal withheld: source access changed"
# Categories always reported, even when the window holds none of them.
REPORTED_CATEGORIES = ("", "objective_scope")
REASON_PREFIX = 60
TOP_REASONS = 5


def window_days(raw) -> int:
    """The window as days: the default when empty, 0 for all time, and
    never more than ten years. Anything else is a bad request."""
    text = str(raw if raw is not None else "").strip()
    if not text:
        return DEFAULT_DAYS
    if not text.isdigit():
        raise Invalid("days must be a whole number of days (0 for all time)")
    return min(int(text), MAX_DAYS)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def _parse(stamp: str):
    try:
        value = datetime.fromisoformat((stamp or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _seconds(start: str, end: str):
    a, b = _parse(start), _parse(end)
    if a is None or b is None:
        return None
    return max(0, int(round((b - a).total_seconds())))


def _detail(raw):
    if isinstance(raw, str) and raw.startswith("{"):
        try:
            return json.loads(raw)
        except ValueError:
            return {}
    return {}


def _percentiles(values):
    """Median and p90 (nearest rank) of a list of seconds, as integers."""
    values = sorted(v for v in values if v is not None)
    if not values:
        return {"count": 0, "median_seconds": None, "p90_seconds": None}
    n = len(values)
    median = values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2
    p90 = values[max(0, -(-9 * n // 10) - 1)]
    return {"count": n, "median_seconds": int(round(median)), "p90_seconds": int(p90)}


def _rate(part, whole):
    return round(part / whole, 3) if whole else None


class _Record:
    __slots__ = ("id", "repo", "category", "question", "status", "signoff", "owner_id", "source_id", "created_at",
                 "in_window", "proposal", "outcome", "asked", "memory", "rule", "duplicate", "handon",
                 "routed", "answer_seconds", "signature_seconds", "first_human_at", "reused_from")


def _records(db, cutoff: str, lookback: str, repo_filter: str):
    """Every decision since the lookback with the ledger events that
    matter here, folded into one record per decision."""
    args, where = [], ["d.draft=0"]
    if lookback:
        where.append("d.created_at >= ?")
        args.append(lookback)
    rows = db.execute(
        "SELECT d.id, d.run_id, d.question, d.status, d.signoff, d.owner_id, d.source_id, d.created_at, "
        "d.category, d.repo AS decision_repo, r.repo AS run_repo FROM decisions d JOIN runs r ON r.id=d.run_id "
        "WHERE " + " AND ".join(where) + " ORDER BY d.created_at, d.id", args).fetchall()
    marks = ",".join("?" for _ in EVENT_KINDS)
    event_args = list(EVENT_KINDS)
    event_where = f"decision_id IS NOT NULL AND kind IN ({marks})"
    if lookback:
        event_where += " AND created_at >= ?"
        event_args.append(lookback)
    events = {}
    for e in db.execute(f"SELECT decision_id, kind, detail, created_at FROM events WHERE {event_where} ORDER BY id",
                        event_args):
        events.setdefault(e["decision_id"], []).append((e["kind"], e["detail"], e["created_at"]))
    out = []
    for row in rows:
        if row["status"] in EXCLUDED_STATUSES:
            continue
        rec = _Record()
        rec.id, rec.question, rec.status = row["id"], row["question"], row["status"]
        rec.signoff, rec.owner_id, rec.source_id, rec.created_at = (row["signoff"] or "", row["owner_id"],
                                                                     row["source_id"] or "", row["created_at"])
        rec.repo = repo_key(row["decision_repo"] or row["run_repo"] or "")
        rec.category = row["category"] or ""
        if repo_filter and rec.repo != repo_filter:
            continue
        rec.in_window = not cutoff or rec.created_at >= cutoff
        _fold(rec, events.get(row["id"], []))
        out.append(rec)
    return out


def _fold(rec: _Record, events):
    """What the ledger says happened to one decision."""
    proposal_events = [(k, _detail(d), at) for k, d, at in events if k in PROPOSAL_EVENTS]
    rec.proposal = bool(proposal_events)
    rec.memory = any(d.get("source") == "memory" for k, d, _ in proposal_events if k in ("resolve", "proposed"))
    rec.rule = rec.signoff == "rule"
    rec.duplicate = rec.status == "duplicate" or any(k in TWIN_EVENTS for k, _, _ in events)
    rec.asked = not rec.proposal and not rec.duplicate
    rec.handon = any(k == "owner_changed" and _detail(d).get("referral") for k, d, _ in events)
    rec.reused_from = ""
    for k, d, _ in proposal_events:
        if d.get("cited"):
            rec.reused_from = str(d["cited"])
    for k, d, _ in events:
        if k in TWIN_EVENTS:
            twin = _detail(d)
            rec.reused_from = str(twin.get("settled_by") or twin.get("twin") or rec.reused_from)
    rec.reused_from = rec.reused_from or rec.source_id
    human = [(k, d, at) for k, d, at in events if k in HUMAN_EVENTS]
    rec.first_human_at = human[0][2] if human else ""
    rec.routed = rec.owner_id is not None or bool(human) or rec.handon
    first_answer = next((at for k, _, at in human if k in ANSWER_EVENTS), "")
    rec.answer_seconds = _seconds(rec.created_at, first_answer) if rec.asked and first_answer else None
    rec.signature_seconds = _seconds(rec.created_at, rec.first_human_at) if rec.proposal and human else None
    rec.outcome = ""
    if rec.proposal:
        if human:
            kind, raw, at = human[0]
            if kind in SIGN_EVENTS:
                rec.outcome = "signed_unchanged"
            else:
                signed = _detail(raw).get("answer", "")
                previous = [_detail(d).get("answer", "") for k, d, when in events
                            if k == "previous_answer" and when <= at]
                rec.outcome = ("signed_unchanged" if previous and _norm(previous[-1]) == _norm(signed)
                               else "corrected")
        elif rec.rule:
            rec.outcome = "by_rule"
        else:
            rec.outcome = "open"


def _answer_seconds(db, ids):
    """How long a person took on each of these decisions: creation to
    the first human event, for decisions outside the fetched range."""
    out = {}
    ids = [i for i in dict.fromkeys(ids) if i]
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        marks = ",".join("?" for _ in chunk)
        kinds = ",".join("?" for _ in HUMAN_EVENTS)
        for row in db.execute(
                f"SELECT d.id, d.created_at, MIN(e.created_at) AS first_human FROM decisions d "
                f"JOIN events e ON e.decision_id=d.id AND e.kind IN ({kinds}) WHERE d.id IN ({marks}) "
                "GROUP BY d.id, d.created_at", (*HUMAN_EVENTS, *chunk)):
            seconds = _seconds(row["created_at"], row["first_human"])
            if seconds is not None:
                out[row["id"]] = seconds
    return out


def _aggregate(records, answered_index, known_waits):
    """The numbers for one group of decisions (a repository, or all)."""
    proposals = [r for r in records if r.proposal]
    outcomes = {key: sum(1 for r in proposals if r.outcome == key)
                for key in ("signed_unchanged", "corrected", "by_rule", "open")}
    decided = outcomes["signed_unchanged"] + outcomes["corrected"]
    asked = [r for r in records if r.asked]
    repeated = 0
    for r in asked:
        created = _parse(r.created_at)
        if created is None:
            continue
        for other_id, answered_at in answered_index.get((r.repo, _norm(r.question)), ()):
            when = _parse(answered_at)
            if other_id != r.id and when is not None and when < created and (created - when).days <= REASK_DAYS:
                repeated += 1
                break
    reused = [r for r in records if r.memory or r.rule or r.duplicate]
    answers = _percentiles([r.answer_seconds for r in asked])
    median = answers["median_seconds"]
    saved, estimated, unknown = 0, 0, 0
    for r in reused:
        seconds = known_waits.get(r.reused_from)
        if seconds is None:
            if median is None:
                unknown += 1
                continue
            seconds, estimated = median, estimated + 1
        saved += seconds
    routed = [r for r in records if r.routed]
    return {
        "decisions": len(records),
        "proposals": {"total": len(proposals), **outcomes,
                      "unchanged_rate": _rate(outcomes["signed_unchanged"], decided),
                      "corrected_rate": _rate(outcomes["corrected"], decided)},
        "time_to_answer": answers,
        "time_to_signature": _percentiles([r.signature_seconds for r in proposals]),
        "reask": {"asked": len(asked), "repeated": repeated, "rate": _rate(repeated, len(asked))},
        "reuse": {"total": len(reused), "from_memory": sum(1 for r in reused if r.memory),
                  "by_rule": sum(1 for r in reused if r.rule),
                  "duplicates": sum(1 for r in reused if r.duplicate and not (r.memory or r.rule)),
                  "rate": _rate(len(reused), len(records))},
        "handon": {"routed": len(routed), "handed_on": sum(1 for r in routed if r.handon),
                   "rate": _rate(sum(1 for r in routed if r.handon), len(routed))},
        "wait_saved": {"seconds": saved, "decisions": len(reused) - unknown, "estimated": estimated},
    }


def _withheld(db, row) -> bool:
    """Whether the proposal's text must be withheld: the same check the
    viewer routes make before showing a proposal, so lost source access
    withholds the title and the reason here too."""
    from .objectives import sources_current_db
    try:
        sources_current_db(db, json.loads(row["sources"] or "[]"), row["repository"])
    except Invalid:
        return True
    return False


def _declined_recent(db, repo):
    """The rows the next investigation of this repository reads
    (objective_head.declined_before), with withheld ones reduced to the
    stub the Today page shows: no title, no reason."""
    from .objective_head import DECLINED_LIMIT
    rows = db.execute("SELECT repository, payload, sources, rationale, updated_at FROM objective_proposals "
                      "WHERE repository=? AND state='dismissed' ORDER BY updated_at DESC LIMIT " + str(int(DECLINED_LIMIT)), (repo,))
    out = []
    for r in rows:
        if _withheld(db, r):
            out.append({"repository": repo, "withheld": True, "title": WITHHELD_TITLE, "updated_at": r["updated_at"]})
        else:
            out.append({"repository": repo, "title": str(json.loads(r["payload"]).get("title") or ""),
                        "rationale": r["rationale"], "updated_at": r["updated_at"]})
    return out


def _work_proposals(db, cutoff: str, repo: str) -> dict:
    """What became of the moves the objective head proposed in the
    window, from the proposal ledger's own states and timestamps: the
    person's action on each proposal, never the proposal's own words."""
    args, where = [], []
    if cutoff:
        where.append("created_at >= ?")
        args.append(cutoff)
    if repo:
        where.append("repository = ?")
        args.append(repo)
    rows = db.execute("SELECT state, rationale, created_at, updated_at, repository, sources FROM objective_proposals"
                      + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at, id", args).fetchall()
    states = [r["state"] for r in rows]
    signed = sum(1 for s in states if s in SIGNED_PROPOSAL_STATES)
    corrected, dismissed = states.count("reconcile"), states.count("dismissed")
    reasons = {}
    for r in rows:
        if r["state"] == "dismissed" and not _withheld(db, r):
            prefix = " ".join((r["rationale"] or "").split())[:REASON_PREFIX]
            reasons[prefix] = reasons.get(prefix, 0) + 1
    top = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_REASONS]
    repos = [repo] if repo else [r["repository"] for r in db.execute(
        "SELECT DISTINCT repository FROM objective_proposals WHERE state='dismissed' ORDER BY repository")]
    return {
        "created": len(rows),
        "open": sum(1 for s in states if s in OPEN_PROPOSAL_STATES),
        "signed": signed, "corrected": corrected, "dismissed": dismissed,
        "precision": _rate(signed, signed + corrected + dismissed),
        # A signed row's updated_at is the acceptance: the activation is the
        # last write to the row (superseding it later does not touch it), so
        # created_at -> updated_at is the wait.
        "time_to_signature": _percentiles([_seconds(r["created_at"], r["updated_at"]) for r in rows if r["state"] in SIGNED_PROPOSAL_STATES]),
        "dismissed_reasons": [{"reason": reason, "count": count} for reason, count in top],
        "declined_recent": [row for name in repos for row in _declined_recent(db, name)],
    }


def summary(store, repo: str = "", days=DEFAULT_DAYS) -> dict:
    """What Raven saved in the last `days` days (0 for all time), for the
    workspace and, when no repository is named, for each repository."""
    days = window_days(days)
    repo = repo_key(repo)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat() if days else ""
    lookback = (now - timedelta(days=days + REASK_DAYS)).isoformat() if days else ""
    db = store.graph.db
    records = _records(db, cutoff, lookback, repo)
    # Where an answer already existed: every decision a person answered
    # or signed, by repository and question text, with when they did.
    answered_index = {}
    for r in records:
        if r.first_human_at:
            answered_index.setdefault((r.repo, _norm(r.question)), []).append((r.id, r.first_human_at))
    windowed = [r for r in records if r.in_window]
    known = {r.id: r.answer_seconds if r.answer_seconds is not None else r.signature_seconds
             for r in records if r.first_human_at}
    missing = [r.reused_from for r in windowed if (r.memory or r.rule or r.duplicate)
               and r.reused_from and known.get(r.reused_from) is None]
    known.update(_answer_seconds(db, missing))
    known = {k: v for k, v in known.items() if v is not None}
    out = {"window_days": days, "since": cutoff, "until": now.isoformat(), "repo": repo,
           **_aggregate(windowed, answered_index, known)}
    if not repo:
        groups = {}
        for r in windowed:
            groups.setdefault(r.repo, []).append(r)
        out["by_repo"] = [{"repo": name, **_aggregate(rows, answered_index, known)}
                          for name, rows in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))]
    categories = sorted(set(REPORTED_CATEGORIES) | {r.category for r in windowed})
    out["by_category"] = [{"category": name, **_aggregate([r for r in windowed if r.category == name], answered_index, known)}
                          for name in categories]
    out["work_proposals"] = _work_proposals(db, cutoff, repo)
    return out
