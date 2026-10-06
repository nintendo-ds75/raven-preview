"""Bounded advisory output is a report, never an exhaustive proof or silent pass.

The truncation shapes replay observed 9,200/10,400-token review exhaustion.
Only status/usage/block kinds are retained; block bodies here are synthetic.
No test calls a provider or treats these fixtures as semantic model evidence.
"""
import json
import os
import unittest
from unittest.mock import patch

from bridge import llm
from bridge.config import Config


DIFF = ('diff --git a/processor.py b/processor.py\n--- a/processor.py\n+++ b/processor.py\n'
        '@@ -1 +1,8 @@\n-def apply_events(pending, events): pass\n'
        '+def apply_events(pending, events):\n+    applied = []\n+    for key in events:\n'
        '+        if key in pending:\n+            del pending[key]\n+            applied.append(key)\n'
        '+    return tuple(applied)\n')
FIXED_DIFF = DIFF.replace('+    applied = []', '+    applied = []\n+    seen = set()').replace(
    '+        if key in pending:', '+        if key in seen:\n+            continue\n'
    '+        seen.add(key)\n+        if key in pending:')
COUNTEREXAMPLE = {'n': 1, 'counterexample': 'A generator yields x, reinserts x, then yields x again; x is applied twice.',
                  'at': 'if key in pending:', 'not_shown': ''}
FIRST = {'status': 'complete', 'requirements': [
    {'needs': 'Ignore later duplicate identifiers', 'kind': 'must', 'found': 'honored', 'at': 'if key in pending:'}],
    'why': 'The membership guard skips repeats.'}
CLEAN = {'status': 'complete', 'checks': [], 'exits': []}
CONDITIONS = {'status': 'complete', 'conditions': []}


class BoundedTransportTests(unittest.TestCase):
    def setUp(self):
        self.saved_thinks = set(llm._THINKS)
        llm._THINKS.clear()
        self.addCleanup(lambda: (llm._THINKS.clear(), llm._THINKS.update(self.saved_thinks)))
        env = patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only-not-a-real-key'})
        env.start()
        self.addCleanup(env.stop)
        self.client = llm.Client(Config(model_api='anthropic', model='test-thinking-model'))
        self.requests = []

    def replies(self, payloads):
        def post(url, headers, body, timeout=120):
            self.requests.append((body['max_tokens'], timeout))
            return payloads.pop(0)
        self.client._post_json = post

    @staticmethod
    def exhausted(tokens, text='{"checks": ['):
        return {'stop_reason': 'max_tokens', 'usage': {'output_tokens': tokens},
                'content': [{'type': 'thinking', 'thinking': ''}, {'type': 'text', 'text': text}]}

    def test_warm_truncation_never_repeats_the_observed_10400_token_attempt(self):
        llm._THINKS.add(self.client.cfg.model)
        for token_count in (9200, 10400):
            with self.subTest(observed_output_tokens=token_count):
                self.requests.clear()
                self.replies([self.exhausted(token_count)])
                with self.assertRaisesRegex(llm.LLMError, 'inconclusive'):
                    self.client.complete_json('counterexample', 's', 'p', max_tokens=1200, bounded=True)
                self.assertEqual(self.requests, [(9200, 120)])

    def test_cold_thinking_discovery_is_retained_without_a_third_attempt(self):
        self.replies([self.exhausted(1200, ''), self.exhausted(9200)])
        with self.assertRaisesRegex(llm.LLMError, 'inconclusive'):
            self.client.complete_json('conformance', 's', 'p', max_tokens=1200, bounded=True)
        self.assertEqual(self.requests, [(1200, 120), (9200, 120)])
        self.assertIn(self.client.cfg.model, llm._THINKS)

    def test_cold_thinking_discovery_can_still_return_a_complete_result(self):
        self.replies([self.exhausted(1200, ''), {'stop_reason': 'end_turn', 'content': [
            {'type': 'thinking', 'thinking': ''}, {'type': 'text', 'text': json.dumps(CLEAN)}]}])
        result = self.client.complete_json('counterexample', 's', 'p', max_tokens=1200, bounded=True)
        self.assertEqual(result, CLEAN)
        self.assertEqual(self.requests, [(1200, 120), (9200, 120)])

    def test_malformed_json_does_not_start_a_second_full_review(self):
        self.replies([{'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': '{"checks": ['}]}])
        with self.assertRaisesRegex(llm.LLMError, 'incomplete JSON'):
            self.client.complete_json('counterexample', 's', 'p', max_tokens=1200, bounded=True)
        self.assertEqual(self.requests, [(1200, 120)])

    def test_text_only_exhaustion_does_not_double_the_answer_budget(self):
        self.replies([{'stop_reason': 'max_tokens', 'content': [{'type': 'text', 'text': '{"checks": ['}]}])
        with self.assertRaisesRegex(llm.LLMError, 'inconclusive'):
            self.client.complete_json('counterexample', 's', 'p', max_tokens=1200, bounded=True)
        self.assertEqual(self.requests, [(1200, 120)])

    def test_cli_malformed_json_uses_the_same_single_attempt_contract(self):
        client = llm.Client(Config(model_api='claude-cli'))
        with patch('bridge.llm._claude_cli', return_value='{"checks": [') as cli:
            with self.assertRaises(llm.LLMError):
                client.complete_json('counterexample', 's', 'p', max_tokens=1200, bounded=True)
        self.assertEqual(cli.call_count, 1)


class BoundedReviewTests(unittest.TestCase):
    def read(self, *, first=FIRST, counter=CLEAN, conditions=CONDITIONS, diff=DIFF):
        self.calls = []

        def complete(client, purpose, system, prompt, **kwargs):
            self.calls.append((purpose, system, prompt, kwargs))
            value = {'conformance': first, 'counterexample': counter, 'conditions': conditions}[purpose]
            if isinstance(value, Exception):
                raise value
            return value
        with patch.object(Config, 'semantic_retrieval', property(lambda self: True)), \
                patch.object(llm.Client, 'complete_json', new=complete):
            return llm.check_conformance(Config(model_api='none'), 'How are repeats handled?',
                                         'Consume events once and ignore later duplicate identifiers.', diff)

    def test_complete_sparse_negative_read_is_accepted(self):
        result = self.read(diff=FIXED_DIFF)
        self.assertEqual(result['verdict'], 'follows')
        self.assertNotIn('incomplete', result)
        self.assertEqual([c[0] for c in self.calls], ['conformance', 'counterexample', 'conditions'])
        for _, _, _, kwargs in self.calls:
            self.assertEqual(kwargs, {'max_tokens': 1200, 'bounded': True})

    def test_empty_checks_without_completion_status_are_not_an_all_clear(self):
        result = self.read(counter={'checks': [], 'exits': []})
        self.assertEqual(result['verdict'], 'unclear')
        self.assertTrue(result['incomplete'])

    def test_completion_status_does_not_rescue_malformed_rows(self):
        for counter in ({'status': 'complete', 'checks': [{'n': 1}]},
                        {'status': 'complete', 'checks': [], 'exits': [{'exit': 1}]}):
            result = self.read(counter=counter)
            self.assertEqual(result['verdict'], 'unclear')
            self.assertTrue(result['incomplete'])

    def test_explicit_inconclusive_does_not_discard_a_concrete_finding(self):
        result = self.read(counter={'status': 'inconclusive', 'checks': [COUNTEREXAMPLE],
                                    'unexamined': 'The exception paths could not be checked.'})
        self.assertTrue(result['incomplete'])
        self.assertTrue(result['requirements'][0]['counterexample']['located'])
        self.assertIn('generator', result['why'])
        self.assertIn('The exception paths could not be checked.', result['unexamined'])

    def test_oversized_reply_preserves_independent_valid_finding_without_all_clear(self):
        result = self.read(counter={'status': 'complete', 'checks': [COUNTEREXAMPLE],
                                    'unexpected': 'x' * (llm.REVIEW_MAX_CHARS + 1)})
        self.assertEqual(result['verdict'], 'unclear')
        self.assertTrue(result['incomplete'])
        self.assertTrue(result['requirements'][0]['counterexample']['located'])
        self.assertLess(len(json.dumps(result)), llm.REVIEW_MAX_CHARS)

    def test_oversized_quote_does_not_hide_a_valid_partial_finding(self):
        result = self.read(counter={'status': 'complete', 'checks': [
            COUNTEREXAMPLE, {'n': 1, 'counterexample': 'unsupported', 'at': 'x' * 10000}]})
        self.assertTrue(result['incomplete'])
        self.assertTrue(result['requirements'][0]['counterexample']['located'])

    def test_empty_legacy_entries_do_not_crowd_out_a_valid_partial_finding(self):
        checks = [{'n': 1, 'counterexample': ''} for _ in range(10)] + [COUNTEREXAMPLE]
        result = self.read(counter={'status': 'complete', 'checks': checks})
        self.assertTrue(result['incomplete'])
        self.assertTrue(result['requirements'][0]['counterexample']['located'])

    def test_unlocated_duplicate_does_not_overwrite_a_located_finding(self):
        result = self.read(counter={'status': 'complete', 'checks': [COUNTEREXAMPLE,
            {**COUNTEREXAMPLE, 'at': 'if not_shown():', 'counterexample': 'A speculative second path.'}]})
        self.assertTrue(result['requirements'][0]['counterexample']['located'])
        self.assertIn('generator', result['requirements'][0]['counterexample']['what'])

    def test_finding_limit_is_not_a_requirement_limit(self):
        first = {**FIRST, 'requirements': [{**FIRST['requirements'][0], 'needs': f'Rule {n}'}
                                           for n in range(1, 13)]}
        result = self.read(first=first)
        self.assertEqual(result['verdict'], 'follows')
        self.assertEqual(len(result['requirements']), 12)
        counter_prompt = next(row[2] for row in self.calls if row[0] == 'counterexample')
        self.assertIn('12. Rule 12', counter_prompt)
        self.assertIn('1. Rule 1', counter_prompt)

    def test_overflowing_findings_must_report_inconclusive(self):
        first = {**FIRST, 'requirements': [{**FIRST['requirements'][0], 'needs': f'Rule {n}'}
                                           for n in range(1, 7)]}
        checks = [{**COUNTEREXAMPLE, 'n': n} for n in range(1, 7)]
        result = self.read(first=first, counter={'status': 'complete', 'checks': checks})
        self.assertTrue(result['incomplete'])
        self.assertEqual(result['verdict'], 'unclear')
        self.assertEqual(sum('counterexample' in item for item in result['requirements']), llm.REVIEW_MAX_FINDINGS)
        self.assertTrue(all(item['state'] == 'unclear' for item in result['requirements']))

    def test_first_stage_failure_is_an_explicit_retryable_inconclusive_result(self):
        for first in (llm.LLMError('exhausted'), {}, {'status': 'complete', 'requirements': 'none'},
                      {'status': 'inconclusive', 'requirements': []}):
            with self.subTest(first_type=type(first).__name__):
                result = self.read(first=first)
                self.assertEqual(result['verdict'], 'unclear')
                self.assertTrue(result['incomplete'])
                self.assertIn('inconclusive', result['why'])
                self.assertEqual(len(self.calls), 1)

    def test_incomplete_first_stage_preserves_valid_departures(self):
        first = {**FIRST, 'status': 'inconclusive', 'requirements': [
            {**FIRST['requirements'][0], 'found': 'violated'}, None],
            'unexamined': 'The rest of the signed requirements were not examined.'}
        result = self.read(first=first)
        self.assertEqual(result['verdict'], 'departs')
        self.assertTrue(result['incomplete'])
        self.assertEqual(result['requirements'][0]['state'], 'departs')

    def test_malformed_conditions_preserve_valid_partial_departure(self):
        result = self.read(conditions={'status': 'complete', 'conditions': [None,
            {'condition': 'Requires a present identifier instead of recording attempts', 'at': 'if key in pending:'}]})
        self.assertTrue(result['incomplete'])
        self.assertEqual(result['verdict'], 'departs')
        self.assertIn('the diff adds a condition', result['requirements'][-1]['note'])

    def test_incomplete_conditions_cannot_leave_a_reassuring_reason(self):
        result = self.read(conditions={'status': 'inconclusive', 'conditions': [],
                                       'unexamined': 'The exemption branches were not examined.'})
        self.assertEqual(result['verdict'], 'unclear')
        self.assertTrue(result['incomplete'])
        self.assertIn('inconclusive', result['why'])
        self.assertIn('exemption branches', result['why'])

    def test_not_shown_is_unknown_not_a_successful_negative_search(self):
        result = self.read(counter={'status': 'complete', 'checks': [
            {'n': 1, 'not_shown': 'The caller that controls iterator advancement.'}]})
        self.assertEqual(result['verdict'], 'unclear')
        self.assertEqual(result['requirements'][0]['state'], 'unclear')
        self.assertIn('inconclusive', result['why'])

    def test_review_prompts_bound_outputs_without_skipping_requirements(self):
        for prompt in (llm.CONFORMANCE_SYSTEM, llm.COUNTEREXAMPLE_SYSTEM, llm.UNSTATED_CONDITIONS_SYSTEM):
            self.assertIn('Examine every signed requirement', prompt)
            self.assertIn('at most 6000 characters', prompt)
            self.assertIn('inconclusive', prompt)
            self.assertIn('exact contiguous fragment', prompt)
        self.assertIn('one focused adversarial pass', llm.COUNTEREXAMPLE_SYSTEM)
        self.assertIn('omitted requirements and exits were examined too', llm.COUNTEREXAMPLE_SYSTEM)

    def test_temporal_controls_have_different_executable_behavior(self):
        def compile_added(diff):
            code = '\n'.join(line[1:] for line in diff.splitlines() if line.startswith('+') and not line.startswith('+++'))
            namespace = {}
            exec(code, namespace)
            return namespace['apply_events']

        for diff, expected in ((DIFF, ('x', 'x')), (FIXED_DIFF, ('x',))):
            pending = {'x': True}
            def events():
                yield 'x'
                pending['x'] = True
                yield 'x'
            self.assertEqual(compile_added(diff)(pending, events()), expected)
        for diff, expected in ((DIFF, ('x',)), (FIXED_DIFF, ())):
            pending = {}
            def events_after_miss():
                yield 'x'
                pending['x'] = True
                yield 'x'
            self.assertEqual(compile_added(diff)(pending, events_after_miss()), expected)
        buggy = self.read(counter={'status': 'complete', 'checks': [COUNTEREXAMPLE]}, diff=DIFF)
        fixed = self.read(diff=FIXED_DIFF)
        self.assertEqual(buggy['verdict'], 'unclear')
        self.assertTrue(buggy['requirements'][0]['counterexample']['located'])
        self.assertEqual(fixed['verdict'], 'follows')
