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
    PROVIDER_URL, AcceptanceFailure, RequestBudget, check_expression, check_guidance,
    human_post, interview_model_observation, novel_scenario, observe_interview_model,
    record_interview_guidance, reuse_scenario, run, run_interview_only,
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

    def test_budget_forwards_timeout_and_opener_arguments_unchanged(self):
        request = urllib.request.Request(PROVIDER_URL, data=b'{}')
        for args, kwargs in (((), {'timeout': 120}), ((), {'timeout': 7}),
                             ((), {}), ((None, 120), {})):
            with self.subTest(args=args, kwargs=kwargs):
                with patch('urllib.request.urlopen', return_value=Response(b'{}')) as opener:
                    budget = RequestBudget(1, opener=opener)
                    with budget.open(request, *args, **kwargs) as response:
                        self.assertEqual(response.read(), b'{}')
                    opener.assert_called_once_with(request, *args, **kwargs)

    def test_budget_reports_fixed_transport_metrics_without_exception_details(self):
        for error, kind, status in (
                (TimeoutError('private-timeout-detail'), 'timeout', None),
                (urllib.error.URLError(TimeoutError('private-nested-detail')), 'timeout', None),
                (urllib.error.URLError('private-network-detail'), 'network', None),
                (urllib.error.HTTPError(PROVIDER_URL, 429, 'private-http-detail', {}, None), 'http', 429)):
            with self.subTest(kind=kind, status=status):
                with patch('urllib.request.urlopen', side_effect=error), \
                     patch('evals.live_workflow_acceptance.time.monotonic', side_effect=[10, 13.5]):
                    budget = RequestBudget(1)
                    with self.assertRaises(type(error)) as caught:
                        budget.open(PROVIDER_URL, timeout=120)
                self.assertIs(caught.exception, error)
                self.assertEqual(budget.summary()['http_attempts'], [
                    {'status': status, 'elapsed_seconds': 3.5, 'error_kind': kind}])
                self.assertNotIn('private-', json.dumps(budget.summary()))

    def test_budget_observes_read_timeouts_without_swallowing_them(self):
        error = TimeoutError('private-read-detail')
        class BrokenResponse(Response):
            def read(self, *args, **kwargs):
                raise error
        budget = RequestBudget(1, opener=lambda *_, **__: BrokenResponse(b''))
        with patch('evals.live_workflow_acceptance.time.monotonic', side_effect=[10, 11, 14]):
            with budget.open(PROVIDER_URL, timeout=120) as response:
                with self.assertRaises(TimeoutError) as caught:
                    response.read()
        self.assertIs(caught.exception, error)
        self.assertEqual(budget.summary()['http_attempts'], [
            {'status': 200, 'elapsed_seconds': 4, 'error_kind': 'timeout'}])
        self.assertNotIn('private-read-detail', json.dumps(budget.summary()))

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
            with self.assertRaisesRegex(AssertionError, 'contradicts_signed_boundary'):
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


    def test_failed_interview_keeps_sanitized_guidance_scenario_and_fixed_code(self):
        secret = 'fixture-key-never-print-this'
        class Harness:
            def __init__(self, **kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
        def workflow(harness, agent, scenario, report, timeout):
            report['stage'] = 'adaptive_interview'
            report['interview_http'] = [{'operation': 'advance', 'status': 200}]
            quote = f"I have not decided whether the {scenario['staging']} environment should use that exception."
            record_interview_guidance({'mode': 'model-assisted', 'status': 'unapproved',
                'question': 'What is your name?', 'question_quote': quote,
                'proposed_answer': secret, 'caveats': [{'text': 'Unresolved staging', 'quote': quote}],
                'unexpected_header': 'must-not-copy-this-field'}, scenario, report)
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': secret, 'BRIDGE_MODEL_API': 'anthropic',
                                    'BRIDGE_SEMANTIC': '1'}), \
             patch('urllib.request.urlopen', side_effect=AssertionError('Network forbidden')) as network:
            result = run(run_live=True, harness_factory=Harness, workflow=workflow)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['failure_code'], 'interview_question_misses_novel_environment')
        self.assertEqual(result['stage'], 'adaptive_interview')
        self.assertEqual(result['interview']['status'], 'unapproved')
        self.assertEqual(result['interview']['question'], 'What is your name?')
        self.assertIn(result['scenario']['staging'], result['interview']['question_quote'])
        self.assertEqual(result['interview']['proposed_answer'], '[redacted]')
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn('unexpected_header', json.dumps(result))
        self.assertNotIn('must-not-copy-this-field', json.dumps(result))
        network.assert_not_called()

    def test_interview_http_failure_records_status_without_body_headers_or_token(self):
        class Harness:
            url = 'http://127.0.0.1:1234'
            request_timeout = 15
        report = {}
        with patch('evals.live_workflow_acceptance.http_json', return_value=(403,
                   {'error': 'raw-provider-detail-canary'}, {'X-Diagnostic': 'private-header-canary'})):
            with self.assertRaisesRegex(AcceptanceFailure, 'interview_http_refused'):
                human_post(Harness(), 'human-token-canary', '/api/tasks/fixture/interviews', {},
                           report=report, operation='create')
        self.assertEqual(report, {'interview_http': [{'operation': 'create', 'status': 403}]})
        for excluded in ('raw-provider-detail-canary', 'private-header-canary', 'human-token-canary'):
            self.assertNotIn(excluded, json.dumps(report))

    def test_production_validator_fallback_is_preserved_without_weakening_rejection(self):
        report = {}
        with self.assertRaisesRegex(AcceptanceFailure, 'interview_not_validated_unapproved_model_draft'):
            record_interview_guidance({'mode': 'deterministic-guided-prompts',
                'reason': 'invalid_model_response'}, novel_scenario(), report)
        self.assertEqual(report['interview'], {'mode': 'deterministic-guided-prompts',
                                              'reason': 'invalid_model_response'})


    def test_previous_readback_reuses_known_values_and_discloses_missing_nonce(self):
        prior = {'readback': 'For cohort pilot_e46c6eb4: more than 795 calls remains billable; at or below 795 calls excluded.'}
        scenario, provenance = reuse_scenario(prior)
        self.assertEqual((scenario['cohort'], scenario['threshold']), ('pilot_e46c6eb4', 795))
        self.assertFalse(provenance['staging_reused'])
        self.assertIn('fresh staging nonce', provenance['note'])
        self.assertRegex(scenario['staging'], r'^staging_[0-9a-f]{8}$')
        preserved, provenance = reuse_scenario({'scenario': scenario})
        self.assertEqual(preserved, scenario)
        self.assertTrue(provenance['staging_reused'])
        with self.assertRaises(AcceptanceFailure):
            reuse_scenario({'readback': 'The threshold might be unknown.'})

    def test_interview_only_driver_uses_real_http_and_one_injected_provider_request(self):
        import re
        import urllib.parse
        from bridge import canvas
        from evals.slack_contract_harness import REFERRED
        actual_open = urllib.request.urlopen
        provider_requests = []
        def gateway(request, *args, **kwargs):
            url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
            if url != PROVIDER_URL:
                self.assertIn(urllib.parse.urlsplit(url).hostname, ('127.0.0.1', 'localhost', '::1'))
                return actual_open(request, *args, **kwargs)
            provider_requests.append(url)  # No headers or credential-bearing request object retained.
            payload = json.loads(request.data)
            prompt = json.loads(payload['messages'][0]['content'])
            staging = re.search(r'staging_[0-9a-f]{8}', prompt['transcript'])[0]
            quote = f"I have not decided whether the {staging} environment should use that exception."
            output = {'question': f"Should {staging} use the production exception?",
                      'question_quote': quote, 'proposed_answer': '', 'proposed_rationale': '',
                      'answer_quotes': [], 'caveats': [{'text': 'Staging remains unresolved', 'quote': quote}]}
            return Response(json.dumps({'type': 'message', 'stop_reason': 'end_turn',
                'content': [{'type': 'text', 'text': json.dumps(output)}],
                'usage': {'input_tokens': 33, 'output_tokens': 42}}).encode())
        def driver(harness, agent, scenario, report, timeout):
            original_id = harness.store.graph.find_person(REFERRED)['id']
            self.assertFalse(canvas.background_running())
            result = run_interview_only(harness, agent, scenario, report, timeout=timeout)
            self.assertEqual(harness.store.graph.find_person(REFERRED)['id'], original_id)
            self.assertEqual(sum(p['name'] == 'Marisol Contract' for p in harness.store.graph.people()), 1)
            row = harness.store.graph.db.execute('SELECT d.owner_id,o.person_id FROM decisions d '
                                                 'JOIN owners o ON o.id=d.owner_id').fetchone()
            self.assertEqual(row['person_id'], original_id)
            self.assertFalse(canvas.background_running())
            return result
        previous = {'readback': 'Cohort pilot_e46c6eb4: more than 795 calls is billable; at or below 795 is excluded.'}
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'fixture-only-not-a-real-key',
                                    'BRIDGE_MODEL_API': 'anthropic', 'BRIDGE_SEMANTIC': '1'}), \
             patch('urllib.request.urlopen', side_effect=gateway):
            report = run(run_live=True, interview_only=True, previous_result=previous, workflow=driver)
        self.assertEqual(report['status'], 'passed', report)
        self.assertEqual(report['stage'], 'interview_diagnostic_complete')
        self.assertEqual(report['physical_http_requests'], 1)
        self.assertEqual(report['max_physical_http_requests'], 3)
        self.assertEqual(provider_requests, [PROVIDER_URL])
        self.assertEqual((report['input_tokens'], report['output_tokens']), (33, 42))
        self.assertTrue(report['diagnostic_only'])
        self.assertTrue(report['diagnostic_setup']['simulated'])
        self.assertTrue(report['diagnostic_setup']['unsigned'])
        self.assertEqual(report['interview_http'], [{'operation': step, 'status': 200}
                                                  for step in ('create', 'draft', 'advance')])
        self.assertEqual(report['interview']['status'], 'unapproved')
        self.assertNotIn('fixture-only-not-a-real-key', json.dumps(report))

    def test_non_object_guidance_has_fixed_code_and_safe_shape(self):
        for guidance in (None, [], 'not-an-object'):
            report = {}
            with self.assertRaisesRegex(AcceptanceFailure, 'interview_guidance_not_object'):
                record_interview_guidance(guidance, novel_scenario(), report)
            self.assertEqual(report['interview'], {'shape': type(guidance).__name__})


    def test_transparent_observer_returns_identical_result_without_extra_inference(self):
        from bridge.config import Config
        raw = {'question': 'Which staging environment?', 'question_quote': 'not retained in supplied input',
               'answer_quotes': ['fixture quote'], 'caveats': [{'text': 'Fixture caveat', 'quote': 'fixture quote'}],
               'wrong_extra_key': 'unexpected-value-not-copied'}
        report = {}
        with patch.object(llm.Client, 'complete_json', return_value=raw) as original:
            with observe_interview_model(report):
                client = llm.Client(Config(model_api='none'))
                returned = client.complete_json('interview_followup', 'system-canary-not-logged', 'prompt-canary-not-logged')
                self.assertIs(returned, raw)
                self.assertIs(client.complete_json('other_purpose', 'system', 'prompt'), raw)
        self.assertEqual(original.call_count, 2)
        observed = report['interview_model_observations']
        self.assertEqual(len(observed), 1)
        self.assertIn({'name': 'wrong_extra_key', 'type': 'str'}, observed[0]['fields'])
        self.assertEqual(observed[0]['values']['question_quote']['value'], raw['question_quote'])
        serialized = json.dumps(report)
        for excluded in ('unexpected-value-not-copied', 'prompt-canary-not-logged', 'system-canary-not-logged'):
            self.assertNotIn(excluded, serialized)

    def test_model_observer_is_bounded_and_does_not_record_provider_errors(self):
        raw = {'question': 'q' * 5000, 'answer_quotes': ['x' * 3000] * 100,
               'caveats': [{'text': 't', 'quote': 'q', 'secret_extra_field': 'never-copy'}] * 100}
        observation = interview_model_observation(raw)
        self.assertEqual(observation['values']['question']['length'], 5000)
        self.assertEqual(len(observation['values']['question']['value']), 2048)
        self.assertTrue(observation['values']['question']['truncated'])
        self.assertEqual(len(observation['values']['answer_quotes']['items']), 12)
        self.assertNotIn('never-copy', json.dumps(observation))
        from bridge.config import Config
        report = {}
        with patch.object(llm.Client, 'complete_json', side_effect=llm.LLMError('provider-error-canary')):
            with observe_interview_model(report), self.assertRaises(llm.LLMError):
                llm.Client(Config(model_api='none')).complete_json('interview_followup', 's', 'p')
        self.assertEqual(report, {})

    def test_production_validation_detail_code_survives_safe_guidance_projection(self):
        report = {}
        with self.assertRaises(AcceptanceFailure):
            record_interview_guidance({'mode': 'deterministic-guided-prompts',
                'reason': 'invalid_model_response', 'validation_error': 'fixture_grounding_error'},
                novel_scenario(), report)
        self.assertEqual(report['interview']['validation_error'], 'fixture_grounding_error')


    def test_model_observer_redacts_before_clipping_credentials(self):
        secret = 'fixture-key-at-the-clipping-boundary'
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': secret}):
            observed = interview_model_observation({'question': 'x' * 2040 + secret,
                                                    secret: 'unknown field value'})
        text = json.dumps(observed)
        self.assertNotIn(secret, text)
        self.assertNotIn('fixture-', text)
        self.assertIn('[redacted]', text)


if __name__ == '__main__':
    unittest.main()
