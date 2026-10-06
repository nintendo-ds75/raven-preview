"""Change evidence survives restarts, detects changes, and never overclaims."""
import copy
from contextlib import contextmanager
import hashlib
import json
import os
import threading
from unittest.mock import patch

from test_contract import ContractCase
from bridge import canvas, proof
from bridge.mcp import TOOLS, call_tool
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

    def test_export_with_no_review_does_not_invent_an_identity_or_status(self):
        task = self.start(title="Fix a documentation typo")
        diff = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-teh\n+the\n"
        canvas.get_tree(self.store, task)
        canvas.finish_task(self.store, {"task_id": task, "diff": diff})
        exported = proof.export(self.store, {"task_id": task})
        self.assertIsNone(exported['bundle']['payload']['review'])
        self.assertIsNone(exported['review'])
        self.assertTrue(exported['review_snapshot_current'])
        self.assertFalse(exported['review_pending'])
        comparison = exported['review_comparison']
        self.assertIsNone(comparison['saved'])
        self.assertIsNone(comparison['current'])
        self.assertIsNone(comparison['same_review_id'])
        self.assertIn('no advisory review recorded', comparison['description'])

    def test_comparison_does_not_infer_identity_when_either_id_is_absent(self):
        for saved, current in ((None, {'id': 'review-b', 'status': 'done'}),
                               ({'id': 'review-a', 'status': 'running'}, None),
                               ({'status': 'done'}, {'status': 'done'})):
            with self.subTest(saved=saved, current=current):
                comparison = proof._review_comparison(saved, current)
                self.assertIsNone(comparison['same_review_id'])
                self.assertNotIn('IDs match', comparison['description'])

    def test_human_answer_can_refine_options_without_rewriting_proposals(self):
        task = self.start()
        proposals = ['Bill one cent per call.', 'Waive the charge.']
        node = self.node(task, options=' | '.join(proposals))
        answer = 'Bill two cents per call, with the first ten calls free.'
        self.answer(node['node_id'], answer)
        record = call_tool(self.store, 'bridge_get_decision', {'decision_id': node['node_id']})
        self.assertEqual(json.loads(record['options']), proposals)
        self.assertEqual(record['answer'], answer)
        self.assertNotIn(answer, proposals)
        self.assertTrue(record['authorized'])
        matches = call_tool(self.store, 'bridge_search_decisions', {
            'query': 'usage charge', 'repo': 'acme/platform'})['matches']
        found = next(item for item in matches if item['id'] == node['node_id'])
        self.assertEqual(found['answer'], answer)
        self.assertEqual(json.loads(found['options']), proposals)
        descriptions = {tool['name']: tool['description'] for tool in TOOLS}
        for name in ('bridge_get_decision', 'bridge_search_decisions'):
            self.assertIn('options are proposals', descriptions[name])
            self.assertIn('refine', descriptions[name])
            self.assertIn('outside them', descriptions[name])
        guidance = descriptions['bridge_export_proof']
        for text in ('bundle.payload.review', 'top-level review', 'review_comparison',
                     'does not imply a new review ID or attempt', 'without rewriting history'):
            self.assertIn(text, guidance)


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

    @contextmanager
    def held_review(self):
        release, entered = threading.Event(), threading.Event()
        coordinators = []
        run_review = canvas._run_review

        def coordinated_review(*args):
            coordinators.append(threading.current_thread())
            return run_review(*args)

        def slow(*args):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test did not release the reader")
            return {"verdict": "follows", "why": "The signed rate is present", "requirements": []}

        def finish_reading():
            release.set()
            # Wait for the real coordinator to persist conformance_read,
            # not just for its inner provider worker to return.
            for coordinator in coordinators:
                coordinator.join(5)
                self.assertFalse(coordinator.is_alive())

        with patch("bridge.llm.check_conformance", side_effect=slow) as reader, \
                patch.object(canvas, "_run_review", side_effect=coordinated_review):
            try:
                yield reader, entered, finish_reading
            finally:
                finish_reading()

    def test_initial_finish_guidance_tracks_the_review_it_started(self):
        task, _ = self.signed_task()
        with self.held_review() as (reader, entered, finish_reading), patch.object(canvas, "FINISH_WAIT", 0):
            first = self.finish(task)
            self.assertTrue(entered.wait(5))
            self.assertEqual(first["review"]["status"], "running")
            self.assertEqual(first["guidance_snapshot"]["review"], {
                "id": first["review"]["id"], "status": "running"})
            self.assertIn("review is still running", first["next"])
            self.assertIn("bridge_wait", first["next"])
            self.assertNotIn("review failed", first["next"])
            saved = proof.export(self.store, {"task_id": task})["bundle"]
            self.assertEqual(saved["payload"]["change"], {
                "diff": DIFF, "sha256": hashlib.sha256(DIFF.encode()).hexdigest(), "bytes": len(DIFF.encode())})
            self.assertEqual(saved["payload"]["review"]["status"], "running")
            finish_reading()
            tree = canvas.get_tree(self.store, task)
            exported = proof.export(self.store, {"task_id": task})
            self.assertEqual(tree["review"]["status"], "done")
            self.assertEqual(exported["bundle"], saved)
            self.assertTrue(exported["integrity"]["valid"])
            refreshed = self.finish(task)
            self.assertEqual(refreshed["review"]["status"], "done")
            self.assertNotIn("review is still running", refreshed["next"])
            self.assertNotIn("review failed", refreshed["next"])
            self.assertEqual(reader.call_count, 1)
            self.assertEqual(self.store.graph.count_events("conformance_started", task_id=task), 1)

    def test_explicit_retry_recovers_failed_review_and_preserves_history(self):
        task, node = self.signed_task()
        with patch("bridge.llm.check_conformance", side_effect=RuntimeError("temporary reader failure")) as reader:
            first = self.finish(task)
        self.assertEqual(first["review"]["status"], "failed")
        self.assertIn("review failed", first["next"])
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
        self.assertNotIn("review failed", recovered["next"])
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
        with self.held_review() as (reader, entered, _), patch.object(canvas, "FINISH_WAIT", 0):
            one = self.finish(task)
            self.assertTrue(entered.wait(5))
            two = self.finish(task)
            for result in (one, two):
                self.assertEqual(result["review"]["status"], "running")
                self.assertEqual(result["guidance_snapshot"]["review"], {
                    "id": result["review"]["id"], "status": "running"})
                self.assertIn("review is still running", result["next"])
                self.assertIn("bridge_wait", result["next"])
                self.assertNotIn("review failed", result["next"])
            self.assertEqual(one["review"]["id"], first["review"]["id"])
            self.assertEqual(reader.call_count, 1)
        self.assertEqual(canvas.get_tree(self.store, task)["review"]["status"], "done")

    def test_finish_guidance_separates_a_correction_during_the_review_wait(self):
        task = self.start()
        node = self.node(task)
        self.settle(task, node["node_id"], "Bill at two cents per call.")
        self.sign(node["node_id"])
        before = call_tool(self.store, "bridge_get_tree", {"task_id": task})
        self.assertEqual(before["counts"]["resolved"], 1)
        receipt = self.store.graph.get_task(task)["agent_read_events"]

        with self.held_review() as (reader, entered, finish_reading):
            def correct_while_waiting(seconds):
                self.assertTrue(entered.wait(5))
                self.sign(node["node_id"], answer="Waive the charge.", rationale="Corrected during review")
                finish_reading()

            result = call_tool(self.store, "bridge_finish_task", {
                "task_id": task, "diff": DIFF, "checks": "pytest: 12 passed"}, sleep=correct_while_waiting)
            self.assertEqual(reader.call_count, 1)
            self.assertEqual(reader.call_args.args[2], "Bill at two cents per call.")
            # The submitted review still describes the old signed answer.
            self.assertEqual(result["review"]["status"], "done")
            self.assertEqual(result["follows"][0]["verdict"], "follows")
            self.assertIn("needs refreshing", result["next"])
            self.assertNotIn("resolved", result["counts"])
            self.assertEqual(result["counts"]["answered"], 1)
            current = result["guidance_snapshot"]
            self.assertEqual(current["review"], {
                "id": result["review"]["id"], "status": "stale", "read_status": "done"})
            self.assertIn("next and counts", current["description"])
            self.assertIn("earlier signed-answer snapshot", current["description"])
            self.assertIn("does not acknowledge", current["description"])
            self.assertEqual(current["task_status"], "completed")
            self.assertFalse(current["needs_review"])
            self.assertTrue(current["observed_at"])
            self.assertEqual(self.store.graph.get_task(task)["agent_read_events"], receipt)
            self.assertEqual([n["node_id"] for n in canvas.unread_by_agent(self.store, task)], [node["node_id"]])
            with self.assertRaisesRegex(Invalid, "have not seen"):
                self.finish(task)
            exported = proof.export(self.store, {"task_id": task})
            self.assertEqual(exported["bundle"]["payload"]["change"]["diff"], DIFF)
            self.assertEqual(exported["bundle"]["payload"]["review"]["status"], "stale")
            self.assertTrue(exported["integrity"]["valid"])
            self.assertEqual(reader.call_count, 1, "guidance, export and a refused retry cannot start inference")

    def test_provider_failure_is_not_cached_as_a_completed_empty_review(self):
        from bridge.llm import LLMError
        task, _ = self.signed_task()
        with patch.dict(os.environ, {"BRIDGE_SEMANTIC": "1"}), \
                patch("bridge.llm.Client.complete_json", side_effect=LLMError("temporary provider outage")) as provider:
            first = self.finish(task)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(first["review"]["status"], "failed")
        self.assertEqual(first["follows"][0]["verdict"], "unclear")
        self.assertTrue(first["follows"][0]["incomplete"])
        self.assertIn("inconclusive", first["follows"][0]["why"])
        with patch("bridge.llm.check_conformance", return_value={
                "verdict": "follows", "why": "The signed rate is present", "requirements": []}):
            recovered = self.finish(task)
        self.assertEqual(recovered["review"]["status"], "done")

    def test_incomplete_structured_review_is_saved_with_findings_and_can_be_retried(self):
        task, _ = self.signed_task()
        finding = {'needs': 'Preserve the signed rate', 'state': 'unclear',
                   'counterexample': {'what': 'A concrete path needs checking.', 'at': 'rate = 2', 'located': True}}
        partial = {'verdict': 'unclear', 'why': 'The search is inconclusive.', 'requirements': [finding],
                   'unexamined': ['The remaining paths were not checked.'], 'incomplete': True}
        with patch.dict(os.environ, {"BRIDGE_SEMANTIC": "1"}), \
                patch("bridge.llm.check_conformance", return_value=partial) as reader:
            first = self.finish(task)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(first['review']['status'], 'failed')
        self.assertEqual(first['follows'][0]['requirements'], [finding])
        self.assertTrue(first['follows'][0]['incomplete'])
        self.assertIn('call bridge_finish_task again with the same diff', first['caveat'])
        exported = proof.export(self.store, {'task_id': task})['bundle']
        self.assertEqual(exported['payload']['review']['status'], 'failed')
        self.assertTrue(exported['payload']['review']['follows'][0]['incomplete'])
        with patch('bridge.llm.check_conformance', return_value={
                'verdict': 'follows', 'why': 'Complete advisory read.', 'requirements': []}) as retry:
            recovered = self.finish(task)
        self.assertEqual(retry.call_count, 1)
        self.assertEqual(recovered['review']['status'], 'done')
        self.assertEqual(exported['payload']['review']['status'], 'failed')

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

    def pending_review(self):
        task, node = self.signed_task()
        release = threading.Event()
        patcher = patch('bridge.llm.check_conformance', side_effect=lambda *args: (
            release.wait(5) and {'verdict': 'follows', 'why': 'The rate is present', 'requirements': []}))
        patcher.start()
        self.addCleanup(patcher.stop)
        with patch.object(canvas, 'FINISH_WAIT', 0.01):
            result = self.finish(task)
        self.assertEqual(result['review']['status'], 'running')

        def finish_reading(*args):
            release.set()
            with canvas._REVIEWS_LOCK:
                worker = canvas._REVIEWS.get(result['review']['id'])
            if worker is not None:
                worker.join(5)
        self.addCleanup(finish_reading)
        return task, node, result, finish_reading

    def test_tree_and_export_explain_pending_advisory_review(self):
        task, _, _, _ = self.pending_review()
        tree = canvas.get_tree(self.store, task)
        self.assertIn('bridge_wait', tree['next'])
        self.assertIn('review', tree['next'])
        exported = proof.export(self.store, {'task_id': task})
        self.assertTrue(exported['review_pending'])
        self.assertTrue(exported['review_snapshot_current'])
        self.assertIn('bridge_wait', exported['next'])
        self.assertNotIn('Attach the bundle', exported['next'])

    def test_whole_task_wait_returns_terminal_advisory_review(self):
        task, _, _, finish_reading = self.pending_review()
        observed = call_tool(self.store, 'bridge_get_tree', {'task_id': task})['observed_at']
        waited = canvas.wait(self.store, {'task_id': task, 'timeout': '1', 'since': observed}, sleep=finish_reading)
        self.assertFalse(waited['timed_out'])
        self.assertEqual(waited['review']['status'], 'done')
        self.assertEqual(waited['waiting_on'], [])
        self.assertIn('bridge_finish_task', waited['next'])

    def test_review_wait_timeout_and_interruption_remain_bounded(self):
        task, _, _, _ = self.pending_review()
        waited = canvas.wait(self.store, {'task_id': task, 'timeout': '0'})
        self.assertTrue(waited['timed_out'])
        self.assertEqual(waited['review']['status'], 'running')
        self.assertIn('review', waited['next'])
        self.assertNotIn('nobody yet', waited['next'])
        stop = threading.Event(); stop.set()
        interrupted = canvas.wait(self.store, {'task_id': task, 'timeout': '1'}, stop=stop)
        self.assertIn('interrupted', interrupted)
        self.assertEqual(interrupted['next'], canvas.STOPPED_NEXT)

    def test_export_keeps_snapshot_but_exposes_completed_live_review(self):
        task, _, _, finish_reading = self.pending_review()
        before = proof.export(self.store, {'task_id': task})
        saved_bytes = json.dumps(before['bundle'], sort_keys=True)
        exports = self.store.graph.count_events('proof_exported', task_id=task)
        finish_reading()
        after = proof.export(self.store, {'task_id': task})
        self.assertEqual(self.store.graph.count_events('proof_exported', task_id=task), exports)
        self.assertEqual(after['bundle'], before['bundle'])
        self.assertEqual(json.dumps(after['bundle'], sort_keys=True), saved_bytes)
        self.assertEqual(after['markdown'], before['markdown'])
        self.assertEqual(after['bundle']['payload']['review']['status'], 'running')
        self.assertEqual(after['review']['status'], 'done')
        self.assertFalse(after['review_pending'])
        self.assertFalse(after['review_snapshot_current'])
        comparison = after['review_comparison']
        review_id = before['review']['id']
        self.assertEqual(comparison['saved'], {'id': review_id, 'status': 'running'})
        self.assertEqual(comparison['current'], {'id': review_id, 'status': 'done'})
        self.assertTrue(comparison['same_review_id'])
        self.assertIn('Saved bundle.payload.review: id=' + review_id + ', status=running', comparison['description'])
        self.assertIn('Current top-level review: id=' + review_id + ', status=done', comparison['description'])
        self.assertIn('A status change alone does not establish a new attempt or pass', comparison['description'])
        self.assertIn('has not been rewritten', comparison['description'])
        self.assertEqual(self.store.graph.count_events('conformance_started', task_id=task), 1)
        self.assertTrue(after['integrity']['valid'])
        self.assertFalse(after['stale'])
        self.assertIn('bridge_finish_task', after['next'])
        self.finish(task)
        refreshed = proof.export(self.store, {'task_id': task})
        self.assertEqual(refreshed['bundle']['payload']['review']['status'], 'done')
        self.assertTrue(refreshed['review_snapshot_current'])
        self.assertEqual(refreshed['review_comparison']['saved'], refreshed['review_comparison']['current'])
        self.assertIn('Attach the bundle', refreshed['next'])

    def test_export_distinguishes_a_different_review_id_without_replacing_saved_proof(self):
        task, node = self.signed_task()
        finding = {'verdict': 'follows', 'why': 'The rate is present', 'requirements': []}
        with patch('bridge.llm.check_conformance', return_value=finding):
            self.finish(task)
            before = proof.export(self.store, {'task_id': task})
            signed = [canvas.node_view(self.store, node['node_id'])]
            current = canvas._review(self.store, task, signed, DIFF + '\n')
        after = proof.export(self.store, {'task_id': task})
        comparison = after['review_comparison']
        self.assertFalse(comparison['same_review_id'])
        self.assertEqual(comparison['saved'], {'id': before['review']['id'], 'status': 'done'})
        self.assertEqual(comparison['current'], {'id': current['id'], 'status': 'done'})
        self.assertIn('review IDs differ', comparison['description'])
        self.assertFalse(after['review_snapshot_current'])
        self.assertEqual(after['bundle'], before['bundle'])
        self.assertTrue(after['integrity']['valid'])
        self.assertEqual(self.store.graph.count_events('proof_exported', task_id=task), 1)

    def test_same_review_id_does_not_claim_the_same_attempt_after_a_retry(self):
        task, node = self.signed_task()
        with patch('bridge.llm.check_conformance', side_effect=RuntimeError('temporary reader failure')):
            self.finish(task)
        before = proof.export(self.store, {'task_id': task})
        with patch('bridge.llm.check_conformance', return_value={
                'verdict': 'follows', 'why': 'The rate is present', 'requirements': []}):
            canvas._review(self.store, task, [canvas.node_view(self.store, node['node_id'])], DIFF)
        after = proof.export(self.store, {'task_id': task})
        comparison = after['review_comparison']
        self.assertTrue(comparison['same_review_id'])
        self.assertEqual(comparison['saved']['status'], 'failed')
        self.assertEqual(comparison['current']['status'], 'done')
        self.assertNotIn('same_attempt', comparison)
        self.assertIn('does not identify a unique attempt', comparison['description'])
        self.assertEqual(after['bundle'], before['bundle'])
        self.assertFalse(after['review_snapshot_current'])
        self.assertEqual(self.store.graph.count_events('conformance_started', task_id=task), 2)

    def test_failed_review_export_instructs_retry_without_claiming_conformance(self):
        task, _ = self.signed_task()
        with patch('bridge.llm.check_conformance', side_effect=RuntimeError('reader unavailable')):
            self.finish(task)
        exported = proof.export(self.store, {'task_id': task})
        self.assertEqual(exported['review']['status'], 'failed')
        self.assertIn('bridge_finish_task', exported['next'])
        self.assertIn('does not establish conformance', exported['next'])
        self.assertNotIn('Attach the bundle', exported['next'])
