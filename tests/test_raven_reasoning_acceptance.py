"""Offline controller tests; model doubles never qualify as live acceptance."""
import json
from pathlib import Path
import re
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from evals import raven_reasoning_acceptance as a


class ModelDouble:
    def __init__(self):
        self.calls = []

    def __call__(self, purpose, system, prompt, *args, **kwargs):
        self.calls.append(purpose)
        if purpose == 'name_decisions':
            return '[]'
        if purpose == 'triage':
            return json.dumps({'verdict': 'engage', 'why': 'Synthetic model-double judgment'})
        if purpose == 'contact_selection':
            candidates = json.loads(prompt)['candidates']
            learned = [c for c in candidates if c['name'] == a.RESPONDENT and any(
                'learned' in text or 'answered' in text for text in c['evidence'].values())]
            chosen = next(iter(learned), None) or next((c for c in candidates if c['name'] in a.ACCEPTABLE_CONTACTS), None)
            chosen = chosen or next((c for c in candidates if c['name'] == a.RESPONDENT), None)
            if chosen is None:
                return json.dumps({'candidate': '', 'evidence': []})
            return json.dumps({'candidate': chosen['id'], 'evidence': [next(iter(chosen['evidence']))]})
        if purpose == 'select':
            return json.dumps({'pick': 'm1' if 'm1 [' in prompt else 'none'})
        if purpose == 'memory_rerank':
            found = re.search(r'\[([0-9a-f]{12})\]', prompt)
            return json.dumps({'id': found[1] if found else 'none', 'why': 'Synthetic fixture matching'})
        if purpose == 'compose':
            found = re.search(r'Retain production audit events.*?boundary\.', prompt)
            return found[0] if found else 'RECORD DOES NOT ANSWER'
        if purpose == 'support':
            answer = prompt.split('\n\nANSWER: ', 1)[-1]
            return json.dumps({'claims': [{'claim': answer, 'support': 'stated'}], 'answer': answer, 'open': []})
        if purpose == 'conflict':
            return 'NO CONFLICT'
        if purpose == 'brief':
            return json.dumps({'brief': ''})
        if purpose == 'proposal':
            return json.dumps({'proposal': '', 'answers': False, 'relation': 'analogy'})
        if purpose == 'assume':
            return 'NO PATTERN'
        raise AssertionError('Unexpected model-double purpose: ' + purpose)


class AcceptanceTests(unittest.TestCase):
    def test_retention_oracle_requires_origin_strict_threshold_and_exact_boundary(self):
        valid = (
            'Retain production audit events for 31 full days from ingestion. Delete only events strictly older than 31 days; retain events exactly at the boundary.',
            'Measure age from ingestion time. Discard events more than 31 full days old; events exactly 31 days old must be retained.',
            'Age is measured since ingestion. Remove events older than 31 days. Do not delete events exactly at the retention boundary.',
        )
        for answer in valid:
            with self.subTest(answer=answer):
                self.assertTrue(a.retention_receipt(answer, 31)['retain_exact_boundary'])
        invalid = (
            'From ingestion, delete events at least 31 days old; retain events exactly at the boundary.',
            'From ingestion, delete events older than 31 days; retain events exactly at the boundary. Delete events exactly 31 days old.',
            'From completion time, delete events older than 31 days; retain events exactly at the boundary.',
            'From ingestion, delete events older than 32 days; retain events exactly at the boundary.',
            'From ingestion, delete events older than 31 days.',
            'Age is measured from ingestion. Delete events older than 31 days; never retain events exactly at the boundary.',
            'Age is measured from ingestion. Delete events older than 31 days; events exactly at the boundary must not be retained.',
            'Age is measured from ingestion. Delete events older than 31 days; retain no events exactly at the boundary.',
            'From ingestion, delete events older than 31 days; retain events exactly at the boundary. Keep events older than 31 days.',
        )
        for answer in invalid:
            with self.subTest(answer=answer), self.assertRaises(a.AcceptanceFailure):
                a.retention_receipt(answer, 31)

    def test_default_is_blocked_without_any_fixture_or_credential_access(self):
        with patch.object(a, 'ReasoningFixture', side_effect=AssertionError('No fixture')):
            self.assertEqual(a.run(case='owners')['status'], 'blocked')
            self.assertEqual(a.main(['--case', 'cache']) , 1)

    def test_http_is_bounded_by_remaining_deadline_and_expiry_prevents_request(self):
        fixture = a.ReasoningFixture.__new__(a.ReasoningFixture)
        fixture.deadline = time.monotonic() + .5
        fixture.stopping = False
        fixture.url, fixture.stage, fixture.transcript = 'http://127.0.0.1:1', 'test', []
        fixture.tokens = {'Quinn Operator': 'synthetic'}
        with patch.object(a, 'urlopen') as opened:
            opened.return_value.status = 200
            opened.return_value.read.return_value = b'{}'
            fixture.http('/api/people')
            self.assertGreater(opened.call_args.kwargs['timeout'], 0)
            self.assertLessEqual(opened.call_args.kwargs['timeout'], .5)
            fixture.deadline = time.monotonic() - 1
            with self.assertRaisesRegex(a.AcceptanceFailure, 'reasoning_timeout'):
                fixture.http('/api/people')
            self.assertEqual(opened.call_count, 1)

    def test_live_transport_remains_bound_to_fixture_deadline_and_stops_at_cleanup(self):
        requests = []
        gateway = SimpleNamespace(placeholder='synthetic-placeholder',
                                  urllib_open=lambda req, **kwargs: requests.append(kwargs))
        with tempfile.TemporaryDirectory() as tmp:
            fixture = a.ReasoningFixture(Path(tmp) / 'f', 'cache', gateway=gateway, timeout=10)
            try:
                fixture.deadline = time.monotonic() + .5
                a.llm._open_model_request(a.Request('https://api.anthropic.com/v1/messages'), timeout=120)
                self.assertLessEqual(requests[0]['timeout'], .5)
                fixture.stopping = True
                with self.assertRaisesRegex(a.AcceptanceFailure, 'fixture_stopping'):
                    a.llm._open_model_request(a.Request('https://api.anthropic.com/v1/messages'), timeout=120)
                self.assertEqual(len(requests), 1)
            finally:
                fixture.close()

    def test_invalid_saved_credential_never_constructs_gateway_or_reads_again(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch('evals.customer_test_credentials.read_saved_key', side_effect=ValueError('PRIVATE')) as read, \
                patch('evals.customer_provider_gateway.Gateway', side_effect=AssertionError('No provider')):
            self.assertEqual(a.main(['--run-live', '--case', 'owners', '--credential-file', '/synthetic/key',
                                     '--output-dir', str(Path(tmp) / 'run')]), 1)
            self.assertEqual(read.call_count, 1)
            self.assertFalse((Path(tmp) / 'run').exists())

    def test_model_failure_preserves_failure_and_never_simulates_human_answer(self):
        def failed_model(*args, **kwargs):
            raise a.llm.LLMError('PRIVATE provider detail must not be recorded')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'run'
            result = a.run(case='owners', output_dir=root, model_double=failed_model, timeout=10)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['passed_stages'], [])
            trace = json.loads((root / 'api-trace.json').read_text())
            self.assertFalse(any(e.get('path', '').endswith(('/answer', '/signoff', '/refer')) for e in trace))
            self.assertNotIn('PRIVATE', (root / 'result.json').read_text())

    def test_contact_oracle_requires_real_selected_candidate_and_owned_citations(self):
        row = {'owner_name': 'Expected', 'authorized': False, 'signatures': '[]', 'events': [{
            'kind': 'contact_reasoned', 'detail': {'selected': 'c2', 'evidence': ['c2:e1'],
                'candidates': [{'id': 'c1', 'person_id': 'wrong', 'name': 'Other', 'evidence': {'c1:e1': 'history'}},
                               {'id': 'c2', 'person_id': 'right', 'name': 'Expected', 'evidence': {'c2:e1': 'review'}}]}}]}
        self.assertEqual(a.contact_receipt(row, {'right'})['selected_person_id'], 'right')
        with self.assertRaises(a.AcceptanceFailure):
            a.contact_receipt(row, {'wrong'})
        row['events'][0]['detail']['evidence'] = ['c1:e1']
        with self.assertRaises(a.AcceptanceFailure):
            a.contact_receipt(row, {'right'})

    def test_owners_flow_real_public_apis_with_explicit_model_double(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = ModelDouble()
            report = a.run(case='owners', output_dir=Path(tmp) / 'run', model_double=model, timeout=25)
            self.assertEqual(report['status'], 'fixture_passed', (report, model.calls))
            self.assertFalse(report['live_provider'])
            self.assertLessEqual(sum(c['requested_max_tokens'] for c in report['model_calls']), a.LIMITS['owners']['max_output_tokens'])
            self.assertEqual(report['cold_start']['authority_rows'], 0)
            self.assertEqual(report['cold_start']['decision_rows'], 0)
            self.assertIn(report['cold_contact']['selected_name'], a.ACCEPTABLE_CONTACTS)
            self.assertEqual(report['learned_contact']['selected_name'], a.RESPONDENT)
            self.assertTrue(any('learned first contact' in value for value in report['learned_contact']['cited_evidence'].values()))
            self.assertNotEqual(report['task_id'], report['heldout_task_id'])
            self.assertGreaterEqual(report['git_fixture']['blame'], 30)
            self.assertFalse(report['git_fixture']['has_codeowners'])
            self.assertEqual([x['outcome'] for x in report['contact_outcomes']], ['referred', 'answered'])
            trace = json.loads((Path(tmp) / 'run/api-trace.json').read_text())
            nodes = [e['arguments'] for e in trace if e.get('tool') == 'bridge_add_node']
            self.assertTrue(nodes)
            self.assertTrue(all('owner_id' not in node and 'answer' not in node for node in nodes))
            cold_imports = [e['arguments'] for e in trace if e.get('tool') == 'bridge_import_record' and e['stage'] == 'bootstrap']
            self.assertNotIn(a.RESPONDENT, [record['author'] for record in cold_imports])

    def test_available_learned_contact_evidence_is_insufficient_without_model_citation(self):
        original = a.contact_receipt
        def without_citation(row, acceptable):
            receipt = original(row, acceptable)
            if receipt['selected_name'] == a.RESPONDENT:
                self.assertTrue(any('learned first contact' in value for value in receipt['available_evidence'].values()))
                receipt['cited_evidence'] = {'other': 'Unrelated historical authorship'}
            return receipt
        with tempfile.TemporaryDirectory() as tmp, patch.object(a, 'contact_receipt', side_effect=without_citation):
            report = a.run(case='owners', output_dir=Path(tmp) / 'run', model_double=ModelDouble(), timeout=25)
            self.assertEqual(report['status'], 'failed')
            self.assertEqual(report['failure_code'], 'selected_respondent_did_not_cite_scoped_learned_evidence')

    def test_correct_duration_with_wrong_boundary_stops_before_derived_human_signoff(self):
        model = ModelDouble()
        def wrong_boundary(purpose, *args, **kwargs):
            result = model(purpose, *args, **kwargs)
            return result.replace('strictly older than', 'at least') if purpose == 'compose' else result
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'run'
            report = a.run(case='cache', output_dir=root, model_double=wrong_boundary, timeout=25)
            self.assertEqual(report['status'], 'failed')
            self.assertEqual(report['failure_code'], 'memory_inclusive_boundary_contradiction')
            trace = json.loads((root / 'api-trace.json').read_text())
            self.assertFalse(any(event.get('path', '').endswith(('/signoff', '/rule')) for event in trace))

    def test_live_memory_and_source_reversion_flow_with_explicit_model_double(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = ModelDouble()
            report = a.run(case='cache', output_dir=Path(tmp) / 'run', model_double=model, timeout=25)
            self.assertEqual(report['status'], 'fixture_passed', (report, model.calls))
            self.assertFalse(report['live_provider'])
            self.assertLessEqual(sum(c['requested_max_tokens'] for c in report['model_calls']), a.LIMITS['cache']['max_output_tokens'])
            self.assertEqual(report['reuse']['source_id'], report['prior_node_id'])
            self.assertFalse(report['reuse']['authorized'])
            self.assertEqual(report['reuse']['policy_check']['origin'], 'ingestion')
            self.assertEqual(len(set(report['source_versions'])), 3)
            self.assertIn('compose', model.calls)
            self.assertIn('support', model.calls)
            self.assertTrue(report['reuse']['source_revision'])
            self.assertTrue(report['warm_cache']['authorized'])
            self.assertTrue(report['warm_cache']['policy_check']['retain_exact_boundary'])
            self.assertEqual(report['warm_cache']['unchanged_node_retry_additional_inference'], 0)
            trace = json.loads((Path(tmp) / 'run/api-trace.json').read_text())
            heldout = [e['arguments'] for e in trace if e.get('tool') in ('bridge_start_task', 'bridge_add_node')
                       and e['stage'] == 'live_memory_reuse']
            for args in heldout:
                for key in ('title', 'goal', 'question', 'context', 'options'):
                    self.assertNotIn(str(report['oracle']['duration_days']), str(args.get(key, '')))


if __name__ == '__main__':
    unittest.main()
