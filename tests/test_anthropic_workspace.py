"""Optional workspace selection uses synthetic keys and captured requests only."""
import io
import json
import os
import unittest
from unittest.mock import PropertyMock, patch

from bridge.config import Config, load
from bridge.llm import Client, LLMError


class AnthropicWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"ANTHROPIC_API_KEY": "synthetic-fixture-key",
                                          "ANTHROPIC_WORKSPACE_ID": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = Client(Config())

    def capture(self, client=None):
        calls = []

        def open_request(request, timeout=None):
            calls.append((request, timeout))
            return io.BytesIO(json.dumps({"content": [{"type": "text", "text": "ok"}]}).encode())

        with patch("urllib.request.urlopen", open_request):
            (client or self.client)._messages("system", "prompt", 12)
        request, timeout = calls[0]
        return {key.lower(): value for key, value in request.header_items()}, json.loads(request.data), timeout

    def test_unset_preserves_existing_headers_and_body(self):
        del os.environ["ANTHROPIC_WORKSPACE_ID"]
        headers, body, timeout = self.capture()
        self.assertNotIn("anthropic-workspace-id", headers)
        self.assertEqual(headers["x-api-key"], "synthetic-fixture-key")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(body["max_tokens"], 12)
        self.assertEqual(body["messages"], [{"role": "user", "content": "prompt"}])
        self.assertEqual(timeout, 120)

    def test_explicit_workspace_is_sent_on_the_actual_request(self):
        os.environ["ANTHROPIC_WORKSPACE_ID"] = "wrkspc_fixture_a"
        headers, body, _ = self.capture()
        self.assertEqual(headers["anthropic-workspace-id"], "wrkspc_fixture_a")
        self.assertNotIn("anthropic_workspace_id", body)

    def test_value_is_read_at_call_time_without_caching_selection(self):
        for value in ("wrkspc_fixture_a", "wrkspc_fixture_b", ""):
            os.environ["ANTHROPIC_WORKSPACE_ID"] = value
            headers, _, _ = self.capture()
            self.assertEqual(headers.get("anthropic-workspace-id", ""), value)

    def test_load_and_fast_configuration_preserve_selection(self):
        os.environ["ANTHROPIC_WORKSPACE_ID"] = "  wrkspc_fixture_a  "
        cfg = load()
        self.assertEqual(cfg.anthropic_workspace_id, "wrkspc_fixture_a")
        headers, _, _ = self.capture(Client(cfg.fast()))
        self.assertEqual(headers["anthropic-workspace-id"], "wrkspc_fixture_a")

    def test_blank_workspace_is_omitted(self):
        os.environ["ANTHROPIC_WORKSPACE_ID"] = "   "
        headers, _, _ = self.capture()
        self.assertNotIn("anthropic-workspace-id", headers)

    def test_invalid_header_value_is_rejected_before_transport_without_echo(self):
        for value in ("fixture\r\nINJECTED", "fixture\tvalue", "fixture value", "fixture\x00value", "fixtureé", "w" * 257):
            with self.subTest(value=value), patch.object(self.client, "_post_json") as post, \
                    patch.object(Config, "anthropic_workspace_id", new_callable=PropertyMock, return_value=value):
                with self.assertRaisesRegex(LLMError, "ANTHROPIC_WORKSPACE_ID") as error:
                    self.client._messages("system", "prompt", 12)
                self.assertNotIn(value, str(error.exception))
                post.assert_not_called()

if __name__ == "__main__":
    unittest.main()
