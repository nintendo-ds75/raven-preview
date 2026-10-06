"""Change evidence survives restarts, detects changes, and never overclaims."""
import copy
import json
from unittest.mock import patch

from test_contract import ContractCase
from bridge import canvas, proof
from bridge.mcp import call_tool
from bridge.store import Invalid, Store

DIFF = "diff --git a/billing/usage.py b/billing/usage.py\n--- a/billing/usage.py\n+++ b/billing/usage.py\n@@ -1 +1 @@\n-rate = 1\n+rate = 2\n"


class ProofTests(ContractCase):
    def complete(self):
        task = self.start()
        node = self.node(task)
        self.answer(node["node_id"], "Bill at two cents per call.")
        canvas.get_tree(self.store, task)
        with patch("bridge.llm.check_conformance", return_value=None):
            result = canvas.finish_task(self.store, {"task_id": task, "diff": DIFF, "checks": "pytest: 12 passed"})
        return task, node, result

    def test_finish_saves_exact_diff_decisions_scope_and_checks(self):
        task, node, result = self.complete()
        exported = call_tool(self.store, "bridge_export_proof", {"task_id": task})
        bundle = exported["bundle"]
        self.assertEqual(result["proof"]["id"], bundle["id"])
        self.assertTrue(exported["integrity"]["valid"])
        self.assertFalse(exported["stale"])
        payload = bundle["payload"]
        self.assertEqual(payload["change"]["diff"], DIFF)
        self.assertEqual(payload["decisions"][0]["node_id"], node["node_id"])
        self.assertEqual(payload["decisions"][0]["signed_by"], "Wes")
        self.assertEqual(payload["checks"]["text"], "pytest: 12 passed")
        self.assertFalse(payload["checks"]["independently_verified"])
        self.assertFalse(exported["integrity"]["authenticity_verified"])
        self.assertIn("not the identity of the exporter or a human digital signature", exported["markdown"])

    def test_offline_integrity_rejects_tampered_diff_answer_and_summary(self):
        task, _, _ = self.complete()
        bundle = proof.export(self.store, {"task_id": task})["bundle"]
        for section, key, value in [("change", "diff", "fake diff"), ("checks", "text", "all passed")]:
            tampered = copy.deepcopy(bundle)
            tampered["payload"][section][key] = value
            self.assertFalse(proof.verify(tampered)["valid"])
        tampered = copy.deepcopy(bundle)
        tampered["payload"]["decisions"][0]["answer"] = "Do not bill."
        self.assertFalse(proof.verify(tampered)["valid"])

    def test_proof_is_durable_and_idempotent(self):
        task, _, _ = self.complete()
        first = proof.export(self.store, {"task_id": task})
        reopened = Store(self.store.path)
        try:
            again = proof.export(reopened, {"task_id": task})
            self.assertEqual(first, again)
            proof.create(reopened, task, DIFF)
            self.assertEqual(reopened.graph.count_events("proof_exported", task_id=task), 1)
        finally:
            reopened.graph.close()

    def test_changed_answer_marks_proof_stale_without_rewriting_history(self):
        task, node, _ = self.complete()
        old = proof.export(self.store, {"task_id": task})["bundle"]
        self.sign(node["node_id"], answer="Waive the charge.", rationale="Corrected after review")
        exported = proof.export(self.store, {"task_id": task})
        self.assertTrue(exported["stale"])
        self.assertEqual(exported["bundle"], old)
        self.assertTrue(exported["integrity"]["valid"])

    def test_unfinished_or_missing_diff_cannot_create_proof(self):
        task = self.start()
        self.node(task)
        with self.assertRaises(Invalid):
            proof.create(self.store, task, DIFF)
        with self.assertRaises(Invalid):
            proof.export(self.store, {"task_id": task})
        completed, _, _ = self.complete()
        with self.assertRaises(Invalid):
            proof.create(self.store, completed, "")

    def test_export_other_task_does_not_return_first_tasks_proof(self):
        task, _, _ = self.complete()
        other = self.start(title="Other task", repo="other/team")
        with self.assertRaises(Invalid):
            proof.export(self.store, {"task_id": other})
        self.assertEqual(proof.export(self.store, {"task_id": task})["bundle"]["payload"]["task"]["id"], task)
