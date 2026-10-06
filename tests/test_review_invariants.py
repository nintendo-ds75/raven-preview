"""Advisory reviews must challenge temporal claims and reject malformed search output."""
import unittest
from unittest.mock import patch

from bridge import llm
from bridge.config import Config

DIFF = ('diff --git a/processor.py b/processor.py\n--- a/processor.py\n+++ b/processor.py\n'
        '@@ -1 +1,8 @@\n-def apply_events(pending, events): pass\n'
        '+def apply_events(pending, events):\n+    applied = []\n+    for key in events:\n'
        '+        if key in pending:\n+            del pending[key]\n+            applied.append(key)\n'
        '+    return tuple(applied)\n')


class ReviewInvariantTests(unittest.TestCase):
    def read(self, counterexample):
        def complete(client, purpose, *args, **kwargs):
            if purpose == 'conformance':
                return {'status': 'complete', 'requirements': [{'needs': 'Ignore later duplicate identifiers', 'kind': 'must',
                    'at': 'if key in pending:', 'found': 'honored'}], 'why': 'The membership guard skips later occurrences.'}
            if purpose == 'counterexample':
                return counterexample
            return {'status': 'complete', 'conditions': []}
        with patch.object(Config, 'semantic_retrieval', property(lambda self: True)), \
                patch.object(llm.Client, 'complete_json', new=complete):
            return llm.check_conformance(Config(model_api='none'), 'How should repeated events be handled?',
                'Consume the input once in order and ignore later duplicate identifiers.', DIFF)

    def test_missing_or_wrong_check_collection_is_inconclusive(self):
        for raw in ({}, {'exits': []}, {'checks': 'none'}, {'checks': None}, {'checks': [None]}):
            with self.subTest(raw=raw):
                result = self.read(raw)
                self.assertEqual(result['verdict'], 'unclear')
                self.assertIn('did not complete', result['why'])

    def test_invalid_number_or_text_cannot_silently_pass_counterexample_search(self):
        for check in ({'n': 0}, {'n': 2}, {'n': 'unknown'}, {'n': 1, 'counterexample': []},
                      {'n': 1, 'at': {}}, {'n': 1, 'not_shown': False}):
            with self.subTest(check=check):
                self.assertEqual(self.read({'checks': [check]})['verdict'], 'unclear')

    def test_invalid_exit_collection_is_inconclusive(self):
        self.assertEqual(self.read({'checks': [], 'exits': 'none'})['verdict'], 'unclear')

    def test_located_state_change_counterexample_overrides_initial_honored_claim(self):
        result = self.read({'status': 'complete', 'checks': [{'n': 1,
            'counterexample': 'A lazy iterator yields x, reinserts x into pending, then yields x again; both occurrences are applied.',
            'at': 'if key in pending:', 'not_shown': ''}], 'exits': []})
        self.assertEqual(result['verdict'], 'unclear')
        self.assertTrue(result['requirements'][0]['counterexample']['located'])
        self.assertIn('reinserts', result['why'])

    def test_valid_counterexample_survives_another_malformed_item(self):
        result = self.read({'status': 'complete', 'checks': [
            {'n': 1, 'counterexample': 'An iterator reinserts an already seen identifier before yielding it again.',
             'at': 'if key in pending:', 'not_shown': ''},
            {'n': 'unknown'}], 'exits': []})
        self.assertEqual(result['verdict'], 'unclear')
        self.assertTrue(result['requirements'][0]['counterexample']['located'])
        self.assertIn('reinserts', result['why'])

    def test_well_formed_negative_search_is_still_advisory_not_automatic_failure(self):
        result = self.read({'status': 'complete', 'checks': [{'n': 1, 'counterexample': '', 'at': '', 'not_shown': ''}], 'exits': []})
        self.assertEqual(result['verdict'], 'follows')

    def test_review_prompts_require_history_and_mutable_state_reasoning(self):
        for prompt in (llm.CONFORMANCE_SYSTEM, llm.COUNTEREXAMPLE_SYSTEM):
            with self.subTest(prompt=prompt[:30]):
                self.assertIn('mutable state', prompt)
                self.assertIn('iterator', prompt)
                self.assertIn('once', prompt)
                self.assertIn('seen', prompt)
