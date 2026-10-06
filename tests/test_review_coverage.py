"""Coverage acknowledgements do not consume the bounded evidence allowance."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from bridge import llm
from bridge.config import Config
from test_behavioral_review import ANSWER, BUG, WITNESS


class ReviewCoverageTests(unittest.TestCase):
    def payload(self, count=12, alleged=(), unseen=()):
        return {'schema': 'coverage-v2', 'status': 'complete', 'unexamined': '',
                'checks': [{'n': n, 'assessment': 'alleged' if n in alleged else 'unseen' if n in unseen else 'honored'}
                           for n in range(1, count + 1)], 'findings': [], 'exits_checked': []}

    def finding(self, number, witness=None):
        return {'n': number, 'at': 'if key in pending:', 'allegation': deepcopy(witness or WITNESS)}

    def read(self, raw, count=12, pending=None):
        self.rows = [{'needs': f'Rule {n}', 'kind': 'must', 'found': 'honored', 'state': 'ok',
                      'at': 'if key in pending:'} for n in range(1, count + 1)]
        if pending is not None:
            self.rows[-1].update(needs=pending, state='unclear', allegation_status='needs_witness')
        with patch.object(llm.Client, 'complete_json', return_value=deepcopy(raw)):
            self.limitations, self.complete = llm._counterexamples(
                Config(model_api='none'), 'context', BUG, llm.diff_kept(BUG, tests=False), self.rows, ANSWER)
        return self.complete

    def witnesses(self):
        return [row['counterexample'] for row in self.rows if row.get('counterexample')]

    def test_twelve_compact_positive_rows_complete_without_consuming_findings(self):
        raw = self.payload()
        report = llm._counterexample_report(raw, 12, [])
        self.assertTrue(report['complete'])
        self.assertEqual(len(report['checks']), 12)
        self.assertEqual(report['counts']['distinct_findings'], 0)
        self.assertTrue(self.read(raw))
        self.assertTrue(all(row['state'] == 'ok' for row in self.rows))
        self.assertEqual(self.limitations, [])

    def test_legacy_eleven_and_twelve_positive_rows_are_not_overflow(self):
        for count in (11, 12):
            raw = {'status': 'complete', 'checks': self.payload(count)['checks']}
            with self.subTest(count=count):
                self.assertTrue(self.read(raw, count))
                self.assertEqual(self.witnesses(), [])

    def test_late_legacy_grounded_witness_survives_more_than_ten_positive_rows(self):
        raw = {'status': 'complete', 'checks': self.payload()['checks'][:-1] + [
            {**self.finding(12), 'assessment': 'alleged'}]}
        self.assertTrue(self.read(raw))
        self.assertEqual(self.rows[-1]['counterexample']['allegation'], WITNESS)
        self.assertEqual(len(self.witnesses()), 1)

    def test_new_coverage_and_late_separate_finding_complete(self):
        raw = self.payload(alleged=(12,)); raw['findings'] = [self.finding(12)]
        self.assertTrue(self.read(raw))
        self.assertEqual(self.rows[-1]['state'], 'unclear')
        self.assertEqual(self.rows[-1]['counterexample']['allegation'], WITNESS)
        self.assertTrue(all(row['state'] == 'ok' for row in self.rows[:-1]))

    def test_more_than_four_grounded_findings_and_late_contradiction_stay_incomplete(self):
        raw = self.payload(alleged=(1, 2, 3, 4, 12))
        raw['findings'] = [self.finding(n) for n in (1, 2, 3, 4, 12)]
        raw['checks'].append({'n': 12, 'assessment': 'honored'})
        report = llm._counterexample_report(raw, 12, [])
        self.assertEqual(report['counts']['distinct_findings'], 5)
        self.assertIn(12, report['conflicts'])
        self.assertFalse(self.read(raw))
        self.assertEqual(len(self.witnesses()), 4)
        self.assertTrue(self.rows[-1].get('counterexample'), 'late contradictory evidence has priority')
        self.assertTrue(all(row['state'] == 'unclear' for row in self.rows))
        self.assertTrue(any('omitted' in reason for reason in self.limitations))
        self.assertTrue(any('exceed the limit of 4' in reason for reason in self.limitations))

    def test_overflowed_late_contradiction_cannot_produce_an_overall_all_clear(self):
        from test_review_sources import first_source, critic_source
        first = first_source(ANSWER, 12, 'if key in pending:')
        critic = critic_source(ANSWER, assessment='alleged')
        source_id = first['requirements'][0]['source_id']
        critic['checks'].append({'n': 1, 'source_id': source_id, 'scope': 'all_obligations', 'assessment': 'honored'})
        critic['findings'] = [{**self.finding(1, {**WITNESS, 'input': f'Generator variant {n} with x present.'}),
                              'source_id': source_id} for n in range(5)]
        with patch.object(Config, 'semantic_retrieval', property(lambda self: True)), \
                patch.object(llm.Client, 'complete_json', side_effect=[first, critic]) as model:
            reading = llm.check_conformance(Config(model_api='none'), 'policy', ANSWER, BUG)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(reading['verdict'], 'unclear')
        self.assertTrue(reading['incomplete'])
        self.assertEqual(len(reading['requirements'][0]['counterexamples']), 4)
        self.assertTrue(all(row['state'] == 'unclear' for row in reading['requirements']))
        self.assertTrue(any('exceed the limit of 4' in reason for reason in reading['unexamined']))
        self.assertTrue(any('omitted' in reason for reason in reading['unexamined']))

    def test_identical_coverage_duplicates_are_idempotent_not_extra_coverage(self):
        raw = self.payload(); raw['checks'].append(deepcopy(raw['checks'][-1]))
        report = llm._counterexample_report(raw, 12, [])
        self.assertTrue(report['complete'])
        self.assertEqual(report['counts']['duplicate_checks'], 1)
        self.assertEqual(len(report['checks']), 12)
        raw['checks'].pop(2)
        self.assertFalse(self.read(raw))
        self.assertIn('missing 1 requirement indices; first indices [3]', ' '.join(self.limitations))

    def test_missing_and_unknown_indices_do_not_establish_complete_coverage(self):
        bad_indices = (0, -1, 13, True, 1.0, 'unknown', None, {})
        for bad in bad_indices:
            raw = self.payload(alleged=(12,)); raw['checks'][0]['n'] = bad
            raw['findings'] = [self.finding(12)]
            with self.subTest(index_type=type(bad).__name__):
                self.assertFalse(self.read(raw))
                self.assertEqual(self.rows[-1]['counterexample']['allegation'], WITNESS)
                self.assertTrue(any('checks[0].n' in reason for reason in self.limitations))

    def test_contradictory_status_duplicates_are_order_independent(self):
        for statuses in (('honored', 'unseen'), ('unseen', 'honored')):
            raw = self.payload(); raw['checks'][0]['assessment'] = statuses[0]
            raw['checks'].append({'n': 1, 'assessment': statuses[1]})
            report = llm._counterexample_report(raw, 12, [])
            self.assertIn(1, report['conflicts'])
            self.assertFalse(self.read(raw))
            self.assertEqual(self.rows[0]['state'], 'unclear')

    def test_positive_coverage_cannot_override_a_negative_finding(self):
        raw = self.payload(); raw['findings'] = [self.finding(12)]
        self.assertFalse(self.read(raw))
        self.assertTrue(self.rows[-1].get('counterexample'))
        self.assertIn('finding contradicts coverage for requirement 12', ' '.join(self.limitations))

    def test_identical_findings_deduplicate_before_the_actionable_limit(self):
        raw = self.payload(alleged=(12,)); raw['findings'] = [self.finding(12) for _ in range(5)]
        self.assertLess(len(json.dumps(raw)), llm.REVIEW_MAX_CHARS)
        report = llm._counterexample_report(raw, 12, [])
        self.assertTrue(report['complete'])
        self.assertEqual(report['counts']['distinct_findings'], 1)
        self.assertEqual(report['counts']['duplicate_findings'], 4)
        self.assertTrue(self.read(raw))
        self.assertEqual(len(self.witnesses()), 1)

    def test_distinct_witnesses_for_one_requirement_are_preserved(self):
        initial_miss = {**WITNESS, 'input': 'pending is empty; use a lazy generator.',
                       'sequence': ['Yield x while it is missing.', 'Insert x into pending.', 'Yield x again.'],
                       'expected': 'No removal occurs for the later duplicate.',
                       'observed': 'The later duplicate removes x.'}
        raw = self.payload(alleged=(12,)); raw['findings'] = [self.finding(12), self.finding(12, initial_miss)]
        self.assertTrue(self.read(raw))
        self.assertEqual(self.rows[-1]['counterexample']['allegation'], WITNESS)
        self.assertEqual([x['allegation'] for x in self.rows[-1]['counterexamples']], [WITNESS, initial_miss])

    def test_malformed_partial_rows_do_not_hide_late_valid_evidence(self):
        raw = self.payload(alleged=(12,)); raw['checks'].append(None)
        raw['findings'] = [False, {'n': 'unknown'}, self.finding(12)]
        self.assertFalse(self.read(raw))
        self.assertEqual(self.rows[-1]['counterexample']['allegation'], WITNESS)
        self.assertTrue(any('expected an object' in reason for reason in self.limitations))

    def test_honest_inconclusive_status_stays_inconclusive(self):
        raw = self.payload(alleged=(12,)); raw.update(status='inconclusive', unexamined='One callback path remains unknown.')
        raw['findings'] = [self.finding(12)]
        self.assertFalse(self.read(raw))
        self.assertIn('One callback path remains unknown.', self.limitations)
        self.assertEqual(len(self.witnesses()), 1)

    def test_original_total_json_budget_still_applies(self):
        raw = self.payload(20)
        for row in raw['checks']: row['at'] = 'if key in pending:' + ' ' * 280
        self.assertGreater(len(json.dumps(raw)), llm.REVIEW_MAX_CHARS)
        self.assertFalse(self.read(raw, 20))
        self.assertTrue(any('6000 normalized JSON characters' in reason for reason in self.limitations))
        self.assertEqual((llm.REVIEW_MAX_TOKENS, llm.THINKING_ROOM, llm.REVIEW_MAX_CHARS), (1200, 8000, 6000))

    def test_sparse_legacy_requires_explicit_complete_attestation(self):
        self.assertTrue(self.read({'status': 'complete', 'checks': []}))
        for raw in ({'checks': []}, {'status': 'inconclusive', 'checks': []}, {'schema': 'unknown', 'status': 'complete', 'checks': []}):
            self.assertFalse(self.read(raw))
            self.assertTrue(all(row['state'] == 'unclear' for row in self.rows))

    def test_new_schema_cannot_use_sparse_omission_as_coverage(self):
        raw = self.payload(); raw['checks'] = []
        self.assertFalse(self.read(raw))
        self.assertIn('missing 12 requirement indices', ' '.join(self.limitations))

    def test_alleged_status_requires_corresponding_actionable_evidence(self):
        raw = self.payload(alleged=(12,))
        self.assertFalse(self.read(raw))
        self.assertIn('no actionable finding for alleged requirement 12', ' '.join(self.limitations))
        self.assertEqual(self.rows[-1]['state'], 'unclear')

    def test_exit_coverage_acknowledgements_do_not_consume_findings(self):
        exits = ['if condition: -> return'] * 10
        raw = self.payload(); raw['exits_checked'] = list(range(1, 11)) + [10]
        self.assertTrue(llm._counterexample_report(raw, 12, exits)['complete'])
        raw['exits_checked'] = list(range(1, 10)) + [11]
        report = llm._counterexample_report(raw, 12, exits)
        self.assertFalse(report['complete'])
        self.assertTrue(any('missing 1 listed exits' in reason for reason in report['issues']))
        legacy = {'status': 'complete', 'checks': [], 'exits': [{'exit': n, 'breaks': 0, 'how': ''} for n in range(1, 11)]}
        self.assertTrue(llm._counterexample_report(legacy, 12, exits)['complete'])

    def test_legacy_negative_exit_without_evidence_is_incomplete(self):
        for how in ('', 'none', 'n/a'):
            raw = {'status': 'complete', 'checks': [], 'exits': [{'exit': 1, 'breaks': 1, 'how': how}]}
            report = llm._counterexample_report(raw, 1, ['if key in pending: -> continue'])
            self.assertFalse(report['complete'])
            self.assertIn('exits[0]: expected an actionable allegation or missing-code detail', report['issues'])
            with patch.object(llm, 'early_exits', return_value=['if key in pending: -> continue']):
                self.assertFalse(self.read(raw, 1))
            self.assertEqual(self.rows[0]['state'], 'unclear')
            self.assertEqual(self.witnesses(), [])

    def test_legacy_no_break_exit_contradiction_preserves_negative_evidence(self):
        negative = {'exit': 1, 'breaks': 12, 'how': '', 'allegation': WITNESS}
        positive = {'exit': 1, 'breaks': 0, 'how': ''}
        for rows in ([positive, negative], [negative, positive]):
            raw = {'status': 'complete', 'checks': [], 'exits': rows}
            report = llm._counterexample_report(raw, 12, ['if key in pending: -> continue'])
            self.assertFalse(report['complete'])
            self.assertEqual(report['conflicts'], [12])
            self.assertEqual(len(report['findings']), 1)
            self.assertEqual(report['findings'][0]['allegation'], WITNESS)
            self.assertTrue(any('contradictory no-break' in reason for reason in report['issues']))

    def test_source_grounding_guard_survives_more_than_ten_honored_rows(self):
        raw = self.payload(); raw['checks'][-1]['at'] = 'if key in pending:'
        self.assertTrue(self.read(raw, pending='Use my invented seen-set preference'))
        self.assertEqual(self.rows[-1]['state'], 'unclear')
        self.assertIn('not quoted from the approval', self.rows[-1]['note'])
        self.assertTrue(self.read(raw, pending=WITNESS['authorized']))
        self.assertEqual(self.rows[-1]['state'], 'ok')

    def test_unseen_status_is_unknown_and_limitation_clipping_is_disclosed(self):
        raw = self.payload(unseen=tuple(range(1, 13)))
        self.assertTrue(self.read(raw))
        self.assertTrue(all(row['state'] == 'unclear' for row in self.rows))
        self.assertEqual(len(self.limitations), 6)
        self.assertIn('additional limitations', self.limitations[-1])

    def test_extremely_long_numeric_string_is_rejected_without_crashing(self):
        raw = self.payload(); raw['checks'][0]['n'] = '1' * 4301
        report = llm._counterexample_report(raw, 12, [])
        self.assertFalse(report['complete'])
        self.assertIn('checks[0].n: expected an index in 1..12', report['issues'])
        self.assertIsNone(llm._review_index(10 ** 5000, 12))

    def test_legacy_no_finding_fields_still_obey_shape_and_length_limits(self):
        for row in ({'n': 1, 'counterexample': ' ' * 501}, {'n': 1, 'not_shown': ' ' * 301}):
            report = llm._counterexample_report({'status': 'complete', 'checks': [row]}, 1, [])
            self.assertFalse(report['complete'])
        for value in ([], {}, False):
            report = llm._counterexample_report({'status': 'complete', 'checks': [],
                'exits': [{'exit': 1, 'breaks': 0, 'how': '', 'allegation': value}]}, 1, ['if done: -> return'])
            self.assertFalse(report['complete'])
            self.assertTrue(any('a no-break exit cannot carry an allegation' in reason for reason in report['issues']))

    def test_whitespace_equivalent_witnesses_do_not_consume_distinct_slots(self):
        first = {**WITNESS, 'input': 'pending has x', 'sequence': ['Yield x.', 'Reinsert x.', 'Yield x again.'],
                 'expected': 'One removal.', 'observed': 'Two removals.'}
        missing = {**first, 'input': 'pending is empty', 'sequence': ['Yield x.', 'Insert x.', 'Yield x again.'],
                   'expected': 'Zero removals.', 'observed': 'One removal.'}
        raw = self.payload(alleged=(12,))
        raw['findings'] = [self.finding(12, {**first, 'input': ' ' * n + first['input']}) for n in range(4)] + [self.finding(12, missing)]
        report = llm._counterexample_report(raw, 12, [])
        self.assertEqual(report['counts']['distinct_findings'], 2)
        self.assertTrue(self.read(raw))
        self.assertEqual(len(self.rows[-1]['counterexamples']), 2)
        self.assertEqual(self.rows[-1]['counterexamples'][1]['allegation']['input'], 'pending is empty')

    def test_kind_irrelevant_fields_and_no_finding_sentinels_cannot_crowd_late_evidence(self):
        first = {**WITNESS, 'input': 'pending has x', 'sequence': ['Yield x.', 'Reinsert x.', 'Yield x again.'],
                 'expected': 'One removal.', 'observed': 'Two removals.'}
        second = {**first, 'input': 'pending is empty', 'sequence': ['Yield x.', 'Insert x.', 'Yield x again.'],
                  'expected': 'Zero removals.', 'observed': 'One removal.'}
        raw = self.payload(alleged=(12,))
        raw['findings'] = [{**self.finding(12, {**first, 'required': f'ignored{n}'}), 'not_shown': sentinel}
                           for n, sentinel in enumerate(('', 'none', 'N/A', 'no.'))] + [self.finding(12, second)]
        report = llm._counterexample_report(raw, 12, [])
        self.assertEqual(report['counts']['distinct_findings'], 2)
        self.assertTrue(self.read(raw))
        self.assertEqual(len(self.rows[-1]['counterexamples']), 2)
        self.assertEqual(self.rows[-1]['counterexamples'][1]['allegation']['input'], 'pending is empty')

    def test_structural_identity_excludes_behavioral_only_fields(self):
        allegation = {'kind': 'structural', 'authorized': 'Use the file output.py',
                      'required': 'output.py', 'observed': 'different.py'}
        raw = self.payload(1, alleged=(1,))
        raw['findings'] = [self.finding(1, {**allegation, 'input': f'ignored{n}',
                           'expected': f'ignored{n}', 'sequence': [f'ignored{n}']}) for n in range(5)]
        report = llm._counterexample_report(raw, 1, [])
        self.assertTrue(report['complete'])
        self.assertEqual(report['counts']['distinct_findings'], 1)
        self.assertEqual(report['findings'][0]['allegation'], allegation)

    def test_overflow_and_late_contradiction_are_visible_despite_malformed_noise(self):
        raw = self.payload(alleged=(1, 2, 3, 4, 12))
        raw['checks'] += [None] * 12 + [{'n': 12, 'assessment': 'honored'}]
        raw['findings'] = [self.finding(n) for n in (1, 2, 3, 4, 12)]
        self.assertFalse(self.read(raw))
        self.assertEqual(len(self.witnesses()), 4)
        self.assertTrue(self.rows[-1].get('counterexample'))
        self.assertTrue(any('contradict' in reason for reason in self.limitations))
        self.assertTrue(any('exceed the limit of 4' in reason for reason in self.limitations))
        self.assertTrue(any('omitted' in reason for reason in self.limitations))

    def test_distinct_missing_code_details_on_one_row_are_all_retained(self):
        details = [f'Missing helper definition {name}()' for name in ('alpha', 'beta', 'gamma', 'delta')]
        raw = self.payload(3, unseen=(1, 2, 3))
        raw['findings'] = [{'n': 1, 'not_shown': detail} for detail in details]
        self.assertTrue(self.read(raw, 3))
        self.assertEqual(self.rows[0]['not_shown'], details[0])
        self.assertEqual(self.rows[0]['not_shown_details'], details)
        self.assertEqual(llm._counterexample_report(raw, 3, [])['counts']['distinct_findings'], 4)

    def test_malformed_ancillary_fields_keep_independent_grounded_evidence(self):
        for extra in ({'not_shown': None}, {'not_shown': 'x' * 301}, {'exit': 99},
                      {'counterexample': None}, {'counterexample': 'x' * 501}):
            raw = self.payload(alleged=(12,))
            raw['findings'] = [{**self.finding(12), **extra}]
            with self.subTest(field=next(iter(extra))):
                self.assertFalse(self.read(raw))
                self.assertEqual(self.rows[-1]['counterexample']['allegation'], WITNESS)
                self.assertTrue(all(row['state'] == 'unclear' for row in self.rows))

    def test_malformed_allegation_keeps_independent_missing_code_detail(self):
        raw = self.payload(unseen=(12,))
        raw['findings'] = [{'n': 12, 'allegation': 'bad', 'not_shown': 'Missing helper implementation'}]
        self.assertFalse(self.read(raw))
        self.assertEqual(self.rows[-1]['not_shown'], 'Missing helper implementation')
        self.assertEqual(self.witnesses(), [])

    def test_valid_exit_location_salvages_bad_optional_quote_and_legacy_explanation(self):
        exits = ['if key in pending: -> continue']
        raw = self.payload(1, alleged=(1,))
        raw['exits_checked'] = [1]
        raw['findings'] = [{**self.finding(1), 'exit': 1, 'at': None}]
        legacy = {'status': 'complete', 'checks': [], 'exits': [
            {'exit': 1, 'breaks': 1, 'how': None, 'allegation': WITNESS}]}
        for payload in (raw, legacy):
            report = llm._counterexample_report(payload, 1, exits)
            with self.subTest(mode=report['mode']):
                self.assertFalse(report['complete'])
                self.assertEqual(len(report['findings']), 1)
                candidate = report['findings'][0]
                self.assertEqual(candidate['at'], 'if key in pending:')
                self.assertEqual(llm._grounded_allegation(candidate['allegation'], ANSWER, candidate['at'],
                                 llm.diff_kept(BUG, tests=False), BUG), WITNESS)

    def test_diagnostics_do_not_echo_unknown_field_names_or_values(self):
        raw = self.payload(); raw['checks'][0]['PRIVATE_UNKNOWN_FIELD'] = 'PRIVATE_UNTRUSTED_VALUE'
        report = llm._counterexample_report(raw, 12, [])
        text = json.dumps(report)
        self.assertNotIn('PRIVATE_UNKNOWN_FIELD', text)
        self.assertNotIn('PRIVATE_UNTRUSTED_VALUE', text)
        self.assertIn('checks[0]: unexpected fields', report['issues'])
