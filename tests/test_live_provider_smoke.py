"""The legacy live smoke counts the production transport seam, fully offline."""
import io
import json
import os
import unittest
from unittest.mock import patch

from bridge import llm
from evals.live_provider_smoke import run


class LiveProviderSmokeTransportTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"BRIDGE_MODEL_API": "anthropic", "BRIDGE_SEMANTIC": "1",
                                     "ANTHROPIC_API_KEY": "synthetic-fixture-key"})
        env.start()
        self.addCleanup(env.stop)
        network = patch("urllib.request.urlopen", side_effect=AssertionError("Global transport forbidden"))
        network.start()
        self.addCleanup(network.stop)

    @staticmethod
    def response(*args, **kwargs):
        return io.BytesIO(json.dumps({"content": [{"type": "text", "text": "fixture"}],
                                     "usage": {"input_tokens": 5, "output_tokens": 2}}).encode())

    def test_counts_physical_request_on_the_production_client_path(self):
        def reading(cfg, payload):
            llm.Client(cfg)._messages("synthetic system", "synthetic prompt", 8)
            return {"kind": "needs_clarification"}

        with patch("bridge.llm._open_model_request", side_effect=self.response) as transport, \
                patch("evals.live_provider_smoke.slack_chat.reading", side_effect=reading):
            result = run()
        self.assertEqual(result["physical_http_requests"], 1)
        self.assertEqual(result["successful_completions"], 1)
        self.assertEqual((result["input_tokens"], result["output_tokens"]), (5, 2))
        transport.assert_called_once()
        self.assertEqual(transport.call_args.args[0].full_url, "https://api.anthropic.com/v1/messages")
        self.assertNotIn("synthetic-fixture-key", json.dumps(result))

    def test_fourth_physical_attempt_is_blocked_before_transport(self):
        def reading(cfg, payload):
            for _ in range(4):
                llm.Client(cfg)._messages("synthetic system", "synthetic prompt", 8)

        with patch("bridge.llm._open_model_request", side_effect=self.response) as transport, \
                patch("evals.live_provider_smoke.slack_chat.reading", side_effect=reading):
            result = run()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "LLMError")
        self.assertEqual(result["physical_http_requests"], 3)
        self.assertEqual(transport.call_count, 3)

    def test_missing_key_remains_blocked_before_transport(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}), patch("bridge.llm._open_model_request") as transport:
            result = run()
        self.assertEqual(result["status"], "blocked")
        transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
