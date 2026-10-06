"""Change evidence survives restarts, detects changes, and never overclaims."""
import copy
import json
import os
import threading
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


class ReviewRecoveryTests(ContractCase):
    """An explicit host retry recovers one failed reading, never loops."""

    def signed_task(self):
        task = self.start()
        node = self.node(task)
        self.answer(node["node_id"], "Bill at two cents per call.")
        call_tool(self.store, "bridge_get_tree", {"task_id": task})
        return task, node

    def finish(self, task):
        return call_tool(self.store, "bridge_finish_task", {
            "task_id": task, "diff": DIFF, "checks": "pytest: 12 passed"})

    def test_explicit_retry_recovers_failed_review_and_preserves_history(self):
        task, node = self.signed_task()
        with patch("bridge.llm.check_conformance", side_effect=RuntimeError("temporary reader failure")) as reader:
            first = self.finish(task)
        self.assertEqual(first["review"]["status"], "failed")
        self.assertEqual(reader.call_count, 1)
        self.assertIn("call bridge_finish_task again with the same diff", first["caveat"])
        old_proof = proof.export(self.store, {"task_id": task})["bundle"]
        # Reading stored state is read-only and must not start another model call.
        reopened = Store(self.store.path)
        self.addCleanup(reopened.graph.close)
        self.assertEqual(canvas.get_tree(reopened, task)["review"]["status"], "failed")
        follows = {"verdict": "follows", "why": "The signed rate is present", "requirements": []}
        with patch("bridge.llm.check_conformance", return_value=follows) as reader:
            recovered = call_tool(reopened, "bridge_finish_task", {
                "task_id": task, "diff": DIFF, "checks": "pytest: 12 passed"})
            repeated = call_tool(reopened, "bridge_finish_task", {"task_id": task, "diff": DIFF})
        self.assertEqual(recovered["review"]["status"], "done")
        self.assertEqual(recovered["review"]["id"], first["review"]["id"])
        self.assertEqual(repeated["follows"], recovered["follows"])
        self.assertEqual(reader.call_count, 1, "a successful review is still idempotent")
        self.assertEqual(self.store.graph.count_events("conformance_started", task_id=task), 2)
        history = self.store.graph.db.execute(
            "SELECT detail FROM events WHERE run_id=? AND kind='conformance_read' ORDER BY id", (task,)).fetchall()
        self.assertEqual([json.loads(row["detail"])["status"] for row in history], ["failed", "done"])
        latest = proof.export(reopened, {"task_id": task})["bundle"]
        self.assertNotEqual(latest["id"], old_proof["id"])
        self.assertEqual(old_proof["payload"]["review"]["status"], "failed")
        self.assertEqual(latest["payload"]["review"]["status"], "done")
        self.assertTrue(proof.verify(latest)["valid"])

    def test_each_failed_explicit_retry_is_one_attempt(self):
        task, _ = self.signed_task()
        with patch("bridge.llm.check_conformance", side_effect=RuntimeError("still unavailable")) as reader:
            for _ in range(2):
                self.assertEqual(self.finish(task)["review"]["status"], "failed")
            self.assertEqual(reader.call_count, 2)
        self.assertEqual(self.store.graph.count_events("conformance_started", task_id=task), 2)

    def test_retried_review_is_shared_while_running(self):
        task, _ = self.signed_task()
        with patch("bridge.llm.check_conformance", side_effect=RuntimeError("temporary reader failure")):
            first = self.finish(task)
        release = threading.Event()

        def slow(*args):
            if not release.wait(5):
                raise RuntimeError("test did not release the reader")
            return {"verdict": "follows", "why": "The signed rate is present", "requirements": []}

        with patch("bridge.llm.check_conformance", side_effect=slow) as reader, patch.object(canvas, "FINISH_WAIT", 0.01):
            try:
                one = self.finish(task)
                two = self.finish(task)
                self.assertEqual(one["review"]["status"], "running")
                self.assertEqual(two["review"]["status"], "running")
                self.assertEqual(one["review"]["id"], first["review"]["id"])
                self.assertEqual(reader.call_count, 1)
            finally:
                release.set()
                with canvas._REVIEWS_LOCK:
                    thread = canvas._REVIEWS.get(first["review"]["id"])
                if thread is not None:
                    thread.join(5)
        self.assertEqual(canvas.get_tree(self.store, task)["review"]["status"], "done")

    def test_provider_failure_is_not_cached_as_a_completed_empty_review(self):
        from bridge.llm import LLMError
        task, _ = self.signed_task()
        with patch.dict(os.environ, {"BRIDGE_SEMANTIC": "1"}), \
                patch("bridge.llm.Client.complete_json", side_effect=LLMError("temporary provider outage")) as provider:
            first = self.finish(task)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(first["review"]["status"], "failed")
        self.assertEqual(first["follows"], [])
        with patch("bridge.llm.check_conformance", return_value={
                "verdict": "follows", "why": "The signed rate is present", "requirements": []}):
            recovered = self.finish(task)
        self.assertEqual(recovered["review"]["status"], "done")

    def test_partial_review_keeps_findings_and_names_retry(self):
        task, node = self.signed_task()
        other = self.node(task, question="Should the invoice list the included allowance?", context="Invoice detail")
        self.answer(other["node_id"], "Show the allowance on the invoice.")
        call_tool(self.store, "bridge_get_tree", {"task_id": task})

        def partial(cfg, question, answer, diff, others):
            if "allowance" in question:
                return {}
            return {"verdict": "follows", "why": "The signed rate is present", "requirements": []}

        with patch.dict(os.environ, {"BRIDGE_SEMANTIC": "1"}), \
                patch("bridge.llm.check_conformance", side_effect=partial):
            first = self.finish(task)
        self.assertEqual(first["review"]["status"], "failed")
        self.assertEqual([item["node_id"] for item in first["follows"]], [node["node_id"]])
        self.assertEqual([item["node_id"] for item in first["unread"]], [other["node_id"]])
        self.assertIn("call bridge_finish_task again with the same diff", first["caveat"])

    def test_disabled_provider_does_not_claim_a_failed_attempt(self):
        task, _ = self.signed_task()
        first = self.finish(task)
        self.assertEqual(first["review"]["status"], "done")
        self.assertEqual(first["follows"], [])
        self.assertIn("Raven did not read the diff", first["caveat"])
