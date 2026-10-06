"""Verified Sonnet 5 effort control is scoped to the bounded advisory API path."""
import os
import unittest
from unittest.mock import patch

from bridge import llm
from bridge.config import Config

WHOLE = {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': '{"status":"complete"}'}]}
CUT = {'stop_reason': 'max_tokens', 'content': [{'type': 'thinking', 'thinking': ''}],
       'usage': {'output_tokens': 9200}}


class ReviewEffortTests(unittest.TestCase):
    def setUp(self):
        old_thinks = set(llm._THINKS)
        llm._THINKS.clear()
        self.addCleanup(lambda: (llm._THINKS.clear(), llm._THINKS.update(old_thinks)))
        env = patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only-not-a-real-key'})
        env.start()
        self.addCleanup(env.stop)
        self.requests = []

    def client(self, model='claude-sonnet-5', backend='anthropic', replies=None):
        client = llm.Client(Config(model_api=backend, model=model))
        pending = list(replies or [WHOLE])

        def post(url, headers, body, timeout=120):
            self.requests.append({'body': body, 'timeout': timeout, 'beta_header': headers.get('anthropic-beta')})
            reply = pending.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        client._post_json = post
        return client

    def test_only_bounded_advisory_sonnet5_requests_add_the_supported_effort_field(self):
        for purpose in ('conformance', 'counterexample', 'conditions'):
            with self.subTest(purpose=purpose):
                client = self.client()
                result = client.complete_json(purpose, 'system', 'prompt', max_tokens=1200, bounded=True)
                self.assertEqual(result, {'status': 'complete'})
                self.assertEqual(self.requests[-1], {'body': {
                    'model': 'claude-sonnet-5', 'max_tokens': 1200, 'system': 'system',
                    'messages': [{'role': 'user', 'content': 'prompt'}], 'output_config': {'effort': 'medium'}},
                    'timeout': 120, 'beta_header': None})

    def test_non_review_or_unbounded_calls_keep_the_previous_body(self):
        for purpose, bounded in (('brief', True), ('compose', False), ('counterexample', False)):
            with self.subTest(purpose=purpose, bounded=bounded):
                self.client().complete_json(purpose, 's', 'p', max_tokens=1200, bounded=bounded)
                self.assertNotIn('output_config', self.requests[-1]['body'])
                self.assertNotIn('thinking', self.requests[-1]['body'])

    def test_unverified_models_and_other_backends_are_unchanged(self):
        models = ('claude-sonnet-4-6', 'claude-haiku-4-5-20251001', 'claude-opus-4-8',
                  'claude-sonnet-5-5', 'claude-sonnet-5-20260630', 'future-model')
        for model in models:
            with self.subTest(model=model):
                self.client(model=model).complete_json('counterexample', 's', 'p', max_tokens=1200, bounded=True)
                self.assertNotIn('output_config', self.requests[-1]['body'])
        self.client(backend='other-provider').complete_json('counterexample', 's', 'p', max_tokens=1200, bounded=True)
        self.assertNotIn('output_config', self.requests[-1]['body'])

    def test_cli_receives_no_new_options(self):
        client = self.client(backend='claude-cli')
        with patch('bridge.llm._claude_cli', return_value='{"status":"complete"}') as cli:
            client.complete_json('counterexample', 's', 'p', max_tokens=1200, bounded=True)
        cli.assert_called_once_with('s', 'p', 'claude-sonnet-5')
        self.assertEqual(self.requests, [])

    def test_thinking_discovery_preserves_medium_without_raising_the_existing_cap(self):
        client = self.client(replies=[CUT, WHOLE])
        client.complete_json('counterexample', 's', 'p', max_tokens=1200, bounded=True)
        self.assertEqual([r['body']['max_tokens'] for r in self.requests], [1200, 9200])
        self.assertTrue(all(r['body']['output_config'] == {'effort': 'medium'} for r in self.requests))
        self.assertTrue(all('thinking' not in r['body'] for r in self.requests))
        self.assertEqual((llm.REVIEW_MAX_TOKENS, llm.THINKING_ROOM), (1200, 8000))

    def test_medium_effort_still_treats_exhaustion_as_inconclusive(self):
        llm._THINKS.add('claude-sonnet-5')
        with self.assertRaisesRegex(llm.LLMError, 'inconclusive'):
            self.client(replies=[CUT]).complete_json('counterexample', 's', 'p', max_tokens=1200, bounded=True)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]['body']['max_tokens'], 9200)
        self.assertEqual(self.requests[0]['body']['output_config'], {'effort': 'medium'})

    def test_rejected_request_does_not_silently_retry_at_uncontrolled_effort(self):
        with self.assertRaises(llm.LLMError):
            self.client(replies=[llm.LLMError('model API error 400')]).complete_json(
                'counterexample', 's', 'p', max_tokens=1200, bounded=True)
        self.assertEqual(len(self.requests), 1)

    def test_critic_has_a_finite_stop_rule_and_retains_full_requirement_coverage(self):
        prompt = llm.COUNTEREXAMPLE_STOP_RULE
        for phrase in ('For every requirement', 'at most three candidate traces',
                       'then move to the next requirement', 'each listed early exit once',
                       'no second verification pass', 'unexamined and use inconclusive',
                       'not exhaustive path coverage', 'permitted temporal state changes'):
            self.assertIn(phrase, prompt)

    def test_finite_stop_prompt_is_scoped_to_the_same_verified_api_path(self):
        configs = ((Config(model_api='anthropic', model='claude-sonnet-5'), True),
                   (Config(model_api='anthropic', model='claude-sonnet-5-5'), False),
                   (Config(model_api='claude-cli', model='claude-sonnet-5'), False),
                   (Config(model_api='other-provider', model='claude-sonnet-5'), False))
        for cfg, expected in configs:
            with self.subTest(model=cfg.model, backend=cfg.model_api), \
                    patch.object(llm.Client, 'complete_json', return_value={'status': 'complete', 'checks': []}) as read:
                llm._counterexamples(cfg, 'context', '+guard', 'guard', [
                    {'state': 'ok', 'kind': 'must', 'needs': 'Required behavior'}])
            expected_system = llm.COUNTEREXAMPLE_SYSTEM + (llm.COUNTEREXAMPLE_STOP_RULE if expected else '')
            self.assertEqual(read.call_args.args[1], expected_system)
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': ''}):
            self.assertIsNone(llm._review_effort(Config(model='claude-sonnet-5'), 'counterexample', True))
