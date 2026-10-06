"""Grounded allegations remain advisory; implementation choices are not policy."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from bridge import llm
from review_source_fixtures import source_conformance
from bridge.config import Config

ANSWER = 'Consume events once in input order, ignore missing keys and later duplicate identifiers, and return removed keys in first-removal order.'
BUG = ('diff --git a/processor.py b/processor.py\n+++ b/processor.py\n@@ -0,0 +1,8 @@\n'
       '+def apply_events(pending, events):\n+    removed = []\n+    for key in events:\n'
       '+        if key in pending:\n+            del pending[key]\n+            removed.append(key)\n'
       '+    return tuple(removed)\n')
SET = BUG.replace('+    removed = []', '+    removed = []\n+    visited = set()').replace(
    '+        if key in pending:', '+        if key in visited: continue\n+        visited.add(key)\n+        if key in pending:')
DICT = SET.replace('visited = set()', 'visited = {}').replace('visited.add(key)', 'visited[key] = True')
WITNESS = {'kind': 'behavioral', 'authorized': 'ignore missing keys and later duplicate identifiers',
           'input': "pending contains x; call apply_events(pending, a lazy generator)",
           'sequence': ['Yield x and let its first removal finish.', 'Reinsert x into pending before the generator resumes.', 'Yield x again.'],
           'expected': 'Only the first x is removed; the result is (x,).',
           'observed': 'Both occurrences remove x; the result is (x, x).'}
FIRST = {'status': 'complete', 'requirements': [{'needs': 'Ignore later duplicate identifiers', 'kind': 'must',
                                               'found': 'honored', 'at': 'if key in pending:'}], 'why': 'A membership check skips duplicates.'}
CLEAN = {'status': 'complete', 'checks': [], 'exits': []}


class BehavioralReviewTests(unittest.TestCase):
    def read(self, first=None, critic=None, diff=BUG, answer=ANSWER, preserve_needs=False):
        self.calls = []
        def complete(client, purpose, system, prompt, **kwargs):
            self.calls.append((purpose, system, prompt))
            if purpose == 'conformance': return deepcopy(first if first is not None else FIRST)
            if purpose == 'counterexample': return deepcopy(critic if critic is not None else CLEAN)
            return {'status': 'complete', 'conditions': []}
        with patch.object(Config, 'semantic_retrieval', property(lambda self: True)), patch.object(llm.Client, 'complete_json', complete):
            return source_conformance(Config(model_api='none'), 'Handle repeats?', answer, diff, preserve_needs=preserve_needs)

    def departure(self, allegation=None):
        raw = deepcopy(FIRST)
        raw['requirements'][0]['found'] = 'violated'
        raw['requirements'][0]['allegation'] = allegation
        raw['why'] = 'An explicit seen-set was specified; ordinary duplicate inputs are processed twice.'
        return raw

    def test_missing_class_quote_cannot_keep_an_all_supported_summary(self):
        first = {'status': 'complete', 'requirements': [{'needs': 'Inherited by processor subclasses',
                 'kind': 'must', 'found': 'honored', 'at': 'class Processor(Mapping):'}],
                 'why': 'All listed requirements are directly supported by the diff.'}
        read = self.read(first=first)
        self.assertEqual(read['verdict'], 'unclear')
        self.assertNotIn('unexamined', read)
        self.assertIn('the line quoted for it is not in the diff', read['why'])
        self.assertNotIn('All listed requirements', read['why'])
        self.assertIn('inconclusive', read['why'])

    def test_captured_seen_set_accusation_is_not_a_behavioral_finding(self):
        read = self.read(first=self.departure())
        self.assertEqual(read['verdict'], 'unclear')
        self.assertEqual(read['requirements'][0]['allegation_status'], 'needs_witness')
        self.assertNotIn('seen-set', read['why'])
        self.assertNotIn('ordinary duplicate inputs', read['why'])
        self.assertIn('counterexample', [call[0] for call in self.calls])

    def test_grounded_behavioral_departure_carries_sequence_and_observation(self):
        read = self.read(first=self.departure(WITNESS))
        self.assertEqual(read['verdict'], 'departs')
        self.assertEqual(read['requirements'][0]['allegation'], WITNESS)
        self.assertIn('Expected:', read['why'])
        self.assertIn('observed:', read['why'])
        self.assertNotIn('seen-set', read['why'])

    def test_a_grounded_negative_uses_the_approved_policy_as_its_label(self):
        first = self.departure(WITNESS)
        first['requirements'][0]['needs'] = 'Use my preferred seen-set implementation'
        read = self.read(first=first)
        self.assertEqual(read['requirements'][0]['needs'], ANSWER)
        self.assertNotIn('seen-set', read['why'])

    def test_critic_can_supply_a_real_temporal_witness_after_an_unsupported_accusation(self):
        read = self.read(first=self.departure(), critic={'status': 'complete', 'checks': [
            {'n': 1, 'assessment': 'alleged', 'at': 'if key in pending:', 'allegation': WITNESS}]})
        self.assertEqual(read['verdict'], 'unclear')
        self.assertEqual(read['requirements'][0]['counterexample']['allegation'], WITNESS)
        self.assertNotIn('allegation_status', read['requirements'][0])
        self.assertIn('needs witness', next(c[2] for c in self.calls if c[0] == 'counterexample'))

    def test_semantically_equivalent_set_and_dict_controls_are_accepted(self):
        for diff in (SET, DICT):
            first = deepcopy(FIRST); first['requirements'][0]['at'] = 'if key in visited: continue'
            with self.subTest(structure='dict' if 'visited = {}' in diff else 'set'):
                self.assertEqual(self.read(first=first, diff=diff)['verdict'], 'follows')

    def test_critic_can_clear_an_invented_set_preference_for_a_dict_control(self):
        allegation = {'kind': 'structural', 'authorized': 'ignore missing keys and later duplicate identifiers',
                      'required': 'set', 'observed': 'A dict is used.'}
        first = self.departure(allegation)
        first['requirements'][0]['needs'] = WITNESS['authorized']
        read = self.read(first=first, diff=DICT, critic={'status': 'complete', 'checks': [
            {'n': 1, 'assessment': 'honored', 'at': 'if key in visited: continue'}]})
        self.assertEqual(read['verdict'], 'follows')
        self.assertNotIn('allegation_status', read['requirements'][0])
        self.assertNotIn('seen-set', read['why'])

    def test_critic_cannot_turn_an_invented_requirement_into_an_honored_policy(self):
        first = self.departure()
        first['requirements'][0]['needs'] = 'Use my preferred seen-set implementation'
        read = self.read(first=first, diff=DICT, preserve_needs=True, critic={'status': 'complete', 'checks': [
            {'n': 1, 'assessment': 'honored', 'at': 'if key in visited: continue'}]})
        self.assertEqual(read['verdict'], 'unclear')
        self.assertEqual(read['requirements'][0]['state'], 'unclear')
        self.assertNotIn('Read as doing each thing', read['why'])

    def test_critic_may_clear_a_disputed_requirement_already_quoted_from_approval(self):
        first = self.departure()
        first['requirements'][0]['needs'] = WITNESS['authorized']
        read = self.read(first=first, diff=DICT, critic={'status': 'complete', 'checks': [
            {'n': 1, 'assessment': 'honored', 'at': 'if key in visited: continue'}]})
        self.assertEqual(read['verdict'], 'follows')
        self.assertEqual(read['requirements'][0]['needs'], ANSWER)

    def test_malformed_honored_claim_cannot_clear_an_unsupported_departure(self):
        read = self.read(first=self.departure(), diff=DICT, critic={'status': 'complete', 'checks': [
            {'n': 1, 'assessment': 'honored', 'at': 'if key in visited: continue', 'allegation': {}}]})
        self.assertEqual(read['verdict'], 'unclear')
        self.assertEqual(read['requirements'][0]['allegation_status'], 'needs_witness')

    def test_negative_code_quotes_must_be_contiguous_not_elided_across_paths(self):
        first = self.departure(WITNESS)
        first['requirements'][0]['at'] = 'if key in pending: ... removed.append(key)'
        self.assertEqual(self.read(first=first)['verdict'], 'unclear')

    def test_ungrounded_or_incomplete_witnesses_remain_unclear(self):
        cases = []
        for field in ('input', 'sequence', 'expected', 'observed', 'authorized'):
            witness = deepcopy(WITNESS); witness.pop(field); cases.append(witness)
        witness = deepcopy(WITNESS); witness['authorized'] = 'Use an explicit seen-set'; cases.append(witness)
        witness = deepcopy(WITNESS); witness['observed'] = witness['expected']; cases.append(witness)
        witness = deepcopy(WITNESS); witness['sequence'] *= 2; cases.append(witness)
        witness = deepcopy(WITNESS); witness['input'] = 'x' * 301; cases.append(witness)
        for witness in cases:
            with self.subTest(fields=tuple(witness)):
                read = self.read(first=self.departure(witness))
                self.assertEqual(read['verdict'], 'unclear')
                self.assertNotIn('allegation', read['requirements'][0])

    def test_legacy_location_and_departs_or_free_text_cannot_count_as_a_witness(self):
        read = self.read(critic={'status': 'complete', 'checks': [
            {'n': 1, 'counterexample': 'Duplicate processing occurs.', 'at': 'if key in pending:'}]})
        self.assertEqual(read['verdict'], 'unclear')
        self.assertNotIn('counterexample', read['requirements'][0])
        self.assertIn('grounded witness', ' '.join(read['unexamined']))

    def test_partial_valid_witness_survives_a_malformed_sibling(self):
        read = self.read(critic={'status': 'complete', 'checks': [
            {'n': 1, 'at': 'if key in pending:', 'allegation': WITNESS}, {'n': 'unknown'}]})
        self.assertTrue(read['incomplete'])
        self.assertEqual(read['requirements'][0]['counterexample']['allegation'], WITNESS)

    def test_truly_explicit_structure_needs_its_exact_approved_literal(self):
        answer = 'Use a set of encountered keys.'
        witness = {'kind': 'structural', 'authorized': answer, 'required': 'set', 'observed': 'Only membership in pending is checked.'}
        read = self.read(first=self.departure(witness), answer=answer)
        self.assertEqual(read['verdict'], 'departs')
        self.assertEqual(read['requirements'][0]['allegation']['kind'], 'structural')
        self.assertFalse(llm._explicit_structure('ignore later duplicate identifiers', 'duplicate'))
        self.assertFalse(llm._explicit_structure('ignore later duplicates.', 'ignore later duplicates.'))
        self.assertFalse(llm._explicit_structure('Return keys and ignore duplicates', 'duplicates'))

    def test_explicit_structural_prohibition_keeps_its_approved_direction_in_summary(self):
        answer = 'Do not use a set of encountered keys.'
        allegation = {'kind': 'structural', 'authorized': answer, 'required': 'set', 'observed': 'The code uses visited = set().'}
        first = self.departure(allegation)
        first['requirements'][0].update(kind='must_not', at='visited = set()')
        read = self.read(first=first, diff=SET, answer=answer)
        self.assertEqual(read['verdict'], 'departs')
        self.assertIn('Approved: Do not use a set', read['why'])
        self.assertNotIn('Requires set', read['why'])

    def test_quote_in_removed_code_or_tests_is_not_grounding(self):
        for at in ('seen.add(key)', 'assert result == (x,)'):
            first = self.departure(WITNESS); first['requirements'][0]['at'] = at
            diff = BUG + '-seen.add(key)\ndiff --git a/tests/test_apply.py b/tests/test_apply.py\n+assert result == (x,)\n'
            self.assertEqual(self.read(first=first, diff=diff)['verdict'], 'unclear')

    def test_trusted_reference_functions_distinguish_original_bug_and_both_controls(self):
        # These are fixed test functions, not execution of a submitted diff.
        def apply(pending, events, history):
            removed = []
            for key in events:
                if history is not None:
                    if key in history: continue
                    if isinstance(history, dict): history[key] = True
                    else: history.add(key)
                if key in pending:
                    del pending[key]; removed.append(key)
            return tuple(removed)
        for initial, buggy, fixed in (({'x': 1}, ('x', 'x'), ('x',)), ({}, ('x',), ())):
            for history, expected in ((None, buggy), (set(), fixed), ({}, fixed)):
                pending = dict(initial)
                def events():
                    yield 'x'
                    pending['x'] = 1
                    yield 'x'
                self.assertEqual(apply(pending, events(), history), expected)
        for history in (None, set(), {}):
            self.assertEqual(apply({'x': 1}, ['x', 'x'], history), ('x',))
