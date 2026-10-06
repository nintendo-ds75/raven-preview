"""Exact source traceability is deterministic; semantic correctness is not.

Campaign regressions below use approved text and sanitized review fields only.
No submitted diff, hidden oracle or provider is executed.
"""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch
from bridge import llm
from bridge.config import Config

TEMPORAL = 'Consume keys once in input order, ignore missing keys and later duplicates'
TYPES = 'separator must be a nonempty string, otherwise ValueError even if the option is missing.'
ABSENCE = 'using missingval only for absent keys; preserve explicit None.'
DIFF = '+if key in self:\n+    del self[key]\n'


def first_source(answer, count=1, at='if key in self:'):
    source = llm._approved_sources(answer)[0]
    return {'schema': 'source-checks-v1', 'status': 'complete',
            'source_coverage': [{'source_id': source['id'], 'scope': 'all_obligations'}], 'requirements': [
        {'source_id': source['id'], 'kind': 'must', 'found': 'honored', 'at': at} for _ in range(count)],
        'why': 'All listed requirements are supported.'}


def critic_source(answer, *, assessment='honored', at=''):
    source = llm._approved_sources(answer)[0]
    return {'schema': 'coverage-v2', 'status': 'complete', 'checks': [
        {'n': 1, 'source_id': source['id'], 'scope': 'all_obligations', 'assessment': assessment, 'at': at}],
        'findings': [], 'exits_checked': []}


class ReviewSourceTests(unittest.TestCase):
    def read(self, answer=TEMPORAL, first=None, critic=None, diff=DIFF, others=()):
        self.prompts = {}
        def complete(client, purpose, system, prompt, **kw):
            self.prompts[purpose] = prompt
            if purpose == 'conformance': return deepcopy(first if first is not None else first_source(answer))
            if purpose == 'counterexample': return deepcopy(critic if critic is not None else critic_source(answer))
            return {'status': 'complete', 'conditions': []}
        with patch.object(Config, 'semantic_retrieval', property(lambda self: True)), patch.object(llm.Client, 'complete_json', complete):
            return llm.check_conformance(Config(model_api='none'), 'Which behavior is approved?', answer, diff, others)

    def test_source_ids_and_spans_preserve_arbitrary_text_without_clause_guessing(self):
        answer = 'Keep x.y; not x?\n\n```python\nx = {"a": None, "b": "a; b. c"}\n```\nUse café, even if blank.'
        source, = llm._approved_sources(answer)
        self.assertEqual((source['start'], source['end'], source['text']), (0, len(answer), answer))
        self.assertEqual(llm._approved_sources(answer), [source])
        self.assertNotEqual(llm._approved_sources(answer + ' ')[0]['id'], source['id'])
        self.assertEqual(answer[source['start']:source['end']], source['text'])

    def test_twelve_code_checks_do_not_replace_one_original_critic_scope(self):
        result = self.read(first=first_source(TEMPORAL, 12))
        self.assertEqual(result['verdict'], 'follows')
        self.assertNotIn('incomplete', result)
        self.assertEqual(len(result['requirements']), 12)
        self.assertTrue(all(row['needs'] == TEMPORAL for row in result['requirements']))
        self.assertEqual(result['approved_sources'], llm._approved_sources(TEMPORAL))
        scope = self.prompts['counterexample'].split('REQUIREMENTS TO CHALLENGE:\n')[1].split('\nDIFF:')[0]
        self.assertEqual(scope.count('ORIGINAL SOURCE'), 1)
        self.assertIn(TEMPORAL, self.prompts['conformance'])
        self.assertIn(TEMPORAL, self.prompts['counterexample'])

    def test_captured_already_removed_weakening_cannot_define_critic_scope(self):
        first = {'status': 'complete', 'requirements': [{'needs': 'Ignore later duplicates of already-removed keys',
                 'kind': 'must', 'found': 'honored', 'at': 'if key in self:'}], 'why': 'All requirements honored.'}
        result = self.read(first=first)
        self.assertEqual(result['verdict'], 'unclear')
        self.assertTrue(result['incomplete'])
        self.assertIn(TEMPORAL, self.prompts['counterexample'])
        self.assertNotIn('already-removed', self.prompts['counterexample'])
        self.assertTrue(result['source_issues'])

    def test_valid_id_does_not_authorize_a_weakened_text_echo(self):
        first = first_source(TEMPORAL)
        for key in ('needs', 'source_text'):
            first['requirements'][0][key] = 'Ignore later duplicates of already-removed keys'
            result = self.read(first=first)
            self.assertTrue(result['incomplete'])
            self.assertEqual(result['verdict'], 'unclear')
            self.assertTrue(any(key + ': does not match' in issue for issue in result['source_issues']))
            first['requirements'][0].pop(key)

    def test_missing_invented_or_wrong_type_ids_are_incomplete(self):
        for value in (None, 'invented-source', {}, [], True, 1):
            first = first_source(TEMPORAL); first['requirements'][0]['source_id'] = value
            first['requirements'][0]['needs'] = TEMPORAL
            result = self.read(first=first)
            self.assertEqual(result['verdict'], 'unclear')
            self.assertTrue(result['incomplete'])

    def test_critic_requires_source_id_even_when_all_numbered_checks_are_honored(self):
        for value in (None, 'invented-source', [], True):
            critic = critic_source(TEMPORAL); critic['checks'][0]['source_id'] = value
            result = self.read(critic=critic)
            self.assertTrue(result['incomplete'])
            self.assertEqual(result['verdict'], 'unclear')
            self.assertTrue(any('source_id' in issue for issue in result['unexamined']))

    def test_legacy_sparse_attestation_does_not_complete_source_bound_production_read(self):
        result = self.read(critic={'status': 'complete', 'checks': []})
        self.assertTrue(result['incomplete'])
        self.assertEqual(result['verdict'], 'unclear')

    def test_non_temporal_narrowed_type_or_presence_clause_is_synthetic_and_rejected(self):
        # Iniconfig's captured needs already contained its full rule. These are
        # synthetic narrowed responses, not fabricated observed critic rows.
        for weakened in ('separator must be nonempty', 'Validate separator when the option is present'):
            first = first_source(TYPES, at='if not separator:')
            first['requirements'][0]['needs'] = weakened
            result = self.read(TYPES, first=first, diff='+if not separator:\n+    raise ValueError()\n')
            self.assertEqual(result['verdict'], 'unclear')
            self.assertTrue(result['incomplete'])
            self.assertIn(TYPES, self.prompts['counterexample'])

    def test_absent_and_explicit_none_cannot_be_merged_in_a_source_echo(self):
        # Tabulate had no saved normalized review; this response is synthetic.
        first = first_source(ABSENCE, at='value = record.get(name)')
        first['requirements'][0]['needs'] = 'Use missingval for missing or None values'
        result = self.read(ABSENCE, first=first, diff='+value = record.get(name)\n')
        self.assertEqual(result['verdict'], 'unclear')
        self.assertTrue(result['incomplete'])
        self.assertIn(ABSENCE, self.prompts['counterexample'])

    def test_exact_source_binding_does_not_claim_semantic_type_verification(self):
        # The real iniconfig failure retained the rule yet falsely honored it.
        # A synthetic model can still repeat that error under exact source IDs.
        result = self.read(TYPES, first=first_source(TYPES, at='if not separator:'),
                           diff='+if not separator:\n+    raise ValueError()\n')
        self.assertEqual(result['verdict'], 'follows')
        self.assertNotIn('incomplete', result)
        self.assertEqual(result['approved_sources'][0]['text'], TYPES)

    def test_canvas_passes_exact_signed_text_before_assigning_source_offsets(self):
        from types import SimpleNamespace
        from bridge import canvas
        answer = ' \nUse a.b; preserve None.\n '
        captured = []
        def read(cfg, question, original, diff, others):
            captured.append(original)
            return {'verdict': 'unclear', 'why': 'advisory', 'requirements': [],
                    'approved_sources': llm._approved_sources(original)}
        store = SimpleNamespace(graph=SimpleNamespace(db=SimpleNamespace(
            execute=lambda *args: SimpleNamespace(fetchone=lambda: None))))
        with patch.object(llm, 'check_conformance', side_effect=read):
            result = canvas._conformance(store, 'task', [{'node_id': 'node', 'question': 'policy', 'answer': answer}], DIFF)
        self.assertEqual(captured, [answer])
        self.assertEqual(result[0]['approved_sources'][0]['text'], answer)
        self.assertEqual(result[0]['approved_sources'][0]['end'], len(answer))

    def test_shared_context_preserves_exact_text_and_every_small_sibling(self):
        siblings = tuple((f'  Question {n}?\n', '  ' + (('long clause; ' * 26 + 'Apply this qualification even if the option is missing.') if n == 6 else 'Keep value.') + '\n')
                         for n in range(7))
        result = self.read(others=siblings)
        self.assertNotIn('incomplete', result)
        expected = [{'question': q, 'answer': a} for q, a in siblings]
        self.assertEqual(result['approved_context']['other_decisions'], expected)
        for prompt in self.prompts.values():
            if 'OTHER DECISIONS ON THIS TASK' in prompt:
                encoded = prompt.split('OTHER DECISIONS ON THIS TASK, also authorized (not requirements of this one):\n')[1].splitlines()[0]
                self.assertEqual(json.loads(encoded), expected)
        self.assertGreater(len(siblings[-1][1]), 300)
        self.assertTrue(result['approved_context']['other_decisions'][-1]['answer'].endswith('Apply this qualification even if the option is missing.\n'))

    def test_oversized_or_malformed_shared_authority_never_starts_inference(self):
        with patch.object(Config, 'semantic_retrieval', property(lambda self: True)), patch.object(llm.Client, 'complete_json') as model:
            for others in ((('q', 'x' * 6000),), (('q', None),), 'invalid', ((None, 'answer'),)):
                result = llm.check_conformance(Config(model_api='none'), 'q', TEMPORAL, DIFF, others)
                self.assertEqual(result['verdict'], 'unclear')
                self.assertTrue(result['incomplete'])
            model.assert_not_called()

    def test_context_overflow_retains_full_authority_metadata_without_inference(self):
        answer = 'Keep the original answer; ' + 'x' * llm.REVIEW_CONTEXT_MAX_CHARS
        others = (('  long sibling?\n', 'qualification: ' + 'y' * 5000 + ' preserve explicit None.  '),)
        with patch.object(Config, 'semantic_retrieval', property(lambda self: True)), patch.object(llm.Client, 'complete_json') as model:
            result = llm.check_conformance(Config(model_api='none'), '  question?\n', answer, DIFF, others)
        model.assert_not_called()
        self.assertTrue(result['incomplete'])
        self.assertEqual(result['approved_sources'], llm._approved_sources(answer))
        self.assertEqual(result['approved_context'], {'question': '  question?\n', 'other_decisions': [
            {'question': others[0][0], 'answer': others[0][1]}]})

    def test_canvas_retains_full_overflow_metadata_when_inference_abstains(self):
        from types import SimpleNamespace
        from bridge import canvas
        answer = 'Preserve qualification; ' + 'x' * llm.REVIEW_CONTEXT_MAX_CHARS
        store = SimpleNamespace(graph=SimpleNamespace(db=SimpleNamespace(
            execute=lambda *args: SimpleNamespace(fetchone=lambda: None))))
        with patch.object(Config, 'semantic_retrieval', property(lambda self: True)), patch.object(llm.Client, 'complete_json') as model:
            readings = canvas._conformance(store, 'task', [{'node_id': 'node', 'question': ' q ', 'answer': answer}], DIFF)
        model.assert_not_called()
        self.assertTrue(readings[0]['incomplete'])
        self.assertEqual(readings[0]['approved_sources'][0]['text'], answer)
        self.assertEqual(readings[0]['approved_context']['question'], ' q ')

    def test_context_and_model_output_limits_are_independent(self):
        with patch.object(llm, 'REVIEW_MAX_CHARS', 10):
            result = self.read()
        self.assertIn('conformance', self.prompts)
        self.assertTrue(result['incomplete'])
        with patch.object(llm, 'REVIEW_CONTEXT_MAX_CHARS', 10):
            result = self.read()
        self.assertEqual(self.prompts, {})
        self.assertTrue(result['incomplete'])
        self.assertEqual(result['approved_sources'][0]['text'], TEMPORAL)

    def test_compound_source_requires_explicit_full_scope_beside_code_checks(self):
        answer = TYPES + ' Missing options return the exact default object. ' + ABSENCE
        first = first_source(answer, 8, 'if not separator:')
        for field in (None, [], [{'source_id': first['requirements'][0]['source_id'], 'scope': 'selected_checks'}]):
            first['source_coverage'] = field
            result = self.read(answer, first=first, diff='+if not separator:\n+    raise ValueError()\n')
            self.assertTrue(result['incomplete'])
            self.assertEqual(result['verdict'], 'unclear')
        critic = critic_source(answer); critic['checks'][0]['scope'] = 'selected_checks'
        result = self.read(answer, first=first_source(answer, 8, 'if not separator:'), critic=critic,
                           diff='+if not separator:\n+    raise ValueError()\n')
        self.assertTrue(result['incomplete'])
        self.assertEqual(result['verdict'], 'unclear')
        for fragment in (TYPES, 'Missing options return the exact default object.', ABSENCE):
            self.assertIn(fragment, self.prompts['counterexample'])
        self.assertNotIn('source_coverage', llm.COUNTEREXAMPLE_SYSTEM)
        self.assertIn('For every requirement within each original source', llm.COUNTEREXAMPLE_STOP_RULE)
        self.assertIn('do not treat three traces for one clause as coverage of its other clauses', llm.COUNTEREXAMPLE_STOP_RULE)

    def test_empty_or_oversized_source_never_starts_inference(self):
        with patch.object(Config, 'semantic_retrieval', property(lambda self: True)), patch.object(llm.Client, 'complete_json') as model:
            for answer in ('', 'x' * (llm.REVIEW_MAX_CHARS + 1)):
                result = llm.check_conformance(Config(model_api='none'), 'q', answer, DIFF)
                self.assertTrue(result['incomplete'])
            model.assert_not_called()
