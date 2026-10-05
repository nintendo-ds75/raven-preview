"""A prior answer is reusable only inside its human-declared boundaries;
retrieved records alone are context, not verified answers."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bridge.config import Config
from bridge.graph import parse_applicability
from bridge.ladder import run_task
from bridge.store import Invalid, Store


CFG = Config(model_api="none")
REPO = "acme/billing"
QUESTION = "Should the overage charge be rounded to whole cents?"


class ApplicabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "decisions.db")
        self.store.add_owner({"name": "Priya", "team": "Billing", "patterns": "billing/*"})

    def source(self, applicability):
        run = self.store.add_run({"title": "Set invoice rounding", "repo": REPO})
        decision = self.store.request({"run_id": run["id"], "question": QUESTION,
                                       "context": "Invoice rounding", "path": "billing/rates.py"})
        self.store.answer(decision["id"], {"answer": "Round half up", "rationale": "Invoice policy",
                                            "applicability": applicability})
        return decision["id"]

    def ask(self, **changes):
        return run_task(self.store.graph, CFG, "Invoice work", repo=REPO,
                        decisions=[{"question": QUESTION, "path": "billing/rates.py", **changes}])

    def test_required_and_excluded_facts_and_paths_gate_reuse(self):
        source = self.source({"requires": {"plan": "enterprise"},
                              "excludes": {"contract": "monthly"}, "paths": ["billing/"]})
        matching = self.ask(facts={"plan": "enterprise", "contract": "annual"})
        self.assertEqual(len(matching.resolved), 1)
        self.assertEqual(self.store.graph.get_decision(matching.resolved[0]["id"]).source_id, source)

    def assert_unmet(self, changes, phrase):
        self.source({"requires": {"plan": "enterprise"},
                     "excludes": {"contract": "monthly"}, "paths": ["billing/"]})
        result = self.ask(**changes)
        self.assertFalse(result.resolved)
        self.assertEqual(len(result.open), 1)
        self.assertIn(phrase, self.store.graph.get_decision(result.open[0]["id"]).evidence)

    def test_missing_fact_is_not_inferred(self):
        self.assert_unmet({}, "did not state plan")

    def test_mismatched_fact_is_not_reused(self):
        self.assert_unmet({"facts": {"plan": "starter"}}, "needs plan=enterprise")

    def test_exception_is_not_reused(self):
        self.assert_unmet({"facts": {"plan": "enterprise", "contract": "monthly"}}, "excludes contract=monthly")

    def test_other_path_is_not_reused(self):
        self.assert_unmet({"facts": {"plan": "enterprise"}, "path": "other/rates.py"}, "not other/rates.py")

    def test_expired_answer_is_context_not_reused(self):
        source = self.source({"valid_until": (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()})
        result = self.ask()
        self.assertFalse(result.resolved)
        self.assertIn("expired", self.store.graph.get_decision(result.open[0]["id"]).evidence)
        self.assertEqual(self.store.graph.get_decision(source).applicability["valid_until"],
                         (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat())

    def test_an_answer_reaches_open_twins_only_inside_its_declared_scope(self):
        """Measured live on eb9d22d: an answer declared for customer=acme was
        corrected, and the same question already open for customer=globex
        and for an unnamed customer became resolved with the new answer."""
        source = self.source({"requires": {"customer": "acme"}})
        self.store.graph.db.execute("UPDATE decisions SET facts=? WHERE id=?", ('{"customer": "acme"}', source))
        opened = {}
        for name, facts in (("globex", {"customer": "globex"}), ("unnamed", {}), ("acme", {"customer": "acme"})):
            run = self.store.add_run({"title": f"Invoice work for {name}", "repo": REPO})
            row = self.store.request({"run_id": run["id"], "question": QUESTION, "context": "Invoice rounding",
                                      "path": "billing/rates.py"})
            # As the canvas stores them for a node asked with facts.
            self.store.graph.db.execute("UPDATE decisions SET facts=? WHERE id=?", (json.dumps(facts), row["id"]))
            opened[name] = row["id"]
        for name in opened:
            self.assertEqual(self.store.graph.get_decision(opened[name]).status, "pending", name)
        current = self.store.get_decision(source)
        self.store.answer(source, {"answer": "Round half down", "rationale": "Changed invoice policy",
                                   "applicability": {"requires": {"customer": "acme"}},
                                   "expected_updated_at": current["updated_at"]})
        for name in ("globex", "unnamed"):
            twin = self.store.graph.get_decision(opened[name])
            self.assertEqual(twin.status, "pending", f"{name}: {twin.evidence}")
            self.assertNotEqual(twin.answer, "Round half down", name)
        same = self.store.graph.get_decision(opened["acme"])
        self.assertEqual((same.status, same.answer), ("resolved", "Round half down"))
        self.assertFalse(same.authorized)

    def test_an_answer_reused_from_memory_carries_its_boundary_to_open_twins(self):
        source = self.source({"requires": {"customer": "acme"}})
        run = self.store.add_run({"title": "Invoice work, customer not named", "repo": REPO})
        unnamed = self.store.request({"run_id": run["id"], "question": QUESTION, "context": "Invoice rounding",
                                      "path": "billing/rates.py"})["id"]
        result = self.ask(facts={"customer": "acme"})
        self.assertEqual(len(result.resolved), 1)
        self.assertEqual(self.store.graph.get_decision(result.resolved[0]["id"]).source_id, source)
        self.assertEqual(self.store.graph.get_decision(unnamed).status, "pending")

    def test_a_failure_settling_open_twins_is_logged_and_the_answer_stands(self):
        import contextlib
        import io
        from unittest.mock import patch
        run = self.store.add_run({"title": "Set invoice rounding", "repo": REPO})
        decision = self.store.request({"run_id": run["id"], "question": QUESTION,
                                       "context": "Invoice rounding", "path": "billing/rates.py"})
        err = io.StringIO()
        with patch("bridge.ladder.close_open_twins", side_effect=RuntimeError("twin store unavailable")), \
                contextlib.redirect_stderr(err):
            self.store.answer(decision["id"], {"answer": "Round half up", "rationale": "Invoice policy"})
        self.assertEqual(self.store.get_decision(decision["id"])["answer"], "Round half up")
        self.assertIn(f"could not settle open twins of {decision['id']}: RuntimeError: twin store unavailable",
                      err.getvalue())

    def test_a_signed_figure_and_a_record_that_disagree_are_escalated_and_neither_is_served(self):
        """Measured live on eb9d22d: memory signed 11 days, a newer settled
        record said 19, and the record was served as the answer."""
        from unittest.mock import patch
        question = "What retention period applies to retry error summaries?"
        run = self.store.add_run({"title": "Set retention", "repo": REPO})
        signed = self.store.request({"run_id": run["id"], "question": question, "context": "Retry error summaries",
                                     "path": "billing/retention.py"})
        self.store.answer(signed["id"], {"answer": "Retain retry error summaries for 11 days.",
                                         "rationale": "Storage budget", "signed_by": "Priya"})
        # The live wording: the record names the signed figure to replace it.
        self.store.add_record({"repo": REPO, "kind": "ticket", "ref": "CONFLICT-1",
                               "title": "Retry error summary retention decision",
                               "body": "Approved current policy: retain retry error summaries for 19 days, not 11 days.",
                               "status": "Done", "created_at": "2030-01-01T00:00:00+00:00",
                               "paths": ["billing/retention.py"]})
        with patch("bridge.ladder._disagrees", return_value=True):
            result = run_task(self.store.graph, CFG, "Retention again", repo=REPO,
                              decisions=[{"question": question, "path": "billing/retention.py"}])
        self.assertFalse(result.resolved)
        node = self.store.graph.get_decision(result.open[0]["id"], exact=True)
        self.assertEqual((node.status, node.answer, node.prediction or ""), ("pending", "", ""))
        self.assertIn(f"conflict: decision {signed['id']}, signed by Priya, says \"Retain retry error summaries for "
                      "11 days.\", and ticket CONFLICT-1", node.evidence)
        self.assertIn("19 days", node.evidence)
        self.assertIn("serves neither and proposes neither", node.evidence)
        # The same record agreeing with the signed figure is no conflict.
        self.store.add_record({"repo": REPO, "kind": "ticket", "ref": "CONFLICT-1",
                               "title": "Retry error summary retention decision",
                               "body": "Decision: retry error summaries are retained for 11 days.", "status": "Done",
                               "created_at": "2030-01-01T00:00:00+00:00", "paths": ["billing/retention.py"]})
        with patch("bridge.ladder._disagrees", return_value=True):
            again = run_task(self.store.graph, CFG, "Retention once more", repo=REPO,
                             decisions=[{"question": question, "path": "billing/retention.py"}])
        self.assertEqual(len(again.resolved), 1)

    def test_invalid_applicability_is_rejected(self):
        for raw in ({"requires": {"plan": ""}}, {"paths": ["../private"]}, {"valid_until": "someday"}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_applicability(raw)
        run = self.store.add_run({"title": "Invoice work", "repo": REPO})
        decision = self.store.request({"run_id": run["id"], "question": QUESTION,
                                       "context": "Invoice rounding", "path": "billing/rates.py"})
        with self.assertRaises(Invalid):
            self.store.answer(decision["id"], {"answer": "Round half up", "applicability": {"paths": ["../private"]}})

    def test_legacy_request_suggestion_respects_declared_boundary(self):
        self.source({"requires": {"plan": "enterprise"}})
        run = self.store.add_run({"title": "More invoice work", "repo": REPO})
        absent = self.store.request({"run_id": run["id"], "question": QUESTION,
                                     "context": "Invoice rounding", "path": "billing/rates.py"})
        self.assertIsNone(absent["prediction"])
        present = self.store.request({"run_id": run["id"], "question": QUESTION,
                                      "context": "Invoice rounding", "path": "billing/rates.py",
                                      "facts": {"plan": "enterprise"}})
        self.assertEqual(present["prediction"], "Round half up")

    def test_record_body_is_context_not_an_answer_without_model(self):
        self.store.add_record({"repo": REPO, "kind": "doc", "ref": "POLICY-42",
                               "title": "Invoice reconciliation policy",
                               "body": "Invoice reconciliation runs daily; exceptions require review.",
                               "author": "Priya"})
        result = run_task(self.store.graph, CFG, "Invoice work", repo=REPO,
                          decisions=[{"question": "What does POLICY-42 require for invoice reconciliation?",
                                      "path": "billing/rates.py"}])
        self.assertFalse(result.resolved)
        self.assertEqual(len(result.open), 1)
        row = self.store.graph.get_decision(result.open[0]["id"])
        self.assertEqual(row.status, "pending")
        self.assertIn("retrieved doc POLICY-42:", row.evidence)
        self.assertIn("as context; no answerability check", row.evidence)


if __name__ == "__main__":
    unittest.main()
