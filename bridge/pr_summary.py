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


def _lead(text):
    """(first sentence, the rest): the first is bolded so the list stays scannable."""
    head, _, rest = re.sub(r"\s+", " ", text).partition(". ")
    return (head + ".", rest) if rest else (head, "")


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
    if prior_id:
        prior = store.get_decision(prior_id) or {}
        item.update(kind="cache", who=prior.get("answered_by") or node.get("answered_by") or "",
                    answer=_whole(prior.get("answer") or node.get("answer")),
                    source={"node_id": prior_id, "task_id": prior.get("run_id") or "",
                            "task": _task_title(store, prior.get("run_id") or ""),
                            "decided_at": _day(prior.get("updated_at"))})
        rationale = _whole(prior.get("rationale"))
        if rationale:
            item["why"] = rationale
    elif status == "pending" or node.get("blocking") and not node.get("answer"):
        item.update(kind="open", who=node.get("owner") or "")
    elif source == "human" or status == "approved" or node.get("kind") == "answer":
        item.update(kind="live", who=node.get("answered_by") or node.get("owner") or "", **_channel(store, node["node_id"]))
        rationale = _whole(node.get("rationale"))
        if rationale:
            item["why"] = rationale
    elif source == "record" or node.get("kind") == "evidence":
        item.update(kind="record", who="", detail=_first(node.get("evidence"), 140))
    else:
        item.update(kind="agent", who="")
    item["label"] = KINDS[item["kind"]]
    item["person_id"] = _person_id(store, item.get("who"))
    return item


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


def _line(x):
    lead, rest = _lead(x["answer"] or x["question"])
    head = f"- **{lead}**" + (f" {rest}" if rest else "")
    if x["kind"] == "cache":
        s = x["source"]
        where = f" in “{s['task']}”" if s.get("task") else ""
        when = f" on {s['decided_at']}" if s.get("decided_at") else ""
        body = f"  `Raven cache hit` {x['who'] or 'Someone'} decided this{where}{when}. Reused; nobody was asked again."
    elif x["kind"] == "live":
        body = f"  `Live reply` New judgment from {x['who'] or 'the owner'}, {x.get('how', 'in Raven')}."
    elif x["kind"] == "record":
        body = "  `From records` " + (x.get("detail") or "Found in the team's records.")
    elif x["kind"] == "agent":
        body = "  `Agent default` Nobody was asked; the agent chose this."
    else:
        return f"- **{x['question']}**\n  `Still open` Waiting on {x['who'] or 'an owner'}."
    if x.get("why"):
        body += f" Why: {x['why']}"
    if x["signatures"] or x["signed_by"]:
        body += f" Signed off by {', '.join(x['signatures']) or x['signed_by']}."
    question = f"\n  <sub>Q: {x['question']}</sub>" if x["answer"] and x["question"] else ""
    return f"{head}\n{body}{question}"


def markdown(result):
    """The PR body section. Plain Markdown that GitHub renders as is."""
    c = result["counts"]
    tally = [f"{c['live']} live repl" + ("ies" if c["live"] != 1 else "y"),
             f"{c['cache']} Raven cache hit" + ("s" if c["cache"] != 1 else "")]
    tally += [f"{c[k]} {KINDS[k].lower()}" for k in ("record", "agent", "open") if c[k]]
    lines = ["## Decisions behind this change", "",
             f"Raven gathered {len(result['items'])} decision" + ("s" if len(result["items"]) != 1 else "")
             + " for *" + result["task"]["title"] + "*: " + ", ".join(tally) + ".", ""]
    lines += [_line(x) for x in result["items"]]
    lines += ["", "**Sign-off:** " + ("every decision is signed off by the person who owns it."
                                       if result["signed_off"] else "not complete; see the open lines above."),
              "", f"<sub>Recorded by Raven · task {result['task']['id']}</sub>"]
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
