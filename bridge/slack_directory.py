"""Slack contacts, without browser accounts or a hand-entered ownership map.

Directory identity is matched only by immutable Slack id or exact email.
Names can help routing, but must never merge accounts or grant admin rights.
"""
from __future__ import annotations

import json
from collections import Counter

from .graph import now_iso


def eligible(user: dict, team: str) -> bool:
    return bool(user.get("id") and user["id"] != "USLACKBOT"
                and not any(user.get(k) for k in ("deleted", "is_bot", "is_app_user",
                                                  "is_restricted", "is_ultra_restricted"))
                and (not user.get("team_id") or user["team_id"] == team))


def workspace(graph, transport) -> str:
    team = transport.workspace_id()
    previous = graph.get_setting("slack_team_id")
    if not team or (previous and team != previous):
        raise ValueError("This Slack token belongs to a different workspace. Use a separate Raven instance.")
    return team


def upsert(graph, user: dict, team: str) -> dict | None:
    """Called in a transaction, with a user supplied by Slack's Web API."""
    if not eligible(user, team):
        return None
    uid = user["id"]
    profile = user.get("profile") or {}
    email = (profile.get("email") or "").strip().lower()
    existing = graph.db.execute("SELECT * FROM people WHERE slack_id=?", (uid,)).fetchone()
    if existing is None and email:
        existing = graph.db.execute("SELECT * FROM people WHERE email=?", (email,)).fetchone()
        if existing is not None and existing["slack_id"] and existing["slack_id"] != uid:
            raise ValueError("Two Slack members claim the same email; contact discovery needs review")
    if existing is not None:
        # An inactive person was disabled deliberately. A directory refresh
        # is not permission to reactivate their account or change their role.
        if not existing["active"]:
            return None
        pid = existing["id"]
        graph.db.execute("UPDATE people SET slack_id=?,updated_at=? WHERE id=?", (uid, now_iso(), pid))
    else:
        name = " ".join((profile.get("real_name") or user.get("real_name") or user.get("name") or uid).split())
        # Do not let add_person's owner-row linking attach a namesake to
        # someone else's decisions, or let fuzzy name matching merge them.
        if graph.db.execute("SELECT 1 FROM people WHERE lower(name)=lower(?)", (name,)).fetchone():
            name = f"{name} · {uid}"
        pid = graph.add_person(name, email=email, slack_id=uid, source="slack-directory", merge=False)
    graph._bump("")
    return graph.get_person(pid)


def sync(graph, transport) -> dict:
    """Fetch all pages before writing; a partial/failed fetch changes nothing."""
    try:
        team = workspace(graph, transport)
        users = transport.list_users()
        counts = Counter(((u.get("profile") or {}).get("email") or "").strip().lower()
                         for u in users if eligible(u, team))
        if any(email and count > 1 for email, count in counts.items()):
            raise ValueError("Duplicate email addresses in Slack's directory; no contacts were changed")
        with graph.transaction():
            imported = [p for u in users if (p := upsert(graph, u, team))]
            active = {u["id"] for u in users if eligible(u, team)}
            # Retain identity bindings; recycling an email must not attach a
            # different Slack id to an existing (possibly privileged) person.
            unavailable = [p["slack_id"] for p in graph.people(active_only=False)
                           if p["slack_id"] and p["slack_id"] not in active]
            graph.set_setting("slack_unavailable", json.dumps(unavailable))
            graph._bump("")
            graph.set_setting("slack_team_id", team)
            graph.set_setting("slack_discovery", "1")
            status = {"synced_at": now_iso(), "people": len(imported), "error": ""}
            graph.set_setting("slack_directory", json.dumps(status))
            graph.append_event("slack_directory_synced", {"people": len(imported)})
        return status
    except Exception as error:
        with graph.transaction():
            graph.set_setting("slack_directory", json.dumps({"error": str(error)[:300]}))
        raise


def contact(graph, transport, user_id: str) -> dict | None:
    """A newly joined member or a referral target can reply immediately."""
    # Recheck with Slack so deleted members cannot act on old notifications.
    team = graph.get_setting("slack_team_id") or workspace(graph, transport)
    user = transport.user_info(user_id)
    if user.get("id") != user_id:
        raise ValueError("Slack returned a different member identity")
    with graph.transaction():
        graph.set_setting("slack_team_id", team)
        return upsert(graph, user, team)
