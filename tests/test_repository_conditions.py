"""Repository conditions bind current scope without editing historical facts."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fixtures import OfflineCase

from bridge import canvas, proof
from bridge.config import Config
from bridge.graph import applicability_status, condition_facts, rule_status
from bridge.ladder import run_task
from bridge.store import Invalid, Store


CFG = Config(model_api="none")
REPO = "cedar/service"
QUESTION = "Keep the queue audit events for nine days?"
FACTS = {"customer": "lumen", "environment": "preview", "review_kind": "maintenance"}
CONDITIONS = "; ".join(f"{key}={value}" for key, value in {"repo": REPO, **FACTS}.items())
DIFF = ("diff --git a/queue/audit.py b/queue/audit.py\n"
        "--- a/queue/audit.py\n+++ b/queue/audit.py\n"
        "@@ -1 +1 @@\n-DAYS = 4\n+DAYS = 9\n")


class ConditionFactsTests(unittest.TestCase):
    def source(self):
        return SimpleNamespace(id="audit-policy", authorized=True, reusable=True,
            source_reuse_uncertain=False, rule_expires="", rule_conditions=CONDITIONS,
            applicability={"requires": {"repo": REPO, **FACTS}}, repo="historical/other")

    def checks(self, facts, repo):
        source = self.source()
        return (rule_status(source, QUESTION, facts=facts, repo=repo),
                applicability_status(source, "queue/audit.py", facts, repo=repo))

    def test_current_repository_supplies_only_missing_repository(self):
        original = {"Customer": " LUMEN ", "environment": "preview", "review_kind": "maintenance"}
        before = dict(original)
        bound, conflict = condition_facts(original, " CEDAR/SERVICE ")
        self.assertEqual(bound, {"repo": REPO, **FACTS})
        self.assertEqual(conflict, "")
        self.assertEqual(original, before)
        self.assertTrue(all(ok for ok, _ in self.checks(FACTS, REPO)))

    def test_matching_explicit_fact_is_allowed_and_contradiction_is_refused(self):
        for repo_fact, expected in ((REPO, True), ("birch/service", False), ("", False)):
            with self.subTest(repo_fact=repo_fact):
                results = self.checks({**FACTS, "repo": repo_fact}, REPO)
                self.assertTrue(all(ok == expected for ok, _ in results), results)
                if not expected:
                    self.assertTrue(all("current repository" in why for _, why in results))

    def test_omitted_repository_retains_legacy_fact_comparison(self):
        facts = {"repo": REPO, **FACTS}
        source = self.source()
        self.assertTrue(rule_status(source, QUESTION, facts=facts)[0])
        self.assertTrue(applicability_status(source, facts=facts)[0])
        self.assertEqual(condition_facts(facts), (facts, ""))

    def test_explicit_unknown_scope_is_not_supplied_by_facts_or_source(self):
        for repo in ("", "unknown", " UNKNOWN "):
            for facts in (FACTS, {"repo": REPO, **FACTS}):
                with self.subTest(repo=repo, facts=facts):
                    results = self.checks(facts, repo)
                    self.assertTrue(all(not ok and "current repository is unknown" in why
                                        for ok, why in results), results)

    def test_current_repository_and_prose_do_not_supply_business_facts(self):
        source = self.source()
        for key in FACTS:
            facts = {name: value for name, value in FACTS.items() if name != key}
            prose = "; ".join(f"{name}={value}" for name, value in FACTS.items())
            with self.subTest(missing=key):
                for ok, why in (rule_status(source, QUESTION, prose, facts, repo=REPO),
                                applicability_status(source, facts=facts, repo=REPO)):
                    self.assertFalse(ok)
                    self.assertIn(key, why)

    def test_repository_exclusion_checks_current_scope(self):
        source = self.source()
        source.applicability = {"excludes": {"repo": REPO}}
        ok, why = applicability_status(source, facts=FACTS, repo=REPO)
        self.assertFalse(ok)
        self.assertIn(f"excludes repo={REPO}", why)


class RepositoryRuleTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "repository-conditions.db")
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.store.add_owner({"name": "Morgan Hale", "team": "Queue", "patterns": "queue/*"})
        self.store.update_settings({"auto_rules": True})

    def task(self, key):
        return canvas.start_task(self.store, CFG, {"title": "Maintain queue audit records", "repo": REPO,
            "paths": "queue/audit.py", "client_key": key})["task_id"]

    def node(self, task, facts=None, context="Queue audit retention", ref="audit"):
        return canvas.add_node(self.store, CFG, {"task_id": task, "question": QUESTION,
            "context": context, "paths": "queue/audit.py", "client_ref": ref,
            "facts": {**FACTS, "work_item": "AUDIT-22"} if facts is None else facts})

    def source(self, applicability=None, **grant):
        task = self.task("policy")
        source = self.node(task, {**FACTS, "work_item": "AUDIT-21"})["node_id"]
        self.store.answer(source, {"answer": "Keep queue audit events for nine days.", "signed_by": "Morgan Hale",
            "applicability": applicability or {}, "expected_updated_at": self.store.get_decision(source)["updated_at"]})
        canvas.finish_task(self.store, {"task_id": task})
        self.grant(source, **grant)
        return source

    def grant(self, source, **changes):
        return self.store.make_rule(source, {"by": "Morgan Hale", "conditions": CONDITIONS, "scope": "any",
            "expected_updated_at": self.store.get_decision(source)["updated_at"], **changes})

    def raw(self, node_id):
        return dict(self.graph.db.execute("SELECT * FROM decisions WHERE id=?", (node_id,)).fetchone())

    def pins(self, node_id):
        return [dict(row) for row in self.graph.db.execute(
            "SELECT * FROM decision_links WHERE decision_id=? ORDER BY related_id,kind", (node_id,))]

    def assert_waiting(self, node_id):
        node = canvas.node_view(self.store, node_id)
        self.assertFalse(node["authorized"], node)
        self.assertTrue(node["needs_review"], node)
        return node

    def test_any_scope_allows_new_work_item_without_duplicate_repository_fact(self):
        source = self.source({"requires": {"repo": REPO, **FACTS}})
        historical = self.raw(source)
        task = self.task("current-work")
        facts = {**FACTS, "work_item": "AUDIT-22"}
        node = self.node(task, facts)
        self.assertTrue(node["authorized"], node)
        self.assertEqual(node["signoff"], "rule")
        self.assertEqual(json.loads(self.raw(node["node_id"])["facts"]), facts)
        before = self.raw(node["node_id"])
        pins = self.pins(node["node_id"])
        self.assertTrue(pins)
        self.assertTrue(all(pin["source_version_id"] for pin in pins))
        self.assertTrue(canvas.node_view(self.store, node["node_id"])["authorized"])
        self.assertEqual(self.raw(node["node_id"]), before)
        self.assertEqual(self.pins(node["node_id"]), pins)
        self.assertEqual(self.raw(source), historical)

    def test_request_uses_current_repository_for_declared_applicability(self):
        source = self.source({'requires': {'repo': REPO, **FACTS}})
        historical = self.raw(source)
        task = self.task('legacy-request')
        row = self.store.request({'run_id': task, 'question': QUESTION,
            'context': 'Current audit question', 'path': 'queue/audit.py', 'facts': FACTS})
        self.assertEqual(row['source_id'], source)
        self.assertFalse(row['authorized'])
        count = self.graph.db.execute('SELECT count(*) AS n FROM decisions WHERE run_id=?', (task,)).fetchone()['n']
        with self.assertRaisesRegex(Invalid, 'current repository'):
            self.store.request({'run_id': task, 'question': QUESTION,
                'context': 'Current audit question', 'path': 'queue/audit.py',
                'facts': {**FACTS, 'repo': 'birch/service'}})
        self.assertEqual(self.graph.db.execute('SELECT count(*) AS n FROM decisions WHERE run_id=?',
            (task,)).fetchone()['n'], count)
        self.assertEqual(self.raw(source), historical)

    def test_same_scope_still_refuses_a_different_work_item(self):
        self.source(scope="same")
        node = self.node(self.task("other-work"), {**FACTS, "work_item": "AUDIT-22"})
        self.assertFalse(node["authorized"], node)
        self.assertNotEqual(node["signoff"], "rule")

    def test_initial_checks_refuse_wrong_repository_and_missing_business_fact(self):
        source = self.source()
        historical = self.raw(source)
        attempts = [{**FACTS, "repo": "birch/service"},
                    *({k: v for k, v in FACTS.items() if k != missing} for missing in FACTS)]
        for index, facts in enumerate(attempts):
            with self.subTest(facts=facts):
                result = run_task(self.graph, CFG, "Check current audit conditions", repo=REPO,
                    run_id=self.task(f"missing-{index}"), decisions=[{"question": QUESTION,
                        "path": "queue/audit.py", "facts": facts, "context": CONDITIONS}])
                node = self.graph.get_decision(result.decision_ids[0], exact=True)
                self.assertFalse(node.authorized, node)
                self.assertNotEqual(node.signoff, "rule")
        self.assertEqual(self.raw(source), historical)

    def test_initial_unknown_repository_does_not_use_historical_scope(self):
        self.source()
        result = run_task(self.graph, CFG, "Unscoped audit work", repo="",
            decisions=[{"question": QUESTION, "path": "queue/audit.py", "facts": {"repo": REPO, **FACTS}}])
        node = self.graph.get_decision(result.decision_ids[0], exact=True)
        self.assertFalse(node.authorized, node)
        self.assertNotEqual(node.signoff, "rule")
        self.assertIn("current repository is unknown", node.evidence)

    def check_live_conditions(self, applicability, conditions):
        source = self.source(applicability, conditions=conditions)
        historical = self.raw(source)
        changes = [("repo", ""), ("repo", "unknown"), ("repo", "birch/service"),
                   ("facts", json.dumps({**FACTS, "repo": "birch/service"})),
                   ("facts", json.dumps({"environment": "preview", "review_kind": "maintenance"}))]
        for index, (column, value) in enumerate(changes):
            with self.subTest(column=column, value=value):
                node = self.node(self.task(f"live-{index}"))
                self.assertTrue(node["authorized"], node)
                pins = self.pins(node["node_id"])
                self.graph.db.execute(f"UPDATE decisions SET {column}=? WHERE id=?", (value, node["node_id"]))
                self.assert_waiting(node["node_id"])
                self.assertEqual(self.pins(node["node_id"]), pins)
        self.assertEqual(self.raw(source), historical)

    def test_live_rule_checks_use_current_node_repository_and_facts(self):
        self.check_live_conditions({}, CONDITIONS)

    def test_live_applicability_checks_use_current_node_repository_and_facts(self):
        self.check_live_conditions({"requires": {"repo": REPO, **FACTS}},
                                   "; ".join(f"{key}={value}" for key, value in FACTS.items()))

    def test_structured_expiry_still_invalidates_bound_repository_reuse(self):
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        self.source({"requires": {"repo": REPO}, "valid_until": expiry.isoformat()})
        node = self.node(self.task("structured-expiry"))
        self.assertTrue(node["authorized"], node)
        self.graph.expire_rules(now=(expiry + timedelta(seconds=1)).isoformat())
        self.assertIn("expired", self.assert_waiting(node["node_id"])["review_reason"])

    def test_rule_expiry_still_invalidates_bound_repository_reuse(self):
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        self.source(expires=expiry.isoformat())
        node = self.node(self.task("rule-expiry"))
        self.assertTrue(node["authorized"], node)
        self.graph.expire_rules(now=(expiry + timedelta(seconds=1)).isoformat())
        self.assertIn("expired", self.assert_waiting(node["node_id"])["review_reason"])

    def test_source_correction_invalidates_without_rewriting_derived_facts_or_pins(self):
        source = self.source()
        node = self.node(self.task("correction"), {**FACTS, "work_item": "AUDIT-22"})
        self.assertTrue(node["authorized"], node)
        facts, pins = self.raw(node["node_id"])["facts"], self.pins(node["node_id"])
        self.store.answer(source, {"answer": "Keep queue audit events for six days.",
            "expected_updated_at": self.store.get_decision(source)["updated_at"]})
        self.assert_waiting(node["node_id"])
        self.assertEqual(self.raw(node["node_id"])["facts"], facts)
        self.assertEqual(self.pins(node["node_id"]), pins)
        self.assertFalse(self.node(self.task("after-correction"))["authorized"])

    def test_ending_rule_preserves_completed_row_proof_and_source_pins(self):
        source = self.source()
        task = self.task("completed-work")
        completed = self.node(task, {**FACTS, "work_item": "AUDIT-22"})
        self.assertTrue(completed["authorized"], completed)
        with patch("bridge.llm.check_conformance", return_value=None):
            canvas.finish_task(self.store, {"task_id": task, "diff": DIFF})
        saved = proof.export(self.store, {"task_id": task})["bundle"]
        historical, pins = self.raw(completed["node_id"]), self.pins(completed["node_id"])
        live = self.node(self.task("unfinished-work"))
        self.assertTrue(live["authorized"], live)
        self.grant(source, end=True)
        self.assert_waiting(live["node_id"])
        self.assertEqual(self.raw(completed["node_id"]), historical)
        self.assertEqual(self.pins(completed["node_id"]), pins)
        exported = proof.export(self.store, {"task_id": task})
        self.assertFalse(exported["stale"])
        self.assertEqual(exported["bundle"], saved)
        self.assertTrue(canvas.node_view(self.store, completed["node_id"])["authorized"])
