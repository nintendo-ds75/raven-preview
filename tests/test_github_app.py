"""The browser-to-GitHub-to-Bridge installation journey, with GitHub simulated."""
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import http.client

from bridge.auth import Auth
from bridge.github import Syncer, sync_state
from bridge.github_app import GitHubAppConnector
from bridge.github_app import manifest_state
from fixtures import ready_server as make_server
from bridge.store import Store


class Response:
    def __init__(self, payload):
        self.body = io.BytesIO(json.dumps(payload).encode())
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.body.close()

    def read(self):
        return self.body.read()


class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.installation_app_id = 777

    def __call__(self, request, timeout=30):
        url = urlsplit(request.full_url)
        self.calls.append((request.get_method(), url.path))
        if url.path.endswith("/conversions"):
            return Response({"id": 777, "slug": "bridge-test-app", "pem": "-----BEGIN PRIVATE KEY-----\ntest\n"})
        if url.path == "/app/installations/12":
            return Response({"app_id": self.installation_app_id, "account": {"login": "octo"}, "suspended_at": None})
        if url.path == "/app/installations/12/access_tokens":
            return Response({"token": "temporary-installation-token"})
        if url.path == "/installation/repositories":
            return Response({"repositories": [{"full_name": "octo/platform"}, {"full_name": "octo/docs"}]})
        if url.path.startswith("/repos/") and url.path.endswith("/pulls"):
            return Response([])
        raise AssertionError(f"Unexpected GitHub request: {request.get_method()} {url.path}")


class GitHubAppJourney(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "bridge.db")
        self.fake = FakeGitHub()
        self.connector = GitHubAppConnector(self.store.graph, Path(self.temp.name) / "github-app.json", self.fake)
        self.syncer = Syncer(self.store.graph, self.connector.api_for_repo)
        self.auth = Auth(self.store, enabled=False)
        self.server = make_server(self.store, 0, auth=self.auth,
                                  github_app=self.connector, github_syncer=self.syncer)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, data=None, csrf=""):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        headers = {"Host": f"127.0.0.1:{self.server.server_port}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
            headers["X-Bridge-CSRF"] = csrf
        connection.request(method, path, body=json.dumps(data).encode() if data is not None else None, headers=headers)
        response = connection.getresponse()
        status, location, body = response.status, response.getheader("Location"), response.read()
        connection.close()
        return status, location, json.loads(body) if body else None

    def test_connect_choose_repositories_and_sync_without_pasted_token(self):
        csrf = self.request("GET", "/api/state")[2]["csrf_token"]
        status, _, error = self.request("POST", "/api/github/connect", {}, csrf)
        self.assertEqual(status, 400)
        self.assertIn("not enabled", error["error"])
        # Legacy operator-owned app callbacks remain usable; users are never sent to app creation.
        start = {"manifest": self.connector.manifest("http://localhost:7333")}
        self.assertEqual(start["manifest"]["default_permissions"]["pull_requests"], "read")
        self.assertNotIn("hook_attributes", start["manifest"])
        self.assertNotIn("default_events", start["manifest"])
        state = manifest_state(self.auth.secret, "")
        status, _, _ = self.request("GET", "/auth/github-app/created?code=abcdef123456&state=bad")
        self.assertEqual(status, 302)
        self.assertFalse(self.connector.credentials())
        status, install_url, _ = self.request("GET", "/auth/github-app/created?code=abcdef123456&state=" + state)
        self.assertEqual(status, 302)
        self.assertIn("github.com/apps/bridge-test-app/installations/new", install_url)
        self.assertEqual(self.connector.path.stat().st_mode & 0o777, 0o600)
        install_state = parse_qs(urlsplit(install_url).query)["state"][0]
        with patch("bridge.github_app._jwt", return_value="signed-app-jwt"):
            status, location, _ = self.request("GET", "/auth/github-app/installed?installation_id=12&state=bad")
            self.assertEqual(status, 302)
            self.assertIn("github_error=", location)
            self.assertFalse(self.connector.installations())
            self.fake.installation_app_id = 999
            status, location, _ = self.request("GET", "/auth/github-app/installed?installation_id=12&state=" + install_state)
            self.assertEqual(status, 302)
            self.assertIn("github_error=", location)
            self.assertFalse(self.connector.installations())
            self.fake.installation_app_id = 777
            status, location, _ = self.request("GET", "/auth/github-app/installed?installation_id=12&state=" + install_state)
            self.assertEqual(status, 302)
            self.assertIn("github_connected=", location)
            self.assertEqual(self.connector.status()["repositories"], ["octo/docs", "octo/platform"])
            self.assertEqual(len(self.syncer.tick()), 2)
        self.assertTrue(sync_state(self.store.graph, "octo/platform")["last_success_at"])
        status, _, again = self.request("POST", "/api/github/connect", {}, csrf)
        self.assertEqual(status, 200)
        self.assertIn("installations/new?state=", again["url"])
        self.assertNotIn("GITHUB_TOKEN", json.dumps(again))


if __name__ == "__main__":
    unittest.main()
