"""The decisions behind a change, written for the pull request.

One line per decision a task needed, marked by where its answer came from:

- **Raven cache hit**: an earlier decision answered it, so nobody was asked
  again. The line names who made the earlier call and in which task.
- **New judgment**: Raven asked a person and they answered for this task,
  on a call, on their task page, in Slack or in Raven.
- **From records**, **Agent default** and **Still open** for the rest.

Everything is read from the task's stored rows and events; nothing here is
written by a model.
"""
import json
import re

from .store import Invalid

KINDS = {"cache": "Raven cache hit", "live": "Live reply", "record": "From records",
         "agent": "Agent default", "open": "Still open"}


def _first(text, limit=180):
    """The answer's first sentence or line, whole words, for a scannable bullet."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return ""
    head = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
    if len(head) <= limit:
        return head
    cut = head[:limit].rsplit(" ", 1)[0].rstrip(",;:")
    return cut + "…"


def _whole(text):
    """The whole answer or reason on one line. A PR description that drops
    the second sentence drops conditions people signed ("run it in staging first")."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


# The placeholder an answer with no reason is stored with: not a reason.
NO_REASON = "No additional rationale supplied"
CALL_ID = re.compile(r"\b(decision\s+)?([0-9a-f]{12})\b(?:\s*\(\d{4}-\d{2}-\d{2}\))?")


def _calls(store, text):
    """A decision id in an answer or reason, as people say it: "Jordan's Oct
    28 call". A PR reader cannot open an id, and the For you page already
    shows the same text this way."""
    from .attention import _day as said_day

    def name(match):
        row = store.graph.db.execute("SELECT answered_by, signed_by, created_at FROM decisions WHERE id=?",
                                     (match.group(2),)).fetchone()
        if row is None:
            return "an earlier call" if match.group(1) else match.group(0)
        who = str(row["answered_by"] or row["signed_by"] or "").split(" ")[0]
        day = said_day(row["created_at"])
        if who:
            return f"{who}'s {day} call".replace("  ", " ") if day else f"{who}'s call"
        return f"an earlier call from {day}" if day else "an earlier call"
    return CALL_ID.sub(name, text or "")


def _reason(text):
    """A reason as a sentence, or nothing when none was given."""
    text = _whole(text)
    if not text or text.rstrip(".") == NO_REASON:
        return ""
    return text if text[-1] in ".!?\"”)" else text + "."


def _lead(text):
    """(first sentence, the rest): the first is bolded so the list stays scannable."""
    parts = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text).strip())
    # A bare "Yes." or "No, not yet." says nothing on its own: bold it with
    # the sentence that carries the decision.
    take = 2 if len(parts) > 1 and len(parts[0].split()) <= 3 else 1
    return " ".join(parts[:take]), " ".join(parts[take:])


def _day(iso):
    return str(iso or "")[:10]


def _channel(store, decision_id):
    """How a person's answer reached Raven, from what was recorded."""
    row = store.graph.db.execute(
        "SELECT id, person_id, turns, updated_at FROM interviews WHERE decision_id=? AND status='confirmed' "
        "ORDER BY updated_at DESC LIMIT 1", (decision_id,)).fetchone()
    if row:
        try:
            turns = len(json.loads(row["turns"] or "[]"))
        except ValueError:
            turns = 0
        return {"how": "on a call with Raven", "interview_id": row["id"], "turns": turns}
    event = store.graph.db.execute(
        "SELECT detail FROM events WHERE decision_id=? AND kind IN ('owner_approved','answer_corrected') "
        "ORDER BY id DESC LIMIT 1", (decision_id,)).fetchone()
    source = ""
    if event:
        try:
            source = str(json.loads(event["detail"]).get("source") or "")
        except ValueError:
            source = ""
    if source.startswith("link"):
        return {"how": "on their task page"}
    if "slack" in source.lower():
        return {"how": "in Slack"}
    return {"how": "in Raven"}


def _task_title(store, task_id):
    row = store.graph.db.execute("SELECT title FROM runs WHERE id=?", (task_id,)).fetchone()
    return row["title"] if row else ""


def _item(store, node):
    status, source = node.get("status") or "", node.get("source") or ""
    item = {"node_id": node["node_id"], "question": node.get("question") or "",
            "answer": _whole(node.get("answer")), "signed_by": node.get("signed_by") or "",
            "signatures": node.get("signatures") or [], "authorized": bool(node.get("authorized")),
            "updated_at": node.get("updated_at") or ""}
    prior_id = node.get("source_id") if source == "memory" else ""
    if not prior_id and status == "duplicate":
        prior_id = (node.get("resolution") or {}).get("node_id") or node.get("duplicate_of") or ""
    confirmed = ""
    if not prior_id and node.get("answer"):
        prior_id = _confirmed_from(store, node)
        confirmed = node.get("answered_by") or node.get("owner") or "" if prior_id else ""
    if prior_id:
        prior = store.get_decision(prior_id) or {}
        item.update(kind="cache", who=prior.get("answered_by") or node.get("answered_by") or "",
                    answer=_whole(prior.get("answer") or node.get("answer")),
                    source={"node_id": prior_id, "task_id": prior.get("run_id") or "",
                            "task": _task_title(store, prior.get("run_id") or ""),
                            "decided_at": _day(prior.get("updated_at"))})
        rationale = _reason(prior.get("rationale"))
        if rationale:
            item["why"] = rationale
        if confirmed:
            item.update(confirmed_by=confirmed, answer=_whole(node.get("answer")))
    elif status == "pending" or node.get("blocking") and not node.get("answer"):
        item.update(kind="open", who=node.get("owner") or "")
    elif source == "human" or status == "approved" or node.get("kind") == "answer":
        item.update(kind="live", who=node.get("answered_by") or node.get("owner") or "", **_channel(store, node["node_id"]))
        rationale = _reason(node.get("rationale"))
        if rationale:
            item["why"] = rationale
    elif source == "record" or node.get("kind") == "evidence":
        item.update(kind="record", who="", detail=_first(node.get("evidence"), 140))
    else:
        item.update(kind="agent", who="")
    item["answer"] = _calls(store, item["answer"])
    if item.get("why"):
        item["why"] = _calls(store, item["why"])
    item["label"] = KINDS[item["kind"]]
    item["person_id"] = _person_id(store, item.get("who"))
    if node.get("answered_via") and "meeting" in str(node["answered_via"]).lower():
        item["how"] = "in a meeting"
    names = item["signatures"] or ([item["signed_by"]] if item["signed_by"] else []) or ([item["who"]] if item.get("who") else [])
    item["signed_label"] = ", ".join(_signer(store, n) for n in names)
    return item


def _signer(store, name):
    """"Ana (Carrier ops)": a first name and the team, as a PR reader knows people."""
    row = store.graph.db.execute("SELECT team FROM people WHERE lower(name)=lower(?) LIMIT 1", (name,)).fetchone()
    first = str(name).split(" ")[0]
    return f"{first} ({row['team']})" if row and row["team"] else first


def _norm(text):
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", str(text or "").lower())).strip()


def _confirmed_from(store, node):
    """The earlier call an owner confirmed: Raven proposed an answer from it
    ("Raven would apply this, but isn't sure: your call") and the owner sent
    that answer. A PR reader should see it as a reuse the owner checked, not
    as a new judgment."""
    rows = store.graph.db.execute(
        "SELECT kind, detail FROM events WHERE decision_id=? AND kind IN "
        "('proposal_composed','resolve','previous_answer','owner_approved') ORDER BY id", (node["node_id"],)).fetchall()
    row = store.get_decision(node["node_id"]) or {}
    prior, took, proposed = "", False, [node.get("prediction") or "", row.get("prediction") or ""]
    for row in rows:
        try:
            detail = json.loads(row["detail"] or "{}")
        except ValueError:
            continue
        if row["kind"] == "proposal_composed" and detail.get("from"):
            prior = detail["from"]
        elif row["kind"] == "resolve" and detail.get("source") == "memory" and detail.get("cited"):
            prior = detail["cited"]
        elif row["kind"] == "previous_answer":
            proposed.append(detail.get("answer") or "")
        elif row["kind"] == "owner_approved":
            # Raven's guess is not kept after approval; the approval says
            # whether the owner sent it as it stood.
            took = took or detail.get("took_guess") is True
    if not prior or not store.get_decision(prior):
        return ""
    if took:
        return prior
    said = _norm(node.get("answer"))[:24]
    return prior if said and any(_norm(p).startswith(said) for p in proposed if p) else ""


def _person_id(store, name):
    if not name:
        return ""
    row = store.graph.db.execute("SELECT id FROM people WHERE lower(name)=lower(?) LIMIT 1", (name,)).fetchone()
    return row["id"] if row else ""


def summary(store, task_id):
    from . import canvas
    if not task_id:
        raise Invalid("task_id is required")
    tree = canvas.get_tree(store, task_id)
    nodes = [n for n in canvas._flatten(tree["nodes"]) if n.get("status") not in ("adopted", "withdrawn")]
    items = [_item(store, n) for n in nodes]
    order = {"live": 0, "cache": 1, "record": 2, "agent": 3, "open": 4}
    items.sort(key=lambda x: (order[x["kind"]], x["updated_at"]))
    counts = {k: sum(1 for x in items if x["kind"] == k) for k in KINDS}
    task = {"id": task_id, "title": tree.get("title") or _task_title(store, task_id),
            "repo": tree.get("repo") or "", "goal": tree.get("goal") or "", "status": tree.get("status") or ""}
    result = {"task": task, "items": items, "counts": counts,
              "signed_off": bool(items) and all(x["authorized"] for x in items if x["kind"] != "open")
              and not counts["open"]}
    result["markdown"] = markdown(result)
    return result


SOURCES = {"live": "🔵", "record": "🟢", "cache": "🟡", "agent": "⚪", "open": "🔴"}
LEGEND = ("**Source:** 🔵 Escalation: routed to the person who owns it and answered live · 🟢 Document: "
          "pulled from a written record (ticket, PR, code, ADR) · 🟡 Memory: reused from a past decision already on record")
HOW = {"on a call with Raven": "📞 Call", "in Slack": "💬 Slack", "on their task page": "Task page", "in Raven": "In Raven",
       "in a meeting": "Meeting"}


def _cell(text):
    """Text that stays inside one Markdown table cell."""
    return re.sub(r"\s+", " ", str(text or "")).replace("|", "\\|").strip()


def _when(iso):
    return _day(iso) or "—"


def _row(n, x):
    """One table row per decision: the answer, who signed it, how it was
    settled and when. The questions and reasons are folded away below the
    table, so a reviewer reads what was decided and by whom at a glance."""
    if x["kind"] == "open":
        return f"| {n} | {SOURCES['open']} | {_cell(x['question'])} | — | Waiting on {_cell(x['who'] or 'an owner')} | — |"
    lead, rest = _lead(x["answer"] or x["question"])
    decision = f"**{_cell(lead)}**" + (f" {_cell(rest)}" if rest else "")
    signed = x.get("signed_label") or "—"
    if x["kind"] == "cache":
        src = x["source"]
        first = (x["who"] or "").split(" ")[0]
        how = f"Reused {first + chr(39) + 's' if first else 'an'} call" + (f" from “{src['task']}”" if src.get("task") else "")
        if x.get("confirmed_by"):
            how += f"; {x['confirmed_by'].split(' ')[0]} confirmed it in a click"
        else:
            how += "; nobody asked"
        when = _when(src.get("decided_at"))
    elif x["kind"] == "live":
        how, when = HOW.get(x.get("how", ""), x.get("how", "In Raven")), _when(x["updated_at"])
    elif x["kind"] == "record":
        how, when = "From records" + (f": {x['detail']}" if x.get("detail") else ""), _when(x["updated_at"])
    else:
        how, when = "Agent default; nobody asked", _when(x["updated_at"])
    return f"| {n} | {SOURCES[x['kind']]} | {decision} | {_cell(signed)} | {_cell(how)} | {when} |"


def _detail(n, x):
    """The question and the reason, for the collapsed block."""
    line = f"{n}. {_cell(x['question'])}"
    if x["answer"] and x.get("why"):
        line += f" *Why:* {_cell(x['why'])}"
    return line


def markdown(result):
    """The PR body section. Plain Markdown that GitHub renders as is: one
    short table a reviewer reads, and the questions and reasons folded away."""
    c, items = result["counts"], result["items"]
    asked = c["live"]
    parts = [f"{asked} asked {'its owner' if asked == 1 else 'their owners'}" if asked else "",
             f"{c['cache']} reused from past decisions" if c["cache"] else "",
             f"{c['record']} from records" if c["record"] else "",
             f"{c['agent']} agent default" + ("s" if c["agent"] != 1 else "") if c["agent"] else "",
             f"{c['open']} still open" if c["open"] else ""]
    signed = ("Each is signed off by the person who owns it." if result["signed_off"]
              else "Sign-off is not complete.")
    lines = ["## Decisions behind this change", "",
             f"Raven gathered {len(items)} decision" + ("s" if len(items) != 1 else "")
             + " for *" + result["task"]["title"] + "*: " + ", ".join(p for p in parts if p) + ". " + signed, ""]
    if items:
        lines += [LEGEND, "", "| # | Source | Decision | Signed by | How it was settled | When |",
                  "|---|---|---|---|---|---|"]
        lines += [_row(i, x) for i, x in enumerate(items, 1)]
        lines += ["", "<details><summary>The questions, and the reasons people gave</summary>", ""]
        lines += [_detail(i, x) for i, x in enumerate(items, 1)]
        lines += ["", "</details>"]
    lines += ["", f"<sub>Recorded by Raven · task {result['task']['id']}</sub>"]
    return "\n".join(lines) + "\n"


def node_line(store, node_id):
    """One scannable line for an agent about a node it just wrote: answered
    from memory (whose earlier call, no ping), found in records, or who
    Raven is asking."""
    from . import canvas
    try:
        node = canvas.node_view(store, node_id)
    except Invalid:
        return ""
    x = _item(store, node)
    if x["kind"] == "cache":
        s = x["source"]
        where = f" in “{s['task']}”" if s.get("task") else ""
        return f"Answered from memory: {x['who'] or 'someone'} decided this{where}. Nobody was pinged."
    if x["kind"] == "live":
        return f"Live reply: {x['who'] or 'a person'} answered {x.get('how', 'in Raven')}."
    if x["kind"] == "record":
        return "Found in records: " + (x.get("detail") or "see evidence") + "."
    if x["kind"] == "open":
        owner = node.get("owner") or ""
        return f"Asking {owner}." if owner else "Waiting for an owner; nobody is known to decide this."
    return "Settled by the agent's default; nobody was asked."


def tree_lines(store, task_id):
    from . import canvas
    tree = canvas.get_tree(store, task_id)
    return [f"{n['question'][:90]} → {node_line(store, n['node_id'])}"
            for n in canvas._flatten(tree["nodes"]) if n.get("status") not in ("adopted", "withdrawn")]
