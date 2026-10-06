"""A new reader contract needs an explicit new finish, never silent history edits."""
import copy
import hashlib
from unittest.mock import patch

from bridge import canvas, llm, proof
from bridge.mcp import call_tool
from test_contract import ContractCase

DIFF = 'diff --git a/billing/usage.py b/billing/usage.py\n--- a/billing/usage.py\n+++ b/billing/usage.py\n@@ -1 +1 @@\n-rate = 1\n+rate = 2\n'


class ReviewContractVersionTests(ContractCase):
    def signed(self):
        task = self.start()
        node = self.node(task)
        self.answer(node['node_id'], 'Bill at two cents per call.')
        call_tool(self.store, 'bridge_get_tree', {'task_id': task})
        return task

    def finish(self, task):
        return call_tool(self.store, 'bridge_finish_task', {'task_id': task, 'diff': DIFF})

    def test_contract_version_changes_key_without_changing_diff_identity(self):
        signed = [{'node_id': 'n', 'answer': 'Use two cents.'}]
        current, diff_hash = canvas._review_key('t', DIFF, signed)
        revisions = sorted(f'{k}:{v}' for k, v in canvas._revisions(signed).items())
        old_key = hashlib.sha256('\n'.join(['t', diff_hash, *revisions]).encode()).hexdigest()[:12]
        self.assertNotEqual(current, old_key)
        with patch.object(llm, 'REVIEW_CONTRACT_VERSION', 'older-contract'):
            changed, same_diff = canvas._review_key('t', DIFF, signed)
        self.assertNotEqual(current, changed)
        self.assertEqual(diff_hash, same_diff)

    def test_explicit_finish_upgrades_contract_while_reads_and_old_proof_stay_immutable(self):
        task = self.signed()
        old = {'verdict': 'follows', 'why': 'An older reading.', 'requirements': []}
        with patch.object(llm, 'REVIEW_CONTRACT_VERSION', 'older-contract'), patch.object(llm, 'check_conformance', return_value=old):
            first = self.finish(task)
        old_bundle = copy.deepcopy(proof.export(self.store, {'task_id': task})['bundle'])
        with patch.object(llm, 'check_conformance') as reader:
            tree = canvas.get_tree(self.store, task)
            exported = proof.export(self.store, {'task_id': task})
        reader.assert_not_called()
        self.assertEqual(tree['review']['contract_version'], 'older-contract')
        self.assertFalse(tree['review']['contract_current'])
        self.assertIn('Read-only access does not rerun', tree['review']['contract_notice'])
        self.assertEqual(exported['bundle'], old_bundle)
        new = {'verdict': 'unclear', 'why': 'No grounded witness.', 'requirements': []}
        with patch.object(llm, 'check_conformance', return_value=new) as reader:
            current = self.finish(task)
        reader.assert_called_once()
        self.assertNotEqual(first['review']['id'], current['review']['id'])
        self.assertEqual(current['review']['contract_version'], llm.REVIEW_CONTRACT_VERSION)
        self.assertEqual(current['follows'][0]['verdict'], 'unclear')
        self.assertEqual(old_bundle['payload']['review']['contract_version'], 'older-contract')
        self.assertTrue(proof.verify(old_bundle)['valid'])

    def test_missing_contract_metadata_is_labeled_legacy_without_inference(self):
        task = self.signed()
        self.store.graph.append_event('conformance_read', {'task_id': task, 'review_id': 'legacy-key',
            'status': 'done', 'follows': [], 'diff_hash': 'old-hash', 'revisions': {}})
        with patch.object(llm, 'check_conformance') as reader:
            stored = canvas._stored_review(self.store.graph, task)
        reader.assert_not_called()
        self.assertEqual(stored['contract_version'], 'legacy')
