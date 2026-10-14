"""A cached advisory reading belongs to the backend configuration that made it."""
import copy
import hashlib
import json
import os
from unittest.mock import PropertyMock, patch

from test_contract import ContractCase
from bridge import canvas, proof
from bridge.config import Config


class ReviewConfigIdentityTests(ContractCase):
    DIFF = ('diff --git a/billing/usage.py b/billing/usage.py\n'
            '--- a/billing/usage.py\n+++ b/billing/usage.py\n'
            '@@ -1 +1 @@\n-rate = 1\n+rate = 2\n')
    READING = {'verdict': 'follows', 'why': 'Controlled offline reading', 'requirements': []}

    def setUp(self):
        super().setUp()
        self.task = self.start()
        self.decision = self.node(self.task)['node_id']
        self.answer(self.decision, 'Bill at two cents per call.')
        canvas.get_tree(self.store, self.task)

    def finish(self):
        return canvas.finish_task(self.store, {'task_id': self.task, 'diff': self.DIFF})

    def review_events(self):
        return [json.loads(row['detail']) for row in self.store.graph.db.execute(
            "SELECT detail FROM events WHERE run_id=? AND kind='conformance_read' ORDER BY id", (self.task,))]

    def test_enabling_model_refreshes_same_diff_without_rewriting_signed_proof(self):
        with patch.dict(os.environ, {'BRIDGE_SEMANTIC': '0', 'BRIDGE_MODEL_API': 'none'}):
            disabled = self.finish()
            saved = copy.deepcopy(proof.export(self.store, {'task_id': self.task})['bundle'])
        self.assertEqual(disabled['review']['status'], 'done')
        self.assertEqual(disabled['follows'], [])
        original_events = self.review_events()
        with patch.dict(os.environ, {'BRIDGE_SEMANTIC': '1', 'BRIDGE_MODEL_API': 'anthropic'}), \
                patch('bridge.llm.check_conformance', return_value=self.READING) as reader:
            enabled = self.finish()
            repeated = self.finish()
            current = proof.export(self.store, {'task_id': self.task})['bundle']
        self.assertEqual(reader.call_count, 1)
        self.assertNotEqual(enabled['review']['id'], disabled['review']['id'])
        self.assertEqual(repeated['review']['id'], enabled['review']['id'])
        self.assertEqual(enabled['review']['status'], 'done')
        self.assertEqual(enabled['follows'][0]['node_id'], self.decision)
        self.assertEqual(saved['payload']['change'], current['payload']['change'])
        self.assertEqual(saved['payload']['decisions'], current['payload']['decisions'])
        self.assertTrue(proof.verify(saved)['valid'])
        self.assertTrue(proof.verify(current)['valid'])
        self.assertEqual(self.review_events()[:1], original_events)
        self.assertEqual(len(self.review_events()), 2)

    def test_semantic_enabled_state_is_part_of_identity_on_the_same_backend(self):
        with patch.dict(os.environ, {'BRIDGE_MODEL_API': 'anthropic'}):
            disabled = self.finish()
            with patch.dict(os.environ, {'BRIDGE_SEMANTIC': '1'}), \
                    patch('bridge.llm.check_conformance', return_value=self.READING) as reader:
                enabled = self.finish()
        self.assertEqual(reader.call_count, 1)
        self.assertNotEqual(disabled['review']['id'], enabled['review']['id'])

    def test_backend_and_effective_model_changes_refresh_but_unchanged_config_hits_cache(self):
        configurations = [
            {'BRIDGE_MODEL_API': 'anthropic', 'BRIDGE_MODEL': 'review-a', 'BRIDGE_CONFORMANCE_MODEL': 'main'},
            {'BRIDGE_MODEL_API': 'anthropic', 'BRIDGE_MODEL': 'review-b', 'BRIDGE_CONFORMANCE_MODEL': 'main'},
            {'BRIDGE_MODEL_API': 'openai', 'BRIDGE_MODEL': 'review-b', 'BRIDGE_CONFORMANCE_MODEL': 'main'},
            {'BRIDGE_MODEL_API': 'openai', 'BRIDGE_MODEL': 'review-b', 'BRIDGE_CONFORMANCE_MODEL': 'fast'},
            {'BRIDGE_MODEL_API': 'openai', 'BRIDGE_MODEL': 'review-b', 'BRIDGE_CONFORMANCE_MODEL': 'fast',
             'BRIDGE_FAST_MODEL': 'fast-b'},
        ]
        identities = []
        with patch.dict(os.environ, {'BRIDGE_SEMANTIC': '1', 'BRIDGE_FAST_MODEL': 'fast-a'}), \
                patch('bridge.llm.check_conformance', return_value=self.READING) as reader:
            for config in configurations:
                with self.subTest(config=config), patch.dict(os.environ, config):
                    first = self.finish()
                    repeated = self.finish()
                    self.assertEqual(first['review']['id'], repeated['review']['id'])
                    selected = (os.environ['BRIDGE_FAST_MODEL'] if config['BRIDGE_CONFORMANCE_MODEL'] == 'fast'
                                else config['BRIDGE_MODEL'])
                    self.assertEqual(reader.call_args.args[0].model, selected)
                    self.assertEqual(reader.call_args.args[0].fast_model, selected)
                    identities.append(first['review']['id'])
        self.assertEqual(reader.call_count, len(configurations))
        self.assertEqual(len(set(identities)), len(configurations))

    def test_legacy_configless_completed_review_remains_auditable_but_is_not_reused(self):
        signed = canvas._flatten(canvas.get_tree(self.store, self.task)['nodes'])
        diff_hash = hashlib.sha256(self.DIFF.encode()).hexdigest()[:16]
        inputs = json.dumps(canvas._review_inputs(signed), sort_keys=True, separators=(',', ':'))
        legacy_id = hashlib.sha256('\n'.join([self.task, diff_hash, inputs]).encode()).hexdigest()[:12]
        legacy = {'task_id': self.task, 'review_id': legacy_id, 'diff_hash': diff_hash,
                  'status': 'done', 'follows': [], 'revisions': canvas._revisions(signed),
                  'review_inputs': canvas._review_inputs(signed)}
        self.store.graph.append_event('conformance_read', legacy)
        with patch.dict(os.environ, {'BRIDGE_SEMANTIC': '1'}), \
                patch('bridge.llm.check_conformance', return_value=self.READING) as reader:
            self.assertEqual(canvas.get_tree(self.store, self.task)['review']['id'], legacy_id)
            self.assertEqual(reader.call_count, 0, 'read-only state must not start inference')
            refreshed = self.finish()
        self.assertEqual(reader.call_count, 1)
        self.assertNotEqual(refreshed['review']['id'], legacy_id)
        self.assertEqual(self.review_events()[0], legacy)
        self.assertEqual(len(self.review_events()), 2)

    def test_unused_fast_model_and_secret_values_do_not_change_identity_or_enter_events(self):
        signed = canvas._flatten(canvas.get_tree(self.store, self.task)['nodes'])
        with patch.dict(os.environ, {'BRIDGE_SEMANTIC': '1', 'BRIDGE_MODEL_API': 'anthropic',
                                     'BRIDGE_CONFORMANCE_MODEL': 'main', 'BRIDGE_MODEL': 'review-main'}), \
                patch.object(Config, 'api_key', new_callable=PropertyMock,
                             side_effect=AssertionError('The cache must not read credentials')), \
                patch('bridge.llm.check_conformance', return_value=self.READING) as reader:
            with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'synthetic-secret-one', 'BRIDGE_FAST_MODEL': 'unused-a'}):
                before = canvas._review_key(self.task, self.DIFF, signed)
                first = self.finish()
            with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'synthetic-secret-two', 'BRIDGE_FAST_MODEL': 'unused-b'}):
                after = canvas._review_key(self.task, self.DIFF, signed)
                repeated = self.finish()
        self.assertEqual(before, after)
        self.assertEqual(first['review']['id'], repeated['review']['id'])
        self.assertEqual(reader.call_count, 1)
        events = json.dumps(self.review_events())
        for value in ('synthetic-secret-one', 'synthetic-secret-two'):
            self.assertNotIn(value, events)
            self.assertNotIn(hashlib.sha256(value.encode()).hexdigest(), events)
        self.assertEqual(self.review_events()[0]['review_config'], {
            'backend': 'anthropic', 'model': 'review-main', 'enabled': True})
