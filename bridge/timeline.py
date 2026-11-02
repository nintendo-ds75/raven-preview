"""What's been done on a task: the story of its judgment calls, one plain
sentence per row, built from the events Raven already stores.

The Being built card and the task page show this under WHAT'S BEEN DONE.
Nothing here is composed by a model and nothing is invented: every row is
a stored event (the kickoff verdict, a call settled from an earlier call,
a record or a meeting, a guess routed to its owner, a call flagged to the
person who decides, a hand-on, an answer, a correction, the agent stopping,
the finish) rendered through the templates in this module. Consecutive
rows of the same kind fold into one ("Settled 4 from earlier calls: …"),
so a task with twenty calls still reads in a dozen lines. Times are ISO;
the page turns them into the viewer's local clock.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from .agent_names import display_name

HIDDEN = ("duplicate", "suggested", "adopted", "withdrawn")
LIVE_EXCLUDED = ("completed", "failed", "cancelled", "abandoned")
# Rows that fold into their neighbour of the same kind.
GROUPABLE = ("settled", "settled_record", "settled_meeting", "checking", "confirmed", "guess", "assumed",
             "flagged", "folded", "conflict")
# Rows of one kind this close together fold into one row, so that a
# burst of settles with a route in between still reads as one line.
FOLD_SECONDS = 120
# A later outcome for the same call makes an earlier static route noise:
# the person was never reached by it.
OUTCOMES = ("resolve", "proposed", "assume", "dedup_open", "meeting_precedent")
TOPIC_WIDTH = 48
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# At most this many live tasks carry a timeline on /api/state.
STATE_LIMIT = 25

_CITE_RE = re.compile(r"^(merge|pull|pr|commit|slack|ticket|jira|issue|doc|adr|meeting|note|email|postmortem)\s+#?(\S+)", re.I)
_AREA_RE = re.compile(r"\b(decides|approves|knows)\s+for\s+([^(;]+?)(?:\s*\(|;|$)")


def _json(raw):
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first(name) -> str:
    return str(name or "").strip().split()[0] if str(name or "").strip() else ""


def _clip(text, width) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= width:
        return text
    cut = text[:width].rsplit(" ", 1)[0].rstrip(" ,;:")
    return (cut if len(cut) >= width // 2 else text[:width - 1].rstrip()) + "…"


def topic(question) -> str:
    """A question as a short topic inside a sentence: no trailing question
    mark, cut at a word."""
    return _clip(str(question or "").strip().rstrip("?").rstrip(), TOPIC_WIDTH)


def when(iso) -> str:
    """'Sep 17' from an ISO stamp, or '' when there is none."""
    text = str(iso or "")
    try:
        d = datetime.fromisoformat(text[:19]) if len(text) >= 10 else None
    except ValueError:
        d = None
    return f"{MONTHS[d.month - 1]} {d.day}" if d else ""


def _area(evidence) -> str:
    """'pricing decisions' from the owner evidence the router wrote
    ('verified: Dana Whitfield decides for pricing decisions (...)'): the
    first area that is not a path, else the path."""
    found = [m.group(2).strip() for m in _AREA_RE.finditer(evidence or "")]
    for area in found:
        if "/" not in area and "." not in area:
            return area
    return found[0] if found else ""


def _call_day(prior) -> str:
    """When the earlier call was made: the meeting's day for one settled
    from a meeting, else the day the decision was recorded."""
    if not prior:
        return ""
    if prior.get("source") == "meeting" or prior.get("answered_via") == "meeting":
        return when(prior.get("source_revision")) or when(prior.get("created_at"))
    return when(prior.get("created_at"))


def _call_label(prior) -> str:
    """'Raj's Sep 17 call' for an earlier decision."""
    who = _first(prior.get("answered_by") or prior.get("signed_by") or prior.get("owner_name")) if prior else ""
    day = _call_day(prior)
    return f"{who}'s {day + ' ' if day else ''}call" if who else "an earlier call"


def _call_ref(prior, label="") -> dict:
    return {"kind": "call", "id": prior.get("id", ""), "run_id": prior.get("run_id", ""),
            "label": label or _call_label(prior)}


def record_refs(cited, meetings=None) -> list[dict]:
    """The records a resolve cites ('merge 36; commit 387e508c8d5d; slack
    pricing-2026-09-24 [decided]; ticket BILL-201 (resolved): title') as
    short labels: PR #36, commit 387e508, #pricing, BILL-201."""
    out: list[dict] = []
    seen = set()
    for token in re.split(r";\s*", str(cited or "")):
        token = token.strip()
        if not token:
            continue
        if token.lower().startswith("ownership graph"):
            label, kind, ref = "the ownership graph", "graph", ""
        else:
            m = _CITE_RE.match(token)
            if not m:
                continue
            kind, ref = m.group(1).lower(), m.group(2).rstrip(":,")
            if kind in ("merge", "pull", "pr") and ref.isdigit():
                label, kind = f"PR #{ref}", "pr"
            elif kind == "commit":
                label = f"commit {ref[:7]}"
            elif kind == "slack":
                label = "#" + re.sub(r"-\d{4}-\d{2}-\d{2}$", "", ref)
            elif kind in ("ticket", "jira", "issue"):
                label, kind = ref.upper(), "ticket"
            elif kind in ("doc", "adr"):
                label, kind = ref.upper() if re.match(r"(?i)adr-?\d+", ref) else ref, "doc"
            elif kind == "meeting":
                mid = ref.split(":")[1] if ref.startswith("meeting:") and ref.count(":") >= 1 else ref
                title = (meetings or {}).get(mid) or ""
                label, ref = (f"the {title}" if title else "a meeting"), mid
            else:
                label = f"{kind} {ref}"
        if label.lower() in seen:
            continue
        seen.add(label.lower())
        out.append({"kind": kind, "id": ref, "label": label})
    return out


def _via(source) -> str:
    label = str(source or "").lower()
    if label.startswith("slack"):
        return " on Slack"
    if label.startswith("teams"):
        return " on Teams"
    if label.startswith("github"):
        return " in a GitHub review"
    if label.startswith("meeting"):
        return " in a meeting"
    if label.startswith("confirmed interview"):
        return " on a Raven call"
    return ""


def _join(items, limit=4) -> str:
    shown = [i for i in items[:limit]]
    rest = len(items) - len(shown)
    return " · ".join(shown) + (f" · and {rest} more" if rest > 0 else "")


# ---------------- the templates ----------------
# Each kind renders one row from its items: a single item is a sentence,
# several are the folded form. An item is what the event said about one
# call, already resolved to names and labels.

def _t_engaged(items):
    it = items[0]
    who = _first(it.get("requester"))
    n = it.get("count", 0)
    if it.get("verdict") == "pass":
        return "Passed: nothing in this task needs a person's call" + (f"; {it['why']}" if it.get("why") else "")
    head = f"Engaged: {who}'s prompt read" if who else "Engaged: the prompt read"
    if it.get("verdict") == "unplaced":
        return head + "; the task names no area Raven knows yet"
    return head + (f"; {n} judgment call{'s' if n != 1 else ''} identified" if n else "; the agent is writing down its judgment calls")


def _t_settled(items):
    tail = items[0].get("tail", "nobody asked")
    if len(items) == 1:
        it = items[0]
        part = " in part" if it.get("partial") else ""
        agrees = f"; {it['agrees']} agrees" if it.get("agrees") else ""
        return f"Settled “{it['topic']}”{part} from {it['call']}{agrees}; {tail}"
    def label(it):
        inside = ", ".join(x for x in (it.get("who"), it.get("day")) if x)
        if it.get("agrees"):
            inside += f"; {it['agrees']} agrees"
        return f"{it['topic']}{' in part' if it.get('partial') else ''} ({inside})" if inside else it["topic"]
    return f"Settled {len(items)} from earlier calls, {tail}: {_join([label(it) for it in items])}"


def _t_settled_record(items):
    if len(items) == 1:
        it = items[0]
        part = " in part" if it.get("partial") else ""
        refs = " · ".join(r["label"] for r in it.get("refs", [])) or "the repo's own record"
        return f"Settled “{it['topic']}”{part} from the repo's own record: {refs}; {it.get('tail', 'nobody asked')}"
    body = _join([f"{it['topic']} ({', '.join(r['label'] for r in it.get('refs', [])[:2]) or 'record'})" for it in items])
    return f"Settled {len(items)} from the repo's own record: {body}"


def _t_settled_meeting(items):
    if len(items) == 1:
        it = items[0]
        quote = f": {it['speaker']} said “{it['quote']}”" if it.get("quote") else f": {it['speaker']} said so" if it.get("speaker") else ""
        return f"Settled “{it['topic']}” from {it['meeting']}{quote}; {it.get('tail', 'nobody asked')}"
    return f"Settled {len(items)} from {items[0]['meeting']}, nobody asked: {_join([it['topic'] for it in items])}"


def _t_checking(items):
    if len(items) == 1:
        it = items[0]
        return f"Checking “{it['topic']}” against {it['call']}; nobody asked yet"
    body = _join([f"{it['topic']} ({it['who']}, {it['day']})" for it in items])
    return f"Checking {len(items)} against earlier calls, nobody asked yet: {body}"


def _t_confirmed(items):
    if len(items) == 1:
        it = items[0]
        return f"Confirmed: {it['call']} covers “{it['topic']}”; {it.get('tail', 'nobody asked')}"
    body = _join([f"{it['topic']} ({it['who']}, {it['day']})" for it in items])
    return f"Confirmed {len(items)} against earlier calls, nobody asked: {body}"


def _t_guess(items):
    who = items[0].get("confirmer") or "its owner"
    if len(items) == 1:
        it = items[0]
        return f"Raven's guess on “{it['topic']}” from {it['from']}; {who} confirms"
    body = _join([f"{it['topic']} ({it['from']})" for it in items])
    return f"Raven's guess on {len(items)}, {who} confirms: {body}"


def _t_assumed(items):
    if len(items) == 1:
        it = items[0]
        return f"Assumed “{it['topic']}” (reversible): {it['default']}" + (f"; {it['owner']} can correct it" if it.get("owner") else "")
    body = _join([f"{it['topic']}: {it['default']}" for it in items])
    return f"Assumed {len(items)} (reversible): {body}"


def _t_flagged(items):
    it = items[0]
    who, area = it.get("owner", ""), it.get("area", "")
    if not who:
        return (f"Nobody on the map owns “{it['topic']}”; it waits for someone to take it" if len(items) == 1
                else f"Nobody on the map owns {len(items)}: {_join([i['topic'] for i in items])}")
    decides = f", who decides for {area}" if area else ""
    if len(items) == 1:
        return f"Flagged “{it['topic']}” to {who}{decides}; waiting on {who}"
    return f"Flagged {len(items)} to {who}{decides}: {_join([i['topic'] for i in items])}"


def _t_folded(items):
    if len(items) == 1:
        it = items[0]
        return f"Folded “{it['topic']}” into the open call “{it['twin']}”: one answer settles both"
    return f"Folded {len(items)} into calls already open: {_join([it['topic'] for it in items])}"


def _t_conflict(items):
    it = items[0]
    what = f"on “{it['topic']}”" if len(items) == 1 else f"on {len(items)}: {_join([i['topic'] for i in items])};"
    return (f"Two earlier calls disagreed {what} preferred {it['kept']} over {it['over']}; "
            + ("the older is retired" if it.get("retired") else "both stand")).replace(";; ", "; ").replace("” preferred", "”; preferred")


def _t_handed(items):
    it = items[0]
    text = f"{it['by']} handed “{it['topic']}” to {it['to']}" if it.get("by") else f"“{it['topic']}” reassigned to {it['to']}"
    if it.get("scopes"):
        text += f"; Raven routes {', '.join(it['scopes'][:3])} to {it['to']} from now on"
    return text


def _t_answered(items):
    it = items[0]
    if it.get("guessed"):
        return f"{it['by']} confirmed Raven's guess on “{it['topic']}”{it.get('via', '')}"
    if it.get("signed"):
        return f"{it['by']} signed “{it['topic']}”{it.get('via', '')}" + (f"; {it['remaining']} still to sign" if it.get("remaining") else "")
    return f"{it['by']} answered “{it['topic']}”{it.get('via', '')}"


def _t_corrected(items):
    it = items[0]
    why = f": {it['why']}" if it.get("why") else ""
    tail = ("; the earlier call is retired and this is the standing rule now" if it.get("replaced")
            else "; the earlier call stays for other cases" if it.get("scope") == "here" else "")
    return f"{it['by']} corrected “{it['topic']}”{why}{tail}"


def _t_withdrawn(items):
    it = items[0]
    return f"Withdrew Raven's guess on “{it['topic']}”" + (f": {it['why']}" if it.get("why") else "")


def _t_review(items):
    it = items[0]
    return f"“{it['topic']}” needs another look" + (f": {it['why']}" if it.get("why") else "")


def _t_reframed(items):
    it = items[0]
    return f"{it['by']} corrected the question to “{it['topic']}”; it is routed afresh"


def _t_followup(items):
    it = items[0]
    return f"{it['by']} added a follow-up: “{it['topic']}”"


def _t_note(items):
    it = items[0]
    return f"{it['by']} added a note: “{it['text']}”"


def _t_proposed(items):
    it = items[0]
    return f"{it['agent']} proposed an answer for “{it['topic']}”" + (f"; {it['owner']} signs it" if it.get("owner") else "; it waits for a signature")


def _t_stopped(items):
    it = items[0]
    n = it.get("waiting", 0)
    return f"{it['agent']} stopped" + (f", waiting on {n} open call{'s' if n != 1 else ''}" if n else "")


def _t_triaged(items):
    it = items[0]
    return f"A second read changed the verdict to {it['verdict']}" + (f": {it['why']}" if it.get("why") else "")


def _t_finished(items):
    it = items[0]
    if it.get("abandoned"):
        return "Abandoned" + (f": {it['why']}" if it.get("why") else "")
    if it.get("reopened"):
        return "Reopened" + (f": {it['why']}" if it.get("why") else "")
    n, signed = it.get("count", 0), it.get("signed", 0)
    return f"Finished: {signed} of {n} call{'s' if n != 1 else ''} authorized by a person" if n else "Finished"


TEMPLATES = {
    "engaged": _t_engaged, "settled": _t_settled, "settled_record": _t_settled_record,
    "settled_meeting": _t_settled_meeting, "checking": _t_checking, "confirmed": _t_confirmed,
    "guess": _t_guess, "assumed": _t_assumed, "flagged": _t_flagged, "folded": _t_folded,
    "conflict": _t_conflict, "handed": _t_handed, "answered": _t_answered, "corrected": _t_corrected,
    "withdrawn": _t_withdrawn, "review": _t_review, "reframed": _t_reframed, "followup": _t_followup,
    "note": _t_note, "proposed": _t_proposed, "stopped": _t_stopped, "triaged": _t_triaged,
    "finished": _t_finished,
}


def render(kind, items) -> str:
    return TEMPLATES[kind](items)


# ---------------- the build ----------------

def _row(kind, at, item, refs=(), people=(), group=None):
    names = []
    for p in people:
        if p and p not in names:
            names.append(p)
    return {"kind": kind, "at": at, "items": [item], "refs": list(refs),
            "people": names, "group": group if group is not None else kind}


def _visible(decisions):
    return [d for d in decisions.values() if d.get("status") not in HIDDEN and not d.get("superseded_by")
            and not d.get("draft")]


def build(run, decisions, events, priors=None, meetings=None) -> list[dict]:
    """The rows for one task, oldest first. `decisions` maps id to the
    task's own decision rows (with owner_name), `priors` maps id to the
    earlier decisions the events cite, `meetings` maps meeting id to its
    title. Pure: nothing here reads a store."""
    priors = priors or {}
    meetings = meetings or {}
    agent = display_name(run.get("agent")) if run.get("agent") else "The coding agent"
    visible = _visible(decisions)
    count = len(visible)

    def dec(did):
        return decisions.get(did) or priors.get(did) or {}

    def prior_of(did_or_row):
        row = dec(did_or_row) if isinstance(did_or_row, str) else did_or_row
        return priors.get(row.get("source_id") or "") or decisions.get(row.get("source_id") or "") or {}

    def tail_for(d):
        if d.get("signoff") == "rule":
            return "nobody asked"
        if d.get("signoff") == "signed" and d.get("signed_by"):
            return f"{_first(d['signed_by'])} signed it"
        if d.get("status") == "partial" and d.get("owner_name"):
            return f"{_first(d['owner_name'])} signs the rest"
        if d.get("owner_name"):
            return f"{_first(d['owner_name'])} signs it"
        return "nobody asked"

    # Which calls have a later outcome: a static route that the model pass
    # then settled never reached anyone.
    last_outcome: dict[str, int] = {}
    for e in events:
        if e["kind"] in OUTCOMES and e.get("decision_id"):
            last_outcome[e["decision_id"]] = e["id"]
    rows: list[dict] = []
    seen: dict[str, set] = {}
    agrees: dict[str, str] = {}
    started_verdict = ""

    def mark(did, kind):
        seen.setdefault(did, set()).add(kind)

    def has(did, kind):
        return kind in seen.get(did, set())

    for e in events:
        kind, did, at = e["kind"], e.get("decision_id") or "", e["at"]
        detail = _json(e.get("detail"))
        d = dec(did) if did else {}
        if kind == "task_started":
            started_verdict = detail.get("verdict", "")
            rows.append(_row("engaged", at, {"requester": detail.get("requester") or run.get("requester") or "",
                                             "count": count, "verdict": started_verdict,
                                             "why": _clip(detail.get("why"), 120) if started_verdict == "pass" else ""},
                             people=[detail.get("requester") or run.get("requester") or ""]))
        elif kind == "task_triaged":
            verdict = detail.get("verdict", "")
            if verdict and started_verdict and verdict != started_verdict:
                rows.append(_row("triaged", at, {"verdict": verdict, "why": _clip(detail.get("why"), 120)}))
        elif kind == "rule_over_record" and did:
            record = record_refs(detail.get("record", ""))
            if record:
                agrees[did] = record[0]["label"]
        elif kind == "checking" and did and not has(did, "checking"):
            prior = dec(detail.get("cited") or "") or prior_of(d)
            mark(did, "checking")
            rows.append(_row("checking", at, {"topic": topic(d.get("question")), "call": _call_label(prior),
                                              "who": _first(prior.get("answered_by") or prior.get("owner_name")),
                                              "day": _call_day(prior)},
                             refs=[_call_ref(prior)] if prior else [],
                             people=[prior.get("answered_by") or ""]))
        elif kind == "resolve" and did:
            if has(did, "settled"):
                continue
            source = detail.get("source", "")
            if source == "memory" and detail.get("signed", detail.get("kind") == "historical-attribution") is False:
                # A near match nobody made a rule of: a guess, confirmed by a person.
                if has(did, "guess"):
                    continue
                prior = dec(detail.get("cited") or "") or prior_of(d)
                mark(did, "guess")
                rows.append(_row("guess", at, {"topic": topic(d.get("question")), "from": _call_label(prior),
                                               "confirmer": _first(d.get("owner_name"))},
                                 refs=[_call_ref(prior)] if prior else [], people=[d.get("owner_name") or ""],
                                 group=("guess", _first(d.get("owner_name")))))
                continue
            if source == "memory":
                prior = dec(detail.get("cited") or "") or prior_of(d)
                mark(did, "settled")
                item = {"topic": topic(d.get("question")), "call": _call_label(prior),
                        "who": _first(detail.get("answered_by") or prior.get("answered_by") or prior.get("owner_name")),
                        "day": _call_day(prior), "partial": d.get("status") == "partial",
                        "agrees": agrees.get(did, ""), "tail": tail_for(d)}
                refs = [_call_ref(prior)] if prior else []
                if agrees.get(did):
                    refs.append({"kind": "record", "id": agrees[did], "label": agrees[did]})
                row_kind = "confirmed" if has(did, "checking") else "settled"
                rows.append(_row(row_kind, at, item, refs=refs, people=[prior.get("answered_by") or ""],
                                 group=(row_kind, item["tail"])))
            else:
                mark(did, "settled")
                refs = record_refs(detail.get("cited", ""), meetings)
                partial = d.get("status") == "partial" or detail.get("kind") == "retro-pointer"
                item = {"topic": topic(d.get("question")), "refs": refs, "partial": partial, "tail": tail_for(d)}
                rows.append(_row("settled_record", at, item, refs=refs, group=("settled_record", item["tail"])))
        elif kind == "proposed" and did and not has(did, "settled") and not has(did, "guess"):
            mark(did, "guess")
            prior = dec(detail.get("cited") or "") or prior_of(d)
            if detail.get("ticket_status") is not None:
                refs = record_refs(detail.get("cited", ""), meetings)
                source = (refs[0]["label"] if refs else "a record") + f" (still {detail.get('ticket_status') or 'open'})"
            else:
                refs = [_call_ref(prior)] if prior else []
                source = _call_label(prior)
            rows.append(_row("guess", at, {"topic": topic(d.get("question")), "from": source,
                                           "confirmer": _first(d.get("owner_name"))},
                             refs=refs, people=[d.get("owner_name") or ""], group=("guess", _first(d.get("owner_name")))))
        elif kind == "meeting_precedent" and did and not has(did, "settled") and not has(did, "guess"):
            mark(did, "settled" if detail.get("kind") == "settle" else "guess")
            title = meetings.get(detail.get("meeting_id") or "") or ""
            head = str(d.get("fit_reason") or "").split(":")[0].strip()
            meeting = f"the {title}" if title else head[5:] if head.startswith("From ") and head[5:] else "a meeting"
            if not meeting.startswith("the ") and meeting != "a meeting":
                meeting = "the " + meeting
            ref = [{"kind": "meeting", "id": detail.get("meeting_id") or "", "label": meeting}]
            speaker = _first(detail.get("speaker") or d.get("answered_by"))
            if detail.get("kind") == "settle":
                rows.append(_row("settled_meeting", at, {"topic": topic(d.get("question")), "meeting": meeting,
                                                         "speaker": speaker, "quote": _clip(d.get("rationale"), 90),
                                                         "tail": tail_for(d)},
                                 refs=ref, people=[detail.get("speaker") or ""], group=("settled_meeting", meeting)))
            else:
                confirmer = _first(d.get("owner_name")) or speaker
                rows.append(_row("guess", at, {"topic": topic(d.get("question")),
                                               "from": f"what {speaker} said in {meeting}" if speaker else meeting,
                                               "confirmer": confirmer},
                                 refs=ref, people=[d.get("owner_name") or detail.get("speaker") or ""],
                                 group=("guess", confirmer)))
        elif kind == "assume" and did and not has(did, "settled"):
            mark(did, "settled")
            rows.append(_row("assumed", at, {"topic": topic(d.get("question")), "default": _clip(detail.get("default"), 80),
                                             "owner": _first(d.get("owner_name"))},
                             people=[d.get("owner_name") or ""]))
        elif kind in ("ask_drafted", "route_unknown") and did:
            if has(did, "flagged") or last_outcome.get(did, 0) > e["id"]:
                continue
            mark(did, "flagged")
            owner = detail.get("owner") or ""
            rows.append(_row("flagged", at, {"topic": topic(d.get("question")), "owner": _first(owner),
                                             "area": _area(d.get("owner_evidence"))},
                             refs=[{"kind": "decision", "id": did, "run_id": run.get("id", ""), "label": topic(d.get("question"))}],
                             people=[owner], group=("flagged", _first(owner))))
        elif kind == "dedup_open" and did and not has(did, "folded"):
            mark(did, "folded")
            twin = dec(detail.get("twin") or "")
            rows.append(_row("folded", at, {"topic": topic(detail.get("question") or d.get("question")),
                                            "twin": topic(twin.get("question"))},
                             refs=[{"kind": "decision", "id": twin.get("id", ""), "run_id": twin.get("run_id", ""),
                                    "label": topic(twin.get("question"))}] if twin else []))
        elif kind == "conflict_resolved" and did:
            kept_id = detail.get("preferred") or detail.get("kept") or ""
            over_ids = list(detail.get("over") or detail.get("retired") or [])
            if has(did, f"conflict:{kept_id}") or not kept_id:
                continue
            mark(did, f"conflict:{kept_id}")
            kept, over = dec(kept_id), dec(over_ids[0]) if over_ids else {}
            rows.append(_row("conflict", at, {"topic": topic(d.get("question")), "kept": _call_label(kept),
                                              "over": _call_label(over) if over else "an older one",
                                              "retired": bool(over.get("superseded_by"))},
                             refs=[_call_ref(kept)] + ([_call_ref(over)] if over else []),
                             people=[kept.get("answered_by") or "", over.get("answered_by") or ""],
                             group=("conflict", kept_id, over_ids[0] if over_ids else "")))
        elif kind == "owner_changed" and did:
            rows.append(_row("handed", at, {"topic": topic(d.get("question")), "by": _first(detail.get("by")) if detail.get("referral") else "",
                                            "to": _first(detail.get("to")), "scopes": []},
                             people=[detail.get("by") or "", detail.get("to") or ""]))
        elif kind == "route_learned" and did and rows and rows[-1]["kind"] == "handed":
            rows[-1]["items"][0]["scopes"] = [str(s) for s in (detail.get("scopes") or [])][:3]
        elif kind in ("owner_approved", "answer_corrected", "signoff", "signature") and did:
            by = detail.get("actor") or detail.get("by") or detail.get("owner") or d.get("answered_by") or ""
            rows.append(_row("answered", at, {"topic": topic(d.get("question")), "by": _first(by),
                                              "via": _via(detail.get("source")),
                                              "guessed": kind == "owner_approved" and d.get("kind") == "prediction",
                                              "signed": kind in ("signoff", "signature") and not detail.get("corrected"),
                                              "remaining": ", ".join(_first(x) for x in (detail.get("remaining") or []))},
                             people=[by]))
        elif kind == "precedent_corrected" and did:
            rows.append(_row("corrected", at, {"topic": topic(d.get("question")), "by": _first(detail.get("by")),
                                               "why": _clip(detail.get("why"), 90), "replaced": bool(detail.get("replaced")),
                                               "scope": detail.get("scope", "")},
                             people=[detail.get("by") or ""]))
        elif kind == "prediction_withdrawn" and did:
            rows.append(_row("withdrawn", at, {"topic": topic(d.get("question")), "why": _clip(detail.get("reason"), 100)}))
        elif kind == "dependent_flagged" and did:
            rows.append(_row("review", at, {"topic": topic(d.get("question")), "why": _clip(detail.get("reason"), 100)}))
        elif kind == "legacy_source_reuse_review" and did:
            # A standing authorization withdrawn by a later Raven, which
            # could not tell what source revisions it was made from.
            before = detail.get("previous_authorization") or {}
            if before.get("signoff") in ("rule", "signed"):
                rows.append(_row("review", at, {"topic": topic(d.get("question")),
                                                "why": "the source it was settled from has no known revision, so a person signs it"}))
        elif kind == "question_reframed" and did:
            rows.append(_row("reframed", at, {"topic": topic(detail.get("question") or d.get("question")),
                                              "by": _first(detail.get("by")) or "Someone"},
                             people=[detail.get("by") or ""]))
        elif kind == "followup_added" and did:
            rows.append(_row("followup", at, {"topic": topic(d.get("question") or detail.get("question")),
                                              "by": _first(detail.get("by")) or "Someone"},
                             people=[detail.get("by") or ""]))
        elif kind == "task_note":
            rows.append(_row("note", at, {"text": _clip(detail.get("text"), 90), "by": _first(detail.get("by")) or "Someone"},
                             people=[detail.get("by") or ""]))
        elif kind == "node_settled" and did:
            rows.append(_row("proposed", at, {"topic": topic(d.get("question")), "agent": agent,
                                              "owner": _first(d.get("owner_name"))},
                             people=[d.get("owner_name") or ""]))
        elif kind == "host_stopped_waiting":
            rows.append(_row("stopped", at, {"agent": agent, "waiting": int(detail.get("waiting") or 0)}))
        elif kind == "task_abandoned":
            rows.append(_row("finished", at, {"abandoned": True, "why": _clip(detail.get("reason"), 100)}))
        elif kind == "task_reopened":
            rows.append(_row("finished", at, {"reopened": True, "why": _clip(detail.get("why") or detail.get("reason"), 100)}))
        elif kind == "run_updated" and str(e.get("detail") or "").strip() == "completed":
            signed = sum(1 for x in visible if x.get("status") == "approved" or x.get("signoff") in ("signed", "rule"))
            rows.append(_row("finished", at, {"count": count, "signed": signed}))
    return _fold(rows)


def _seconds(iso) -> float | None:
    try:
        return datetime.fromisoformat(str(iso or "")).timestamp()
    except ValueError:
        return None


def _fold(rows) -> list[dict]:
    """Rows of one groupable kind within FOLD_SECONDS of each other
    become one row, placed where the first of them happened; every row
    then gets its text and loses its grouping key."""
    out: list[dict] = []
    for row in rows:
        home = None
        if row["kind"] in GROUPABLE:
            t = _seconds(row["at"])
            for candidate in reversed(out):
                if candidate["kind"] != row["kind"] or candidate["group"] != row["group"]:
                    continue
                t0 = _seconds(candidate["at"])
                if t is not None and t0 is not None and 0 <= t - t0 <= FOLD_SECONDS:
                    home = candidate
                break
        if home is not None:
            home["items"].extend(row["items"])
            for ref in row["refs"]:
                if ref not in home["refs"]:
                    home["refs"].append(ref)
            for person in row["people"]:
                if person not in home["people"]:
                    home["people"].append(person)
            continue
        out.append(dict(row, refs=list(row["refs"]), people=list(row["people"]), items=list(row["items"])))
    for row in out:
        row["text"] = render(row["kind"], row["items"])
        row["count"] = len(row["items"])
        del row["group"], row["items"]
    return out


# ---------------- reading a task ----------------

def _decisions(db, task_id) -> dict:
    return {r["id"]: dict(r) for r in db.execute(
        "SELECT d.*, o.name AS owner_name FROM decisions d LEFT JOIN owners o ON o.id=d.owner_id WHERE d.run_id=? "
        "ORDER BY d.created_at", (task_id,))}


def _events(db, task_id) -> list[dict]:
    return [{"id": r["id"], "kind": r["kind"], "decision_id": r["decision_id"] or "", "detail": r["detail"],
             "at": r["created_at"]}
            for r in db.execute("SELECT id, kind, decision_id, detail, created_at FROM events WHERE run_id=? ORDER BY id",
                                (task_id,))]


def _priors(db, decisions, events) -> dict:
    wanted = {d.get("source_id") for d in decisions.values() if d.get("source_id")}
    for e in events:
        detail = _json(e.get("detail"))
        for key in ("cited", "preferred", "kept", "twin", "replaced"):
            value = detail.get(key)
            if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{12,32}", value):
                wanted.add(value)
        for key in ("over", "retired"):
            for value in detail.get(key) or []:
                if isinstance(value, str):
                    wanted.add(value)
    wanted -= set(decisions)
    if not wanted:
        return {}
    marks = ",".join("?" * len(wanted))
    return {r["id"]: dict(r) for r in db.execute(
        f"SELECT d.*, o.name AS owner_name FROM decisions d LEFT JOIN owners o ON o.id=d.owner_id WHERE d.id IN ({marks})",
        tuple(wanted))}


def _meetings(db, events) -> dict:
    ids = set()
    for e in events:
        detail = _json(e.get("detail"))
        if detail.get("meeting_id"):
            ids.add(detail["meeting_id"])
        for m in re.finditer(r"meeting:([0-9a-f-]{8,})", str(detail.get("cited") or "")):
            ids.add(m.group(1))
    if not ids:
        return {}
    try:
        rows = db.execute(f"SELECT id, title FROM meeting_occurrences WHERE id IN ({','.join('?' * len(ids))})",
                          tuple(ids)).fetchall()
    except Exception:
        return {}
    return {r["id"]: r["title"] or "" for r in rows}


def rows(store, task_id: str) -> list[dict]:
    """The timeline of one task from the store, oldest first."""
    from .store import Invalid
    graph = store.graph
    run = graph.get_task(task_id)
    if run is None:
        raise Invalid("Task not found")
    run = dict(run)
    db = graph.db
    decisions = _decisions(db, task_id)
    events = _events(db, task_id)
    return build(run, decisions, events, _priors(db, decisions, events), _meetings(db, events))


def view(store, task_id: str) -> dict:
    """GET /api/tasks/<id>/timeline."""
    run = dict(store.graph.get_task(task_id) or {})
    out = rows(store, task_id)
    return {"task_id": task_id, "title": run.get("title", ""), "status": run.get("status", ""), "rows": out}


def attach(store, runs: list[dict]) -> None:
    """Put a `timeline` on each live task row /api/state serves, newest
    tasks first, at most STATE_LIMIT of them: the Being built card draws
    it without another request per task."""
    live = [r for r in runs if r.get("status") not in LIVE_EXCLUDED][:STATE_LIMIT]
    for r in live:
        try:
            r["timeline"] = rows(store, r["id"])
        except Exception:
            r["timeline"] = []
