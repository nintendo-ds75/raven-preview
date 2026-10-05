"""GitHub as a source, kept current.

What git alone cannot tell Bridge: who approved a pull request (a
squash merge carries no trailer), who is on the team a CODEOWNERS line
names, and what the pull request said. Synced on a cursor, by command
(`bridge sync owner/name`), on a schedule (GITHUB_TOKEN in the server's
environment) or by webhook (`/webhooks/github`), and kept in tables of
its own so a rebuild of the git index lays it on again: approvals land
on the commits that merged (role approved-by, merged-by), the team's
members become people, descriptions and review summaries become
records. The sync state (cursor, last success, last error) is shown in
routing evidence when it is stale, so nobody trusts an old map."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from .graph import Graph, now_iso
from .store import Invalid, repo_key

API = "https://api.github.com"
MAX_PULLS_BOOTSTRAP = 300
MAX_FILES = 300
MAX_BODY = 20000
MAX_REVIEW_BODY = 4000
MIN_REVIEW_BODY = 40
MAX_TEAM_MEMBERS = 500
USER_TTL_DAYS = 30
STALE_DAYS = 3.0
SOURCE = "github"
# A poll re-reads this much of the interval before its watermark, so a
# pull request updated a moment before the last poll's newest one, or
# at the same second, is never left behind; writes are idempotent.
OVERLAP_HOURS = 24


class GitHubError(Exception):
    def __init__(self, message: str, status: int = 0, retry_after: float = 0.0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class GitHubAPI:
    """The REST API over the standard library: one token, JSON in and
    out, pagination by the Link header, rate limits surfaced as errors
    the caller records rather than retried blindly."""

    def __init__(self, token: str, base: str = API, opener=None, timeout: float = 30.0):
        self.token = token
        self.base = base.rstrip("/")
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def get(self, path: str, params: dict | None = None) -> tuple:
        url = path if path.startswith("http") else self.base + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "bridge-sync"})
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode() or "null"), dict(resp.headers)
        except urllib.error.HTTPError as error:
            headers = dict(error.headers or {})
            retry = 0.0
            if error.code in (403, 429) and headers.get("X-RateLimit-Remaining") == "0":
                try:
                    retry = max(0.0, float(headers.get("X-RateLimit-Reset", "0")) - time.time())
                except ValueError:
                    retry = 60.0
                raise GitHubError(f"GitHub rate limit reached; resets in {int(retry)}s", error.code, retry)
            try:
                detail = json.loads(error.read().decode()).get("message", "")
            except Exception:
                detail = ""
            raise GitHubError(f"GitHub {error.code} on {path}: {detail or error.reason}", error.code)
        except (urllib.error.URLError, OSError) as error:
            raise GitHubError(f"GitHub unreachable on {path}: {error}")

    def list(self, path: str, params: dict | None = None):
        """Every item of a paginated collection, page by page, so a caller
        that stops early never fetches the pages after it."""
        params = {"per_page": 100, **(params or {})}
        url = path
        while url:
            items, headers = self.get(url, params)
            params = None
            if not isinstance(items, list):
                return
            yield from items
            url = _next_link(headers.get("Link", "") or headers.get("link", ""))


def _next_link(header: str) -> str:
    for part in (header or "").split(","):
        if 'rel="next"' in part:
            start, end = part.find("<"), part.find(">")
            if 0 <= start < end:
                return part[start + 1:end]
    return ""


# ---------------- sync state ----------------

def sync_state(store: Graph, repo: str) -> dict | None:
    row = store.db.execute("SELECT * FROM sync_state WHERE repo=? AND source=?", (repo, SOURCE)).fetchone()
    if row is None:
        return None
    out = dict(row)
    try:
        out["stats"] = json.loads(out.get("stats") or "{}")
    except ValueError:
        out["stats"] = {}
    return out


def sync_states(store: Graph) -> list[dict]:
    return [sync_state(store, r["repo"]) for r in
            store.db.execute("SELECT repo FROM sync_state WHERE source=? ORDER BY repo", (SOURCE,)).fetchall()]


def _set_state(store: Graph, repo: str, **values) -> None:
    store.db.execute("INSERT OR IGNORE INTO sync_state(repo, source) VALUES(?,?)", (repo, SOURCE))
    if values:
        if "stats" in values and not isinstance(values["stats"], str):
            values["stats"] = json.dumps(values["stats"], sort_keys=True)
        store.db.execute("UPDATE sync_state SET " + ", ".join(f"{k}=?" for k in values) + " WHERE repo=? AND source=?",
                         (*values.values(), repo, SOURCE))


def _merge_stats(store: Graph, repo_name: str, extra: dict) -> dict:
    """The stats JSON with these keys added, keeping the rest."""
    current = (sync_state(store, repo_name) or {}).get("stats") or {}
    current.update({k: v for k, v in extra.items() if k != "repo"})
    return current


def _overlap_since(cursor: str) -> str:
    """The instant a poll reads back to: the watermark less the overlap
    window, in GitHub's timestamp form."""
    if not cursor:
        return ""
    try:
        at = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
    except ValueError:
        return cursor
    return (at - timedelta(hours=OVERLAP_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")


def register(store: Graph, repo: str) -> None:
    """A repository the scheduler should keep current."""
    with store.transaction():
        _set_state(store, repo_key(repo))


def sync_note(store: Graph, repo: str, now: float | None = None) -> str:
    """The staleness of what GitHub told Bridge about this repository,
    for the routing evidence: nothing when fresh, the age when old, and
    the fact when a hosted repository was never synced."""
    if not repo or "/" not in repo:
        return ""
    state = sync_state(store, repo)
    if state is None:
        # About what Bridge knows of past reviews, for routing; not about how
        # this decision is approved. Worded as it was ("approvals come from
        # git trailers"), a brief sent to an owner said this decision's
        # approval would come from a commit trailer.
        return (f"GitHub not synced for {repo}: Bridge knows past reviews only from git trailers and merge commits "
                f"(bridge sync {repo}, or GITHUB_TOKEN with BRIDGE_GITHUB_REPOS on the server)")
    if not state["last_success_at"]:
        return f"GitHub sync for {repo} has not succeeded yet" + (f": {state['last_error']}" if state["last_error"] else "")
    try:
        age = ((now or time.time()) - datetime.fromisoformat(state["last_success_at"]).timestamp()) / 86400.0
    except ValueError:
        age = 0.0
    if age >= STALE_DAYS:
        return (f"GitHub sync for {repo} last succeeded {int(age)} days ago; approvals and team changes since "
                "then are missing" + (f" (last error: {state['last_error'][:160]})" if state["last_error"] else ""))
    return ""


# ---------------- people ----------------

def _is_bot(login: str) -> bool:
    login = (login or "").lower()
    return not login or login.endswith("[bot]") or login.endswith("-bot") or login in ("dependabot", "renovate", "github-actions")


def _user_cached(store: Graph, login: str) -> dict | None:
    row = store.db.execute("SELECT * FROM gh_users WHERE login=?", (login.lower(),)).fetchone()
    if row is None:
        return None
    try:
        age = (time.time() - datetime.fromisoformat(row["fetched_at"]).timestamp()) / 86400.0
    except ValueError:
        age = USER_TTL_DAYS + 1
    return dict(row) if age < USER_TTL_DAYS else None


def _fetch_users(store: Graph, api: GitHubAPI, logins: set[str]) -> dict[str, dict]:
    """Display names and public emails for logins Bridge has not seen
    lately; the network happens here, outside any transaction."""
    out: dict[str, dict] = {}
    fresh: list[tuple] = []
    for login in sorted(item for item in logins if item and not _is_bot(item)):
        cached = _user_cached(store, login)
        if cached is not None:
            out[login.lower()] = cached
            continue
        try:
            data, _ = api.get(f"/users/{urllib.parse.quote(login)}")
        except GitHubError as error:
            if error.status in (403, 429):
                raise
            data = {}
        row = {"login": login.lower(), "name": (data or {}).get("name") or "", "email": (data or {}).get("email") or ""}
        out[login.lower()] = row
        fresh.append((row["login"], row["name"], row["email"], now_iso()))
    if fresh:
        with store.transaction():
            store.db.executemany("INSERT OR REPLACE INTO gh_users(login, name, email, fetched_at) VALUES(?,?,?,?)", fresh)
    return out


def person_for_login(store: Graph, login: str) -> dict | None:
    if not login:
        return None
    row = store.db.execute("SELECT * FROM people WHERE lower(github_login)=? AND active=1", (login.lower(),)).fetchone()
    return dict(row) if row else None


def resolve_login(store: Graph, login: str) -> tuple[str, str]:
    """(name, email) the graph should carry for a login: the verified
    person's, else the display name GitHub gave, else the login."""
    person = person_for_login(store, login)
    if person is not None:
        return person["name"], person.get("email") or ""
    row = store.db.execute("SELECT name, email FROM gh_users WHERE login=?", (login.lower(),)).fetchone()
    if row is not None and row["name"]:
        return row["name"], row["email"] or ""
    return login, (row["email"] if row is not None else "") or ""


# ---------------- pull requests ----------------

def _pull_row(api: GitHubAPI, owner: str, name: str, pr: dict) -> dict:
    """One merged pull request with what routing and records need: the
    merge commit, the files, the reviews, who merged it."""
    number = int(pr["number"])
    base = f"/repos/{owner}/{name}/pulls/{number}"
    if "merged_by" not in pr or pr.get("merged_by") is None:
        full, _ = api.get(base)
        pr = {**pr, **(full or {})}
    files: list[str] = []
    truncated = 0
    for item in api.list(base + "/files"):
        if len(files) >= MAX_FILES:
            truncated = 1
            break
        if item.get("filename"):
            files.append(item["filename"])
    reviews = []
    for r in api.list(base + "/reviews"):
        user = (r.get("user") or {}).get("login") or ""
        reviews.append({"id": r.get("id"), "user": user, "state": (r.get("state") or "").upper(),
                        "body": (r.get("body") or "")[:MAX_REVIEW_BODY], "submitted_at": r.get("submitted_at") or ""})
    return {"number": number, "merge_sha": pr.get("merge_commit_sha") or "", "title": pr.get("title") or "",
            "body": (pr.get("body") or "")[:MAX_BODY], "author": (pr.get("user") or {}).get("login") or "",
            "merged_by": (pr.get("merged_by") or {}).get("login") or "", "merged_at": pr.get("merged_at") or "",
            "updated_at": pr.get("updated_at") or "", "files": files, "reviews": reviews, "truncated": truncated}


def _store_pull(store: Graph, repo: str, row: dict) -> None:
    store.db.execute(
        "INSERT OR REPLACE INTO gh_pulls(repo, number, merge_sha, title, body, author, merged_by, merged_at, "
        "updated_at, files, reviews, truncated, synced_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (repo, row["number"], row["merge_sha"], row["title"], row["body"], row["author"], row["merged_by"],
         row["merged_at"], row["updated_at"], json.dumps(row["files"]), json.dumps(row["reviews"]),
         row["truncated"], now_iso()))


def _logins_of(rows: list[dict]) -> set[str]:
    out: set[str] = set()
    for row in rows:
        out.add(row["author"])
        out.add(row["merged_by"])
        for r in row["reviews"]:
            out.add(r["user"])
    return {login for login in out if login}


def apply_pull(store: Graph, target: str, row: dict) -> dict:
    """Lay one pull request onto the graph of `target`: approvals and the
    merger onto the merge commit (an author git already recorded is not
    written twice under another name), the description and the review
    summaries as records with the files they touched."""
    from .ingest import _is_machine_author
    stats = {"approvals": 0, "records": 0}
    sha = row["merge_sha"]
    files = row["files"]
    author_name, author_email = resolve_login(store, row["author"])
    people: list[tuple[str, str, str]] = []
    if sha and files:
        has_author = store.db.execute("SELECT 1 FROM change_people WHERE repo=? AND sha=? AND role='author'",
                                      (target, sha)).fetchone()
        if not has_author and row["author"] and not _is_bot(row["author"]) and not _is_machine_author(author_name):
            people.append((author_name, author_email, "author"))
        seen: set[str] = set()
        for r in row["reviews"]:
            login = r["user"]
            if r["state"] != "APPROVED" or not login or login == row["author"] or _is_bot(login) or login in seen:
                continue
            seen.add(login)
            name, email = resolve_login(store, login)
            if _is_machine_author(name):
                continue
            people.append((name, email, "approved-by"))
            stats["approvals"] += 1
        merger = row["merged_by"]
        if merger and merger != row["author"] and merger not in seen and not _is_bot(merger):
            name, email = resolve_login(store, merger)
            if not _is_machine_author(name):
                people.append((name, email, "merged-by"))
                stats["approvals"] += 1
        for name, email, _role in people:
            store.upsert_engineer(name, email)
        if people:
            store.add_change(target, sha, row["merged_at"], files[:200], people)
    ref = str(row["number"])
    body = row["body"].strip()
    if body or row["title"]:
        store.upsert_intent(target, "pr", ref, row["title"].strip(), body, author_name, row["merged_at"],
                            status="merged", resolved=True)
        store.add_intent_paths(target, "pr", ref, files[:40])
        stats["records"] += 1
    for r in row["reviews"]:
        text = (r["body"] or "").strip()
        if len(text) < MIN_REVIEW_BODY or _is_bot(r["user"]):
            continue
        name, _email = resolve_login(store, r["user"])
        rref = f"{ref}:{r['id']}"
        store.upsert_intent(target, "review", rref, f"Review of #{ref}: {row['title'].strip()}", text, name,
                            r["submitted_at"] or row["merged_at"], status=(r["state"] or "").lower(), resolved=True)
        store.add_intent_paths(target, "review", rref, files[:40])
        stats["records"] += 1
    return stats


def _pull_rows(store: Graph, repo: str) -> list[dict]:
    out = []
    for r in store.db.execute("SELECT * FROM gh_pulls WHERE repo=? ORDER BY number", (repo,)).fetchall():
        row = dict(r)
        row["files"] = json.loads(row["files"] or "[]")
        row["reviews"] = json.loads(row["reviews"] or "[]")
        out.append(row)
    return out


def apply_pulls(store: Graph, name: str) -> dict:
    """Every synced pull request whose repository resolves onto the graph
    `name` (the qualified sync key, or the bare checkout name it maps
    to), applied in the caller's transaction. Called at the end of every
    git rebuild, and after every sync."""
    stats = {"pulls": 0, "approvals": 0, "records": 0}
    repos = [r["repo"] for r in store.db.execute("SELECT DISTINCT repo FROM gh_pulls").fetchall()]
    for repo in repos:
        if repo != name and store.resolve_repo(repo) != name:
            continue
        for row in _pull_rows(store, repo):
            part = apply_pull(store, name, row)
            stats["pulls"] += 1
            stats["approvals"] += part["approvals"]
            stats["records"] += part["records"]
    return stats


# ---------------- teams ----------------

def team_handles(store: Graph, repo: str) -> list[str]:
    """The teams CODEOWNERS names for the repository, as org/slug."""
    target = store.resolve_repo(repo)
    out: list[str] = []
    for r in store.db.execute("SELECT DISTINCT person FROM listings WHERE repo=? AND role='team'", (target,)).fetchall():
        handle = r["person"].lstrip("@")
        if "/" in handle and handle not in out:
            out.append(handle)
    return out


def _fetch_team(api: GitHubAPI, handle: str) -> tuple[str, list[str]]:
    org, _, slug = handle.partition("/")
    base = f"/orgs/{urllib.parse.quote(org)}/teams/{urllib.parse.quote(slug)}"
    try:
        team, _ = api.get(base)
    except GitHubError as error:
        if error.status == 404:
            return "", []
        raise
    logins: list[str] = []
    for member in api.list(base + "/members"):
        login = member.get("login") or ""
        if login and not _is_bot(login):
            logins.append(login)
        if len(logins) >= MAX_TEAM_MEMBERS:
            break
    return (team or {}).get("name") or slug, logins


def _apply_team(store: Graph, handle: str, team_name: str, logins: list[str], users: dict) -> dict:
    """The team and its members as people; members who left are dropped
    (the tombstone is the event), members who joined are added, a login
    nobody verified becomes a person with source github."""
    existing = store.db.execute("SELECT id FROM teams WHERE lower(handle)=?", (handle.lower(),)).fetchone()
    team_id = existing["id"] if existing else store.add_team(team_name or handle.split("/", 1)[1], handle, source=SOURCE)
    before = {r["person_id"] for r in store.db.execute("SELECT person_id FROM team_members WHERE team_id=?", (team_id,))}
    ids: list[str] = []
    for login in logins:
        person = person_for_login(store, login)
        if person is None:
            info = users.get(login.lower(), {})
            pid = store.add_person(info.get("name") or login, email=info.get("email") or "", github_login=login,
                                   source=SOURCE)
        else:
            pid = person["id"]
        ids.append(pid)
    store.set_team_members(team_id, ids, source=SOURCE, replace=True)
    after = set(ids)
    if before != after:
        store.append_event("team_synced", {"team": handle, "added": sorted(after - before),
                                           "removed": sorted(before - after)})
    return {"team": handle, "members": len(ids), "added": len(after - before), "removed": len(before - after)}


def sync_teams(store: Graph, api: GitHubAPI, repo: str) -> list[dict]:
    """The members of every team the repository's CODEOWNERS names."""
    fetched = []
    for handle in team_handles(store, repo):
        team_name, logins = _fetch_team(api, handle)
        if not logins and not team_name:
            continue
        fetched.append((handle, team_name, logins))
    users = _fetch_users(store, api, {login for _handle, _name, logins in fetched for login in logins})
    out = []
    with store.transaction():
        for handle, team_name, logins in fetched:
            out.append(_apply_team(store, handle, team_name, logins, users))
    return out


# ---------------- the sync ----------------

def sync_repo(store: Graph, api: GitHubAPI, repo: str, limit: int = MAX_PULLS_BOOTSTRAP) -> dict:
    """Bring one repository up to date: merged pull requests updated
    since the polling watermark less an overlap window (the watermark is
    the newest updated_at a complete poll covered; a webhook never moves
    it), their approvals, files and reviews; the CODEOWNERS teams'
    members. Every network read happens before the write; the watermark
    moves only when the write succeeded, so a failed sync is repeated,
    never skipped, and a pull request updated at the watermark's very
    second is read again rather than left behind. The state records the
    attempt, the success and the last error."""
    repo = repo_key(repo)
    if "/" not in repo:
        raise Invalid("sync needs the repository as owner/name")
    owner, _, name = repo.partition("/")
    state = sync_state(store, repo)
    cursor = state["cursor"] if state else ""
    with store.transaction():
        _set_state(store, repo, last_attempt_at=now_iso())
    stats = {"repo": repo, "pulls": 0, "approvals": 0, "records": 0, "skipped": 0, "teams": [], "cursor": cursor}
    try:
        rows: list[dict] = []
        newest = cursor
        seen = 0
        since = _overlap_since(cursor)
        for pr in api.list(f"/repos/{owner}/{name}/pulls", {"state": "closed", "sort": "updated", "direction": "desc"}):
            updated = pr.get("updated_at") or ""
            if since and updated < since:
                break
            if not cursor and seen >= limit:
                break
            seen += 1
            newest = max(newest, updated)
            if not pr.get("merged_at"):
                stats["skipped"] += 1
                continue
            rows.append(_pull_row(api, owner, name, pr))
        users = _fetch_users(store, api, _logins_of(rows))
        target = store.resolve_repo(repo)
        with store.transaction():
            for row in rows:
                _store_pull(store, repo, row)
                part = apply_pull(store, target, row)
                stats["pulls"] += 1
                stats["approvals"] += part["approvals"]
                stats["records"] += part["records"]
        stats["teams"] = sync_teams(store, api, repo)
        stats["cursor"] = newest
        with store.transaction():
            _set_state(store, repo, cursor=newest, last_success_at=now_iso(), last_error="",
                       stats=_merge_stats(store, repo, {**{k: v for k, v in stats.items() if k != "teams"},
                                                        "teams": len(stats["teams"]), "overlap_hours": OVERLAP_HOURS}))
            store.append_event("github_synced", {"repo": repo, "pulls": stats["pulls"], "approvals": stats["approvals"],
                                                 "records": stats["records"], "teams": len(stats["teams"]),
                                                 "users": len(users)})
    except GitHubError as error:
        with store.transaction():
            _set_state(store, repo, last_error=str(error)[:500])
        raise
    return stats


def sync_pull(store: Graph, api: GitHubAPI, repo: str, number: int) -> dict:
    """One pull request, as a webhook names it."""
    repo = repo_key(repo)
    owner, _, name = repo.partition("/")
    pr, _ = api.get(f"/repos/{owner}/{name}/pulls/{int(number)}")
    if not pr or not pr.get("merged_at"):
        return {"repo": repo, "number": number, "merged": False}
    row = _pull_row(api, owner, name, pr)
    _fetch_users(store, api, _logins_of([row]))
    target = store.resolve_repo(repo)
    with store.transaction():
        _set_state(store, repo)
        _store_pull(store, repo, row)
        part = apply_pull(store, target, row)
        # One object is not evidence that every earlier object was
        # processed: the polling watermark stays where the last complete
        # poll left it; the webhook's own time is recorded apart.
        _set_state(store, repo, stats=_merge_stats(store, repo, {"webhook_at": now_iso(), "webhook_number": number,
                                                                 "webhook_updated_at": row["updated_at"]}))
        store.append_event("github_synced", {"repo": repo, "pulls": 1, "number": number, "via": "webhook", **part})
    return {"repo": repo, "number": number, "merged": True, **part}


# ---------------- webhook ----------------

def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def handle_webhook(store: Graph, api: GitHubAPI | None, event: str, delivery_id: str, payload: dict) -> dict:
    """A verified delivery: a merged pull request or a submitted review
    syncs that pull request; a team or membership change syncs the
    teams of every registered repository in that organization. Each
    delivery id is handled once: one that fails lets go of its id, so
    GitHub's redelivery is handled rather than dropped as a duplicate.
    Measured on a live install: a merged pull request whose sync failed
    was answered 502, and its redelivery was then ignored for good."""
    try:
        return _handle_webhook(store, api, event, delivery_id, payload)
    except Exception:
        if delivery_id:
            with store.transaction():
                store.db.execute("DELETE FROM webhook_receipts WHERE id=?", (f"github:{delivery_id}",))
        raise


def _handle_webhook(store: Graph, api: GitHubAPI | None, event: str, delivery_id: str, payload: dict) -> dict:
    if delivery_id and store.db.execute("SELECT 1 FROM webhook_receipts WHERE id=?",
                                        (f"github:{delivery_id}",)).fetchone():
        return {"ok": True, "duplicate": True}
    if api is None and event != "ping":
        # Nothing is fetched, so nothing is recorded as received: GitHub's
        # redelivery once the token is set is handled, not dropped as a
        # duplicate of a delivery that did nothing.
        return {"ok": False, "error": "GITHUB_TOKEN is not set on the server, so nothing was fetched; set it, then "
                                      "redeliver this event from the webhook's page on GitHub"}
    if delivery_id:
        with store.transaction():
            store.db.execute("INSERT INTO webhook_receipts(id, channel, received_at) VALUES(?,?,?)",
                             (f"github:{delivery_id}", SOURCE, now_iso()))
    if event == "ping":
        return {"ok": True, "pong": True}
    repo = repo_key((payload.get("repository") or {}).get("full_name") or "")
    if event in ("pull_request", "pull_request_review"):
        pr = payload.get("pull_request") or {}
        if event == "pull_request" and not (payload.get("action") == "closed" and pr.get("merged")):
            return {"ok": True, "ignored": payload.get("action")}
        if event == "pull_request_review" and payload.get("action") != "submitted":
            return {"ok": True, "ignored": payload.get("action")}
        if not repo or not pr.get("number"):
            return {"ok": False, "error": "no repository or pull request number in the delivery"}
        return {"ok": True, **sync_pull(store, api, repo, int(pr["number"]))}
    if event in ("team", "membership", "team_add"):
        org = ((payload.get("organization") or {}).get("login") or "").lower()
        synced = []
        for state in sync_states(store):
            if state["repo"].startswith(org + "/"):
                synced.extend(sync_teams(store, api, state["repo"]))
        return {"ok": True, "teams": synced}
    return {"ok": True, "ignored": event}


# ---------------- the scheduler ----------------

class Syncer:
    """Keeps every registered repository current from a background
    thread: the ones named at startup (BRIDGE_GITHUB_REPOS) and the
    ones synced before. One failure is recorded on its repository and
    the loop goes on."""

    def __init__(self, store: Graph, api, repos: list[str] | None = None, minutes: float = 15.0):
        self.store = store
        self.api = api
        self.minutes = max(1.0, float(minutes))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        for repo in repos or []:
            try:
                register(store, repo)
            except Exception as error:
                print(f"Bridge GitHub: cannot register {repo}: {error}")

    def tick(self) -> list[dict]:
        out = []
        for state in sync_states(self.store):
            try:
                api = self.api(state["repo"]) if callable(self.api) else self.api
                if api is not None:
                    out.append(sync_repo(self.store, api, state["repo"]))
            except GitHubError as error:
                print(f"Bridge GitHub: sync of {state['repo']} failed: {error}")
                if error.retry_after:
                    self._stop.wait(min(error.retry_after, 900))
            except Exception as error:  # the loop never dies on one repository
                print(f"Bridge GitHub: sync of {state['repo']}: {type(error).__name__}: {error}")
        return out

    def start(self) -> None:
        if self._thread is not None:
            return

        def loop():
            while not self._stop.is_set():
                self.tick()
                self._wake.wait(self.minutes * 60)
                self._wake.clear()
        self._thread = threading.Thread(target=loop, name="bridge-github-sync", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
