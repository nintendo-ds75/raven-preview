"""Standing permission must remain valid until unfinished work completes."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from test_rules import CFG, QUESTION, RuleCase
from test_proof import DIFF
from bridge import canvas, proof
from bridge.store import Invalid, Store


class RuleInvalidationTests(RuleCase):
    def covered(self, applicability=None):
        source = self.signed_answer()
        if applicability:
            current = self.store.get_decision(source)
            self.store.answer(source, {"answer": current["answer"], "applicability": applicability,
                                      "expected_updated_at": current["updated_at"]})
        self.rule(source)
        task = self.task("covered")
        node = self.node(task, ref="covered")
        self.assertTrue(node["authorized"])
        return source, task, node

    def assert_invalidated(self, task, *nodes):
        for node in nodes:
            view = canvas.node_view(self.store, node["node_id"])
            self.assertFalse(view["authorized"], view)
            self.assertTrue(view["needs_review"], view)
            self.assertEqual(view["signatures"], [])
        with self.assertRaises(Invalid):
            canvas.finish_task(self.store, {"task_id": task})

    def test_narrowing_rule_conditions_invalidates_outstanding_reuse(self):
        source, task, node = self.covered()
        changed = self.rule(source, conditions="plan=starter")
        self.assertIn(node["node_id"], changed["invalidated"])
        self.assert_invalidated(task, node)
        self.assertEqual(self.store.get_decision(node["node_id"])["answer"], "Round half up to whole cents")

    def test_changing_scope_or_expiry_requires_review_but_identical_rule_does_not(self):
        source, task, node = self.covered()
        self.assertEqual(self.rule(source)["invalidated"], [])
        self.assertTrue(canvas.node_view(self.store, node["node_id"])["authorized"])
        self.rule(source, scope="any")
        self.assert_invalidated(task, node)
        later = self.node(self.task("later"), ref="later")
        self.assertTrue(later["authorized"])
        self.rule(source, scope="any", expires=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        self.assert_invalidated(later["task_id"], later)

    def test_structured_applicability_expiry_invalidates_existing_reuse(self):
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        _, task, node = self.covered({"valid_until": expiry.isoformat()})
        with patch("bridge.graph.time.time", return_value=(expiry + timedelta(seconds=1)).timestamp()):
            self.assert_invalidated(task, node)
        self.assertIn("expired", canvas.node_view(self.store, node["node_id"])["review_reason"])

    def test_existing_database_backfills_live_rule_checks_after_restart(self):
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        _, task, node = self.covered({"valid_until": expiry.isoformat()})
        # Simulate a pre-migration database that already has standing reuse.
        self.graph.db.execute("DELETE FROM settings WHERE key='rule_checks_needed'")
        reopened = Store(self.store.path)
        self.addCleanup(reopened.graph.close)
        self.assertEqual(reopened.graph.get_setting("rule_checks_needed"), "1")
        with patch("bridge.graph.time.time", return_value=(expiry + timedelta(seconds=1)).timestamp()):
            self.assertFalse(canvas.node_view(reopened, node["node_id"])["authorized"])
            with self.assertRaises(Invalid):
                canvas.finish_task(reopened, {"task_id": task})

    def test_negative_marker_is_recomputed_after_legacy_rows_are_imported(self):
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        _, task, node = self.covered({"valid_until": expiry.isoformat()})
        # import_database initializes the target before copying legacy rows,
        # then reopens Store to apply the backfill. A cached negative from
        # before that copy must not suppress the newly imported rule.
        self.graph.set_setting("rule_checks_needed", "0")
        reopened = Store(self.store.path)
        self.addCleanup(reopened.graph.close)
        self.assertEqual(reopened.graph.get_setting("rule_checks_needed"), "1")
        with patch("bridge.graph.time.time", return_value=(expiry + timedelta(seconds=1)).timestamp()):
            self.assertFalse(canvas.node_view(reopened, node["node_id"])["authorized"])
            with self.assertRaises(Invalid):
                canvas.finish_task(reopened, {"task_id": task})

    def test_empty_import_destination_has_no_synthetic_settings_row(self):
        empty = Store(Path(self.temp.name) / "empty-import-target.db")
        self.addCleanup(empty.graph.close)
        self.assertIsNone(empty.graph.db.execute("SELECT 1 FROM settings LIMIT 1").fetchone())

    def test_old_negative_marker_is_removed_from_empty_import_destination(self):
        empty = Store(Path(self.temp.name) / "old-empty-import-target.db")
        self.addCleanup(empty.graph.close)
        empty.graph.set_setting("rule_checks_needed", "0")
        reopened = Store(empty.path)
        self.addCleanup(reopened.graph.close)
        self.assertIsNone(reopened.graph.db.execute("SELECT 1 FROM settings LIMIT 1").fetchone())

    def supersede(self, source):
        task = self.task("replacement", title="Change rounding policy")
        node = canvas.add_node(self.store, CFG, {"task_id": task,
            "question": "Should we change invoice rounding to use half even instead?",
            "context": "Replacement policy", "paths": "billing/rates.py"})
        self.store.answer(node["node_id"], {"answer": "Round half even", "rationale": "Revised policy",
                                          "supersedes": source})
        return node

    def test_superseding_source_invalidates_outstanding_reuse(self):
        source, task, node = self.covered()
        replacement = self.supersede(source)
        self.assert_invalidated(task, node)
        self.assertTrue(canvas.node_view(self.store, replacement["node_id"])["authorized"])
        self.assertEqual(self.store.get_decision(source)["superseded_by"], replacement["node_id"])

    def mixed_chain(self, task, node):
        child = canvas.add_node(self.store, CFG, {"task_id": task,
            "question": "What should the invoice show for rounded charges?", "context": "Invoice presentation",
            "paths": "billing/rates.py", "parent_id": node["node_id"]})
        self.store.answer(child["node_id"], {"answer": "Show the rounded amount."})
        explicit = canvas.add_node(self.store, CFG, {"task_id": task,
            "question": "What message should the receipt contain?", "context": "Receipt presentation",
            "paths": "billing/rates.py", "depends_on": [child["node_id"]]})
        self.store.answer(explicit["node_id"], {"answer": "State the displayed amount was rounded."})
        other = self.task("other", title="Review receipt behavior")
        reused = canvas.add_node(self.store, CFG, {"task_id": other,
            "question": explicit["question"], "context": "Receipt presentation", "paths": "billing/rates.py"})
        canvas.sign_off(self.store, reused["node_id"], {"by": "Priya Natarajan",
            "expected_updated_at": reused["updated_at"]})
        self.assertEqual(self.store.get_decision(reused["node_id"])["source_id"], explicit["node_id"])
        return child, explicit, reused

    def test_rule_change_invalidates_mixed_transitive_dependencies_and_saves_signatures(self):
        source, task, node = self.covered()
        child, explicit, reused = self.mixed_chain(task, node)
        before = self.store.get_decision(child["node_id"])
        self.rule(source, conditions="plan=starter")
        self.assert_invalidated(task, node, child, explicit)
        self.assert_invalidated(reused["task_id"], reused)
        history = [json.loads(event["detail"]) for event in self.store.get_decision(child["node_id"])["events"]
                   if event["kind"] == "dependent_flagged"]
        self.assertEqual(history[-1]["previous_authorization"]["signatures"], before["signatures"])
        # A read repeats no writes or notifications for already invalidated nodes.
        count = self.graph.count_events("dependent_flagged")
        canvas.get_tree(self.store, task)
        self.assertEqual(self.graph.count_events("dependent_flagged"), count)

    def check_mixed_invalidation(self, method):
        source, task, node = self.covered()
        children = self.mixed_chain(task, node)
        if method == "supersede":
            self.supersede(source)
        else:
            row = self.store.get_decision(source)
            self.store.answer(source, {"answer": "Round half even", "expected_updated_at": row["updated_at"]})
        self.assert_invalidated(task, node, *children[:2])
        self.assert_invalidated(children[-1]["task_id"], children[-1])

    def test_supersession_reaches_mixed_transitive_dependencies(self):
        self.check_mixed_invalidation("supersede")

    def test_correction_reaches_mixed_transitive_dependencies(self):
        self.check_mixed_invalidation("correct")

    def test_explicit_sweep_clock_applies_to_structured_expiry(self):
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        _, task, node = self.covered({"valid_until": expiry.isoformat()})
        self.graph.expire_rules(now=(expiry + timedelta(seconds=1)).isoformat())
        self.assert_invalidated(task, node)

    def save_proof(self, task):
        canvas.get_tree(self.store, task)
        with patch("bridge.llm.check_conformance", return_value=None):
            canvas.finish_task(self.store, {"task_id": task, "diff": DIFF})
        return proof.export(self.store, {"task_id": task})["bundle"]

    def test_ended_or_changed_rule_preserves_completed_history_and_proof(self):
        source, task, node = self.covered()
        old = self.save_proof(task)
        self.rule(source, conditions="plan=starter")
        self.rule(source, end=True)
        exported = proof.export(self.store, {"task_id": task})
        self.assertFalse(exported["stale"])
        self.assertEqual(exported["bundle"], old)
        self.assertTrue(canvas.node_view(self.store, node["node_id"])["authorized"])
        # Reopening is new work: it must pass today's rule boundaries again.
        self.store.update_run(task, {"status": "working"})
        self.assert_invalidated(task, node)
        self.assertTrue(proof.export(self.store, {"task_id": task})["stale"])

    def test_supersession_stales_completed_proof_without_rewriting_it(self):
        source, task, node = self.covered()
        old = self.save_proof(task)
        self.supersede(source)
        exported = proof.export(self.store, {"task_id": task})
        self.assertTrue(exported["stale"])
        self.assertEqual(exported["bundle"], old)
        self.assertTrue(exported["integrity"]["valid"])
        self.assertFalse(canvas.node_view(self.store, node["node_id"])["authorized"])

    def test_historical_lookup_remains_evidence_even_for_live_rule(self):
        source, _, _ = self.covered()
        history = canvas.add_node(self.store, CFG, {"task_id": self.task("history"),
            "question": f"Who decided decision {source}, and what was the reason?", "paths": "billing/rates.py"})
        self.assertFalse(history["authorized"])
        self.assertTrue(history["blocking"])
        self.assertNotEqual(history["signoff"], "rule")
