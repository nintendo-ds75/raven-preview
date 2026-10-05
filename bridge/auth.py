"""Who is asking: sessions, tokens and roles for a shared Bridge.

A Bridge on loopback with auth off is the single-operator workspace it
always was. A Bridge other people reach runs with auth on: every request
carries an identity, a person from the people table, and every write is
attributed to it. Two ways in:

- GitHub OAuth (standard library only): the browser signs in with the
  organization's GitHub, the login or a verified email is matched to a
  person, and a signed cookie carries the session.
- Personal tokens: a person (or an admin for them) mints a token, shown
  once and stored hashed; agents and scripts send it as a Bearer token.
  A bootstrap token in the environment (BRIDGE_ADMIN_TOKEN) is the way
  in before any person exists.

Roles: viewer reads, member answers, signs, kicks off tasks and writes
nodes, admin also maintains people, authority, tokens and settings.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .store import Invalid

SESSION_COOKIE = "bridge_session"
SESSION_DAYS = 14
ROLES = ("viewer", "member", "admin")
ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2}
TOKEN_KINDS = ("agent", "human")


@dataclass
class Identity:
    """The person behind a request. `kind` is how they proved it. An
    agent credential (kind agent) reads and speaks the agent protocol on
    the person's behalf; it decides nothing, whatever the person's role."""
    id: str
    name: str
    role: str
    kind: str            # session | token | agent | bootstrap | operator
    email: str = ""
    token_label: str = ""

    @property
    def is_agent(self) -> bool:
        return self.kind == "agent"

    def allows(self, role: str) -> bool:
        if self.is_agent:
            return role == "viewer"
        return ROLE_RANK.get(self.role, -1) >= ROLE_RANK.get(role, 99)


OPERATOR = Identity(id="", name="Local operator", role="admin", kind="operator")


class Auth:
    """Authentication for one server: reads the environment once, keeps
    the signing secret in the settings table so sessions survive a
    restart, and answers `identify(headers)` for every request."""

    def __init__(self, store, enabled: bool, public_url: str = ""):
        self.store = store
        self.enabled = enabled
        self.public_url = (public_url or os.environ.get("BRIDGE_PUBLIC_URL", "")).rstrip("/")
        self.github_client_id = os.environ.get("BRIDGE_GITHUB_CLIENT_ID", "")
        self.github_client_secret = os.environ.get("BRIDGE_GITHUB_CLIENT_SECRET", "")
        self.github_org = os.environ.get("BRIDGE_GITHUB_ORG", "").strip().lower()
        self.allow_signup = os.environ.get("BRIDGE_AUTH_ALLOW_SIGNUP", "") == "1"
        self.bootstrap_token = os.environ.get("BRIDGE_ADMIN_TOKEN", "").strip()
        self._states: dict[str, float] = {}
        self._secret: bytes | None = None

    # ---------------- the signing secret ----------------

    @property
    def secret(self) -> bytes:
        if self._secret is None:
            env = os.environ.get("BRIDGE_SECRET", "").strip()
            if env:
                self._secret = env.encode()
            else:
                graph = self.store.graph
                stored = graph.get_setting("session_secret")
                if not stored:
                    stored = secrets.token_urlsafe(32)
                    with graph.transaction():
                        graph.set_setting("session_secret", stored)
                self._secret = stored.encode()
        return self._secret

    # ---------------- sessions ----------------

    def session_cookie(self, person_id: str) -> str:
        expires = int(time.time()) + SESSION_DAYS * 86400
        body = f"{person_id}.{expires}"
        sig = hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()[:32]
        value = f"{body}.{sig}"
        attrs = f"{SESSION_COOKIE}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS * 86400}"
        if self.public_url.startswith("https://"):
            attrs += "; Secure"
        return attrs

    def clear_cookie(self) -> str:
        return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"

    def _session_person(self, cookie_header: str) -> str:
        for part in (cookie_header or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name != SESSION_COOKIE or not value:
                continue
            pieces = value.split(".")
            if len(pieces) != 3:
                return ""
            person_id, expires, sig = pieces
            body = f"{person_id}.{expires}"
            expected = hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()[:32]
            if not hmac.compare_digest(sig, expected):
                return ""
            try:
                if int(expires) < time.time():
                    return ""
            except ValueError:
                return ""
            return person_id
        return ""

    # ---------------- tokens ----------------

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create_token(self, person_id: str, label: str = "", created_by: str = "", kind: str = "agent") -> dict:
        """A new token for a person, returned in clear exactly once; only
        its hash is stored. kind 'agent' (the default) is a credential
        for the person's coding agent: it kicks off tasks and writes
        nodes as them, reads, and decides nothing. kind 'human' is for
        the person's own scripts and carries their role."""
        graph = self.store.graph
        person = graph.get_person(person_id)
        if person is None:
            raise Invalid("Person not found")
        if kind not in TOKEN_KINDS:
            raise Invalid("kind must be agent or human")
        token = "brg_" + secrets.token_urlsafe(30)
        tid = secrets.token_hex(6)
        with graph.transaction():
            graph.db.execute("INSERT INTO api_tokens(id, person_id, label, token_hash, created_by, created_at, kind) "
                             "VALUES(?,?,?,?,?,?,?)", (tid, person_id, (label or "agent")[:100], self._hash(token),
                                                       created_by[:100], _now(), kind))
            graph.append_event("token_created", {"token_id": tid, "person_id": person_id, "label": label,
                                                 "created_by": created_by, "kind": kind})
        return {"id": tid, "person_id": person_id, "label": label or "agent", "kind": kind, "token": token,
                "notice": ("Shown once. Store it where the agent runs; Bridge keeps only its hash." if kind == "agent"
                           else "Shown once. This token carries your own role; keep it as you would a password.")}

    def revoke_token(self, token_id: str, by: str = "") -> dict:
        graph = self.store.graph
        with graph.transaction():
            graph.db.execute("UPDATE api_tokens SET revoked_at=? WHERE id=? AND revoked_at=''", (_now(), token_id))
            graph.append_event("token_revoked", {"token_id": token_id, "by": by})
        return {"id": token_id, "revoked": True}

    def tokens(self, person_id: str = "") -> list[dict]:
        sql = "SELECT id, person_id, label, kind, created_by, created_at, last_used_at, revoked_at FROM api_tokens"
        args: tuple = ()
        if person_id:
            sql += " WHERE person_id=?"
            args = (person_id,)
        return [dict(r) for r in self.store.graph.db.execute(sql + " ORDER BY created_at DESC", args)]

    def _token_person(self, token: str) -> tuple[str, str, str]:
        row = self.store.graph.db.execute(
            "SELECT id, person_id, label, kind FROM api_tokens WHERE token_hash=? AND revoked_at=''",
            (self._hash(token),)).fetchone()
        if row is None:
            return "", "", ""
        try:
            with self.store.graph.transaction():
                self.store.graph.db.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (_now(), row["id"]))
        except Exception:
            pass
        return row["person_id"], row["label"], row["kind"] or "agent"

    # ---------------- identifying a request ----------------

    def identify(self, headers) -> Identity | None:
        """The identity a request carries: a Bearer token, the bootstrap
        token, or a session cookie. None when it carries none that is
        valid. With auth off, the local operator."""
        if not self.enabled:
            return OPERATOR
        authorization = headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
            if self.bootstrap_token and hmac.compare_digest(token, self.bootstrap_token):
                return Identity(id="", name="Bootstrap admin", role="admin", kind="bootstrap")
            person_id, label, kind = self._token_person(token)
            if person_id:
                person = self.store.graph.get_person(person_id)
                if person is not None and person.get("active", 1):
                    return Identity(id=person["id"], name=person["name"], role=person.get("role") or "member",
                                    kind="agent" if kind == "agent" else "token", email=person.get("email", ""),
                                    token_label=label)
            return None
        person_id = self._session_person(headers.get("Cookie", ""))
        if person_id:
            person = self.store.graph.get_person(person_id)
            if person is not None and person.get("active", 1):
                return Identity(id=person["id"], name=person["name"], role=person.get("role") or "member",
                                kind="session", email=person.get("email", ""))
        return None

    # ---------------- GitHub OAuth ----------------

    @property
    def github_configured(self) -> bool:
        return bool(self.github_client_id and self.github_client_secret)

    def github_authorize_url(self, redirect_uri: str) -> str:
        if not self.github_configured:
            raise Invalid("GitHub sign-in is not configured (BRIDGE_GITHUB_CLIENT_ID and BRIDGE_GITHUB_CLIENT_SECRET)")
        state = secrets.token_urlsafe(24)
        now = time.time()
        self._states = {s: t for s, t in self._states.items() if now - t < 600}
        self._states[state] = now
        query = urllib.parse.urlencode({"client_id": self.github_client_id, "redirect_uri": redirect_uri,
                                        "scope": "read:user user:email read:org", "state": state})
        return "https://github.com/login/oauth/authorize?" + query

    def github_exchange(self, code: str, state: str, redirect_uri: str, fetch=None) -> dict:
        """Trade the code for the GitHub user: their login and verified
        emails. `fetch` is the HTTP function, replaceable in tests."""
        if state not in self._states:
            raise Invalid("Sign-in state is unknown or expired; start again")
        del self._states[state]
        fetch = fetch or _http_json
        token = fetch("POST", "https://github.com/login/oauth/access_token",
                      {"client_id": self.github_client_id, "client_secret": self.github_client_secret,
                       "code": code, "redirect_uri": redirect_uri}, {"Accept": "application/json"})
        access = token.get("access_token") if isinstance(token, dict) else None
        if not access:
            raise Invalid("GitHub did not issue a token for this sign-in")
        headers = {"Authorization": f"Bearer {access}", "Accept": "application/vnd.github+json"}
        user = fetch("GET", "https://api.github.com/user", None, headers) or {}
        emails = fetch("GET", "https://api.github.com/user/emails", None, headers) or []
        verified = [e["email"] for e in emails if isinstance(e, dict) and e.get("verified") and e.get("email")]
        primary = next((e["email"] for e in emails if isinstance(e, dict) and e.get("primary") and e.get("verified")), "")
        orgs = []
        if self.github_org:
            orgs = [o.get("login", "").lower() for o in (fetch("GET", "https://api.github.com/user/orgs", None, headers) or [])
                    if isinstance(o, dict)]
        return {"id": str(user.get("id") or ""), "login": (user.get("login") or "").lower(),
                "name": user.get("name") or user.get("login") or "",
                "emails": verified, "primary_email": primary or (verified[0] if verified else ""), "orgs": orgs}

    def sign_in_github_user(self, gh: dict) -> dict:
        """The person a GitHub user is, bound exactly: by GitHub's
        immutable user id once linked; before that by the exact login an
        admin recorded, or by a verified email as the one explicit
        initial link (a person with no login on record yet). Names,
        aliases and concatenations never select anyone. Unknown users are
        admitted only when sign-up is allowed (and, when an org is
        required, when they belong to it); the first person ever admitted
        is the admin. Organization membership proves membership, not
        identity."""
        graph = self.store.graph
        if self.github_org and self.github_org not in gh.get("orgs", []):
            raise Invalid(f"Sign in with a member of the {self.github_org} GitHub organization")
        gid = str(gh.get("id") or "").strip()
        login = (gh.get("login") or "").strip().lower()
        person = graph.person_by_github(github_id=gid) if gid else None
        if person is None and login:
            by_login = graph.person_by_github(login=login)
            # A login on record with another GitHub id bound is someone
            # else's; a login reused by a new account never inherits it.
            if by_login is not None and (not by_login.get("github_id") or by_login["github_id"] == gid):
                person = by_login
        if person is None:
            for email in gh.get("emails", []):
                by_email = graph.person_by_email(email)
                if by_email is not None and not by_email.get("github_login") and not by_email.get("github_id"):
                    person = by_email
                    break
        if person is None:
            if not self.allow_signup and graph.people():
                raise Invalid("No person in this Bridge is linked to your GitHub account; ask an admin to add you "
                              "with your GitHub login")
            role = "admin" if not graph.people() else "member"
            with graph.transaction():
                pid = graph.add_person(gh.get("name") or login, email=gh.get("primary_email", ""),
                                       github_login=login, role=role, source="github", merge=False, github_id=gid)
            person = graph.get_person(pid)
        if not person.get("active", 1):
            raise Invalid("This person is inactive; ask an admin")
        # The binding tightens on every sign-in: the immutable id is
        # recorded, and a renamed login follows the id.
        if (gid and person.get("github_id") != gid) or (login and (person.get("github_login") or "").lower() != login):
            with graph.transaction():
                graph.db.execute("UPDATE people SET github_id=CASE WHEN ?='' THEN github_id ELSE ? END, "
                                 "github_login=CASE WHEN ?='' THEN github_login ELSE ? END, updated_at=? WHERE id=?",
                                 (gid, gid, login, login, _now(), person["id"]))
                graph._bump("")
                graph.append_event("identity_linked", {"person_id": person["id"], "github_id": gid, "login": login})
            person = graph.get_person(person["id"])
        return person


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _http_json(method: str, url: str, body: dict | None, headers: dict) -> object:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={**headers, **({"Content-Type": "application/json"} if data else {}),
                                          "User-Agent": "bridge"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode()
    try:
        return json.loads(raw)
    except ValueError:
        return dict(urllib.parse.parse_qsl(raw))


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Raven · Sign in</title><link rel="stylesheet" href="/style.css"></head>
<body class="login-body"><main class="login"><h1>Raven</h1><p>{message}</p>{github}
<form method="post" action="/auth/password"><input type="hidden" name="csrf" value="{csrf}">
<label>Email<input name="email" type="email" autocomplete="username" required></label>
<label>Password<input name="password" type="password" autocomplete="current-password" required maxlength="256"></label>
<button class="button primary" type="submit">Sign in</button></form>{setup}
<details><summary>Developer / personal-token login</summary>
<form method="post" action="/auth/token" class="login-form"><label for="token">Or sign in with a personal token</label>
<input id="token" name="token" type="password" autocomplete="off" placeholder="brg_…" required>
<button class="button primary" type="submit">Sign in with token</button></form></details>
<p class="context">Use the account you created from your workspace invitation. Ask your administrator for an invitation if you do not have one.</p></main></body></html>"""
