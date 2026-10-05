import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from bridge.github import GitHubError
from bridge.github_device import GitHubDeviceConnector, connection_for, DEFAULT_CLIENT_ID, DEFAULT_APP_SLUG
from bridge.store import Store


class Response:
    headers = {}

    def __init__(self, value):
        self.body = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self):
        return self.body


class DeviceTests(unittest.TestCase):
    def test_connection_defaults_and_overrides(self):
        path = Path(self.temp.name) / "github-app.json"
        for env in ({}, {"BRIDGE_GITHUB_APP_CLIENT_ID": "", "BRIDGE_GITHUB_APP_SLUG": ""}):
            connector = connection_for(self.store.graph, path, env)
            self.assertEqual(connector.client_id, DEFAULT_CLIENT_ID)
            self.assertEqual(connector.slug, DEFAULT_APP_SLUG)
        custom = {"BRIDGE_GITHUB_APP_CLIENT_ID": "custom-id", "BRIDGE_GITHUB_APP_SLUG": "custom-app"}
        self.assertEqual(connection_for(self.store.graph, path, custom).client_id, "custom-id")
        for env in ({"BRIDGE_GITHUB_APP_CLIENT_ID": "custom-id"}, {"BRIDGE_GITHUB_APP_SLUG": "custom-app"}):
            with self.assertRaises(ValueError):
                connection_for(self.store.graph, path, env)
        with patch("bridge.github_app.GitHubAppConnector.credentials", return_value={"app_id": 123}):
            from bridge.github_app import GitHubAppConnector
            self.assertIsInstance(connection_for(self.store.graph, path, {}), GitHubAppConnector)
            self.assertIsInstance(connection_for(self.store.graph, path, custom), GitHubDeviceConnector)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.db")
        self.addCleanup(self.store.graph.db.close)
        self.now = 1000
        self.calls = []
        self.repos = ["acme/platform"]
        self.token_result = {"error": "authorization_pending"}
        self.connector = GitHubDeviceConnector(self.store.graph, Path(self.temp.name) / "tokens.json",
            "Iv1.public", "bridge-test", self.fetch, lambda: self.now)

    def fetch(self, request, timeout=30):
        path = urlsplit(request.full_url).path
        data = json.loads(request.data) if request.data else {}
        self.calls.append((path, data))
        self.assertNotIn("client_secret", data)
        if path == "/login/device/code":
            return Response({"device_code": "private-device-code", "user_code": "ABCD-EFGH",
                             "interval": 5, "expires_in": 900})
        if path == "/login/oauth/access_token":
            if data["grant_type"] == "refresh_token":
                return Response({"access_token": "refreshed", "refresh_token": "rotated", "expires_in": 28800})
            return Response(self.token_result)
        if path == "/user/installations":
            return Response({"installations": [{"id": 12, "app_slug": "bridge-test"},
                                               {"id": 13, "app_slug": "another-app"}]})
        if path == "/user/installations/12/repositories":
            return Response({"repositories": [{"full_name": repo} for repo in self.repos]})
        raise AssertionError(path)

    def connect(self):
        flow = self.connector.start_device("admin")
        self.now += 5
        self.token_result = {"access_token": "private-token", "refresh_token": "private-refresh", "expires_in": 28800}
        result = self.connector.poll_device("admin", flow["flow_id"])
        self.assertEqual(result["repositories"], self.repos)
        return flow

    def test_authorization_persists_locally_and_refreshes_without_app_secret(self):
        flow = self.connect()
        self.assertNotIn("private", json.dumps(flow))
        self.assertEqual(self.connector.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("private", json.dumps(self.connector.status()))
        self.now += 28800
        self.assertIsNotNone(self.connector.api_for_repo("acme/platform"))
        stored = json.loads(self.connector.path.read_text())
        self.assertEqual(stored["refresh_token"], "rotated")
        self.assertIsNone(self.connector.api_for_repo("other/private"))

    def test_flow_is_bound_to_admin_and_rate_limited(self):
        flow = self.connector.start_device("admin")
        with self.assertRaises(GitHubError):
            self.connector.poll_device("someone-else", flow["flow_id"])
        before = len(self.calls)
        self.assertTrue(self.connector.poll_device("admin", flow["flow_id"])["pending"])
        self.assertEqual(len(self.calls), before)
        self.now += 5
        self.token_result = {"error": "slow_down"}
        result = self.connector.poll_device("admin", flow["flow_id"])
        self.assertEqual(result["interval"], 10)
        self.now += 900
        with self.assertRaisesRegex(GitHubError, "expired"):
            self.connector.poll_device("admin", flow["flow_id"])

    def test_removed_repositories_and_replayed_codes_are_rejected(self):
        flow = self.connect()
        with self.assertRaises(GitHubError):
            self.connector.poll_device("admin", flow["flow_id"])
        self.repos = []
        self.connector.refresh_repositories()
        self.assertIsNone(self.connector.api_for_repo("acme/platform"))

    def test_changing_app_does_not_reuse_old_user_credentials(self):
        self.connect()
        self.connector.client_id = "Iv1.other"
        self.assertFalse(self.connector.status()["authorized"])

    def test_denial_does_not_store_a_credential(self):
        flow = self.connector.start_device("admin")
        self.now += 5
        self.token_result = {"error": "access_denied"}
        with self.assertRaisesRegex(GitHubError, "declined"):
            self.connector.poll_device("admin", flow["flow_id"])
        self.assertFalse(self.connector.path.exists())
