import json
from unittest.mock import patch

from test_contract import ContractCase
from bridge.config import Config, backend_status
from bridge.mcp import call_tool


class InferenceStatusTests(ContractCase):
    def test_missing_inference_is_actionable_and_does_not_claim_live_verification(self):
        with patch('bridge.config.load', return_value=Config(model_api='none')), patch('bridge.llm.find_claude', return_value=None):
            status = call_tool(self.store, 'bridge_connection_status', {})
        self.assertFalse(status['inference']['configured'])
        self.assertFalse(status['inference']['semantic_enabled'])
        self.assertFalse(status['inference']['live_verified'])
        self.assertIn('no_inference', {item['key'] for item in status['readiness']})

    def test_configured_key_never_appears_in_status(self):
        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'secret-canary-for-unit-test-only'}), \
             patch('bridge.llm.find_claude', return_value=None):
            status = backend_status(Config(model_api='anthropic'))
        self.assertTrue(status['configured'])
        self.assertFalse(status['live_verified'])
        self.assertNotIn('secret-canary', json.dumps(status))

    def test_forced_semantics_without_backend_is_not_reported_ready(self):
        with patch.dict('os.environ', {'BRIDGE_SEMANTIC': '1', 'ANTHROPIC_API_KEY': ''}), \
             patch('bridge.llm.find_claude', return_value=None):
            status = backend_status(Config(model_api='anthropic'))
        self.assertFalse(status['semantic_enabled'])
