"""A standing rule that covers the question settles it, whatever record
agrees with it, and a settlement reads the same in its status, its sign-off
and its fit reason; a static first pass that a model pass will revisit
publishes a near match as a check in progress, not as settled.

Measured on the Harborline dry run (demo v6, runs 5 and 6): "What counts as
one billable use of Pilot" and "paging" had Raj's standing rule (scope any)
and a git record that agreed with it, and the record won the cross-source
arbitration, so the calls wanted a signature; "Where do Pilot charges post"
was left partial with sign-off required while its fit reason said nobody
needed to be asked; and the static first pass settled "per-use rate" from
Dana's "which plans are metered" call at term overlap 0.80, shown as settled
on the Being built page until the model pass withdrew it a minute later."""
import datetime
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import canvas, ladder, llm
from bridge.config import Config

REPO = "harborline/harborline-app"
RULE_Q = "What counts as one billable use of Pilot: a request, a completed response, or tokens?"
RULE_A = ("One completed Pilot response is one use (BILL-201, PR #7). Tokens, requests, streamed chunks and "
          "prompts that never completed are not units of use.")
ASKED = "What counts as one billable use of Pilot?"
NEAR = "What counts as one billable use of Pilot for Enterprise orgs?"
PLANS_Q = "Which plans are charged per use for Pilot: Enterprise only, or Team and Starter too?"
PLANS_A = "Enterprise only. Pilot is not on Team or Starter, and nothing metered goes on a self-serve plan."
RATE_Q = "What is the per-use rate Enterprise pays for Pilot?"


def semantic_on():
    """The model rungs on, as a deployment with a model has them; the
    deterministic first pass of the canvas stays without them."""
    return patch.object(Config, "semantic_retrieval", property(lambda self: not self.deterministic))


def pick_memory(cfg, question, cands, **_scope):
    """A selector that picks the memory candidate, as the model would."""
    return next((v for v in cands.values() if v[0] == "memory"), None)


class RuleCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store("rule-over-record.db")
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.store.add_owner({"name": "Raj Patel", "team": "Platform data", "patterns": "services/usage/*"})
        self.store.add_owner({"name": "Dana Whitfield", "team": "Monetization", "patterns": "app/billing/plans.py"})
        self.graph.set_setting("auto_rules", "1")
        for method in ("complete", "complete_json"):
            guard = patch.object(llm.Client, method, side_effect=AssertionError("No model in this test"))
            guard.start()
            self.addCleanup(guard.stop)

    def precedent(self, question, answer, by, path, scope="any"):
        task = self.store.add_run({"title": "Earlier call", "repo": REPO})["id"]
        did = self.graph.add_decision(task, question, "policy", "pending", repo=REPO, owner=by, path=path)
        self.store.answer(did, {"answer": answer, "signed_by": by, "source": f"inbox: {by}"})
        self.store.make_rule(did, {"by": by, "scope": scope,
                                   "expected_updated_at": self.store.get_decision(did)["updated_at"]})
        return did

    def record(self, days_from_now=1):
        """A merge record that says what the rule says, newer than it."""
        when = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days_from_now)).isoformat()
        self.graph.upsert_intent(REPO, "merge", "7", "Merge pull request #7 from harborline/raj/bill-201-billable-use",
                                 "BILL-201: what counts as one billable use of Pilot. One completed Pilot response "
                                 "counts as one billable use; the event kind is pilot.response.completed.",
                                 "Raj Patel", when, paths=["services/usage/"])

    def ask(self, question, path="services/usage/events.py", cfg=None, **extra):
        task = self.store.add_run({"title": "Charge Enterprise for Pilot", "repo": REPO})["id"]
        return ladder.ask(self.store, cfg or Config(model_api="none"), task, question, path=path,
                          category="definition", **extra)


class RuleOverRecordTests(RuleCase):
    def test_a_standing_rule_wins_over_a_newer_record_that_agrees_with_it(self):
        source = self.precedent(RULE_Q, RULE_A, "Raj Patel", "services/usage/")
        self.record()
        row = self.ask(ASKED)
        self.assertEqual((row["status"], row["source"], row["signoff"]), ("resolved", "memory", "rule"), row["evidence"])
        self.assertEqual(row["source_id"], source)
        self.assertTrue(row["authorized"])
        # Both are cited: the rule it settled from, and the record that agrees.
        self.assertIn("merge 7", row["evidence"])
        self.assertIn("agrees", row["evidence"])
        self.assertIn("nobody needed to be asked", row["fit_reason"])

    def test_the_rule_wins_before_the_selector_is_asked(self):
        """With a model, the selector used to choose between the rule and
        the record; a rule that covers the question is not up for
        arbitration when the record agrees."""
        source = self.precedent(RULE_Q, RULE_A, "Raj Patel", "services/usage/")
        self.record()
        with semantic_on(), patch("bridge.ladder._selector_pick") as selector, \
                patch("bridge.ladder._disagrees", return_value=False), \
                patch("bridge.ladder._compose_answer", return_value=RULE_A), \
                patch("bridge.ladder._record_contradicting", return_value=None):
            row = self.ask(ASKED, cfg=Config())
        selector.assert_not_called()
        self.assertEqual((row["status"], row["source"], row["signoff"]), ("resolved", "memory", "rule"), row["evidence"])
        self.assertEqual(row["source_id"], source)
        self.assertIn("merge 7", row["evidence"])

    def test_a_record_that_disagrees_keeps_the_arbitration(self):
        self.precedent(RULE_Q, RULE_A, "Raj Patel", "services/usage/")
        self.record()
        with semantic_on(), patch("bridge.ladder._selector_pick", return_value=None) as selector, \
                patch("bridge.ladder._disagrees", return_value=True), \
                patch("bridge.ladder._record_contradicting", return_value=None), \
                patch("bridge.ladder._expand_query", return_value=[]), \
                patch("bridge.ladder._wide_select", return_value=None), \
                patch("bridge.ladder._compose_joint", return_value=None), \
                patch("bridge.ladder._open_twin", return_value=None), \
                patch("bridge.ladder._how_they_decide", return_value=("", "", "")):
            row = self.ask(ASKED, cfg=Config())
        selector.assert_called()
        self.assertNotEqual(row["signoff"], "rule")


class SettledFromRuleConsistencyTests(RuleCase):
    def test_a_rule_covered_answer_that_settles_only_part_says_so_everywhere(self):
        """The composed answer states a gap: the node is partial, a person
        signs it, and the fit reason says that rather than "nobody needed
        to be asked"."""
        self.precedent(RULE_Q, RULE_A, "Raj Patel", "services/usage/")
        hedged = RULE_A + " The record does not specify whether a streamed chunk that times out counts."
        with semantic_on(), patch("bridge.ladder._selector_pick", side_effect=pick_memory), \
                patch("bridge.ladder._compose_answer", return_value=hedged), \
                patch("bridge.ladder._disagrees", return_value=False), \
                patch("bridge.ladder._record_contradicting", return_value=None), \
                patch("bridge.ladder._compose_joint", return_value=None):
            row = self.ask(NEAR, cfg=Config())
        self.assertEqual(row["status"], "partial", row["evidence"])
        # The canvas marks the sign-off required when it places the node;
        # the ladder leaves no rule on it.
        self.assertNotEqual(row["signoff"], "rule")
        self.assertFalse(row["authorized"])
        self.assertIn("only in part", row["fit_reason"])
        self.assertIn("a person still signs it here", row["fit_reason"])
        self.assertNotIn("nobody needed to be asked", row["fit_reason"])
        self.assertIn("only in part", row["evidence"])

    def test_a_rule_covered_whole_answer_is_settled_everywhere(self):
        source = self.precedent(RULE_Q, RULE_A, "Raj Patel", "services/usage/")
        with semantic_on(), patch("bridge.ladder._selector_pick", side_effect=pick_memory), \
                patch("bridge.ladder._compose_answer", return_value=RULE_A), \
                patch("bridge.ladder._disagrees", return_value=False), \
                patch("bridge.ladder._record_contradicting", return_value=None):
            row = self.ask(NEAR, cfg=Config())
        self.assertEqual((row["status"], row["signoff"], row["source_id"]), ("resolved", "rule", source))
        self.assertTrue(row["authorized"])
        self.assertIn("nobody needed to be asked", row["fit_reason"])


class StaticFirstPassTests(RuleCase):
    def test_a_near_match_is_a_check_in_progress_when_a_model_pass_follows(self):
        source = self.precedent(PLANS_Q, PLANS_A, "Dana Whitfield", "app/billing/plans.py")
        row = self.ask(RATE_Q, path="app/billing/plans.py", provisional=True)
        self.assertEqual(row["status"], "proposed", row["evidence"])
        self.assertNotEqual(row["signoff"], "rule")
        self.assertFalse(row["authorized"])
        self.assertEqual(row["source_id"], source)
        self.assertEqual(row["prediction"], PLANS_A)
        self.assertIn("checking", row["evidence"])
        self.assertFalse(row["fit_reason"])

    def test_the_same_question_still_settles_in_the_static_pass(self):
        source = self.precedent(PLANS_Q, PLANS_A, "Dana Whitfield", "app/billing/plans.py")
        row = self.ask(PLANS_Q, path="app/billing/plans.py", provisional=True)
        self.assertEqual((row["status"], row["signoff"], row["source_id"]), ("resolved", "rule", source))

    def test_a_near_match_settles_when_no_model_pass_follows(self):
        """A deployment with no model at all keeps the static settlement."""
        source = self.precedent(PLANS_Q, PLANS_A, "Dana Whitfield", "app/billing/plans.py")
        row = self.ask(RATE_Q, path="app/billing/plans.py")
        self.assertEqual((row["status"], row["signoff"], row["source_id"]), ("resolved", "rule", source))

    def test_the_canvas_shows_checking_then_the_model_pass_decides(self):
        """On the canvas, the node is published as a prediction still being
        read, never as settled, and the model pass that follows routes it
        to Dana when the earlier call does not answer it."""
        self.precedent(PLANS_Q, PLANS_A, "Dana Whitfield", "app/billing/plans.py")
        with self.graph.transaction():
            dana = self.graph.add_person("Dana Whitfield", email="dana@harborline.example")
            self.graph.add_authority("path", "app/billing/plans.py", "decides", person_id=dana, repo=REPO,
                                     source="config")
        self.graph.upsert_artifact(REPO, "app/billing/plans.py")
        task = canvas.start_task(self.store, Config(model_api="none"), {
            "title": "Charge Enterprise for Pilot", "repo": REPO, "agent": "test", "requester": "Alex Moreno",
            "goal": "Charge Enterprise customers for Pilot by use.", "paths": "app/billing/plans.py"})["task_id"]
        scheduled = []
        with semantic_on(), patch("bridge.canvas._start_background", side_effect=lambda key, fn, *a: scheduled.append((fn, a))):
            node = canvas.add_node(self.store, Config(), {"task_id": task, "question": RATE_Q,
                                                           "paths": "app/billing/plans.py", "category": "policy"})
        self.assertEqual(node["status"], "predicted", node)
        self.assertTrue(node["model_pending"])
        self.assertFalse(node["authorized"])
        self.assertNotEqual(node["signoff"], "rule")
        self.assertIn("checking", node["evidence"])
        row = self.store.get_decision(node["node_id"])
        self.assertEqual(row["status"], "proposed")
        self.assertTrue(row["model_pending"])
        # The model pass: the earlier call does not answer this, so Dana is asked.
        fn, args = scheduled[-1]
        with semantic_on(), patch.object(llm.Client, "complete", side_effect=llm.LLMError("offline")), \
                patch.object(llm.Client, "complete_json", side_effect=llm.LLMError("offline")), \
                patch("bridge.ladder._disagrees", return_value=False):
            fn(*args)
        after = self.store.get_decision(node["node_id"])
        self.assertEqual(after["status"], "pending", after["evidence"])
        self.assertFalse(after["model_pending"])
        self.assertEqual(after["owner_name"], "Dana Whitfield")
