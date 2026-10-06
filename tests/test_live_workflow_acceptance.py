"""Offline tests for opt-in gating, driver wiring and semantic acceptance oracles.

Only these tests inject provider HTTP responses. The live CLI never does.
"""
import io
import json
import os
import unittest
import urllib.request
from unittest.mock import patch

from fixtures import ROOT
from bridge import llm
from evals.live_workflow_acceptance import (
    PROVIDER_URL, RequestBudget, check_expression, check_guidance, novel_scenario, run,
)


class Response(io.BytesIO):
    status = 200


class LiveWorkflowDriverTests(unittest.TestCase):
    def test_default_refuses_even_when_a_key_exists(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'fixture-only-not-a-real-key'}), \
             patch('urllib.request.urlopen', side_effect=AssertionError('Network forbidden')) as network:
            result = run()
        self.assertEqual(result['status'], 'blocked')
        network.assert_not_called()
        self.assertNotIn('fixture-only-not-a-real-key', json.dumps(result))

    def test_explicit_flag_without_key_refuses_before_constructing_harness(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': '', 'BRIDGE_MODEL_API': 'anthropic', 'BRIDGE_SEMANTIC': '1'}), \
             patch('urllib.request.urlopen', side_effect=AssertionError('Network forbidden')) as network:
            result = run(run_live=True, harness_factory=lambda **_: self.fail('Harness should not start'))
        self.assertEqual(result['status'], 'blocked')
        network.assert_not_called()

    def test_budget_counts_physical_attempts_and_usage_without_changing_response(self):
        body = {'content': [{'type': 'text', 'text': '{"result":"fixture"}'}],
                'usage': {'input_tokens': 17, 'output_tokens': 9, 'cache_read_input_tokens': 3}}
        budget = RequestBudget(2, opener=lambda *_, **__: Response(json.dumps(body).encode()))
        request = urllib.request.Request(PROVIDER_URL, data=b'{}')
        for _ in range(2):
            with budget.open(request) as response:
                self.assertEqual(json.loads(response.read()), body)
        with self.assertRaisesRegex(llm.LLMError, 'budget exhausted'):
            budget.open(request)
        self.assertEqual(budget.summary()['physical_http_requests'], 2)
        self.assertEqual(budget.summary()['input_tokens'], 34)
        self.assertEqual(budget.summary()['output_tokens'], 18)
        self.assertEqual(budget.summary()['cache_read_input_tokens'], 6)
        with self.assertRaisesRegex(llm.LLMError, 'unexpected external'):
            budget.open('https://unexpected.invalid/data')

    def test_fixture_injected_driver_uses_live_mode_and_real_client_http_path(self):
        captured = {}
        class Harness:
            def __init__(self, **kwargs):
                captured.update(kwargs)
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
        def workflow(harness, agent, scenario, report, timeout):
            result = agent.complete_json('offline_driver_contract', 'Return JSON', '{}', max_tokens=64)
            self.assertEqual(result, {'fixture_driver': True})
            self.assertGreater(scenario['threshold'], 100)
            report.update(status='passed', stage='fixture_driver_only')
        def provider_response(*args, **kwargs):
            self.assertEqual(args[0].full_url, PROVIDER_URL)
            return Response(json.dumps({'type': 'message', 'stop_reason': 'end_turn',
                'content': [{'type': 'text', 'text': '{"fixture_driver":true}'}],
                'usage': {'input_tokens': 7, 'output_tokens': 5}}).encode())
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'fixture-only-not-a-real-key',
                                    'BRIDGE_MODEL_API': 'anthropic', 'BRIDGE_SEMANTIC': '1'}), \
             patch('urllib.request.urlopen', side_effect=provider_response):
            result = run(run_live=True, max_calls=2, harness_factory=Harness, workflow=workflow)
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(captured['model_mode'], 'live')
        self.assertEqual(result['physical_http_requests'], 1)
        self.assertEqual((result['input_tokens'], result['output_tokens']), (7, 5))
        self.assertNotIn('fixture-only-not-a-real-key', json.dumps(result))

    def test_actual_randomized_conditions_drive_behavior_oracle(self):
        for _ in range(3):
            scenario = novel_scenario()
            expression = f"not is_internal or (cohort == {scenario['cohort']!r} and calls > {scenario['threshold']})"
            self.assertEqual(len(check_expression(expression, scenario)), 16)
            with self.assertRaisesRegex(AssertionError, 'contradicts'):
                check_expression(expression.replace(' > ', ' >= '), scenario)
            with self.assertRaises(AssertionError):
                check_expression("__import__('os').system('whoami')", scenario)

    def test_adaptive_followup_requires_new_grounded_environment_caveat(self):
        scenario = novel_scenario()
        quote = f"I have not decided whether the {scenario['staging']} environment should use that exception."
        guidance = {'mode': 'model-assisted', 'status': 'unapproved', 'question_quote': quote,
                    'question': f"Should {scenario['staging']} use the production exception?",
                    'caveats': [{'text': 'Staging remains unresolved', 'quote': quote}]}
        check_guidance(guidance, scenario)
        with self.assertRaises(AssertionError):
            check_guidance({**guidance, 'question_quote': 'invented evidence'}, scenario)
        with self.assertRaises(AssertionError):
            check_guidance({**guidance, 'question': 'What is your name?'}, scenario)


if __name__ == '__main__':
    unittest.main()
