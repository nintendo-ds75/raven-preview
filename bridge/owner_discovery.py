"""Bounded, read-only directory discovery. Never used for routing or signoff.

Each call reads one consistent database snapshot. A cursor is an untrusted
position plus a content fingerprint, not a credential or an authority grant.
Changed results require restarting; we do not retain old directory snapshots.
"""

import base64
import binascii
import hashlib
import json

from .store import Invalid, graph_summary

DEFAULT_LIMIT = 100
MAX_LIMIT = 200
# Count the JSON-encoded MCP text, including escaped quotes and Unicode. The
# JSON-RPC envelope adds only its fixed fields and the caller's request ID.
MAX_CONTENT_BYTES = 48 * 1024
COLLECTIONS = ("people", "owners", "ownership", "authority", "teams", "repos")
NOTICE = (
    "Verified authority (people, teams, the authority map) outranks what git history suggests; "
    "otherwise Raven infers a first contact from connected sources and asks in Slack. "
    "An optional coordinator or Slack triage channel receives questions it cannot route. "
    "This is discovery, never an approval or a complete list of required approvers. "
    "Filtered, paginated or absent rows never establish that there is no owner or required approver. "
    "Use bridge_add_node for routing and bridge_get_tree for current authorization and signoff."
)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _cursor(binding, snapshot, offsets):
    data = {"v": 1, "binding": binding, "snapshot": snapshot, "offsets": offsets}
    return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")


def _read_cursor(value, binding):
    try:
        if not isinstance(value, str) or not value or len(value) > 2048:
            raise ValueError
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        data = json.loads(raw)
        if (not isinstance(data, dict) or set(data) != {"v", "binding", "snapshot", "offsets"}
                or type(data["v"]) is not int or data["v"] != 1
                or not isinstance(data["snapshot"], str) or len(data["snapshot"]) != 64
                or not isinstance(data["offsets"], dict) or set(data["offsets"]) != set(COLLECTIONS)
                or any(type(n) is not int or n < 0 for n in data["offsets"].values())):
            raise ValueError
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
        raise Invalid("Invalid owner discovery cursor; restart bridge_list_owners without cursor") from None
    if data["binding"] != binding:
        raise Invalid("Owner discovery cursor belongs to different query, repo or limit; repeat the original arguments or restart without cursor")
    return data


def _snapshot(db, repo):
    """Preserve the original projections and scope, with deterministic ties.

    Read directly on the caller's snapshot, not across Store/Graph connections
    or per-process caches. No identity resolution or authority calculation.
    """
    owners = [dict(r) for r in db.execute("SELECT * FROM owners ORDER BY created_at,id")]
    people = [dict(r) for r in db.execute(
        "SELECT id,name,email,github_login,team,active FROM people ORDER BY name,id")]
    teams = [dict(r) for r in db.execute("SELECT * FROM teams ORDER BY name,id")]
    by_team = {t["id"]: t for t in teams}
    person_teams = {}
    for t in teams:
        t["members"] = []
    for row in db.execute("SELECT team_id,person_id FROM team_members ORDER BY team_id,person_id"):
        if row["team_id"] in by_team:
            by_team[row["team_id"]]["members"].append(row["person_id"])
            person_teams.setdefault(row["person_id"], set()).add(row["team_id"])
    team_order = {t["id"]: i for i, t in enumerate(teams)}
    for p in people:
        p["teams"] = [by_team[tid]["name"] for tid in sorted(person_teams.get(p["id"], ()), key=team_order.get)]
    person_names = {p["id"]: p["name"] for p in people}
    authority = []
    where = " AND (repo=? OR repo='')" if repo else ""
    for row in db.execute("SELECT * FROM authority WHERE ended_at=''" + where + " ORDER BY created_at DESC,id", (repo,) if repo else ()):
        a = dict(row)
        a["who"] = person_names.get(a["person_id"]) or by_team.get(a["team_id"], {}).get("name") or ""
        a["is_team"] = bool(a["team_id"])
        authority.append({k: a[k] for k in ("id", "who", "is_team", "scope_kind", "scope", "role", "repo", "source",
                                           "asserted_by", "accepted", "effective_to")})
    where = " AND repo=?" if repo else ""
    ownership = [dict(r) for r in db.execute(
        "SELECT rowid AS id,repo,path_prefix,engineer,source,weight,evidence FROM ownership WHERE valid_to IS NULL" +
        where + " ORDER BY repo,path_prefix,weight DESC,rowid", (repo,) if repo else ())]
    graph = graph_summary(db)
    graph["repos"].sort()
    coordinator = db.execute(
        "SELECT p.name FROM people p JOIN settings s ON s.value=p.id WHERE s.key='coordinator' AND p.active=1").fetchone()
    return {"people": people, "owners": owners, "ownership": ownership, "authority": authority,
            "teams": teams, "repos": graph.pop("repos")}, graph, coordinator["name"] if coordinator else ""


def _matches(value, query):
    if isinstance(value, str):
        return query in value.casefold()
    if isinstance(value, dict):
        return any(_matches(v, query) for v in value.values())
    if isinstance(value, list):
        return any(_matches(v, query) for v in value)
    return False


def list_owners(store, args):
    query, repo, limit = args.get("query", ""), args.get("repo", ""), args.get("limit", DEFAULT_LIMIT)
    for key, value, bound in (("query", query, 200), ("repo", repo, 500)):
        if not isinstance(value, str) or len(value) > bound:
            raise Invalid(f"{key} must be text of at most {bound} characters")
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise Invalid(f"limit must be an integer from 1 to {MAX_LIMIT}")
    query = query.strip().casefold()
    binding = _digest({"query": query, "repo": repo, "limit": limit})
    cursor = _read_cursor(args["cursor"], binding) if "cursor" in args else None
    with store.connect() as db:
        db.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                   if getattr(db, "dialect", "") == "postgres" else "BEGIN")
        collections, graph, coordinator = _snapshot(db, repo)
    snapshot = _digest([collections, graph, coordinator])
    available = {k: len(rows) for k, rows in collections.items()}
    if query:
        collections = {k: [row for row in rows if _matches(row, query)] for k, rows in collections.items()}
    if cursor and cursor["snapshot"] != snapshot:
        raise Invalid("Owner discovery results changed; discard accumulated pages and restart bridge_list_owners without cursor. No continuation rows were returned")
    starts = cursor["offsets"] if cursor else dict.fromkeys(COLLECTIONS, 0)
    totals = {k: len(rows) for k, rows in collections.items()}
    if cursor and (any(starts[k] > totals[k] for k in COLLECTIONS) or all(starts[k] == totals[k] for k in COLLECTIONS)):
        raise Invalid("Owner discovery cursor is out of range; restart bridge_list_owners without cursor")
    offsets = dict(starts)
    page = {k: [] for k in COLLECTIONS}

    def response():
        has_more = any(offsets[k] < totals[k] for k in COLLECTIONS)
        return {**{k: page[k] for k in COLLECTIONS if k != "repos"},
                "graph": {**graph, "repos": page["repos"]}, "coordinator": coordinator, "notice": NOTICE,
                "pagination": {"query": query, "repo": repo, "limit": limit, "snapshot": snapshot,
                    "max_content_bytes": MAX_CONTENT_BYTES,
                    "truncated": any(len(page[k]) < totals[k] for k in COLLECTIONS), "has_more": has_more,
                    "next_cursor": _cursor(binding, snapshot, offsets) if has_more else None,
                    "collections": {k: {"available": available[k], "total": totals[k], "offset": starts[k],
                        "returned": len(page[k]), "truncated": len(page[k]) < totals[k],
                        "has_more": offsets[k] < totals[k]} for k in COLLECTIONS},
                    "scope": "repo filters ownership and authority (including global authority); people, owners, teams and graph totals remain organization-wide. query is a case-insensitive substring of displayed text in each collection independently.",
                    "next": "Repeat with the same query, repo and limit plus next_cursor until has_more is false; combine each list, including graph.repos. A changed snapshot requires discarding prior pages and restarting. Use query to narrow discovery; zero matches do not establish absence of ownership or required approval."}}

    if len(json.dumps(json.dumps(response())).encode()) > MAX_CONTENT_BYTES:
        raise Invalid("Owner discovery metadata exceeds its content budget; read the complete directory through the authorized HTTP /api/people and /api/state endpoints")
    returned = 0
    # Round-robin lists so discovery shows contact, ownership and authority
    # signals together. Every row stays intact, including nested team lists.
    while returned < limit:
        advanced = False
        for key in COLLECTIONS:
            if offsets[key] >= totals[key]:
                continue
            page[key].append(collections[key][offsets[key]])
            offsets[key] += 1
            candidate = response()
            if len(json.dumps(json.dumps(candidate)).encode()) > MAX_CONTENT_BYTES:
                page[key].pop()
                offsets[key] -= 1
                if not returned:
                    raise Invalid(f"One {key} row exceeds the {MAX_CONTENT_BYTES}-byte owner discovery content budget; no part of it was returned. Read complete rows through the authorized HTTP /api/people (people, teams, authority), /api/ownership?limit=0 (ownership), or /api/state (owners, graph) endpoints. Use query to discover other rows")
                return response()
            returned += 1
            advanced = True
            if returned == limit:
                break
        if not advanced:
            break
    return response()
