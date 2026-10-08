"""Synthetic, provider-free selection controls for current standing grants."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from fixtures import OfflineCase
from bridge import canvas, ladder, llm
from bridge.config import Config
from bridge.graph import Decision
from bridge.store import Store


CFG = Config(model_api="none")
REPO = "cedar/service"
QUESTION = "Keep queue audit events for nine days?"
ANSWER = "Keep queue audit events for nine days."
FACTS = {"customer": "lumen", "environment": "preview", "review_kind": "maintenance"}
CONDITIONS = "; ".join(f"{key}={value}" for key, value in {"repo": REPO, **FACTS}.items())


class RuleCandidateFixture(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "rule-candidates.db")
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.store.add_owner({"name": "Morgan Hale", "team": "Queue", "patterns": "queue/*"})
        self.store.update_settings({"auto_rules": True})
        for method in ("complete", "complete_json"):
            guard = patch.object(llm.Client, method, side_effect=AssertionError("No provider calls allowed"))
            guard.start()
            self.addCleanup(guard.stop)

    def note(self, question=QUESTION, answer=ANSWER, *, facts=None, repo=REPO, **fields):
        task = self.graph.create_task("Synthetic audit review", repo=repo)
        node = self.graph.add_decision(task, question, "policy", "resolved", source="agent",
            answer=answer, rationale="Historical episode: obtain task-specific confirmation.",
            repo=repo, path="queue/audit.py", context="Queue audit review", owner="Morgan Hale")
        self.graph.update_decision(node, signoff="required", **fields)
        self.graph.db.execute("UPDATE decisions SET facts=? WHERE id=?",
            (json.dumps(facts or {**FACTS, "work_item": "AUDIT-22"}), node))
        canvas.settle_node(self.store, {"task_id": task, "node_id": node, "answer": answer,
            "rationale": "Historical episode: obtain task-specific confirmation."})
        return node

    def grant(self, *, question="For routine review, keep queue audit events for nine days?", answer=ANSWER,
              source_evidence=None, **changes):
        source = self.note(question, answer, facts={**FACTS, "work_item": "AUDIT-21"})
        if source_evidence is not None:
            canvas.settle_node(self.store, {"task_id": self.raw(source)["run_id"], "node_id": source,
                "answer": answer, "rationale": "Reviewed synthetic source", "source_evidence": source_evidence})
        canvas.sign_off(self.store, source, {"by": "Morgan Hale",
            "expected_updated_at": self.store.get_decision(source)["updated_at"]})
        self.store.make_rule(source, {"by": "Morgan Hale", "scope": "any", "conditions": CONDITIONS,
            "expires": (datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
            "expected_updated_at": self.store.get_decision(source)["updated_at"], **changes})
        return source

    def raw(self, node):
        return dict(self.graph.db.execute("SELECT * FROM decisions WHERE id=?", (node,)).fetchone())

    def ask(self, *, facts=None, question=QUESTION, repo=REPO):
        result = ladder.run_task(self.graph, CFG, "Current audit review", repo=repo,
            decisions=[{"question": question, "path": "queue/audit.py", "context": "Queue audit review",
                        "facts": {**FACTS, "work_item": "AUDIT-22"} if facts is None else facts}])
        return self.store.get_decision(result.decision_ids[0])


class RuleCandidateSelectionTests(RuleCandidateFixture):
    def test_same_answer_unsigned_episode_does_not_hide_current_grant(self):
        source = self.grant()
        unsigned = self.note()
        before = {node: self.raw(node) for node in (source, unsigned)}
        self.assertEqual(before[source]["source"], "agent")
        self.assertEqual(before[source]["signoff"], "signed")
        result = self.ask()
        self.assertEqual(result["source_id"], source, result["evidence"])
        self.assertTrue(result["authorized"], result)
        self.assertEqual(result["signoff"], "rule")
        self.assertEqual({node: self.raw(node) for node in before}, before)

    def test_twelve_unsigned_retries_do_not_crowd_out_relevant_grant(self):
        source = self.grant()
        unsigned = [self.note() for _ in range(12)]
        before = {node: self.raw(node) for node in [source, *unsigned]}
        result = self.ask()
        self.assertEqual(result["source_id"], source)
        self.assertTrue(result["authorized"])
        self.assertEqual({node: self.raw(node) for node in before}, before)

    def test_differing_newer_answer_keeps_existing_arbitration(self):
        source = self.grant()
        unsigned = self.note(answer="Keep queue audit events for seventeen days.")
        before = {node: self.raw(node) for node in (source, unsigned)}
        result = self.ask()
        self.assertEqual(result["source_id"], unsigned)
        self.assertFalse(result["authorized"])
        self.assertEqual({node: self.raw(node) for node in before}, before)

    def test_matching_grant_conditions_do_not_make_an_unrelated_question_relevant(self):
        source = self.grant(question="Which wallpaper colour belongs on the office display?")
        unsigned = self.note()
        self.assertEqual(self.ask()["source_id"], unsigned)
        self.assertFalse(self.ask()["authorized"])
        self.assertTrue(self.store.get_decision(source)["reusable"])

    def test_missing_or_changed_business_facts_and_repository_do_not_promote(self):
        self.grant()
        unsigned = self.note()
        attempts = [*({key: value for key, value in FACTS.items() if key != missing} for missing in FACTS),
                    *({**FACTS, key: "different"} for key in FACTS), {**FACTS, "repo": "birch/service"}]
        for facts in attempts:
            with self.subTest(facts=facts):
                result = self.ask(facts=facts)
                self.assertFalse(result["authorized"])
                self.assertNotEqual(result["signoff"], "rule")
        self.assertFalse(self.store.get_decision(unsigned)["authorized"])

    def test_same_scope_rule_does_not_cover_changed_work_item(self):
        self.grant(scope="same")
        unsigned = self.note()
        result = self.ask()
        self.assertEqual(result["source_id"], unsigned)
        self.assertFalse(result["authorized"])

    def test_ended_expired_superseded_and_reviewed_sources_do_not_promote(self):
        source = self.grant()
        unsigned = self.note()
        original = self.raw(source)
        variants = ({"reusable": 0}, {"rule_expires": "2000-01-01T00:00:00+00:00"},
                    {"superseded_by": unsigned}, {"needs_review": 1})
        for changes in variants:
            with self.subTest(changes=changes):
                keys = set().union(*(row.keys() for row in variants))
                values = {key: changes.get(key, original[key]) for key in keys}
                self.graph.db.execute("UPDATE decisions SET " + ",".join(f"{key}=?" for key in values)
                                      + " WHERE id=?", [*values.values(), source])
                candidate = self.graph.get_decision(source, exact=True)
                before = self.raw(source)
                self.assertFalse(ladder._current_grant(self.graph, candidate, QUESTION, "Queue audit review",
                    "queue/audit.py", {**FACTS, "work_item": "AUDIT-22"}, REPO)[0])
                self.assertEqual(self.raw(source), before)
                self.assertFalse(self.ask()["authorized"])

    def test_declared_applicability_is_separate_from_matching_grant_conditions(self):
        source = self.grant()
        unsigned = self.note()
        for spec in ({"requires": {"region": "east"}}, {"excludes": {"customer": "lumen"}},
                     {"paths": ["other/"]}, {"valid_until": "2000-01-01"}):
            with self.subTest(spec=spec):
                self.graph.db.execute("UPDATE decisions SET applicability=? WHERE id=?", (json.dumps(spec), source))
                result = self.ask()
                self.assertEqual(result["source_id"], unsigned)
                self.assertFalse(result["authorized"])

    def test_opaque_source_review_rejection_prevents_promotion(self):
        source = self.grant()
        unsigned = self.note()
        with patch.object(self.graph, "source_review_reason", return_value="source revision needs refresh") as guard:
            result = self.ask()
        self.assertEqual(result["source_id"], unsigned)
        self.assertFalse(result["authorized"])
        self.assertIn(source, [call.args[0] for call in guard.call_args_list])

    def test_opaque_observation_refresh_rejection_prevents_promotion(self):
        source = self.grant()
        unsigned = self.note()
        with patch("bridge.context_connectors.blocked_decisions", return_value={source}) as guard:
            result = self.ask()
        self.assertEqual(result["source_id"], unsigned)
        self.assertFalse(result["authorized"])
        self.assertTrue(guard.called)

    def test_repository_separation_keeps_foreign_grant_out(self):
        self.grant(conditions="")
        unsigned = self.note(repo="birch/service")
        result = self.ask(repo="birch/service")
        self.assertEqual(result["source_id"], unsigned)
        self.assertFalse(result["authorized"])

    def test_disabled_automatic_rules_still_require_a_signature(self):
        source = self.grant()
        self.note()
        self.store.update_settings({"auto_rules": False})
        result = self.ask()
        self.assertEqual(result["source_id"], source)
        self.assertFalse(result["authorized"])
        self.assertIn("automatic rule authorization is off", result["evidence"])

    def test_semantic_and_wide_unsigned_picks_retain_eligible_grant(self):
        source = self.grant()
        unsigned = self.note()
        prompts = []

        def choose(_client, purpose, system, prompt, **kwargs):
            if purpose == "select":
                prompts.append(prompt)
                import re
                label = next(line.split(" ", 1)[0] for line in prompt.splitlines()
                             if re.match(r"m\d+ \[", line) and "unsigned" in line)
                return {"pick": label}
            if purpose == "expand":
                return {"phrases": []}
            return {"pick": "none"}

        with patch.object(Config, "semantic_retrieval", property(lambda _: True)), \
                patch.object(llm.Client, "complete_json", choose), \
                patch.object(ladder, "_compose_answer", return_value=ANSWER):
            result = self.ask()
            self.assertEqual(result["source_id"], source)
            self.assertTrue(result["authorized"])
            # Force the initial selector to defer, then exercise the real
            # wide candidate presentation and its unsigned selection.
            initial = ladder._selector_pick
            calls = 0

            def defer_once(*args, **kwargs):
                nonlocal calls
                calls += 1
                return None if calls == 1 else initial(*args, **kwargs)

            with patch.object(ladder, "_selector_pick", defer_once):
                result = self.ask()
            self.assertGreater(calls, 1)
            self.assertEqual(result["source_id"], source)
            self.assertTrue(result["authorized"])
        self.assertTrue(all("Current repository: " + REPO in prompt for prompt in prompts))
        self.assertTrue(all("work_item=AUDIT-22" in prompt for prompt in prompts))
        self.assertTrue(all("Current grant eligible=True" in prompt for prompt in prompts))
        self.assertFalse(self.store.get_decision(unsigned)["authorized"])

    def test_entailment_rejection_still_blocks_relevant_grant(self):
        self.grant(question="Retain queue audit events for nine days?")
        self.note()
        with patch.object(Config, "semantic_retrieval", property(lambda _: True)), \
                patch.object(Config, "deep_retrieval", property(lambda _: False)), \
                patch.object(llm.Client, "complete_json", return_value={"pick": "m1"}), \
                patch.object(ladder, "_compose_answer", return_value=None) as compose, \
                patch.object(ladder, "_compose_joint", return_value=None):
            result = self.ask()
        self.assertTrue(compose.called)
        self.assertFalse(result["authorized"])

    def test_background_publication_keeps_rule_after_selector_picks_unsigned(self):
        source = self.grant()
        unsigned = self.note()
        live = self.ask()
        self.assertTrue(live["authorized"])
        old = {node: self.raw(node) for node in (source, unsigned)}
        self.graph.db.execute("UPDATE decisions SET model_pending=1 WHERE id=?", (live["id"],))
        with patch.object(Config, "semantic_retrieval", property(lambda _: True)), \
                patch.object(llm.Client, "complete_json", return_value={"pick": "m2"}) as select, \
                patch.object(ladder, "_compose_answer", return_value=ANSWER):
            canvas._model_node_pass(self.store, CFG, live["id"], "Queue audit review", ["queue/audit.py"],
                                   [], "", None, "Queue audit review", live["updated_at"])
        result = self.store.get_decision(live["id"])
        self.assertTrue(select.called)
        self.assertIn("m2 [earlier resolution from agent", select.call_args.args[2])
        self.assertEqual(result["source_id"], source)
        self.assertTrue(result["authorized"], result["evidence"])
        self.assertEqual(result["signoff"], "rule")
        self.assertFalse(result["model_pending"])
        self.assertTrue(any(event["kind"] == "model_read" for event in result["events"]))
        self.assertFalse(any(event["kind"] == "model_read_failed" for event in result["events"]))
        self.assertEqual({node: self.raw(node) for node in old}, old)


class SelectorGrantPresentationTests(unittest.TestCase):
    def source(self, **changes):
        fields = dict(id="signed-agent-grant", task_id="historical-task", question=QUESTION,
            category="policy", status="resolved", source="agent", answer=ANSWER, answered_by="",
            evidence="", owner="Morgan Hale", owner_evidence="", created_at=1, updated_at=1,
            signoff="signed", signed_by="Morgan Hale", reusable=True, rule_by="Morgan Hale",
            rule_scope="any", rule_conditions=CONDITIONS, rule_expires="2099-01-01T00:00:00+00:00")
        return Decision(**{**fields, **changes})

    def test_agent_origin_does_not_hide_real_human_signoff(self):
        source = self.source()
        with patch.object(llm.Client, "complete_json", return_value={"pick": "m1"}) as select:
            result = ladder._selector_pick(CFG, QUESTION, {"m1": ("memory", source)})
        self.assertEqual(result, ("memory", source))
        prompt = select.call_args.args[2]
        self.assertIn("signed answer", prompt)
        self.assertIn("Morgan Hale", prompt)
        self.assertNotIn("unsigned", prompt)

    def test_grant_terms_current_facts_and_applicability_are_presented_separately(self):
        source = self.source()
        store = Mock()
        store.source_review_reason.return_value = ""
        with patch.object(llm.Client, "complete_json", return_value={"pick": "m1"}) as select, \
                patch("bridge.context_connectors.blocked_decisions", return_value=set()):
            ladder._selector_pick(CFG, QUESTION, {"m1": ("memory", source)}, store=store,
                repo=REPO, path="queue/audit.py", context="Current audit request", facts=FACTS)
        prompt = select.call_args.args[2]
        for wanted in ("Current repository: cedar/service", "Current path: queue/audit.py",
                       "Context: Current audit request", "customer=lumen", "Applicability: True",
                       "Standing grant: declared=True", "scope=any", "expires=2099-01-01",
                       "Current grant eligible=True"):
            self.assertIn(wanted, prompt)

    def test_a_source_label_and_answerer_alone_do_not_claim_a_signature(self):
        source = self.source(source="human", answered_by="Morgan Hale", signoff="required", reusable=False)
        with patch.object(llm.Client, "complete_json", return_value={"pick": "m1"}) as select:
            ladder._selector_pick(CFG, QUESTION, {"m1": ("memory", source)})
        self.assertIn("unsigned", select.call_args.args[2])
        self.assertNotIn("[signed answer", select.call_args.args[2])

    def test_inherited_rule_authority_is_not_presented_as_a_personal_signature(self):
        source = self.source(source="memory", signoff="rule", reusable=False)
        with patch.object(llm.Client, "complete_json", return_value={"pick": "m1"}) as select:
            ladder._selector_pick(CFG, QUESTION, {"m1": ("memory", source)})
        self.assertIn("rule-authorized answer", select.call_args.args[2])
        self.assertIn("no personal signoff", select.call_args.args[2])

    def test_retrospective_and_differing_answers_are_not_replaced(self):
        candidate = self.source()
        picked = self.source(id="unsigned", reusable=False, signoff="required")
        for question, answer in (("Who decided the original queue audit retention?", ANSWER),
                                 (QUESTION, "Keep events for seventeen days.")):
            with self.subTest(question=question, answer=answer):
                picked.answer = answer
                result = ladder._same_answer_grant(Mock(), picked, [(1.0, 1.0, candidate)],
                    question, "", "queue/audit.py", FACTS, REPO)
                self.assertIs(result, picked)


if __name__ == "__main__":
    unittest.main()
