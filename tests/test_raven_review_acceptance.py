"""Offline transport doubles exercise the real production review validator/runtime."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from bridge import canvas
from evals import claude_mcp_acceptance as host
from evals import raven_review_acceptance as a
from evals.customer_provider_gateway import Gateway
from evals.live_workflow_acceptance import novel_scenario


class ModelTransport:
    def __init__(self, *, false_follows=False, incomplete=False):
        self.false_follows, self.incomplete = false_follows, incomplete
        self.requests = []

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        prompt = body['messages'][0]['content']
        if isinstance(prompt, list):
            prompt = '\n'.join(item.get('text', '') for item in prompt)
        added = next(line[1:] for line in prompt.splitlines() if line.startswith('+    return ')).strip()
        if self.incomplete:
            response = {'status': 'partial', 'requirements': [], 'why': 'Synthetic incomplete response'}
        elif 'REQUIREMENTS READ AS HONORED:' in prompt:
            response = {'status': 'complete', 'checks': [], 'exits': []}
        elif 'REQUIREMENTS' in prompt and 'AUTHORIZED' not in prompt:
            raise AssertionError('Unexpected prompt')
        elif 'conditions' in str(body['system']).lower() and 'extra' in str(body['system']).lower():
            response = {'status': 'complete', 'conditions': []}
        else:
            response = {'status': 'complete', 'requirements': [{
                'needs': 'Internal pilot traffic remains excluded at the exact signed threshold',
                'kind': 'must', 'found': 'violated' if 'calls >=' in added and not self.false_follows else 'honored',
                'at': added}], 'why': 'Controlled offline model transport response'}
        return httpx.Response(200, json={'stop_reason': 'end_turn',
            'content': [{'type': 'text', 'text': json.dumps(response)}],
            'usage': {'input_tokens': 10, 'output_tokens': 20}})


def source_fixture(root):
    """Use the existing authenticated synthetic harness; never provider inference."""
    scenario = novel_scenario(threshold=162, cohort='pilot_9d4c18d4', staging='staging_20758a99')
    clean = {k: os.environ[k] for k in ('PATH', 'LANG') if k in os.environ}
    clean.update(BRIDGE_MODEL_API='none', BRIDGE_SEMANTIC='0', BRIDGE_EMBEDDINGS='disabled')
    with patch.dict(os.environ, clean, clear=True), host.Fixture(root / 'fixture') as fixture:
        fixture.prepare()
        task = fixture.call('bridge_start_task', title='Production invoice policy', goal=host.PROMPT,
            repo=host.REPO, client_key='review-source', paths=host.POLICY)['task_id']
        node = fixture.call('bridge_add_node', task_id=task,
            question='Should internal load-test traffic count on production invoices?', paths=host.POLICY,
            category='policy', facts='environment=production')['node_id']
        fixture.human('/api/decisions/' + node + '/signoff', {
            'answer': scenario['base_answer'] + '\n' + scenario['caveat_message'],
            'rationale': 'Synthetic owner policy', 'expected_updated_at': fixture.store.get_decision(node)['updated_at'],
            'applicability': {'requires': {'environment': 'production'}}, 'evidence_mode': 'independent'})
        source = ('def is_billable(is_internal, cohort, calls):\n'
                  "    return not is_internal or (cohort == 'pilot_9d4c18d4' and calls > 162)\n")
        (root / 'fixture/checkout' / host.POLICY).write_text(source)
        diff = a.policy_diff(source)
        fixture.call('bridge_get_tree', task_id=task)
        fixture.call('bridge_finish_task', task_id=task, diff=diff)
        proof = fixture.call('bridge_export_proof', task_id=task)
        saved = {'synthetic_people': True, 'synthetic_slack': True, 'task_id': task, 'node_id': node,
                 'actual_patch': diff, 'scenario': scenario, 'proof_integrity_valid': proof['integrity']['valid']}
        (root / 'result.json').write_text(json.dumps(saved))
    return root


class AcceptanceTests(unittest.TestCase):
    def test_default_does_not_read_credentials_or_source(self):
        with patch.object(a, 'load_source', side_effect=AssertionError('Must not read')), \
                patch('evals.customer_test_credentials.read_saved_key', side_effect=AssertionError('Must not read')), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(a.run()['status'], 'blocked')
            self.assertEqual(a.main([]), 1)

    def test_invalid_credential_is_sanitized_and_never_starts_gateway(self):
        with patch('evals.customer_test_credentials.read_saved_key', side_effect=ValueError('PRIVATE_VALUE')), \
                patch('evals.customer_provider_gateway.Gateway', side_effect=AssertionError('No gateway')), \
                redirect_stdout(io.StringIO()) as captured:
            self.assertEqual(a.main(['--run-live', '--source-run', '/synthetic/source', '--output-dir', '/synthetic/out',
                                     '--credential-file', '/synthetic/key']), 1)
        self.assertNotIn('PRIVATE_VALUE', captured.getvalue())

    def test_output_ceiling_is_checked_before_credential_access(self):
        with patch('evals.customer_test_credentials.read_saved_key', side_effect=AssertionError('Must not read')):
            with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
                a.main(['--max-output-tokens', '20001'])

    def test_transport_and_http_deadline_reject_before_opening(self):
        fixture = a.ReviewFixture.__new__(a.ReviewFixture)
        fixture.deadline, fixture.stopping = time.monotonic() - 1, False
        fixture.url, fixture.agent, fixture.trace = 'http://127.0.0.1:1', 'test-agent', []
        fixture.opener = SimpleNamespace(open=lambda *args, **kwargs: self.fail('Request after deadline'))
        with self.assertRaisesRegex(a.AcceptanceFailure, 'review_deadline'):
            fixture.http('/mcp', {})
        fixture.deadline, fixture.stopping = time.monotonic() + 10, True
        with self.assertRaisesRegex(a.AcceptanceFailure, 'review_fixture_stopping'):
            fixture.http('/mcp', {})

    def test_output_cannot_be_created_inside_retained_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gateway = SimpleNamespace()
            result = a.run(source_run=root, output_dir=root / 'review', offline_test=True, gateway=gateway)
            self.assertEqual(result['status'], 'blocked')
            self.assertFalse((root / 'review').exists())

    def test_unsigned_source_never_reaches_provider_and_stays_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = source_fixture(root / 'source')
            with a.sqlite3.connect(source / 'fixture/raven.db') as db:
                db.execute("UPDATE decisions SET signoff='required',signed_hash='' WHERE run_id=?",
                           (json.loads((source / 'result.json').read_text())['task_id'],))
            transport = ModelTransport()
            with Gateway('offline-test-only', transport=httpx.MockTransport(transport), **a.LIMITS) as gateway:
                result = a.run(source_run=source, output_dir=root / 'review', gateway=gateway,
                               offline_test=True, timeout=30)
            self.assertEqual(result['failure_code'], 'source_signed_answer_not_current', result)
            self.assertEqual(result['provider_usage']['physical_requests'], 0)
            self.assertTrue(result['source_database_unchanged'])
            self.assertFalse(transport.requests)

    def exercise(self, transport):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = source_fixture(root / 'source')
            with Gateway('offline-test-only', transport=httpx.MockTransport(transport), **a.LIMITS) as gateway:
                result = a.run(source_run=source, output_dir=root / 'review', gateway=gateway,
                               offline_test=True, timeout=30)
            self.assertTrue(result['source_database_unchanged'], result)
            self.assertTrue(result['cleanup_quiescent'], result)
            self.assertFalse(canvas._REVIEWS)
            return result

    def test_two_cases_use_production_runtime_and_retained_signoff_pins(self):
        transport = ModelTransport()
        result = self.exercise(transport)
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(result['mode'], 'offline_transport_double')
        self.assertFalse(result['integrated_host_acceptance'])
        self.assertEqual(result['independent_correct_boundary_cases'], 16)
        self.assertEqual(result['independent_wrong_boundary_check'], 'rejected_at_exact_threshold')
        for case in result['cases'].values():
            self.assertTrue(case['pins_unchanged'])
            self.assertFalse(case['verified'])
            self.assertGreater(case['source_advisory_events_preserved'], 0)
            self.assertTrue(case['model_calls'])
            self.assertTrue(case['unchanged_enabled_review_cache_hit'])
            self.assertTrue(case['proof_review_snapshot_current'])
        self.assertGreater(result['provider_usage']['physical_requests'], 0)
        self.assertLessEqual(result['provider_usage']['physical_requests'], 8)

    def test_false_follows_on_wrong_boundary_is_a_hard_failure(self):
        result = self.exercise(ModelTransport(false_follows=True))
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['cases']['wrong_boundary']['status'], 'failed_false_follows')

    def test_incomplete_model_result_never_passes(self):
        result = self.exercise(ModelTransport(incomplete=True))
        self.assertEqual(result['status'], 'inconclusive', result)
        self.assertEqual({r['status'] for r in result['cases'].values()}, {'inconclusive'})

    def test_classifier_does_not_call_unclear_a_detected_bug(self):
        unclear = {'review': {'status': 'done', 'follows': [{'verdict': 'unclear', 'requirements': []}]}}
        self.assertEqual(a.classify('wrong_boundary', unclear), 'inconclusive')
        unclear['review']['follows'][0]['requirements'] = [{'counterexample': {'located': True}}]
        self.assertEqual(a.classify('wrong_boundary', unclear), 'passed')
        unclear['review']['follows'][0]['incomplete'] = True
        self.assertEqual(a.classify('wrong_boundary', unclear), 'inconclusive')

    def test_partial_false_follows_is_still_a_failure(self):
        result = {'review': {'status': 'failed', 'follows': [{'verdict': 'follows', 'incomplete': True}]}}
        self.assertEqual(a.classify('wrong_boundary', result), 'failed_false_follows')


if __name__ == '__main__':
    unittest.main()
