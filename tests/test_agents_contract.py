"""SDK wire checks run with requirements-agents.txt; never contact OpenAI."""

import importlib.util
import json
import unittest
import base64
import hashlib
import hmac
import time

from bridge.agents_api import AgentsAPI, result_event, safe_error

SDK_INSTALLED = bool(importlib.util.find_spec("openai"))


class ResultTests(unittest.TestCase):
    def test_negative_judgment_is_successful_transport(self):
        action = {"type": "function_call", "turn_id": "turn_one", "call_id": "call_one"}
        event = result_event(action, {"answer": "Do not proceed", "revision": "revision_one"})
        self.assertTrue(event["success"])
        self.assertEqual(json.loads(event["output"])["answer"], "Do not proceed")
        self.assertEqual(event["turn_id"], "turn_one")

    def test_environment_action_cannot_receive_answer(self):
        with self.assertRaises(ValueError):
            result_event({"type": "environment_connection"}, {})

    def test_error_does_not_capture_credentials_or_body(self):
        error = RuntimeError("Authorization: Bearer sensitive-value")
        self.assertEqual(safe_error(error), {"type": "RuntimeError"})


@unittest.skipUnless(SDK_INSTALLED, "Optional OpenAI SDK is not installed")
class SDKContractTests(unittest.TestCase):
    def setUp(self):
        import httpx2
        import openai
        from bridge.validate_agents import sdk_contract
        self.assertEqual(sdk_contract()["contract"], "passed")
        self.requests = []
        self.responses = []

        def handle(request):
            self.requests.append(request)
            status, payload = self.responses.pop(0)
            return httpx2.Response(status, json=payload)

        self.client = openai.OpenAI(api_key="contract-only", max_retries=0,
                                   http_client=httpx2.Client(transport=httpx2.MockTransport(handle)))
        self.addCleanup(self.client.close)
        self.api = AgentsAPI(self.client)

    def test_session_creation_and_result_wire_format(self):
        self.responses = [(200, {"id": "sess_one", "environment": {"type": "none"}}), (200, None)]
        self.api.start_task(model="configured-model", instructions="configured instructions", tools=[],
                            environment={"type": "none"}, task="original task",
                            metadata={"bridge_run": "run_one"})
        action = {"type": "function_call", "turn_id": "turn_one", "call_id": "call_one"}
        self.api.send_events("sess_one", [result_event(action, {"answer": "No"})], "delivery-one")
        create, result = self.requests
        self.assertEqual(create.url.path, "/v1/agents/sessions")
        self.assertEqual(create.headers["OpenAI-Beta"], "agents=v1")
        self.assertEqual(json.loads(create.content)["metadata"], {"bridge_run": "run_one"})
        self.assertEqual(result.url.path, "/v1/agents/sessions/sess_one/events")
        self.assertEqual(result.headers["Idempotency-Key"], "delivery-one")
        payload = json.loads(result.content)
        self.assertIsInstance(payload["events"][0]["output"], str)
        self.assertEqual(payload["events"][0]["call_id"], "call_one")

    def test_recovery_search_paginates_and_filters_metadata(self):
        self.responses = [(200, {"data": [{"id": "sess_other", "metadata": {}}], "has_more": True,
                                 "last_id": "sess_other"}),
                          (200, {"data": [{"id": "sess_match", "metadata": {"bridge_run": "one"}}],
                                 "has_more": False})]
        self.assertEqual(self.api.find_sessions({"bridge_run": "one"})[0]["id"], "sess_match")
        self.assertEqual(len(self.requests), 2)
        self.assertIn("after=sess_other", str(self.requests[-1].url))

    def test_mutations_do_not_retry_in_sdk(self):
        import openai
        self.responses = [(500, {"error": {"message": "temporary", "type": "server_error"}})]
        with self.assertRaises(openai.APIStatusError):
            self.api.send_events("sess_one", [], "delivery-one")
        self.assertEqual(len(self.requests), 1)

    def test_webhook_signature_and_replay_window(self):
        import openai
        payload = b'{"id":"event-one","type":"agent.session.action_required","data":{"id":"session-one"}}'
        secret = b"synthetic-webhook-secret"
        self.client.webhook_secret = "whsec_" + base64.b64encode(secret).decode()
        timestamp = str(int(time.time()))
        signed = b"event-one." + timestamp.encode() + b"." + payload
        signature = base64.b64encode(hmac.new(secret, signed, hashlib.sha256).digest()).decode()
        headers = {"webhook-id": "event-one", "webhook-timestamp": timestamp, "webhook-signature": "v1," + signature}
        self.client.webhooks.verify_signature(payload=payload, headers=headers)
        with self.assertRaises(openai.InvalidWebhookSignatureError):
            self.client.webhooks.verify_signature(payload=payload + b" ", headers=headers)
        with self.assertRaises(openai.InvalidWebhookSignatureError):
            self.client.webhooks.verify_signature(payload=payload, headers={**headers, "webhook-timestamp": "1"})
