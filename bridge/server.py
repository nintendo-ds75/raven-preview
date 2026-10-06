"""The web UI and REST API.

On loopback with auth off it is the single-operator workspace: Host and
Origin are checked, browser writes carry a CSRF token, and the local
operator records answers on behalf of owners. Bound to an address other
people reach it runs with auth on: every request carries an identity (a
session cookie from GitHub sign-in or a personal token, or a Bearer
token), roles gate what it may do, and every answer, signature,
follow-up and kickoff is attributed to the person who made it. Agents
elsewhere reach the same Raven through the standard /mcp HTTP endpoint with
an agent token.
"""

import json
import mimetypes
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .auth import LOGIN_PAGE, Auth, Identity
from .authz import Refused
from .briefing import mode as briefing_mode
from .store import Invalid, field

WEB = Path(__file__).resolve().parent.parent / "web"
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def loopback_aliases(netloc: str) -> set[str]:
    """Every spelling of one loopback address, or the address itself if
    it is not loopback. `127.0.0.1:7333` and `localhost:7333` are the
    same Raven on the same machine: accepting one and refusing the
    other protects nothing and only breaks the link people are given.
    A real hostname stands alone, so a name that resolves to loopback
    still cannot speak for this server."""
    parts = urlsplit(f"//{netloc}")
    try:
        port = parts.port
    except ValueError:
        return {netloc.lower()}
    host = (parts.hostname or "").lower()
    if host not in LOOPBACK:
        return {netloc.lower()}
    suffix = f":{port}" if port else ""
    return {f"127.0.0.1{suffix}", f"localhost{suffix}", f"[::1]{suffix}"}


class Forbidden(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def make_server(store, port=7331, executions=None, host="127.0.0.1", auth=None, public_url="",
                slack_signing_secret=None, wait_cap=50.0, github_webhook_secret=None, github_api=None,
                github_app=None, github_syncer=None, teams_adapter=None):
    """The HTTP server. `auth` is an Auth (from bridge.auth); None means
    the local operator mode, which is refused off loopback. Slack's
    Events API posts to /webhooks/slack, verified with the signing
    secret (SLACK_SIGNING_SECRET when not given); GitHub posts to
    /webhooks/github, verified with GITHUB_WEBHOOK_SECRET and served by
    a GitHubAPI on GITHUB_TOKEN when none is given. wait_cap bounds one
    bridge_wait request."""
    import os
    csrf = secrets.token_urlsafe(32)
    waits = _Waits()
    if auth is None:
        auth = Auth(store, enabled=False, public_url=public_url)
    from .accounts import Accounts
    accounts = Accounts(auth)
    if slack_signing_secret is None:
        slack_signing_secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if github_webhook_secret is None:
        github_webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if github_api is None and os.environ.get("GITHUB_TOKEN", "").strip():
        from .github import GitHubAPI
        github_api = GitHubAPI(os.environ["GITHUB_TOKEN"].strip())
    if host not in LOOPBACK and not auth.enabled:
        raise Invalid(f"Binding to {host} needs auth on: set BRIDGE_AUTH=on (with a BRIDGE_ADMIN_TOKEN or GitHub "
                      "sign-in configured) so every request carries an identity")
    public = urlsplit(auth.public_url) if auth.public_url else None
    public_host = public.netloc.lower() if public else ""
    # Published through Docker, the port people reach is not the port the
    # process bound, so the public URL is the only thing that knows it.
    public_hosts = loopback_aliases(public_host) if public_host else set()

    class Handler(BaseHTTPRequestHandler):
        me: Identity | None = None

        def send(self, code, value, content_type="application/json", extra_headers=None):
            body = json.dumps(value).encode() if content_type == "application/json" else value
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            # Only local Raven pages may reach the fixed, paired host launcher.
            launcher = " http://127.0.0.1:7334" if urlsplit("//" + self.headers.get("Host", "")).hostname in LOOPBACK else ""
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'" + launcher + "; frame-ancestors 'none'; base-uri 'none'; form-action 'self' https://github.com")
            for key, val in (extra_headers or {}).items():
                self.send_header(key, val)
            self.end_headers()
            self.wfile.write(body)

        def redirect(self, location, extra_headers=None):
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            for key, val in (extra_headers or {}).items():
                for item in (val if isinstance(val, list) else [val]):
                    self.send_header(key, item)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def after_login(self, session_cookie):
            if not accounts.ready():
                return self.redirect('/auth/profile' if accounts.workspace() else '/auth/setup', {'Set-Cookie': session_cookie})
            if any(part.strip() == "bridge_github_connect=1" for part in self.headers.get("Cookie", "").split(";")):
                clear = "bridge_github_connect=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"
                return self.redirect("/?github_connect=1#connect", {"Set-Cookie": [session_cookie, clear]})
            return self.redirect("/", {"Set-Cookie": session_cookie})

        def local_request(self):
            """Host and Origin match this server: loopback on its port, or
            the public URL it was told it is served at."""
            allowed = loopback_aliases(f"127.0.0.1:{self.server.server_port}") | public_hosts
            host = self.headers.get("Host", "").lower()
            if host not in allowed:
                self.send(403, {"error": "Only requests for this Raven's own host are accepted"})
                return False
            origin = self.headers.get("Origin")
            scheme = public.scheme if public and host in public_hosts else "http"
            if origin and origin.lower() != f"{scheme}://{host}":
                self.send(403, {"error": "Cross-origin requests are not accepted"})
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.send(403, {"error": "Cross-site requests are not accepted"})
                return False
            return True

        def base_url(self):
            if auth.public_url:
                return auth.public_url
            return f"http://{self.headers.get('Host', f'127.0.0.1:{self.server.server_port}')}"

        def identify(self):
            self.me = auth.identify(self.headers)
            return self.me

        def require(self, role):
            """The identity behind this request, or a refusal: 401 with
            no identity, 403 with one below the role."""
            if self.me is None:
                raise Forbidden(401, "Sign in: this Raven is shared, and every request carries an identity")
            if not self.me.allows(role):
                raise Forbidden(403, f"Your role ({self.me.role}) does not allow this; it needs {role}")
            return self.me

        def do_GET(self):
            if not self.local_request():
                return
            url = urlsplit(self.path)
            parts = url.path.strip("/").split("/")
            try:
                if url.path.startswith("/auth/"):
                    return self.auth_get(url)
                self.identify()
                if not accounts.ready() and url.path == '/':
                    return self.redirect('/auth/profile' if accounts.workspace() else '/auth/setup')
                if not accounts.ready() and (url.path.startswith('/api/') or url.path == '/mcp'):
                    return self.send(409, {'error': 'Complete workspace and admin profile setup first', 'setup_url': '/auth/setup'})
                if url.path == "/mcp":
                    return self.send(405, {"error": "Use POST for MCP requests"},
                                     extra_headers={"Allow": "POST"})
                if url.path in ("/brief", "/brief.js"):
                    # The task page opens without an account: the person's
                    # own link rides in the fragment and the page sends it.
                    file = WEB / ("brief.html" if url.path == "/brief" else "brief.js")
                    return self.send(200, file.read_bytes(), (mimetypes.guess_type(str(file))[0] or "text/plain")
                                     + "; charset=utf-8", extra_headers={"Referrer-Policy": "no-referrer"})
                if url.path == "/api/brief":
                    from . import briefing
                    link = self.brief_link()
                    # A decision on the task opened from the page's list.
                    focus = parse_qs(url.query).get("focus", [""])[0][:100]
                    view = briefing.overview(store, link, accounts.workspace(), self.has_account(link),
                                             {"enabled": auth.enabled, "github": auth.github_configured}, focus_id=focus)
                    # Signed in as this person too, "Open in Raven" goes
                    # straight to the task; signed out, sign-in lands on
                    # the inbox, and the page says "Sign in" instead.
                    view["viewer"]["signed_in"] = bool(auth.enabled and self.me is not None
                                                       and self.me.id == link.person["id"])
                    view["viewer"]["interview_decision_id"] = link.decision_id
                    return self.send(200, view)
                if url.path in ("/", "/app.js", "/style.css", "/favicon.svg", "/claude.svg", "/cursor.svg", "/openai.svg", "/onboarding.js", "/task.js", "/interview.js"):
                    if url.path == "/" and auth.enabled and self.me is None:
                        if parse_qs(url.query).get("github_connect") == ["1"]:
                            cookie = "bridge_github_connect=1; Max-Age=600; Path=/; HttpOnly; SameSite=Lax"
                            if self.base_url().startswith("https://"):
                                cookie += "; Secure"
                            return self.redirect("/auth/login", {"Set-Cookie": cookie})
                        return self.redirect("/auth/login" if accounts.workspace() else "/auth/setup")
                    files = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css", "/favicon.svg": "favicon.svg", "/claude.svg": "claude.svg", "/cursor.svg": "cursor.svg", "/openai.svg": "openai.svg"}
                    files['/onboarding.js'] = 'onboarding.js'
                    files['/task.js'] = 'task.js'
                    files['/interview.js'] = 'interview.js'
                    file = WEB / files[url.path]
                    return self.send(200, file.read_bytes(), (mimetypes.guess_type(str(file))[0] or "text/plain") + "; charset=utf-8")
                self.require("viewer")
                if url.path == "/api/state":
                    return self.send(200, {**store.state(), "execution_config": {
                        "enabled": executions is not None,
                        "repositories": [{"id": r["id"], "name": r["name"]} for r in executions.repositories]
                        if executions else []},
                        "csrf_token": csrf, "me": self.me_view(), "auth": self.auth_view(),
                        "workspace": {"name": accounts.workspace(), "needs_setup": not accounts.ready()},
                        "agent_connections": [{k: t[k] for k in ('id', 'label', 'last_used_at')}
                                              for t in (auth.tokens(self.me.id) if auth.enabled and self.me.id else [])
                                              if t['kind'] == 'agent' and not t['revoked_at']],
                        "delivery": self.delivery_view(), "mcp_config": self.mcp_config(),
                        "sync": self.sync_view(), "github_app": github_app.status() if github_app else None,
                        # The decision card says whether a rule authorizes on its
                        # own, and the inbox marks overdue questions; without these
                        # it always said automatic rules were off and used 72 hours.
                        "settings": {"auto_rules": store.graph.get_setting("auto_rules") == "1",
                                     "overdue_hours": store.overdue_hours(),
                                     # The task overview offers the task page
                                     # unless messages link to none.
                                     "brief_mode": briefing_mode(store.graph)}})
                if url.path == "/api/deliveries":
                    query = parse_qs(url.query)
                    return self.send(200, {"notifications": store.delivery.list(query.get("state", [""])[0]),
                                           "inbound_failed": store.delivery.inbound_failed(),
                                           "reply_failures": store.delivery.reply_failures(),
                                           "delivery": self.delivery_view()})
                if url.path == "/api/sync":
                    from .github import sync_states
                    return self.send(200, {"sync": sync_states(store.graph),
                                           "github": github_api is not None or bool(github_app and github_app.status()["repositories"])})
                if url.path == "/api/me":
                    return self.send(200, {"me": self.me_view(), "auth": self.auth_view()})
                if url.path == "/api/tokens":
                    who = self.me.id if not self.me.allows("admin") else parse_qs(url.query).get("person_id", [""])[0]
                    return self.send(200, {"tokens": auth.tokens(who)})
                if url.path == "/api/search":
                    query = parse_qs(url.query)
                    return self.send(200, store.search(query.get("q", [""])[0], repo=query.get("repo", [""])[0]))
                if url.path == "/api/ownership":
                    query = parse_qs(url.query)
                    try:
                        limit = int(query.get("limit", ["200"])[0])
                    except ValueError:
                        raise Invalid("limit must be an integer")
                    return self.send(200, {"ownership": store.ownership(query.get("repo", [""])[0], limit=limit)})
                if url.path == "/api/pending":
                    from .mcp import _list_pending
                    query = parse_qs(url.query)
                    return self.send(200, _list_pending(store, query.get("run_id", [None])[0], query.get("path", [None])[0]))
                if url.path == "/api/people":
                    query = parse_qs(url.query)
                    return self.send(200, {"people": store.people(), "teams": store.graph.teams(),
                                           "authority": store.authority(query.get("repo", [""])[0]),
                                           "settings": store.settings()})
                if url.path == "/api/inbox":
                    query = parse_qs(url.query)
                    try:
                        page, size = int(query.get("page", ["1"])[0]), int(query.get("size", ["50"])[0])
                    except ValueError:
                        raise Invalid("page and size must be integers")
                    return self.send(200, store.inbox(page, size, query.get("owner_id", [""])[0],
                                                      overdue=query.get("overdue", ["0"])[0] in ("1", "true")))
                if len(parts) == 3 and parts[:2] == ["api", "decisions"]:
                    decision = store.get_decision(parts[2])
                    # What handing it on would teach, for the scope picker on the card.
                    decision["handon"] = store.handon_scopes(decision, self.actor())
                    from .authz import ACTIONS, basis_for
                    decision["allowed_actions"] = [action for action in ACTIONS
                        if basis_for(store.graph, self.actor(), decision, action)[0]]
                    # The records its question names, with their status, as
                    # the Slack message states them.
                    from .ladder import named_records, records_line
                    decision["records_named"] = records_line(named_records(
                        store.graph, decision.get("repo") or "", decision.get("question") or "",
                        decision.get("context") or ""))
                    return self.send(200, decision)
                if url.path in ("/api/decisions", "/api/runs"):
                    query = parse_qs(url.query)
                    try:
                        page, size = int(query.get("page", ["1"])[0]), int(query.get("size", ["50"])[0])
                    except ValueError:
                        raise Invalid("page and size must be integers")
                    def one(key):
                        return query.get(key, [""])[0][:200]
                    if url.path == "/api/runs":
                        return self.send(200, store.list_runs(page, size, one("status")))
                    return self.send(200, store.list_decisions(page, size, one("status"), one("owner_id"), one("run_id"), one("q")))
                if len(parts) in (4, 5) and parts[:2] == ["api", "tasks"] and parts[3] == "interviews":
                    from . import interview
                    self.require("member")
                    result = (interview.list_for_task(store, parts[2], self.actor()) if len(parts) == 4
                              else interview.get(store, parts[2], parts[4], self.actor()))
                    return self.send(200, result)
                if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "tree":
                    from .canvas import get_tree
                    if self.me is not None and self.me.is_agent:
                        from .mcp import call_tool
                        # A full authenticated agent REST read acknowledges the
                        # same actual decision revisions as the MCP tree tool.
                        return self.send(200, call_tool(store, "bridge_get_tree", {"task_id": parts[2]}))
                    return self.send(200, get_tree(store, parts[2]))
                if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "trace":
                    from .canvas import trace
                    return self.send(200, trace(store, parts[2]))
                if url.path == "/api/export":
                    self.require("member")
                    return self.send(200, store.state(full_history=True))
                if len(parts) == 5 and parts[:2] == ["api", "executions"] and parts[3] == "artifacts":
                    if not executions:
                        return self.send(503, {"error": "Execution provider is disabled"})
                    run = executions.get(parts[2])
                    snapshot = json.loads(run["snapshot"])
                    if not any(a["id"] == parts[4] for a in snapshot.get("artifacts", [])):
                        return self.send(404, {"error": "Artifact not found in this run"})
                    try:
                        content = executions.api.artifact_content(run["session_id"], parts[4])
                    except Exception:
                        return self.send(502, {"error": "Unable to retrieve this resource; check provider status and retry"})
                    return self.send(200, content, "application/octet-stream")
                return self.send(404, {"error": "Not found"})
            except Forbidden as error:
                self.send(error.code, {"error": str(error)})
            except Refused as error:
                self.send(403, {"error": str(error)})
            except Invalid as error:
                self.send(404, {"error": str(error)})
            except Exception as error:
                print(f"Raven: {type(error).__name__}: {error}", file=sys.stderr)
                self.send(500, {"error": "Raven could not complete this request; see the server log"})

        # ---------------- sign in ----------------

        def auth_get(self, url):
            if url.path in ('/auth/setup', '/auth/profile', '/auth/join'):
                if not auth.enabled:
                    return self.send(409, {'error': 'Enable authentication to create the workspace and first admin profile'})
                if url.path in ('/auth/setup', '/auth/profile') and accounts.ready():
                    return self.redirect('/auth/login')
                if url.path == '/auth/setup' and accounts.workspace():
                    return self.redirect('/auth/profile')
                if url.path == '/auth/profile' and not accounts.workspace():
                    return self.redirect('/auth/setup')
                self.identify()
                return self.account_page(url.path)
            if url.path in ("/auth/github-app/created", "/auth/github-app/installed"):
                if not accounts.ready():
                    return self.send(409, {'error': 'Complete workspace setup before connecting repositories'})
                from .github import GitHubError
                from .github_app import manifest_state, verify_manifest_state
                self.identify()
                self.require("admin")
                query = parse_qs(url.query)
                try:
                    if github_app is None:
                        raise GitHubError("GitHub App setup is unavailable")
                    if url.path.endswith("/created"):
                        if not verify_manifest_state(auth.secret, query.get("state", [""])[0], self.me.id):
                            raise GitHubError("GitHub setup expired; start again from Raven")
                        app = github_app.convert_manifest(query.get("code", [""])[0])
                        state = manifest_state(auth.secret, self.me.id)
                        return self.redirect(f"https://github.com/apps/{app['slug']}/installations/new?state=" + _quote(state))
                    if not verify_manifest_state(auth.secret, query.get("state", [""])[0], self.me.id):
                        raise GitHubError("GitHub installation did not start from this Raven session")
                    installation_id = int(query.get("installation_id", ["0"])[0])
                    connected = github_app.connect_installation(installation_id)
                    if github_syncer:
                        github_syncer.wake()
                    return self.redirect("/?github_connected=" + _quote(", ".join(connected["repositories"])) + "#connect")
                except (GitHubError, ValueError) as error:
                    return self.redirect("/?github_error=" + _quote(str(error)) + "#connect")
            if url.path == "/auth/login":
                if not auth.enabled:
                    return self.redirect("/")
                message = parse_qs(url.query).get("message", ["Sign in to your team's Raven."])[0]
                github = ('<a class="button primary" href="/auth/github">Sign in with GitHub</a>'
                          if auth.github_configured else "")
                page = LOGIN_PAGE.format(message=_escape(message), github=github, csrf=csrf,
                    setup='<p><a href="/auth/setup">Create your workspace</a></p>' if not accounts.workspace() else '')
                return self.send(200, page.encode(), "text/html; charset=utf-8")
            if url.path == "/auth/github":
                return self.redirect(auth.github_authorize_url(self.base_url() + "/auth/github/callback"))
            if url.path == "/auth/github/callback":
                query = parse_qs(url.query)
                try:
                    gh = auth.github_exchange(query.get("code", [""])[0], query.get("state", [""])[0],
                                              self.base_url() + "/auth/github/callback")
                    person = auth.sign_in_github_user(gh)
                except Invalid as error:
                    return self.redirect("/auth/login?message=" + _quote(str(error)))
                store.graph.append_event("signed_in", {"person_id": person["id"], "via": "github"})
                return self.after_login(auth.session_cookie(person["id"]))
            if url.path == "/auth/logout":
                return self.redirect("/auth/login", {"Set-Cookie": auth.clear_cookie()})
            return self.send(404, {"error": "Not found"})

        def account_page(self, path, error='', invite_token=''):
            setup = path == '/auth/setup'
            profile = path == '/auth/profile'
            title = 'Create your workspace' if setup else 'Create your profile' if profile else 'Join your team'
            fields = ('<label>Workspace name<input name="workspace" maxlength="100" required></label>' if setup else
                      '' if profile else '<input name="invite" id="invite-code" type="hidden" value="' + _escape(invite_token) + '">')
            if not setup:
                fields += '<label>Your name<input name="name" autocomplete="name" maxlength="100" required></label>'
            if profile:
                fields += '<label>Email<input name="email" type="email" autocomplete="username" required></label>'
            if (setup or profile) and not self.setup_identity():
                fields += '<label>Setup credential<input name="setup_token" type="password" autocomplete="off" required></label><p>Use your existing admin credential. For local Docker setup, run <code>./dev login</code> once. This prevents someone else from claiming your workspace.</p>'
            if not setup:
                fields += '<label>Your account password<input name="password" type="password" autocomplete="new-password" minlength="12" maxlength="256" required></label>'
            description = 'Step 1 of 2 · Name your workspace. No user profile is created yet.' if setup else 'Step 2 of 2 · Create your personal login. You become the workspace admin automatically.' if profile else 'Create your profile to join this workspace.'
            button = 'Create workspace & continue' if setup else 'Create admin profile' if profile else 'Accept invitation'
            page = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Raven · {title}</title><link rel="stylesheet" href="/style.css"><script src="/onboarding.js" defer></script></head><body class="login-body"><main class="login"><h1>{title}</h1><p>{_escape(error) if error else description}</p><form method="post" action="{path}"><input type="hidden" name="csrf" value="{csrf}">{fields}<button class="button primary" type="submit">{button}</button></form><p><a href="/auth/login">Already have an account? Sign in</a></p></main></body></html>'''
            return self.send(400 if error else 200, page.encode(), 'text/html; charset=utf-8')

        def setup_identity(self):
            if self.me and self.me.allows('admin'):
                return self.me
            from http.cookies import SimpleCookie
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            token = cookie.get('bridge_setup')
            return accounts.profile_identity(token.value if token else '')

        def auth_post(self, path, data):
            if path in ('/auth/setup', '/auth/profile', '/auth/join', '/auth/password'):
                if not auth.enabled or not secrets.compare_digest(str(data.get('csrf', '')), csrf):
                    return self.send(403, {'error': 'Reload the page before submitting'})
                try:
                    if path in ('/auth/setup', '/auth/profile'):
                        identity = self.setup_identity()
                        if not identity or not identity.allows('admin'):
                            identity = auth.identify({'Authorization': 'Bearer ' + str(data.get('setup_token', ''))})
                        if path == '/auth/setup':
                            continuation = accounts.create_workspace(data, identity)
                            cookie = 'bridge_setup=' + continuation + '; Path=/auth; HttpOnly; SameSite=Strict; Max-Age=3600'
                            if self.base_url().startswith('https://'):
                                cookie += '; Secure'
                            return self.redirect('/auth/profile', {'Set-Cookie': cookie})
                        pid = accounts.setup(data, identity)
                    elif path == '/auth/join':
                        pid = accounts.accept(data)
                    else:
                        pid = accounts.login(data.get('email'), data.get('password'))
                except Invalid as error:
                    if path == '/auth/password':
                        return self.redirect('/auth/login?message=' + _quote(str(error)))
                    return self.account_page(path, str(error), str(data.get('invite', '')))
                return self.redirect('/#connect' if path == '/auth/profile' else '/#inbox', {'Set-Cookie': ['bridge_setup=; Path=/auth; HttpOnly; SameSite=Strict; Max-Age=0', auth.session_cookie(pid)]})
            if path == "/auth/token":
                token = str(data.get("token") or "").strip()
                me = auth.identify({"Authorization": f"Bearer {token}"})
                if me is None or not me.id:
                    return self.redirect("/auth/login?message=" + _quote("That token is not valid"))
                if me.is_agent:
                    return self.redirect("/auth/login?message=" + _quote(
                        "That is an agent credential: it speaks the agent protocol and never signs in a person. "
                        "Sign in with GitHub, or mint a token of kind human for yourself"))
                store.graph.append_event("signed_in", {"person_id": me.id, "via": "token"})
                return self.after_login(auth.session_cookie(me.id))
            if path == "/auth/logout":
                return self.redirect("/auth/login", {"Set-Cookie": auth.clear_cookie()})
            return self.send(404, {"error": "Not found"})

        def brief_link(self):
            """The task link this request carries, or a refusal. It is a
            header the page sets, never a cookie: nothing ambient, so a
            link request needs no CSRF token."""
            from . import briefing
            link = briefing.resolve(store.graph, self.headers.get("X-Raven-Link", ""))
            if link is None:
                # One answer whatever the cause, so it tells nobody which.
                raise Forbidden(401, "This link isn't valid: it may be incomplete, expired or revoked. Ask for a "
                                     "new one, or sign in")
            return link

        def has_account(self, link):
            return accounts.has_password(link.person["id"]) or bool(link.person.get("github_id"))

        def brief_post(self, path, data):
            from . import briefing
            if path == "/api/brief/resend":
                # The one link request that works with a link that no
                # longer does. Whatever happened, the answer is the same.
                briefing.resend(store, self.headers.get("X-Raven-Link", ""))
                return self.send(200, {"notice": "If this link was yours, a new one is on its way to your Slack "
                                                 "direct messages."})
            link = self.brief_link()
            if path == "/api/brief/interview":
                from . import interview
                actor = interview.actor_for_link(link)
                # The link, never request fields, supplies identity and task.
                if data.get("task_id") not in (None, "", link.run_id):
                    raise Forbidden(403, "This interview is outside the task link's scope")
                action = field(data, "action", limit=20)
                if action == "list":
                    result = interview.list_for_task(store, link.run_id, actor)
                elif action == "create":
                    result = interview.create(store, link.run_id, data, actor)
                else:
                    iid = field(data, "interview_id", limit=100)
                    if action == "get":
                        result = interview.get(store, link.run_id, iid, actor)
                    elif action == "confirm":
                        result = interview.confirm(store, link.run_id, iid, data, actor)
                    elif action == "advance":
                        result = interview.advance(store, link.run_id, iid, data, actor)
                    elif action in ("draft", "cancel", "failed"):
                        result = interview.update(store, link.run_id, iid, data, actor, action)
                    else:
                        raise Invalid("Unknown interview action")
                return self.send(200, result)
            if path == "/api/brief/answer":
                return self.send(200, briefing.act(store, link, data))
            if path == "/api/brief/refer":
                return self.send(200, briefing.refer(store, link, data))
            if path == "/api/brief/note":
                return self.send(200, briefing.add_note(store, link, data))
            if path == "/api/brief/withdraw":
                return self.send(200, briefing.withdraw_note(store, link, data.get("note_id")))
            if path == "/api/brief/account":
                if not auth.enabled:
                    raise Invalid("This workspace runs without sign-in; there is no account to create")
                pid = accounts.claim(link.person, data)
                cookie = auth.session_cookie(pid)
                return self.send(200, {"redirect": f"/#runs/{link.run_id}"}, extra_headers={"Set-Cookie": cookie})
            return self.send(404, {"error": "Not found"})

        def me_view(self):
            if self.me is None:
                return None
            return {"id": self.me.id, "name": self.me.name, "role": self.me.role, "kind": self.me.kind,
                    "email": self.me.email}

        def auth_view(self):
            return {"enabled": auth.enabled, "github": auth.github_configured, "public_url": auth.public_url}

        def delivery_view(self):
            delivery = store.delivery
            counts = {"queued": 0, "failed": 0, "sent": 0}
            for r in store.graph.db.execute("SELECT state, count(*) c FROM notifications GROUP BY state"):
                if r["state"] in counts:
                    counts[r["state"]] = r["c"]
            return {"enabled": delivery.enabled, "channel": delivery.channel if delivery.enabled else "",
                    "fallback_channel": delivery.fallback_channel or store.graph.get_setting("slack_fallback_channel"),
                    "signing_secret": bool(slack_signing_secret),
                    "directory": json.loads(store.graph.get_setting("slack_directory") or "{}"),
                    "teams_replies": teams_adapter is not None, **counts}

        def mcp_config(self):
            """URL-based agent configuration for the shared HTTP MCP endpoint."""
            url = (auth.public_url or self.base_url()).rstrip("/") + "/mcp"
            config = {"type": "http", "url": url}
            if auth.enabled:
                config["headers"] = {"Authorization": "Bearer <your agent token>"}
            return {"mcpServers": {"bridge": config}, "remote": auth.enabled,
                    "notice": "Use a limited agent token from this page." if auth.enabled else ""}

        def attribute(self, data, *keys):
            """With auth on, the fields that name who acted are the person
            who is signed in, whatever the payload says."""
            if auth.enabled and self.me is not None and self.me.id:
                for key in keys:
                    data[key] = self.me.name
            return data

        def actor(self, data=None):
            """Who acts on a decision through this request, for the
            permission check every decision write runs. An admin's
            override is explicit (override: true) and recorded."""
            from .authz import Actor
            override = bool((data or {}).get("override")) if isinstance(data, dict) else False
            return Actor.from_identity(self.me, override=override)

        def agent_may(self, path):
            """The routes an agent credential may write: the agent
            protocol over HTTP, and nothing a person decides with."""
            parts = path.strip("/").split("/")
            return (path in ("/api/tasks/start", "/api/runs", "/api/decisions")
                    or (len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] in {"nodes", "settle", "finish"})
                    or (len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "status"))

        def do_POST(self):
            if not self.local_request():
                return
            path = urlsplit(self.path).path
            content_type = self.headers.get("Content-Type", "").split(";")[0]
            try:
                length = int(self.headers.get("Content-Length", "0"))
                limit = (1 << 20) if path.startswith("/webhooks/") else 65536
                if not 0 < length <= limit:
                    return self.send(413, {"error": f"Request must be between 1 and {limit} bytes"})
                raw = self.rfile.read(length)
                if not path.startswith('/auth/') and not accounts.ready():
                    return self.send(409, {'error': 'Complete workspace and admin profile setup first'})
                if path == "/webhooks/github":
                    # GitHub proves itself with its signature; no session, no CSRF.
                    from .github import GitHubError, handle_webhook, verify_signature
                    if not verify_signature(github_webhook_secret, raw, self.headers.get("X-Hub-Signature-256", "")):
                        return self.send(401, {"error": "GitHub signature missing or invalid"})
                    event = json.loads(raw)
                    if not isinstance(event, dict):
                        raise Invalid("Expected a JSON object")
                    try:
                        return self.send(200, handle_webhook(store.graph, github_api, self.headers.get("X-GitHub-Event", ""),
                                                             self.headers.get("X-GitHub-Delivery", ""), event))
                    except GitHubError as error:
                        return self.send(502, {"error": str(error)})
                if path == "/webhooks/teams":
                    # The Bot Connector JWT proves the service; the adapter
                    # additionally pins tenant/channel and maps Entra identity.
                    if teams_adapter is None:
                        return self.send(404, {"error": "Teams bot replies are not configured"})
                    if content_type != "application/json":
                        return self.send(415, {"error": "Expected application/json"})
                    from .teams import TeamsAuthError, TeamsUnavailable
                    try:
                        return self.send(200, teams_adapter.handle(self.headers.get("Authorization", ""), json.loads(raw)))
                    except TeamsAuthError as error:
                        return self.send(403, {"error": str(error)})
                    except TeamsUnavailable as error:
                        return self.send(503, {"error": str(error)})
                if path == "/webhooks/slack":
                    # Slack proves itself with its signature; no session, no CSRF.
                    from .delivery import verify_slack_signature
                    if not verify_slack_signature(slack_signing_secret, self.headers.get("X-Slack-Request-Timestamp", ""),
                                                  raw, self.headers.get("X-Slack-Signature", "")):
                        return self.send(401, {"error": "Slack signature missing or invalid"})
                    event = json.loads(raw)
                    if not isinstance(event, dict):
                        raise Invalid("Expected a JSON object")
                    return self.send(200, store.delivery.inbox.enqueue(event))
                if path.startswith("/auth/"):
                    if content_type == "application/x-www-form-urlencoded":
                        data = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
                    else:
                        data = json.loads(raw)
                    self.identify()
                    return self.auth_post(path, data)
                if path.startswith("/api/brief/"):
                    if content_type != "application/json":
                        return self.send(415, {"error": "Expected application/json"})
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        raise Invalid("Expected a JSON object")
                    return self.brief_post(path, data)
                bearer = self.headers.get("Authorization", "").lower().startswith("bearer ")
                # A browser session proves itself with the CSRF token; a
                # token is not ambient, so it needs none.
                if path != "/mcp" and not bearer and not secrets.compare_digest(self.headers.get("X-Bridge-CSRF", ""), csrf):
                    return self.send(403, {"error": "Refresh the inbox to renew your session token"})
                if content_type != "application/json":
                    return self.send(415, {"error": "Expected application/json"})
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise Invalid("Expected a JSON object")
                self.identify()
                if path == "/mcp":
                    if auth.enabled and (not bearer or self.me is None or not self.me.is_agent):
                        return self.send(401, {"error": "MCP requires an agent bearer token"},
                                         extra_headers={"WWW-Authenticate": "Bearer"})
                    return self.mcp_http(data)
                if self.me is not None and self.me.is_agent:
                    if not self.me.can_write_as_agent:
                        raise Forbidden(403, "A viewer's agent credential is read-only")
                    if not self.agent_may(path):
                        raise Forbidden(403, "An agent credential writes nodes and reads the tree; a person answers, "
                                             "signs, hands on and administers. Sign in as yourself for this")
                else:
                    self.require("member")
                if path == "/api/owners":
                    self.require("admin")
                    result = store.add_owner(data)
                elif path == "/api/people":
                    self.require("admin")
                    result = store.add_person(data)
                elif path == "/api/teams":
                    self.require("admin")
                    result = store.add_team(data)
                elif path == "/api/authority":
                    self.require("admin")
                    result = store.add_authority(self.attribute(data, "by"))
                elif path == "/api/settings":
                    self.require("admin")
                    result = store.update_settings(data)
                elif path == "/api/slack/sync":
                    self.require("admin")
                    result = store.delivery.sync_directory()
                elif path == "/api/records":
                    result = store.add_record(data)
                elif path == "/api/sync":
                    self.require("admin")
                    from .github import GitHubError, register, sync_repo
                    repo = field(data, "repo", limit=200)
                    api = github_app.api_for_repo(repo) if github_app else None
                    api = api or github_api
                    if api is None:
                        register(store.graph, repo)
                        result = {"registered": repo, "synced": False,
                                  "error": "Connect the repository through GitHub to enable sync"}
                    else:
                        try:
                            result = sync_repo(store.graph, api, repo)
                        except GitHubError as error:
                            raise Invalid(str(error))
                elif path == "/api/github/connect":
                    self.require("admin")
                    if github_app is None:
                        raise Invalid("GitHub App connection is unavailable")
                    from .github_app import manifest_state
                    from urllib.parse import quote
                    credentials = github_app.credentials()
                    if hasattr(github_app, "start_device"):
                        result = {"mode": "device", **github_app.status()}
                    elif credentials:
                        state = manifest_state(auth.secret, self.me.id)
                        result = {"url": f"https://github.com/apps/{credentials['slug']}/installations/new?state=" + quote(state)}
                    else:
                        raise Invalid("GitHub connection is not enabled yet. The Raven operator must configure the registered app's public client ID and slug.")
                elif path in ("/api/github/device/start", "/api/github/device/poll", "/api/github/repositories/refresh"):
                    self.require("admin")
                    from .github import GitHubError
                    if not github_app or not hasattr(github_app, "start_device"):
                        raise Invalid("GitHub account authorization is not configured")
                    try:
                        if path.endswith("/start"):
                            result = github_app.start_device(self.me.id)
                        elif path.endswith("/poll"):
                            result = github_app.poll_device(self.me.id, field(data, "flow_id", limit=200))
                        else:
                            result = github_app.refresh_repositories()
                        if result.get("repositories") and github_syncer:
                            github_syncer.wake()
                    except GitHubError as error:
                        raise Invalid(str(error))
                elif path == "/api/invitations":
                    self.require('admin')
                    result = accounts.invite(data.get('email'), data.get('role', 'member'), self.me)
                    result['url'] = self.base_url() + '/auth/join#invite=' + result.pop('token')
                elif path == "/api/tokens":
                    person_id = str(data.get("person_id") or self.me.id)
                    if person_id != self.me.id:
                        self.require("admin")
                    if not person_id:
                        raise Invalid("A bootstrap admin mints tokens for a person: pass person_id")
                    result = auth.create_token(person_id, str(data.get("label") or ""), created_by=self.me.name,
                                               kind=str(data.get("kind") or "agent"))
                elif path == "/api/tasks":
                    if not executions:
                        return self.send(503, {"error": "Start Raven with --agents to launch tasks"})
                    if auth.enabled and self.me.id:
                        data["requester"] = self.me.name
                    result = executions.submit(data)
                elif path == "/api/tasks/start":
                    from .canvas import start_task
                    from .config import load
                    if auth.enabled and self.me.id and not data.get("requester"):
                        data["requester"] = self.me.name
                    result = start_task(store, load(), data)
                elif path == "/api/runs":
                    result = store.add_run(data)
                elif path == "/api/decisions":
                    from .config import load
                    from .ladder import ask
                    result = ask(store, load(), field(data, "run_id", limit=100), field(data, "question", limit=2000),
                                 context=field(data, "context"), path=field(data, "path", "unknown", 1000),
                                 category=field(data, "category", "", 40) if data.get("category") else "",
                                 owner_id=data.get("owner_id") or None)
                elif path == "/api/ingest":
                    self.require("admin")
                    from .ingest import index_repo
                    try:
                        result = index_repo(store.graph, field(data, "path", limit=1000),
                                            max_commits=int(data["max_commits"]) if data.get("max_commits") not in (None, "") else None,
                                            repo_name=str(data.get("repo") or ""))
                    except RuntimeError as error:
                        raise Invalid(str(error))
                else:
                    parts = path.strip("/").split("/")
                    if len(parts) in (4, 6) and parts[:2] == ["api", "tasks"] and parts[3] == "interviews":
                        from . import interview
                        if len(parts) == 4:
                            result = interview.create(store, parts[2], data, self.actor())
                        elif parts[5] == "confirm":
                            result = interview.confirm(store, parts[2], parts[4], data, self.actor())
                        elif parts[5] == "advance":
                            result = interview.advance(store, parts[2], parts[4], data, self.actor())
                        elif parts[5] in ("draft", "cancel", "failed"):
                            result = interview.update(store, parts[2], parts[4], data, self.actor(), parts[5])
                        else:
                            return self.send(404, {"error": "Unknown interview action"})
                    elif len(parts) == 4 and parts[:2] == ["api", "decisions"] and parts[3] == 'reframe':
                        from . import reframe
                        result = reframe.apply(store, parts[2], data, self.actor())
                    elif len(parts) == 4 and parts[:2] == ["api", "decisions"] and parts[3] in {"answer", "assign"}:
                        if parts[3] == "answer":
                            if not data.get("expected_updated_at"):
                                # A human answer over HTTP names the revision it
                                # reviewed; a stale form is refused, never applied blind.
                                raise Invalid("expected_updated_at is required: the decision's updated_at as you reviewed it")
                            self.attribute(data, "signed_by")
                            if auth.enabled and self.me.id:
                                data["source"] = f"inbox: {self.me.name}"
                        result = getattr(store, parts[3])(parts[2], data, actor=self.actor(data))
                    elif len(parts) == 4 and parts[:2] == ["api", "decisions"] and parts[3] in {"followups", "signoff"}:
                        from . import canvas
                        from .config import load
                        self.attribute(data, "by")
                        result = (canvas.add_followups(store, load(), parts[2], data, actor=self.actor(data))
                                  if parts[3] == "followups"
                                  else canvas.sign_off(store, parts[2], data, actor=self.actor(data)))
                    elif len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "link":
                        # A signed-in person opens the task page for
                        # themselves: the same view the people it
                        # messaged get.
                        from . import briefing
                        from .canvas import _task
                        _task(store, parts[2])
                        person_id = self.me.id
                        if self.me.kind == "operator":
                            # The local operator previews the page as a
                            # person on the map, the way it answers for them.
                            person = store.graph.find_person(str(data.get("person") or ""))
                            person_id = person["id"] if person else ""
                        if not person_id:
                            raise Invalid("Sign in as a person to open the task page" if auth.enabled
                                          else "Name the person to preview the page as")
                        decision_id = str(data.get("decision_id") or "")[:100]
                        if decision_id and store.get_decision(decision_id)["run_id"] != parts[2]:
                            raise Invalid("That decision is not on this task")
                        with store.graph.transaction():
                            token = briefing.mint_own(store.graph, person_id, parts[2], decision_id)
                        result = {"url": briefing.url_for(self.base_url(), token)}
                    elif len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "notes":
                        from .canvas import add_note
                        self.attribute(data, "by")
                        result = add_note(store, parts[2], data, actor=self.actor(data))
                    elif len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] in {"nodes", "settle", "finish"}:
                        from . import canvas
                        from .config import load
                        payload = {**data, "task_id": parts[2]}
                        if parts[3] == "finish" and self.me is not None and self.me.is_agent:
                            canvas.require_agent_read(store, payload)
                        result = (canvas.add_node(store, load(), payload) if parts[3] == "nodes"
                                  else canvas.settle_node(store, payload) if parts[3] == "settle"
                                  else canvas.finish_task(store, payload))
                    elif len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "status":
                        if data.get("status") == "completed" and self.me is not None and self.me.is_agent:
                            from .canvas import finish_task, require_agent_read
                            payload = {**data, "task_id": parts[2]}
                            require_agent_read(store, payload)
                            # This marks the same task complete, not separate
                            # telemetry. Preserve all canonical finish gates.
                            result = finish_task(store, payload)
                        else:
                            result = store.update_run(parts[2], data)
                    elif len(parts) == 4 and parts[:2] == ["api", "authority"] and parts[3] == "end":
                        self.require("admin")
                        result = store.end_authority(parts[2])
                    elif len(parts) == 4 and parts[:2] == ["api", "decisions"] and parts[3] == "refer":
                        self.attribute(data, "by")
                        result = store.refer(parts[2], data, actor=self.actor(data))
                    elif len(parts) == 4 and parts[:2] == ["api", "decisions"] and parts[3] == "rule":
                        self.attribute(data, "by")
                        result = store.make_rule(parts[2], data, actor=self.actor(data))
                    elif len(parts) == 4 and parts[:2] == ["api", "deliveries"] and parts[3] == "retry":
                        result = store.delivery.retry(parts[2])
                    elif len(parts) == 5 and parts[:3] == ["api", "deliveries", "inbound"] and parts[4] == "retry":
                        self.require("admin")
                        result = store.delivery.retry_inbound(parts[3])
                    elif len(parts) == 4 and parts[:2] == ["api", "tokens"] and parts[3] == "revoke":
                        owned = any(t["id"] == parts[2] for t in auth.tokens(self.me.id))
                        if not owned:
                            self.require("admin")
                        result = auth.revoke_token(parts[2], by=self.me.name)
                    else:
                        return self.send(404, {"error": "Not found"})
                self.send(200, result)
            except Forbidden as error:
                self.send(error.code, {"error": str(error)})
            except Refused as error:
                self.send(403, {"error": str(error)})
            except (Invalid, ValueError, UnicodeDecodeError) as error:
                self.send(400, {"error": str(error)})
            except Exception as error:
                print(f"Raven: {type(error).__name__}: {error}", file=sys.stderr)
                self.send(500, {"error": "Raven could not complete this request; see the server log"})

        def sync_view(self):
            from .github import sync_states
            return {"github": github_api is not None or bool(github_app and github_app.status()["repositories"]),
                    "repos": sync_states(store.graph)}

        def call_mcp_tool(self, data, sleep=None, cap=None):
            """Run one MCP tool with the caller's identity and agent label.
            `sleep` and `cap` are a streamed wait's: its progress sleep and
            the canvas cap instead of this server's request bound."""
            from .mcp import READ_ONLY_TOOLS, call_tool
            name = str(data.get("name") or "")
            args = data.get("arguments") or {}
            if not isinstance(args, dict):
                raise Invalid("arguments must be an object")
            if self.me.is_agent and not self.me.can_write_as_agent and name not in READ_ONLY_TOOLS:
                raise Forbidden(403, "A viewer's agent credential is read-only")
            if name == "bridge_ingest_repo":
                self.require("admin")
            if auth.enabled and self.me.id:
                if name == "bridge_start_task":
                    args.setdefault("requester", self.me.name)
                    if self.me.token_label and not args.get("agent"):
                        args["agent"] = self.me.token_label
                if name == "bridge_add_node":
                    args.setdefault("requester", self.me.name)
            try:
                with waits.open():
                    return {"result": call_tool(store, name, args, wait_cap=wait_cap if cap is None else cap,
                                                sleep=sleep, stop=waits.stopping),
                            "isError": False}
            except Invalid as error:
                return {"result": str(error), "isError": True}

        def mcp_http(self, message):
            """Stateless Streamable HTTP: each POST answers one JSON-RPC request."""
            from .mcp import dispatch, params_error, SUPPORTED_HTTP_VERSIONS
            version = self.headers.get('MCP-Protocol-Version')
            if version is not None and version not in SUPPORTED_HTTP_VERSIONS:
                return self.send(400, {'jsonrpc': '2.0', 'id': message.get('id'),
                    'error': {'code': -32600, 'message': 'Unsupported MCP-Protocol-Version'}})
            if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
                return self.send(400, {"jsonrpc": "2.0", "id": message.get("id"),
                                       "error": {"code": -32600, "message": "Invalid request"}})
            if "id" not in message:
                return self.send(202, b"", "text/plain")
            params = message.get("params", {})
            error = params_error(message["method"], params)
            if error:
                return self.send(200, {"jsonrpc": "2.0", "id": message["id"],
                                       "error": {"code": -32602, "message": error}})
            if message["method"] == "tools/call":
                from .mcp import _progress_token
                if (isinstance(params, dict) and params.get("name") in ("bridge_wait", "bridge_finish_task")
                        and _progress_token(params) is not None
                        and "text/event-stream" in self.headers.get("Accept", "")):
                    return self.stream_wait(message["id"], params)
                if not isinstance(params, dict):
                    result = {"jsonrpc": "2.0", "id": message["id"],
                              "error": {"code": -32602, "message": "Invalid params"}}
                else:
                    call = self.call_mcp_tool(params)
                    result = {"jsonrpc": "2.0", "id": message["id"], "result": {
                        "content": [{"type": "text", "text": json.dumps(call["result"]) if not call["isError"]
                                     else str(call["result"])}], "isError": call["isError"]}}
            else:
                result = dispatch(store, message)
            return self.send(200, result)

        def stream_wait(self, rpc_id, params):
            """A wait kept alive: the answer is an event stream carrying a
            progress notification every 15 seconds and then the result, so
            the wait runs to the agent's timeout instead of this server's
            request bound, and a live call never looks idle to the client.
            Measured live on a397f1c: bound to 50 seconds, one task took 28
            wait calls, and the host ran out of budget before finishing."""
            from .canvas import WAIT_CAP
            from .mcp import PROGRESS_MESSAGES, progress_sleep
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            def emit(payload):
                self.wfile.write(b"event: message\ndata: " + json.dumps(payload).encode() + b"\n\n")
                self.wfile.flush()
            try:
                message = PROGRESS_MESSAGES.get(params.get("name"), PROGRESS_MESSAGES[None])
                call = self.call_mcp_tool(params, sleep=progress_sleep(params["_meta"]["progressToken"], emit,
                                                                       message=message), cap=__import__("bridge.canvas", fromlist=["call_budget"]).call_budget())
                emit({"jsonrpc": "2.0", "id": rpc_id, "result": {
                    "content": [{"type": "text", "text": json.dumps(call["result"]) if not call["isError"]
                                 else str(call["result"])}], "isError": call["isError"]}})
            except (BrokenPipeError, ConnectionResetError):
                return  # the client went away; nothing is lost by not waiting
            except Exception as error:
                print(f"Raven: bridge_wait: {type(error).__name__}: {error}", file=sys.stderr)
                try:
                    emit({"jsonrpc": "2.0", "id": rpc_id, "error": {
                        "code": -32603, "message": "Raven could not complete this request; see the server log"}})
                except (BrokenPipeError, ConnectionResetError):
                    pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.stop_waits = waits.stop
    original_shutdown = server.shutdown
    def shutdown():
        store.delivery.close()
        original_shutdown()
        from .canvas import wait_for_background
        wait_for_background(timeout=25.0)
    server.shutdown = shutdown
    return server


class _Waits:
    """The waits open on this server. Stopping ends each one with a result
    that says the server stopped, so a host hears it and calls again,
    instead of a stream that goes quiet until the host's idle limit."""

    def __init__(self):
        import threading
        self.stopping = threading.Event()
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._open = 0

    def open(self):
        from contextlib import contextmanager

        @contextmanager
        def held():
            with self._lock:
                self._open += 1
            try:
                yield
            finally:
                with self._lock:
                    self._open -= 1
                    self._idle.notify_all()
        return held()

    def stop(self, grace: float = 5.0) -> int:
        """Tell every open wait to answer now, and give them up to `grace`
        seconds to send it. Returns how many were still open after that."""
        import time as _time
        self.stopping.set()
        deadline = _time.monotonic() + grace
        with self._lock:
            while self._open and _time.monotonic() < deadline:
                self._idle.wait(max(0.0, deadline - _time.monotonic()))
            return self._open


def _escape(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _quote(text):
    from urllib.parse import quote
    return quote(str(text))
