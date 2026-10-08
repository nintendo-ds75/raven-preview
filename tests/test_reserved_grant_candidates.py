"""Provider-free bounded recall controls for the separate grant nomination budget."""

import re
from unittest.mock import Mock, patch

from bridge import context_memory as cm, graph as graph_mod, ladder, llm
import test_rule_candidates as fixtures
from test_rule_candidates import ANSWER, CFG, FACTS, QUESTION, REPO


class ReservedGrantCandidateTests(fixtures.RuleCandidateFixture):
    def candidates(self, **kwargs):
        return self.graph.standing_grant_candidates(llm.embed(QUESTION), repo=REPO, query=QUESTION, **kwargs)

    def test_hundred_unsigned_retries_do_not_consume_grant_budget(self):
        source = self.grant()
        unsigned = []
        for count in (33, 100):
            with self.subTest(unsigned_count=count):
                unsigned.extend(self.note() for _ in range(count - len(unsigned)))
                before = {node: self.raw(node) for node in [source, *unsigned]}
                result = self.ask()
                self.assertEqual(result["source_id"], source)
                self.assertTrue(result["authorized"])
                self.assertEqual({node: self.raw(node) for node in before}, before)

    def test_reserved_budget_is_bounded_and_deterministic_before_scoring(self):
        sources = [self.grant(question=QUESTION) for _ in range(70)]
        for _ in range(100):
            self.note()
        self.graph.db.execute("UPDATE decisions SET updated_at=? WHERE reusable=1",
                              ("2026-10-01T00:00:00+00:00",))
        connection = Mock(wraps=self.graph.db)
        with patch.object(self.graph._local, "db", connection), \
                patch.object(self.graph, "_row_embedding", wraps=self.graph._row_embedding) as score, \
                patch.object(self.graph, "_memory_rows", wraps=self.graph._memory_rows) as fetch:
            hits = self.candidates(top_k=1000, min_score=0)
        self.assertEqual(len(hits), graph_mod.GRANT_MATCH_LIMIT)
        self.assertEqual([row.id for _, row in hits], sorted(sources)[:graph_mod.GRANT_MATCH_LIMIT])
        self.assertEqual(len({row.id for _, row in hits}), len(hits))
        budget = graph_mod.GRANT_CANDIDATE_FTS + graph_mod.GRANT_CANDIDATE_RECENT
        self.assertLessEqual(score.call_count, budget)
        self.assertLessEqual(connection.execute.call_count, 3)
        self.assertEqual(len(fetch.call_args_list), 1)
        self.assertLessEqual(len(fetch.call_args.kwargs["ids"]), budget)
        self.assertTrue(fetch.call_args.kwargs["standing_grants"])
        self.assertEqual([row.id for _, row in self.candidates()], [row.id for _, row in hits])

    def test_no_fts_keeps_a_finite_recent_grant_budget(self):
        for _ in range(40):
            self.grant(question=QUESTION)
        for _ in range(100):
            self.note()
        with patch.object(self.graph, "has_fts", False), \
                patch.object(self.graph, "_row_embedding", wraps=self.graph._row_embedding) as score:
            hits = self.candidates(top_k=1000, min_score=0)
        self.assertEqual(len(hits), graph_mod.GRANT_MATCH_LIMIT)
        self.assertLessEqual(score.call_count, graph_mod.GRANT_CANDIDATE_RECENT)
        self.assertEqual(len({row.id for _, row in hits}), len(hits))

    def test_fts_can_recover_relevant_grant_outside_recent_grant_budget(self):
        if not self.graph.has_fts:
            self.skipTest("Backend has no FTS")
        source = self.grant(question=QUESTION)
        for index in range(40):
            self.grant(question=f"Which wallpaper colour belongs on office display {index}?")
        for _ in range(100):
            self.note()
        self.assertIn(source, {row.id for _, row in self.candidates()})
        result = self.ask()
        self.assertEqual(result["source_id"], source)
        self.assertTrue(result["authorized"])

    def test_repository_filter_precedes_both_reserved_limits(self):
        source = self.grant()
        for _ in range(40):
            foreign = self.grant(question=QUESTION, conditions="")
            self.graph.db.execute("UPDATE decisions SET repo=? WHERE id=?", ("birch/service", foreign))
        for _ in range(100):
            self.note()
        hits = self.candidates()
        self.assertEqual([row.id for _, row in hits], [source])
        self.assertTrue(self.ask()["authorized"])

    def test_selected_namespace_filters_reserved_grant_under_unsigned_pressure(self):
        record = self.store.add_record({"repo": REPO, "kind": "jira", "provider": "jira", "namespace": "site-b",
            "ref": "POL-1", "external_id": "policy-b", "title": "Audit policy", "body": ANSWER, "status": "Done"})
        source = self.grant(source_evidence=[cm.pin(record["source"])])
        task = self.graph.create_task("Current site-a audit", repo=REPO)
        self.store.add_record({"repo": REPO, "kind": "jira", "provider": "jira", "namespace": "site-a",
            "ref": "POL-2", "external_id": "policy-a", "title": "Current audit", "body": "Unrelated context",
            "task_id": task})
        for _ in range(100):
            self.note()
        with self.graph.source_scope(task):
            self.assertNotIn(source, {row.id for _, row in self.candidates()})
            result = ladder.run_task(self.graph, CFG, "Current namespace audit", repo=REPO, run_id=task,
                decisions=[{"question": QUESTION, "path": "queue/audit.py", "facts": FACTS}])
        node = self.store.get_decision(result.decision_ids[0])
        self.assertNotEqual(node["source_id"], source)
        self.assertFalse(node["authorized"])

    def test_expired_and_unmet_grants_are_nominations_not_authorization(self):
        source = self.grant()
        for _ in range(100):
            self.note()
        original = self.raw(source)
        variants = ({"rule_expires": "2000-01-01T00:00:00+00:00"},
                    {"rule_conditions": "customer=other"}, {"rule_scope": "same"})
        for changes in variants:
            with self.subTest(changes=changes):
                keys = set().union(*(row.keys() for row in variants))
                values = {key: changes.get(key, original[key]) for key in keys}
                self.graph.db.execute("UPDATE decisions SET " + ",".join(f"{key}=?" for key in values)
                                      + ",reusable=1 WHERE id=?", [*values.values(), source])
                self.assertIn(source, {row.id for _, row in self.candidates()})
                self.assertFalse(self.ask()["authorized"])

    def test_ended_superseded_and_review_required_grants_do_not_consume_budget(self):
        source = self.grant()
        for _ in range(40):
            self.note()
        original = self.raw(source)
        variants = ({"reusable": 0}, {"superseded_by": "superseded-test-grant"}, {"needs_review": 1})
        for changes in variants:
            with self.subTest(changes=changes):
                keys = set().union(*(row.keys() for row in variants))
                values = {key: changes.get(key, original[key]) for key in keys}
                self.graph.db.execute("UPDATE decisions SET " + ",".join(f"{key}=?" for key in values)
                                      + " WHERE id=?", [*values.values(), source])
                self.assertNotIn(source, {row.id for _, row in self.candidates()})
                self.assertFalse(self.ask()["authorized"])

    def test_conflicting_reserved_grant_does_not_replace_unsigned_answer(self):
        source = self.grant(answer="Keep queue audit events for seventeen days.")
        for _ in range(100):
            self.note()
        self.assertIn(source, {row.id for _, row in self.candidates()})
        result = self.ask()
        self.assertFalse(result["authorized"])
        self.assertNotEqual(result["source_id"], source)
        self.assertEqual(result["answer"], ANSWER)

    def test_unrelated_reserved_grant_cannot_authorize_the_same_answer_text(self):
        source = self.grant(question="Which wallpaper colour belongs on the office display?")
        for _ in range(100):
            self.note()
        result = self.ask()
        self.assertNotEqual(result["source_id"], source)
        self.assertFalse(result["authorized"])

    def test_source_guards_reject_reserved_nomination_without_rewriting_history(self):
        source = self.grant()
        unsigned = [self.note() for _ in range(40)]
        old = {node: self.raw(node) for node in [source, *unsigned]}
        for guard in (patch.object(self.graph, "source_review_reason", return_value="source changed"),
                      patch("bridge.context_connectors.blocked_decisions", return_value={source})):
            with guard:
                self.assertIn(source, {row.id for _, row in self.candidates()})
                result = self.ask()
                self.assertFalse(result["authorized"])
                self.assertNotEqual(result["source_id"], source)
        self.assertEqual({node: self.raw(node) for node in old}, old)

    def test_reserved_grant_remains_visible_in_bounded_semantic_prompt(self):
        source = self.grant()
        for _ in range(100):
            self.note()
        with patch.object(fixtures.Config, "semantic_retrieval", property(lambda _: True)), \
                patch.object(llm.Client, "complete_json", return_value={"pick": "m2"}) as select:
            result = self.ask()
        self.assertEqual(result["source_id"], source)
        self.assertTrue(result["authorized"])
        prompt = select.call_args.args[2]
        self.assertLessEqual(len(re.findall(r"^m\d+ \[", prompt, re.M)), 8)
        self.assertLess(len(prompt), 12000)
        self.assertIn("m1 [signed answer", prompt)
        self.assertIn("Current grant eligible=True", prompt)
