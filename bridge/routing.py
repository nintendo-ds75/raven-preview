"""Owner routing: the question to the people the shared people-to-paths
structure points at, and above it the authority map the organization
verified. The signals (approvals, authorship, recency, listings, blame,
a recorded routing answer, verified authority) live in signals.py; this
module is the entry point the ladder, the canvas and the tests call.
"""

from __future__ import annotations

from .graph import Graph


def route(store: Graph, repo: str, question: str,
          path: str = "", context: str = "", notes: list[str] | None = None,
          requester: str = "", hints: list[str] | None = None,
          hits: list | None = None, category: str = "", *, task_id: str = "",
          decision_id: str = "", contact_context: dict | None = None) -> tuple[str, list[str], float] | None:
    """Return (owner, evidence_lines, score) or None when no signal clears
    the floor. `path` is the file the agent is working in, which scopes
    the graph before any question-derived hint does. `notes`, when given,
    receives the honest reason for a None. `requester` is the person the
    question is asked for (name, email, or both): their usual approvers
    count, and the question is never routed back to them unless the
    authority map says the decision is theirs. `hits` are the paths the
    caller already resolved, when it did. `category` is the decision
    category the agent named, if any."""
    ranked = route_ranked(store, repo, question, path=path, context=context, notes=notes,
                          requester=requester, hints=hints, hits=hits, category=category, task_id=task_id,
                          decision_id=decision_id, contact_context=contact_context)
    return ranked[0] if ranked else None


def route_ranked(store: Graph, repo: str, question: str,
                 path: str = "", context: str = "", notes: list[str] | None = None,
                 requester: str = "", hints: list[str] | None = None,
                 hits: list | None = None, category: str = "",
                 also_paths: list[str] | None = None, facts: dict | None = None, *, task_id: str = "",
                 decision_id: str = "", contact_context: dict | None = None) -> list[tuple[str, list[str], float]]:
    """The routing candidates best first, each as (owner, evidence_lines,
    score). Empty when no signal clears the floor. route() is the head.
    A store with no changes rows for the repo, and no git source Raven
    may read at ask time, routes nobody unless the authority map names
    someone for the decision's category or the whole repository: the
    inbox's own pattern routing still applies after this."""
    from .signals import signal_route
    ranked = signal_route(store, repo, question, context=context, path=path, notes=notes, requester=requester,
                          hints=hints, hits=hits, category=category, also_paths=also_paths)
    taken = {r[0] for r in ranked}
    for named_path in [path, *(also_paths or [])]:
        ranked.extend(_record_authors(store, repo, named_path, requester, taken))
    from .routing_memory import CONTACT_RESPONSES, candidates, contact_evidence
    learned_details = {}
    learned = candidates(store, repo, question, path, context, category, facts, task_id=task_id,
                         decision_id=decision_id, contact_context=contact_context, details=learned_details, notes=notes)
    declined = {p['name'] for p, outcome, *_ in learned if outcome == 'declined'}
    if learned:
        # Keep the existing authority precedence, including narrower paths and
        # decider versus approver. A pending referral is only a candidate.
        from .signals import _authority_matches
        matches = _authority_matches(store, repo, hits or [], question, context, category, path, also_paths)
        explicit_ids = {a['id'] for a in store.authority_rows(repo) if a['source'] not in ('referral', 'answer')}
        fixed = {a['name'] for a in matches if a['strong'] and a['role'] == 'decides' and a['id'] in explicit_ids}
        verified = [r for r in ranked if r[0] in fixed]
        prior = [(p['name'], [contact_evidence(store, p, outcome, did, learned_details.get((p['id'], outcome, did)))], (2 if outcome in CONTACT_RESPONSES else 1) + score)
                 for p, outcome, score, did, _ in learned
                 if outcome in (*CONTACT_RESPONSES, 'connector') and p['name'] not in fixed]
        seen = {r[0] for r in verified + prior}
        ranked = verified + prior + [r for r in ranked if r[0] not in seen | declined]
    if store.get_setting("slack_discovery") != "1":
        return ranked
    import json
    unavailable = set(json.loads(store.get_setting("slack_unavailable") or "[]"))
    contacts = []
    for name, evidence, score in ranked:
        person = contact_for(store, name)
        verified = any(line.startswith("verified:") for line in evidence)
        if verified:
            # Do not replace a known decider with a reachable bystander.
            contacts.append((person["name"] if person else name, evidence, score))
        elif person and person["slack_id"] and person["slack_id"] not in unavailable:
            contacts.append((person["name"], ["inferred first contact; confirm or refer in Slack", *evidence], score))
    if contacts:
        return contacts
    # A cited Jira/Slack/document author is a reasonable first contact,
    # never evidence of authority. Prefer explicitly named records first.
    from .ladder import named_records
    for record in named_records(store, repo, question, context):
        rows = store.intents_by_ref([record["ref"].lstrip("#")], repo)
        for row in rows:
            person = contact_for(store, row["author"])
            if (person and person['name'] not in declined
                    and person["slack_id"] and person["slack_id"] not in unavailable):
                return [(person["name"], [f"inferred first contact: authored {record['ref']}; "
                                          "ask them to confirm who decides, not presumed authority"], 0.3)]
    from .ladder import _meaningful_terms, _void
    terms = set(_meaningful_terms(question))
    for row in store.intents_matching(terms, limit=8, repo=repo):
        # Two topic matches in the title, or a linked path plus one, keep
        # incidental mentions in a long Slack/Jira body from becoming owners.
        overlap = terms & set(_meaningful_terms(row["title"]))
        linked = store.paths_of_intents(repo, [(row["kind"], row["ref"])])
        same_path = bool(path and any(path == p or path.startswith(p.rstrip("/") + "/")
                                     for p in linked.get((row["kind"], row["ref"]), [])))
        if _void(row) or not (len(overlap) >= 2 or (same_path and overlap)):
            continue
        person = contact_for(store, row["author"])
        if (person and person['name'] not in declined and person["slack_id"] and person["slack_id"] not in unavailable
                and person != store.find_person(requester)):
            return [(person["name"], [f"inferred first contact: authored {row['kind']} {row['ref']} "
                                      f"({row['title']}); confirm or refer in Slack"], 0.3)]
    if notes is not None:
        notes.append("no reachable Slack contact matched the evidence; ask the triage channel")
    return []


def contact_for(store: Graph, name: str) -> dict | None:
    """Join history to Slack by exact email, otherwise an unambiguous name.

    This chooses a contact, never modifies a login identity or authority.
    """
    emails = {r["email"].lower() for r in store.db.execute(
        "SELECT email FROM engineers WHERE name=?", (name,)) if r["email"]}
    handle = name.lstrip("@").lower()
    emails.update(r["email"].lower() for r in store.db.execute(
        "SELECT email FROM gh_users WHERE lower(login)=? OR name=?", (handle, name)) if r["email"])
    people = store.people()
    matches = [p for p in people if p["email"] and p["email"].lower() in emails]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return None
    return store.find_person(name)


def coordinator_route(store: Graph, repo: str, notes: list[str] | None = None,
                      candidates: list[tuple[str, list[str], float]] | None = None) -> tuple[str, list[str], float] | None:
    """The configured coordinator, as a route, for what nobody verified
    owns: the evidence says why it landed there and names the inferred
    candidates, if any, so the coordinator can hand it on."""
    person = store.coordinator(repo)
    if person is None:
        return None
    lines = ["coordinator fallback: no verified owner for this decision"
             + (f" ({'; '.join(notes)})" if notes else "")]
    for name, ev, _score in (candidates or [])[:3]:
        lines.append(f"candidate from git history: {name}" + (f" ({ev[0]})" if ev else ""))
    return (person["name"], lines, 0.0)


RECORD_AUTHOR_SCORE = 0.25


def _record_authors(store, repo, path, requester, taken):
    from .ladder import _void
    from .signals import requester_keys, _is_requester
    if not path:
        return []
    req = requester_keys(requester, store, repo)
    out = []
    rows = store.db.execute("SELECT i.* FROM intents i JOIN intent_paths p ON i.repo=p.repo AND i.kind=p.kind "
                            "AND i.ref=p.ref WHERE i.repo=? AND (p.path=? OR p.path LIKE ?) "
                            "ORDER BY i.created_at DESC LIMIT 40", (repo, path, path.rstrip('/') + '/%'))
    for row in rows:
        if _void(row):
            continue
        person = contact_for(store, row['author'])
        name = person['name'] if person else row['author']
        email = person.get('email', '') if person else ''
        if not name or name in taken or _is_requester(name, email, req):
            continue
        taken.add(name)
        out.append((name, [f"inferred first contact: authored {row['kind']} {row['ref']} for {path}; confirm or refer"],
                    RECORD_AUTHOR_SCORE))
    return out


def rank_for_decision(store, repo, question, paths=(), context='', requester='', category='', hints=None,
                      facts=None, hits=None, notes=None, *, task_id='', decision_id='', contact_context=None):
    return route_ranked(store, repo, question, path=paths[0] if paths else '', also_paths=list(paths[1:]),
                        context=context, requester=requester, category=category, hints=hints,
                        facts=facts, hits=hits, notes=notes, task_id=task_id, decision_id=decision_id,
                        contact_context=contact_context)
