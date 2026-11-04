"""A compound question whose clauses are each settled by a standing rule of
the same owner settles from both: the answer composed from the two rule
texts, sign-off `rule`, the fit reason and the timeline naming both calls,
both cited as support. Any other combination (two owners, a composed
answer that adds a figure or a word the rules do not state) keeps the
fresh sign-off a combined answer always had.

Measured on the Parcelwise dry run (Nov 2, task b37828a2ad87): "Which table
does assistant billing read usage from, and how are reads paged?" had
Jordan's Jun 9 rule (usage_daily, never the raw table) and his Jun 10 rule
(by cursor, never by offset); the Jun 9 row scored 0.54 against the whole
question, under the 0.6 floor, was "read anyway" and found not to answer it
(half the question is paging), the two were never read together, and the
node went to Jordan as pending."""
import re
from unittest.mock import patch

from fixtures import OfflineCase
from raven import ladder, llm, timeline
from raven.config import Config
from raven.judgment import fit_reason

REPO = "parcelwise/parcelwise-app"
TABLE_Q = "Which table does billing read usage from: usage_daily or the raw usage_events?"
TABLE_A = ("usage_daily (ADR 0005, PR #96): one row per merchant, feature and day. The raw usage_events table stays "
           "inside app/usage; it has replays and our own test shops in it.")
PAGE_Q = "How does a reader page through usage_daily for a period: by cursor or by offset?"
PAGE_A = ("By cursor, never by offset (ADR 0005, PR #96): the rollup inserts rows while a reader walks the table. A "
          "reader that wants a period total walks every page.")
COMPOUND = "Which table does assistant billing read usage from, and how are reads paged?"
SINGLE = "Which table does assistant billing read usage from?"
COMPOSED = ("Assistant billing reads usage_daily (ADR 0005, PR #96), never the raw usage_events table, and pages "
            "through it by cursor, never by offset.")


def semantic_on():
    return patch.object(Config, "semantic_retrieval", property(lambda self: not self.deterministic))


def model(joint=COMPOSED, single="", rerank=False):
    """A scripted model: the single-record composer answers `single` or
    refuses, the joint composer answers `joint` to the compound question
    from both records and refuses any other, the selector prefers the
    memory candidate, the support check finds every claim stated. With
    `rerank` the near-miss reader picks the first row it is shown, as the
    model does when the closest row looks like it answers."""
    def complete(self, purpose, system, prompt, **kw):
        if purpose == "compose_joint":
            if f"Question: {COMPOUND}" not in prompt:
                return "RECORDS DO NOT ANSWER"
            return joint + "\nUSED_SOURCES: r1,r2\nCOVERAGE: FULL"
        if purpose == "compose" and single:
            return single
        if purpose == "conflict":
            return "UNRELATED"
        return "RECORD DOES NOT ANSWER"

    def complete_json(self, purpose, system, prompt, **kw):
        if purpose == "support":
            return {"claims": [], "answer": "", "open": []}
        if purpose == "memory_rerank":
            first = re.search(r"\[([0-9a-f]+)\] scored", prompt)
            return {"id": first.group(1), "why": "the closest"} if rerank and first else {}
        if purpose in ("select", "precedent"):
            return {"pick": "m1"}
        if purpose == "expand":
            return {"phrases": []}
        if purpose == "followup":
            return {"needs_context": False, "question": ""}
        return {}
    return [patch.object(llm.Client, "complete", complete), patch.object(llm.Client, "complete_json", complete_json)]


class JointRuleCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store("joint-rules.db")
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.store.add_owner({"name": "Jordan Okafor", "team": "Platform", "patterns": "app/usage/*"})
        self.store.add_owner({"name": "Marcus Lee", "team": "Billing", "patterns": "app/billing/*"})
        self.graph.set_setting("auto_rules", "1")

    def rule(self, question, answer, day, by="Jordan Okafor"):
        """A signed standing rule (scope any) by `by`, recorded on `day` of
        June, as the dry run's seeded rows were."""
        task = self.store.add_run({"title": "Earlier call", "repo": REPO})["id"]
        did = self.graph.add_decision(task, question, "data", "pending", repo=REPO, owner=by, path="app/usage/")
        self.store.answer(did, {"answer": answer, "signed_by": by, "source": f"inbox: {by}"})
        self.store.make_rule(did, {"by": by, "scope": "any", "expected_updated_at": self.store.get_decision(did)["updated_at"]})
        when = f"2026-06-{day:02d}T12:00:00+00:00"
        self.graph.db.execute("UPDATE decisions SET created_at=?, updated_at=? WHERE id=?", (when, when, did))
        return did

    def both(self, page_by="Jordan Okafor"):
        return self.rule(TABLE_Q, TABLE_A, 9), self.rule(PAGE_Q, PAGE_A, 10, by=page_by)

    def ask(self, question, joint=COMPOSED, single="", rerank=False):
        self.task = self.store.add_run({"title": "Charge for the assistant", "repo": REPO})["id"]
        for p in [semantic_on()] + model(joint, single, rerank):
            p.start()
            self.addCleanup(p.stop)
        return ladder.ask(self.store, Config(), self.task, question, path="app/usage/daily.py", category="data")

    def events(self, kind):
        return [r["detail"] for r in self.graph.db.execute("SELECT detail FROM events WHERE kind=?", (kind,))]

    def cited(self, did):
        return {r["related_id"] for r in self.graph.db.execute(
            "SELECT related_id FROM decision_links WHERE decision_id=? AND kind='derived'", (did,))}


class CompoundQuestionTests(JointRuleCase):
    def test_two_rules_of_one_owner_settle_a_compound_question_together(self):
        table, page = self.both()
        row = self.ask(COMPOUND)
        self.assertEqual((row["status"], row["source"], row["signoff"]), ("resolved", "memory", "rule"), row["evidence"])
        self.assertTrue(row["authorized"])
        self.assertEqual((row["source_id"], row["answer"]), (table, COMPOSED))
        self.assertEqual(row["signed_by"], "Jordan Okafor")
        # The fit reason names both calls; nobody was asked.
        self.assertEqual(row["fit_reason"], "The same two questions Jordan Okafor already answered on Jun 9 and Jun 10; "
                                            "Jordan said both apply anywhere, so nobody needed to be asked.")
        # Both rows are cited as its support, and the evidence says they were read together.
        self.assertEqual(self.cited(row["id"]), {table, page})
        self.assertIn(f"composed with decision {table}; decision {page}", row["evidence"])
        self.assertIn(f"the rules Jordan Okafor made of decisions {table} and {page}, together", row["evidence"])
        self.assertTrue(any(table in e and page in e for e in self.events("memory_compound")), self.events("memory_compound"))
        # The timeline row lists both calls.
        texts = [r["text"] for r in timeline.rows(self.store, self.task)]
        self.assertIn(f"Settled “{timeline.topic(COMPOUND)}” from Jordan's Jun 9 and Jun 10 calls; nobody asked", texts)
        settled = next(r for r in timeline.rows(self.store, self.task) if r["kind"] == "settled")
        self.assertEqual([ref["id"] for ref in settled["refs"]], [table, page])

    def test_a_closest_row_the_model_reads_but_that_does_not_answer_still_composes_both(self):
        """The dry run's other path: the near-miss reader picks the Jun 9
        row, the grounded composer finds it does not answer the whole, and
        the clauses are read together from there."""
        table, page = self.both()
        row = self.ask(COMPOUND, rerank=True)
        self.assertEqual((row["status"], row["signoff"], row["source_id"], row["answer"]), ("resolved", "rule", table, COMPOSED), row["evidence"])
        self.assertTrue(row["authorized"])
        self.assertEqual(self.cited(row["id"]), {table, page})
        self.assertIn("Jordan said both apply anywhere, so nobody needed to be asked", row["fit_reason"])
        self.assertTrue(self.events("memory_reranked"))

    def test_the_same_two_rows_by_two_owners_want_a_signature(self):
        table, page = self.both(page_by="Marcus Lee")
        row = self.ask(COMPOUND)
        self.assertEqual((row["status"], row["source"], row["source_id"]), ("resolved", "memory", table), row["evidence"])
        self.assertNotEqual(row["signoff"], "rule")
        self.assertFalse(row["authorized"])
        self.assertEqual(row["answer"], COMPOSED)
        self.assertEqual(self.cited(row["id"]), {table, page})
        self.assertIn("the answer combines sources, so a person still signs it here", row["fit_reason"])
        self.assertIn("combines or contrasts premises", row["evidence"])

    def test_a_composed_answer_that_adds_to_the_rules_wants_a_signature(self):
        for added in (COMPOSED.replace("by cursor,", "by cursor in pages of 500 rows,"),
                      COMPOSED.replace("usage_daily", "usage_hourly")):
            with self.subTest(added=added):
                self.setUp()
                table, page = self.both()
                row = self.ask(COMPOUND, joint=added)
                self.assertEqual((row["status"], row["source_id"], row["answer"]), ("resolved", table, added), row["evidence"])
                self.assertNotEqual(row["signoff"], "rule")
                self.assertFalse(row["authorized"])
                self.assertIn("the answer combines sources", row["fit_reason"])

    def test_a_question_that_asks_one_thing_settles_from_one_rule_as_before(self):
        table, page = self.both()
        row = self.ask(SINGLE, single=TABLE_A, rerank=True)
        self.assertEqual((row["status"], row["signoff"], row["source_id"], row["answer"]), ("resolved", "rule", table, TABLE_A), row["evidence"])
        self.assertEqual(self.cited(row["id"]), {table})
        self.assertNotIn("composed with", row["evidence"])
        self.assertFalse(self.events("memory_compound"))
        self.assertIn("nobody needed to be asked", row["fit_reason"])
        self.assertNotIn("two questions", row["fit_reason"])

    def test_a_compound_question_with_one_rule_only_is_not_settled_from_it(self):
        self.rule(TABLE_Q, TABLE_A, 9)
        row = self.ask(COMPOUND)
        self.assertNotEqual(row["signoff"], "rule")
        self.assertFalse(self.events("memory_compound"))


class ClauseTests(OfflineCase):
    def test_a_question_that_asks_two_things_splits_at_and_how(self):
        self.assertEqual(ladder._compound_clauses(COMPOUND),
                         ["Which table does assistant billing read usage from", "how are reads paged"])
        self.assertEqual(ladder._compound_clauses("Which table is read? And how are reads paged?"),
                         ["Which table is read", "how are reads paged"])
        self.assertEqual(ladder._compound_clauses("Where do charges post; which plans are metered?"),
                         ["Where do charges post", "which plans are metered"])

    def test_one_question_with_options_or_a_joined_subject_is_not_compound(self):
        for q in (TABLE_Q, PAGE_Q, SINGLE, "Which plans are charged per use: Enterprise only, or Team and Starter too?",
                  "Do labels and assistant answers share one allowance?", "What counts as one use?"):
            with self.subTest(q=q):
                self.assertEqual(ladder._compound_clauses(q), [])


class FitReasonTests(OfflineCase):
    def test_several_calls_are_named_with_their_days(self):
        from types import SimpleNamespace
        item = SimpleNamespace(answered_by="Jordan Okafor", signed_by="Jordan Okafor", rule_scope="any", reusable=1)
        self.assertEqual(fit_reason(item, "near match", True, when="2026-06-09", also=["2026-06-10"]),
                         "The same two questions Jordan Okafor already answered on Jun 9 and Jun 10; Jordan said both "
                         "apply anywhere, so nobody needed to be asked.")
        self.assertIn("The same three questions Jordan Okafor already answered on Jun 9, Jun 10 and Jun 11; Jordan said all "
                      "of them apply anywhere", fit_reason(item, "direct", True, when="2026-06-09", also=["2026-06-10", "2026-06-11"]))
        # Without a rule, the other calls do not change the sentence.
        self.assertEqual(fit_reason(item, "direct", False, when="2026-06-09", also=["2026-06-10"]),
                         fit_reason(item, "direct", False, when="2026-06-09"))
