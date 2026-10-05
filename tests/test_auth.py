"""A shared Raven: identities, roles, sign-in and the remote MCP mode.

With auth off on loopback nothing changes. With auth on, every request
carries an identity (a Bearer token, the bootstrap admin token, or a
session cookie), roles gate writes, every answer, signature, follow-up
and kickoff is attributed to the person who is signed in, and an agent
elsewhere reaches the same Raven through the standard HTTP MCP endpoint
with a personal token."""

import json
import os
import threading
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fixtures import OfflineCase

from bridge.auth import Auth
from bridge.server import make_server
from bridge.store import Invalid, Store

BOOTSTRAP = "bootstrap-secret-for-tests"


class SharedServer(OfflineCase):
    """A server with auth on and a bootstrap admin token."""

    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "shared.db")
        env = patch.dict(os.environ, {"BRIDGE_ADMIN_TOKEN": BOOTSTRAP, "BRIDGE_SECRET": "unit-test-secret"})
        env.start()
        self.addCleanup(env.stop)
        self.auth = Auth(self.store, enabled=True)
        if getattr(self, 'workspace_ready', True):
            self.store.graph.set_setting('workspace_name', 'Test workspace')
        self.server = make_server(self.store, port=0, auth=self.auth)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_port
        self.base = f"http://127.0.0.1:{self.port}"

    def call(self, method, path, data=None, token="", cookie="", csrf=""):
        headers = {"Host": f"127.0.0.1:{self.port}"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if cookie:
            headers["Cookie"] = cookie
        if csrf:
            headers["X-Bridge-CSRF"] = csrf
        body = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode()
        req = Request(self.base + path, data=body, method=method, headers=headers)
        with urlopen(req) as r:
            return json.loads(r.read()), r

    def raw(self, method, path, headers=None, body=None):
        """One request without following redirects: status, headers."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers={"Host": f"127.0.0.1:{self.port}", **(headers or {})})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), data

    def get(self, path, **kw):
        return self.call("GET", path, **kw)[0]

    def post(self, path, data, **kw):
        return self.call("POST", path, data, **kw)[0]

    def status_of(self, method, path, data=None, **kw):
        try:
            self.call(method, path, data, **kw)
        except HTTPError as error:
            return error.code
        return 200

    def mcp(self, name, arguments, token):
        response = self.post("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": name, "arguments": arguments}}, token=token)
        result = response["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            return {"result": text, "isError": True}
        return {"result": json.loads(text), "isError": False}


class IdentityAndRoleTests(SharedServer):
    def test_remote_mcp_handshake_tools_and_agent_only_access(self):
        person = self.post("/api/people", {"name": "MCP Tester", "role": "member"}, token=BOOTSTRAP)
        agent = self.post("/api/tokens", {"person_id": person["id"]}, token=BOOTSTRAP)["token"]
        human = self.post("/api/tokens", {"person_id": person["id"], "kind": "human"}, token=BOOTSTRAP)["token"]
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        hello = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
        status, _, _ = self.raw("POST", "/mcp", headers, json.dumps(hello).encode())
        self.assertEqual(status, 401)
        status, _, _ = self.raw("POST", "/mcp", {**headers, "Authorization": "Bearer " + human},
                                json.dumps(hello).encode())
        self.assertEqual(status, 401)
        secure = {**headers, "Authorization": "Bearer " + agent}
        status, _, body = self.raw("POST", "/mcp", secure, json.dumps(hello).encode())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"]["protocolVersion"], "2025-06-18")
        status, _, body = self.raw("POST", "/mcp", secure, json.dumps({
            "jsonrpc": "2.0", "method": "notifications/initialized"}).encode())
        self.assertEqual((status, body), (202, b""))
        status, _, body = self.raw("POST", "/mcp", secure, json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode())
        self.assertEqual(status, 200)
        self.assertIn("bridge_start_task", [t["name"] for t in json.loads(body)["result"]["tools"]])
        status, _, body = self.raw("POST", "/mcp", secure, json.dumps({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "bridge_list_owners", "arguments": {}}}).encode())
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["result"]["isError"])
        status, _, _ = self.raw("GET", "/mcp", secure)
        self.assertEqual(status, 405)

    def test_nothing_without_an_identity(self):
        self.assertEqual(self.status_of("GET", "/api/state"), 401)
        self.assertEqual(self.status_of("POST", "/api/runs", {"title": "t"}, token="brg_not_a_token"), 401)
        # The front page sends a browser to sign in.
        status, headers, _ = self.raw("GET", "/")
        self.assertEqual((status, headers.get("Location")), (302, "/auth/login"))
        status, _, body = self.raw("GET", "/auth/login")
        self.assertEqual(status, 200)
        self.assertIn(b"personal token", body)

    def test_setup_github_choice_survives_sign_in(self):
        status, headers, _ = self.raw("GET", "/?github_connect=1")
        self.assertEqual((status, headers.get("Location")), (302, "/auth/login"))
        self.assertIn("bridge_github_connect=1", headers.get("Set-Cookie", ""))
        admin = self.post("/api/people", {"name": "GitHub Admin", "role": "admin"}, token=BOOTSTRAP)
        token = self.post("/api/tokens", {"person_id": admin["id"], "kind": "human"}, token=BOOTSTRAP)["token"]
        status, headers, _ = self.raw("POST", "/auth/token", {
            "Content-Type": "application/x-www-form-urlencoded", "Cookie": "bridge_github_connect=1"
        }, f"token={token}".encode())
        self.assertEqual((status, headers.get("Location")), (302, "/?github_connect=1#connect"))
        # Both the signed-in session and the one-time setup intent are returned.
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/auth/token", body=f"token={token}", headers={
            "Host": f"127.0.0.1:{self.port}", "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": "bridge_github_connect=1"})
        response = conn.getresponse()
        cookies = response.headers.get_all("Set-Cookie")
        response.read()
        conn.close()
        self.assertEqual(len(cookies), 2)
        self.assertTrue(any("bridge_session=" in cookie for cookie in cookies))
        self.assertTrue(any("bridge_github_connect=; Max-Age=0" in cookie for cookie in cookies))

    def test_task_overview_is_private_and_viewer_cannot_add_context(self):
        run = self.store.add_run({"title": "Private task", "repo": "acme/library"})
        viewer = self.post("/api/people", {"name": "Observer", "role": "viewer"}, token=BOOTSTRAP)
        token = self.post("/api/tokens", {"person_id": viewer["id"], "kind": "human"}, token=BOOTSTRAP)["token"]
        for suffix in ("tree", "trace"):
            path = f"/api/tasks/{run['id']}/{suffix}"
            self.assertEqual(self.status_of("GET", path), 401)
            self.assertEqual(self.get(path, token=token)["task_id"], run["id"])
        self.assertEqual(self.status_of("POST", f"/api/tasks/{run['id']}/notes",
                                        {"text": "Unwanted edit"}, token=token), 403)

    def test_bootstrap_admin_adds_people_and_mints_tokens(self):
        state = self.get("/api/state", token=BOOTSTRAP)
        self.assertEqual(state["me"]["role"], "admin")
        self.assertTrue(state["auth"]["enabled"])
        self.assertTrue(state["mcp_config"]["remote"])
        self.assertTrue(state["mcp_config"]["mcpServers"]["bridge"]["url"].endswith("/mcp"))
        wes = self.post("/api/people", {"name": "Wes Chen", "email": "wes@acme.example", "role": "member"}, token=BOOTSTRAP)
        viewer = self.post("/api/people", {"name": "Val Viewer", "email": "val@acme.example", "role": "viewer"}, token=BOOTSTRAP)
        minted = self.post("/api/tokens", {"person_id": wes["id"], "label": "claude-code"}, token=BOOTSTRAP)
        self.assertTrue(minted["token"].startswith("brg_"))
        listed = self.get(f"/api/tokens?person_id={wes['id']}", token=BOOTSTRAP)["tokens"]
        self.assertEqual([t["label"] for t in listed], ["claude-code"])
        self.assertNotIn("token", listed[0])
        # The person's token identifies them, with their role.
        me = self.get("/api/me", token=minted["token"])["me"]
        self.assertEqual((me["name"], me["role"], me["kind"]), ("Wes Chen", "member", "agent"))
        # An agent credential reads and speaks the agent protocol; it
        # neither maintains people nor mints anything.
        self.assertEqual(self.status_of("POST", "/api/people", {"name": "X"}, token=minted["token"]), 403)
        self.assertEqual(self.status_of("POST", "/api/tokens", {"person_id": viewer["id"]}, token=minted["token"]), 403)
        self.assertEqual(self.status_of("POST", "/api/tokens", {"label": "laptop"}, token=minted["token"]), 403)
        # A person's own token (kind human) carries their role: a member
        # cannot maintain people, and mints for nobody else.
        human = self.post("/api/tokens", {"person_id": wes["id"], "label": "scripts", "kind": "human"}, token=BOOTSTRAP)
        self.assertEqual(self.get("/api/me", token=human["token"])["me"]["kind"], "token")
        self.assertEqual(self.status_of("POST", "/api/people", {"name": "X"}, token=human["token"]), 403)
        self.assertEqual(self.status_of("POST", "/api/tokens", {"person_id": viewer["id"]}, token=human["token"]), 403)
        own = self.post("/api/tokens", {"label": "laptop"}, token=human["token"])
        self.assertEqual((own["person_id"], own["kind"]), (wes["id"], "agent"))
        # A viewer reads and writes nothing.
        vt = self.post("/api/tokens", {"person_id": viewer["id"], "kind": "human"}, token=BOOTSTRAP)["token"]
        self.assertEqual(self.get("/api/me", token=vt)["me"]["role"], "viewer")
        self.assertEqual(self.status_of("POST", "/api/runs", {"title": "t"}, token=vt), 403)
        # Revoking a token ends its access.
        self.post(f"/api/tokens/{own['id']}/revoke", {}, token=human["token"])
        self.assertEqual(self.status_of("GET", "/api/me", token=own["token"]), 401)

    def test_writes_are_attributed_to_the_signed_in_person(self):
        wes = self.post("/api/people", {"name": "Wes Chen", "email": "wes@acme.example"}, token=BOOTSTRAP)
        self.post("/api/authority", {"person": wes["id"], "scope_kind": "path", "scope": "billing/*", "role": "decides"},
                  token=BOOTSTRAP)
        marisol = self.post("/api/people", {"name": "Marisol Vega", "email": "marisol@acme.example"}, token=BOOTSTRAP)
        wt = self.post("/api/tokens", {"person_id": wes["id"], "label": "scripts", "kind": "human"}, token=BOOTSTRAP)["token"]
        mt = self.post("/api/tokens", {"person_id": marisol["id"], "label": "codex"}, token=BOOTSTRAP)["token"]
        # Marisol's agent kicks off: she is the requester without saying so.
        t = self.mcp("bridge_start_task", {
            "title": "Add usage-based pricing", "repo": "acme/platform", "paths": "billing/usage.py"}, mt)
        self.assertFalse(t["isError"])
        task = t["result"]
        self.assertEqual(self.store.graph.get_task(task["task_id"])["requester"], "Marisol Vega")
        n = self.mcp("bridge_add_node", {
            "task_id": task["task_id"], "question": "Should we bill the usage spike?", "context": "enterprise-two",
            "paths": "billing/usage.py"}, mt)["result"]
        self.assertEqual((n["status"], n["owner"]), ("pending", "Wes Chen"))
        # The UI uses the same standing check as writes, not the viewer's
        # display name or an optimistic sign-off button for every member.
        detail_url = f"/api/decisions/{n['node_id']}"
        self.assertIn("answer", self.get(detail_url, token=wt)["allowed_actions"])
        self.assertIn("sign", self.get(detail_url, token=wt)["allowed_actions"])
        other_human = self.post("/api/tokens", {"person_id": marisol["id"], "kind": "human"}, token=BOOTSTRAP)["token"]
        actions = self.get(detail_url, token=other_human)["allowed_actions"]
        self.assertNotIn("answer", actions)
        self.assertNotIn("sign", actions)
        self.assertIn("followup", actions)
        self.assertEqual(self.get(detail_url, token=mt)["allowed_actions"], [])
        # Wes answers as himself, whatever the payload says.
        row = self.post(f"/api/decisions/{n['node_id']}/answer",
                        {"answer": "Bill it.", "rationale": "policy", "by": "Someone Else",
                         "expected_updated_at": n["updated_at"]}, token=wt)
        self.assertEqual((row["signed_by"], row["answered_by"]), ("Wes Chen", "Wes Chen"))
        recorded = [e for e in row["events"] if e["kind"] == "owner_approved"][0]
        self.assertIn("inbox: Wes Chen", recorded["detail"])
        f = self.post(f"/api/decisions/{n['node_id']}/followups", {"questions": ["Should trials be metered?"], "by": "Nobody"},
                      token=wt)
        self.assertIn("added by Wes Chen", self.store.get_decision(f["nodes"][0]["node_id"])["context"])
        # Agents cannot ingest; only admins can.
        self.assertEqual(self.status_of("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                          "params": {"name": "bridge_ingest_repo",
                                                                     "arguments": {"path": "/x"}}}, token=mt), 403)

    def test_a_token_is_not_ambient_but_a_cookie_needs_the_csrf_token(self):
        wes = self.post("/api/people", {"name": "Wes Chen", "email": "wes@acme.example"}, token=BOOTSTRAP)
        agent = self.post("/api/tokens", {"person_id": wes["id"]}, token=BOOTSTRAP)["token"]
        # An agent credential never becomes a browser session.
        status, headers, _ = self.raw("POST", "/auth/token", {"Content-Type": "application/x-www-form-urlencoded"},
                                      f"token={agent}".encode())
        self.assertEqual(status, 302)
        self.assertIn("agent%20credential", headers.get("Location", ""))
        wt = self.post("/api/tokens", {"person_id": wes["id"], "kind": "human"}, token=BOOTSTRAP)["token"]
        # Sign in with a human token on the login form: a session cookie comes back.
        status, headers, _ = self.raw("POST", "/auth/token", {"Content-Type": "application/x-www-form-urlencoded"},
                                      f"token={wt}".encode())
        self.assertEqual((status, headers.get("Location")), (302, "/"))
        cookie = headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        jar = SimpleCookie(cookie)
        session = "bridge_session=" + jar["bridge_session"].value
        state = self.get("/api/state", cookie=session)
        self.assertEqual(state["me"]["kind"], "session")
        self.assertEqual(self.status_of("POST", "/api/runs", {"title": "t"}, cookie=session), 403)
        run = self.post("/api/runs", {"title": "t"}, cookie=session, csrf=state["csrf_token"])
        self.assertEqual(run["title"], "t")
        # A forged cookie is nobody.
        self.assertEqual(self.status_of("GET", "/api/state", cookie=session[:-4] + "zzzz"), 401)


class GitHubSignInTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "github.db")
        env = patch.dict(os.environ, {"BRIDGE_GITHUB_CLIENT_ID": "cid", "BRIDGE_GITHUB_CLIENT_SECRET": "csecret",
                                      "BRIDGE_SECRET": "unit-test-secret"})
        env.start()
        self.addCleanup(env.stop)
        self.auth = Auth(self.store, enabled=True, public_url="https://bridge.acme.test")

    def fake_fetch(self, login, name, emails, orgs=()):
        def fetch(method, url, body, headers):
            if url.endswith("/access_token"):
                return {"access_token": "gho_x"}
            if url.endswith("/user"):
                return {"login": login, "name": name, "id": sum(map(ord, login)) * 7919}
            if url.endswith("/user/emails"):
                return [{"email": e, "verified": True, "primary": i == 0} for i, e in enumerate(emails)]
            if url.endswith("/user/orgs"):
                return [{"login": o} for o in orgs]
            raise AssertionError(url)
        return fetch

    def exchange(self, login, name, emails, orgs=()):
        url = self.auth.github_authorize_url("https://bridge.acme.test/auth/github/callback")
        state = url.split("state=")[1]
        return self.auth.github_exchange("code", state, "https://bridge.acme.test/auth/github/callback",
                                         fetch=self.fake_fetch(login, name, emails, orgs))

    def test_the_first_person_in_is_the_admin_and_later_ones_must_be_known(self):
        self.assertIn("client_id=cid", self.auth.github_authorize_url("https://bridge.acme.test/auth/github/callback"))
        first = self.auth.sign_in_github_user(self.exchange("wchen", "Wes Chen", ["wes@acme.example"]))
        self.assertEqual((first["name"], first["role"], first["github_login"]), ("Wes Chen", "admin", "wchen"))
        with self.assertRaises(Invalid):
            self.auth.sign_in_github_user(self.exchange("stranger", "A Stranger", ["s@else.example"]))
        # Somebody whose display name is the admin's name is not the admin.
        with self.assertRaises(Invalid):
            self.auth.sign_in_github_user(self.exchange("wes-chen", "Wes Chen", ["other@else.example"]))
        with self.store.graph.transaction():
            self.store.graph.add_person("Marisol Vega", email="marisol@acme.example")
        by_email = self.auth.sign_in_github_user(self.exchange("mvega", "M Vega", ["marisol@acme.example"]))
        self.assertEqual(by_email["name"], "Marisol Vega")
        self.assertEqual(self.store.graph.get_person(by_email["id"])["github_login"], "mvega")
        # The email link happened once; another account with that email is a stranger now.
        with self.assertRaises(Invalid):
            self.auth.sign_in_github_user(self.exchange("mvega2", "M Vega", ["marisol@acme.example"]))
        # A session cookie carries the person and survives a restart of the Auth object.
        cookie = self.auth.session_cookie(first["id"])
        self.assertIn("Secure", cookie)
        again = Auth(self.store, enabled=True, public_url="https://bridge.acme.test")
        me = again.identify({"Cookie": cookie.split(";")[0]})
        self.assertEqual(me.name, "Wes Chen")

    def test_an_organization_can_be_required(self):
        with patch.dict(os.environ, {"BRIDGE_GITHUB_ORG": "acme", "BRIDGE_AUTH_ALLOW_SIGNUP": "1"}):
            auth = Auth(self.store, enabled=True)
            url = auth.github_authorize_url("https://bridge.acme.test/auth/github/callback")
            state = url.split("state=")[1]
            gh = auth.github_exchange("code", state, "https://bridge.acme.test/auth/github/callback",
                                      fetch=self.fake_fetch("out", "Out Sider", ["o@x"], orgs=["other"]))
            with self.assertRaises(Invalid):
                auth.sign_in_github_user(gh)
            state = auth.github_authorize_url("https://bridge.acme.test/auth/github/callback").split("state=")[1]
            gh = auth.github_exchange("code", state, "https://bridge.acme.test/auth/github/callback",
                                      fetch=self.fake_fetch("inside", "In Sider", ["i@acme.example"], orgs=["acme"]))
            self.assertEqual(auth.sign_in_github_user(gh)["name"], "In Sider")


class RemoteMcpTests(SharedServer):
    def test_http_mcp_speaks_to_a_shared_bridge(self):
        wes = self.post("/api/people", {"name": "Wes Chen", "email": "wes@acme.example"}, token=BOOTSTRAP)
        wt = self.post("/api/tokens", {"person_id": wes["id"], "label": "claude-code"}, token=BOOTSTRAP)["token"]
        task = self.mcp("bridge_start_task", {
            "title": "Add usage pricing", "repo": "acme/platform", "client_key": "remote-1"}, wt)["result"]
        self.assertIn("task_id", task)
        self.assertEqual(self.store.graph.get_task(task["task_id"])["requester"], "Wes Chen")
        self.assertEqual(self.store.graph.get_task(task["task_id"])["agent"], "claude-code")
        missing = self.mcp("bridge_get_tree", {"task_id": "nope"}, wt)
        self.assertTrue(missing["isError"])
        self.assertIn("Task not found", missing["result"])
        self.assertEqual(self.status_of("POST", "/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "bridge_start_task", "arguments": {"title": "x", "repo": "acme/platform"}}},
            token="brg_wrong"), 401)


class BindingTests(OfflineCase):
    def test_off_loopback_needs_auth(self):
        store = Store(Path(self.temp.name) / "bind.db")
        with self.assertRaises(Invalid):
            make_server(store, port=0, host="0.0.0.0")
        with patch.dict(os.environ, {"BRIDGE_ADMIN_TOKEN": BOOTSTRAP}):
            server = make_server(store, port=0, host="0.0.0.0", auth=Auth(store, enabled=True, public_url="http://bridge.acme.test:7333"))
            server.server_close()

    def serve(self, name, public_url):
        store = Store(Path(self.temp.name) / name)
        store.graph.set_setting('workspace_name', 'Test workspace')
        with patch.dict(os.environ, {"BRIDGE_ADMIN_TOKEN": BOOTSTRAP}):
            server = make_server(store, port=0, auth=Auth(store, enabled=True, public_url=public_url))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        return server.server_port

    def expect(self, port, expected):
        for host, code in expected:
            req = Request(f"http://127.0.0.1:{port}/api/me",
                          headers={"Host": host, "Authorization": f"Bearer {BOOTSTRAP}"})
            try:
                with urlopen(req) as r:
                    self.assertEqual((host, r.status), (host, code))
            except HTTPError as error:
                self.assertEqual((host, error.code), (host, code))

    def test_the_public_host_is_accepted(self):
        port = self.serve("host.db", "http://bridge.acme.test:7333")
        self.expect(port, (("bridge.acme.test:7333", 200), ("evil.example", 403)))

    def test_both_spellings_of_the_published_loopback_address_are_accepted(self):
        """Published through Docker, the container binds 7333 whatever
        port is mapped, so the public URL is the only thing that knows
        the port people reach. A setup that printed the address as
        127.0.0.1 and configured it as localhost served a link that
        returned 403; the two are the same machine, and refusing one of
        them protected nothing. A name that merely resolves to loopback
        still speaks for nobody."""
        port = self.serve("published.db", "http://localhost:17433")
        self.expect(port, (("localhost:17433", 200), ("127.0.0.1:17433", 200), ("[::1]:17433", 200),
                           ("localhost:17434", 403), ("bridge.acme.test:17433", 403)))


if __name__ == "__main__":
    unittest.main()
