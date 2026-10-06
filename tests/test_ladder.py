"""Regression pins ported from the ladder's e2e suite. Every one encodes a
behavior that a blind run, an exam sitting, or a premise loop once got
wrong. All offline: the semantic rung is off unless a test wires fakes."""

import datetime
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase

from bridge import ladder as ladder_mod
from bridge import llm as llm_mod
from bridge.config import Config
from bridge.ladder import (
    _canned_misfit, _cited_refs, _display_terms, _meaningful_terms, _open_twin, _ref_variants, run_task,
)
from bridge.routing import route
from bridge.store import Store


def seed_changes(graph, repo, rows):
    """The rows an ingested repository would hold, from (path, author,
    role, days_ago) tuples: one change per tuple, its path in the tree,
    the clock set so recency counts."""
    now = datetime.datetime.now(datetime.timezone.utc)
    graph.set_source(repo, "git_now", now.isoformat())
    for i, (path, author, role, days_ago) in enumerate(rows):
        graph.upsert_artifact(repo, path)
        graph.upsert_engineer(author)
        graph.add_change(repo, f"c{i:04d}", (now - datetime.timedelta(days=days_ago)).isoformat(),
                         [path], [(author, "", role)])


# One metering area: Yuki holds the live majority, Priya is CODEOWNERS-listed with a fifth of the changes.
METERING = ([("metering/exporter.py", "Yuki Tanaka", "author", i) for i in range(14)]
            + [("metering/exporter.py", "Priya Natarajan", "author", 20 + i) for i in range(4)])


class LadderCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "ladder.db")
        self.graph = self.store.graph
        self.cfg = Config()

    def semantic(self, on=True):
        os.environ["BRIDGE_SEMANTIC"] = "1" if on else "0"

    def patch(self, target, name, value):
        p = patch.object(target, name, value)
        p.start()
        self.addCleanup(p.stop)

    def first(self, result):
        return self.graph.decisions_for_task(result.task_id)[0]


def _wire_fakes(case, json_map=None, text_map=None):
    """Purpose-dispatched fakes: every selector says none, every composer
    refuses, so the ladder's fallback behavior is what runs."""
    json_map = json_map or {}
    text_map = text_map or {}

    def complete_json(self, purpose, system, prompt, **kw):
        if purpose in json_map:
            v = json_map[purpose]
            return v(prompt) if callable(v) else v
        if purpose in ("select", "precedent"):
            return {"pick": "none"}
        if purpose == "expand":
            return {"phrases": []}
        if purpose == "followup":
            return {"needs_context": False, "question": ""}
        return {"same": False}

    def complete(self, purpose, system, prompt, **kw):
        if purpose in text_map:
            value = text_map[purpose]
            if purpose == "compose_joint" and "USED_SOURCES:" not in value:
                labels = __import__('re').findall(r"(?:^|\n)(r\d+) \(", prompt)
                marker = "\nUSED_SOURCES: " + ",".join(labels) + "\n"
                value = value.replace("COVERAGE:", marker + "COVERAGE:") if "COVERAGE:" in value else value + marker
            return value
        if purpose == "conflict":
            return "UNRELATED"
        if purpose == "compose_joint":
            return "RECORDS DO NOT ANSWER"
        return "RECORD DOES NOT ANSWER"

    case.patch(llm_mod.Client, "complete_json", complete_json)
    case.patch(llm_mod.Client, "complete", complete)


class SupportedAnswerTests(LadderCase):
    """A composed answer says only what its source states; what it reached
    by reasoning is named as open. Measured live on 5e967e4: a ticket that
    capped Retry-After jitter at retry_after_max was composed into "for a
    zero parsed delay the same mechanism holds", which the ticket never
    says, and a less careful host could have had it signed as evidence."""

    QUESTION = ("Does jitter apply to both numeric and HTTP-date Retry-After headers, including the case where the "
                "parsed delay is zero?")
    COMPOSED = ("The rule caps Retry-After jitter: nonnegative jitter is added to the capped base and the total is "
                "clamped to retry_after_max. For a zero parsed delay, the same mechanism holds.")

    def setUp(self):
        super().setUp()
        self.semantic()
        self.addCleanup(os.environ.pop, "BRIDGE_SEMANTIC", None)
        self.graph.upsert_intent("lib", "ticket", "NET-201", "Retry-After jitter hard total ceiling",
                                 "Settled library decision: Retry-After jitter must never exceed retry_after_max. Add "
                                 "nonnegative jitter to the existing capped base and clamp the total to "
                                 "retry_after_max.", "Mira Runtime", "2026-09-29T12:00:00Z", status="Done")

    def ask(self, support):
        _wire_fakes(self, json_map={"select": {"pick": "r1"}, "support": support},
                    text_map={"compose": self.COMPOSED})
        return run_task(self.graph, self.cfg, "jitter", repo="lib",
                        decisions=[{"question": self.QUESTION, "category": "definition"}])

    def test_what_the_record_does_not_state_is_split_out_and_named(self):
        result = self.ask({"claims": [
            {"claim": "Jitter is clamped so the total never exceeds retry_after_max", "support": "stated"},
            {"claim": "For a zero parsed delay the same mechanism holds", "support": "inferred"}],
            "answer": "The rule caps Retry-After jitter: nonnegative jitter is added to the capped base and the "
                      "total is clamped to retry_after_max.",
            "open": ["Whether jitter applies when the parsed delay is zero?"]})
        d = self.first(result)
        self.assertEqual(d.status, "partial", d.evidence)
        self.assertNotIn("same mechanism holds", d.answer)
        self.assertTrue(d.answer.endswith("The record does not establish: Whether jitter applies when the parsed "
                                          "delay is zero."), d.answer)

    def test_an_answer_the_record_fully_supports_is_kept_as_it_is(self):
        d = self.first(self.ask({"claims": [{"claim": "capped", "support": "stated"},
                                            {"claim": "zero too", "support": "applied"}],
                                 "answer": "ignored", "open": []}))
        self.assertEqual(d.answer, self.COMPOSED)

    def test_what_the_question_and_the_source_establish_is_never_made_open(self):
        """Measured live on 63eb671: a question calling timeout_slack a numeric
        option got an answer saying that was not established, a signed rule's
        reuse asked whether the decision it came from was real, and a
        who-and-why answer gained questions about motives nobody asked."""
        from bridge.ladder import _supported
        prompts = []

        def support(reply):
            def answer(prompt):
                prompts.append(prompt)
                return reply
            return answer
        cases = [
            # Provenance is never open, as a removed claim or as a question.
            ({"claims": [{"claim": "Negative values raise ValueError", "support": "stated"},
                         {"claim": "The rule comes from Mira Runtime", "support": "inferred"}],
              "answer": "x", "open": ["Is record 5a1dba95d9eb a real reference or assumed?"]}, None),
            # More open points than statements taken out: the statements are named.
            ({"claims": [{"claim": "Zero delays are jittered too", "support": "inferred"}],
              "answer": "Jitter is capped at retry_after_max. Whether zero delays are jittered is not stated.",
              "open": ["Does jitter apply at zero?", "What motivated the cap?"]},
             "Jitter is capped at retry_after_max. The records do not establish: Zero delays are jittered too."),
            # What the question gives is not inferred.
            ({"claims": [{"claim": "timeout_slack is a numeric option", "support": "given"},
                         {"claim": "Negative values raise ValueError", "support": "stated"}],
              "answer": "x", "open": []}, None),
        ]
        text = "Negative or non-finite values raise ValueError for timeout_slack."
        for reply, want in cases:
            _wire_fakes(self, json_map={"support": support(reply)})
            got = _supported(Config(), "For a new numeric retry option named timeout_slack, must negatives raise?",
                             "(earlier answer 5a1dba95d9eb, author/assignee: Mira Runtime)\nNegative or non-finite "
                             "numeric Retry options raise ValueError.", text, "the records",
                             given="Context: timeout_slack is a new numeric Retry option\nStated facts: release=next")
            self.assertEqual(got, want or text)
        self.assertIn("GIVEN:\nContext: timeout_slack is a new numeric Retry option\nStated facts: release=next",
                      prompts[0])
        self.assertIn("author/assignee: Mira Runtime", prompts[0])

    def test_a_rule_the_question_meets_is_given_to_the_support_check(self):
        prompts = []
        _wire_fakes(self, json_map={"select": {"pick": "m1"}, "rerank": {"id": "none"},
                                    "support": lambda p: prompts.append(p) or {"claims": [], "answer": "", "open": []}},
                    text_map={"compose": "Negative or non-finite values raise ValueError; bools count as 0 or 1."})
        store = self.store
        store.add_owner({"name": "Mira Runtime", "team": "Runtime", "patterns": "src/*"})
        run = store.add_run({"title": "validation", "repo": "lib"})
        source = store.request({"run_id": run["id"], "question": "For a numeric Retry option, must negative and "
                                "non-finite values raise ValueError, and are bools accepted?", "context": "validation",
                                "path": "src/retry.py"})
        store.answer(source["id"], {"answer": "Negative or non-finite numeric Retry options raise ValueError; bools "
                                              "are accepted as 0 or 1.", "rationale": "predictable API"})
        store.make_rule(source["id"], {"by": "Mira Runtime", "conditions": "numeric retry option",
                                       "expected_updated_at": store.get_decision(source["id"])["updated_at"]})
        run_task(self.graph, self.cfg, "timeout_slack", repo="lib", decisions=[{
            "question": "Should timeout_slack, a new numeric retry option, reject negative and non-finite values with "
                        "ValueError and accept bools?", "path": "src/retry.py", "category": "policy"}])
        self.assertTrue(prompts, "the support check did not run")
        self.assertIn(f"covers this question: its conditions (numeric retry option) are met here", prompts[0])

    def test_a_prediction_names_no_detail_their_answers_do_not_state(self):
        """Measured live on 63eb671: "Which retry metric label and rollout
        region are approved?" got a prediction to use retry_normal_total "in
        the standard evaluation region", which no answer of theirs names, and
        it was labelled the same policy as their signed answer."""
        from bridge.ladder import _how_they_decide
        task = self.graph.create_task("prior")
        prior = "Which retry metrics suffix should normal customers use?"
        self.graph.add_decision(task, prior, "policy", "approved", source="human", answered_by="Mira Runtime",
                                answer="Use retry_normal_total.", embedding=llm_mod.embed(prior), repo="lib")
        question = "Which retry metric label and rollout region are approved?"
        self.patch(llm_mod, "compose_proposal", lambda *a, **k: (
            "Use retry_normal_total as the metric label and apply rollout to the standard evaluation region.",
            "same_policy"))
        prompts = []
        _wire_fakes(self, json_map={"support": lambda p: prompts.append(p) or {
            "claims": [{"claim": "Use retry_normal_total as the metric label", "support": "applied"},
                       {"claim": "Apply rollout to the standard evaluation region", "support": "inferred"}],
            "answer": "Use retry_normal_total as the metric label.",
            "open": ["Which rollout region is approved?"]}})
        proposal, used, why = _how_they_decide(self.graph, Config(), "lib", question, "", llm_mod.embed(question),
                                               "Mira Runtime")
        self.assertEqual(proposal, "Use retry_normal_total as the metric label. Their earlier answers do not "
                                   "establish: Which rollout region is approved.")
        self.assertIn("how they decide (by analogy with their signed answers)", why)
        self.assertIn("it leaves open what their earlier answers do not settle", why)
        self.assertIn("only when it introduces a concrete value, name, place, region", llm_mod.PREDICTION_SUPPORT_SYSTEM)
        # Measured live: a ticket gave the header as an example, and the
        # composed answer said the format was not established.
        self.assertIn("establishes the format or shape it demonstrates", llm_mod.COMPOSER_SYSTEM)
        self.assertIn("never say the format is not established", llm_mod.COMPOSER_SYSTEM)
        self.assertIn("demonstrates by example", llm_mod.SUPPORT_SYSTEM)
        self.assertIn("Q: Which retry metrics suffix should normal customers use?\nA: Use retry_normal_total.",
                      prompts[0])
        # Nothing of it supported: no prediction at all.
        _wire_fakes(self, json_map={"support": {"claims": [{"claim": "Apply rollout to the standard evaluation "
                                                                     "region", "support": "inferred"}],
                                                "answer": "", "open": ["Which region?"]}})
        self.assertEqual(_how_they_decide(self.graph, Config(), "lib", question, "", llm_mod.embed(question),
                                          "Mira Runtime"), ("", "", ""))
        # A statement made only of what their answers and the question
        # contain is their pattern carried over, whatever the label said.
        _wire_fakes(self, json_map={"support": {"claims": [{"claim": "Use retry_normal_total", "support": "inferred"}],
                                                "answer": "", "open": ["Which label?"]}})
        self.patch(llm_mod, "compose_proposal", lambda *a, **k: ("Use retry_normal_total.", "analogy"))
        self.assertEqual(_how_they_decide(self.graph, Config(), "lib", question, "", llm_mod.embed(question),
                                          "Mira Runtime")[0], "Use retry_normal_total.")
        _wire_fakes(self, json_map={"support": {"claims": [{"claim": "the region", "support": "inferred"}],
                                                "answer": "", "open": ["Which region?"]}})
        self.patch(llm_mod, "compose_proposal", lambda *a, **k: (
            "Use retry_normal_total as the metric label and apply rollout to the standard evaluation region.",
            "same_policy"))
        # A vague label does not launder the new word the prediction brings in.
        self.assertEqual(_how_they_decide(self.graph, Config(), "lib", question, "", llm_mod.embed(question),
                                          "Mira Runtime"), ("", "", ""))

    def test_a_partial_answer_that_says_what_the_records_leave_open_is_kept(self):
        """Measured live on 63eb671: a joint answer naming the approved
        stopped-retry label was thrown away because it said the records do
        not name a rollout region."""
        from bridge.ladder import _compose_joint
        rows = [{"kind": "ticket", "ref": "DONE-8", "title": "Retry audit metric label",
                 "body": "The approved metric label for a stopped retry is retry_stopped_total.",
                 "author": "Mira Runtime", "created_at": "2026-09-29", "status": "Done", "resolved": 1},
                {"kind": "ticket", "ref": "NEW-17", "title": "Current retry telemetry retention",
                 "body": "Keep retry telemetry for 7 days.", "author": "Mira Runtime", "created_at": "2026-09-29",
                 "status": "Done", "resolved": 1}]
        _wire_fakes(self, json_map={"support": {"claims": [], "answer": "", "open": []}},
                    text_map={"compose_joint": "The approved label for a stopped retry is retry_stopped_total; "
                                               "the records do not name a rollout region. COVERAGE: PARTIAL"})
        got = _compose_joint(Config(), "Which retry metric label and rollout region are approved?", rows)
        self.assertIsNotNone(got)
        self.assertTrue(got[0].startswith("The approved label for a stopped retry is retry_stopped_total"), got)
        self.assertTrue(got[2])
        _wire_fakes(self, text_map={"compose_joint": "The records do not specify an approved label or region."})
        self.assertIsNone(_compose_joint(Config(), "Which retry metric label and rollout region are approved?", rows))

    def test_a_proposal_must_take_one_of_the_offered_alternatives(self):
        from bridge.ladder import _how_they_decide, _offered_choices
        self.assertEqual(_offered_choices("Must the trace exporter flush every 11 or 13 events?"), ["11", "13"])
        task = self.graph.create_task("prior")
        prior = "Must the new retry trace exporter reject negative event counts?"
        self.graph.add_decision(task, prior, "policy", "approved", source="human", answered_by="Mira Runtime",
                                answer="Negative or non-finite numeric options raise ValueError.",
                                embedding=llm_mod.embed(prior), repo="lib")
        question = "Must the new retry trace exporter flush every 11 or 13 events?"
        self.patch(llm_mod, "compose_proposal",
                   lambda *a, **k: ("Negative or non-finite numeric options raise ValueError.", "analogy"))
        # Without the guard this earlier answer is what would be proposed.
        with patch.object(ladder_mod, "_takes_a_choice", lambda proposal, choices: True):
            self.assertTrue(_how_they_decide(self.graph, Config(), "lib", question, "", llm_mod.embed(question),
                                             "Mira Runtime")[0])
        self.assertEqual(_how_they_decide(self.graph, Config(), "lib", question, "", llm_mod.embed(question),
                                          "Mira Runtime"), ("", "", ""))
        self.patch(llm_mod, "compose_proposal", lambda *a, **k: ("Flush every 13 events.", "analogy"))
        _wire_fakes(self, json_map={"support": {"claims": [{"claim": "Flush every 13 events", "support": "applied"}],
                                                "answer": "Flush every 13 events.", "open": []}})
        self.assertEqual(_how_they_decide(self.graph, Config(), "lib", question, "", llm_mod.embed(question),
                                          "Mira Runtime")[0], "Flush every 13 events.")


class NotAdoptedRecordTests(LadderCase):
    """A record of something that was not adopted is history, never the
    answer. Measured live on main 35a5f84: a composed answer read a
    cancelled Jira proposal as the current policy, and the node showed as
    resolved from evidence."""

    QUESTION = "Should Retry-After waits add random jitter by default?"

    def cancelled(self, kind="ticket", status="Cancelled", title="Add random jitter to Retry-After waits by default"):
        self.graph.upsert_intent("lib", kind, "RET-7", title,
                                 "Proposal: every Retry-After wait adds a random 0 to 2 seconds, on by default.",
                                 "Ana Ruiz", "2026-03-01T00:00:00Z", status=status)

    def ask(self):
        return run_task(self.graph, self.cfg, "jitter", repo="lib",
                        decisions=[{"question": self.QUESTION, "category": "policy"}])

    def test_a_cancelled_proposal_the_selector_picks_is_not_composed(self):
        self.cancelled()
        self.semantic()
        seen = []

        def select(prompt):
            seen.append(prompt)
            return {"pick": "r1"}
        _wire_fakes(self, json_map={"select": select},
                    text_map={"compose": "Retry-After waits add a random 0 to 2 seconds by default, per RET-7."})
        with patch.object(ladder_mod, "_compose_answer", wraps=ladder_mod._compose_answer) as compose:
            result = self.ask()
        compose.assert_not_called()
        d = self.first(result)
        self.assertNotIn(d.status, ("resolved", "partial", "proposed"), d.evidence)
        self.assertIn("was Cancelled: it records something that was not adopted", d.evidence)
        self.assertTrue(any("NOT ADOPTED" in p for p in seen), seen)

    def test_status_counts_for_any_kind_and_markers_in_the_record_count_too(self):
        for kind, status, title in (("jira", "Won't Do", "Add jitter"), ("ticket", "Done", "[OBSOLETE] Jitter policy"),
                                    ("doc", "", "Jitter policy")):
            row = {"kind": kind, "status": status, "title": title, "resolved": 1,
                   "body": "Superseded by POL-12." if kind == "doc" else ""}
            self.assertTrue(ladder_mod._void(row), row)
            self.assertFalse(ladder_mod._is_settled(row), row)
        self.assertEqual(ladder_mod._void({"kind": "ticket", "status": "Closed", "title": "Retry cap",
                                            "body": "Resolution: Won't Do", "resolved": 1}), "Won't Do")
        done = {"kind": "ticket", "status": "Done", "title": "Add jitter", "body": "Resolution: Done", "resolved": 1}
        self.assertEqual(ladder_mod._void(done), "")
        self.assertTrue(ladder_mod._is_settled(done))
        self.assertEqual(ladder_mod._ticket_state({"kind": "jira", "status": "Won't Do"}), " [Won't Do]")

    def test_a_long_composed_answer_is_kept_whole_or_not_used(self):
        self.semantic()
        long_whole = "Retry-After waits add jitter only when the caller opts in. " * 12
        self.patch(llm_mod.Client, "complete", lambda self, purpose, system, prompt, **kw: long_whole.strip())
        self.assertEqual(ladder_mod._compose_answer(self.cfg, self.QUESTION, "ticket RET-9", "body"), long_whole.strip())
        overlong = "Retry-After waits add jitter only when the caller opts in. " * 40
        self.patch(llm_mod.Client, "complete", lambda self, purpose, system, prompt, **kw: overlong.strip())
        self.assertIsNone(ladder_mod._compose_answer(self.cfg, self.QUESTION, "ticket RET-9", "body"))

    ROWS = [{"kind": "ticket", "ref": "RET-7", "title": "Jitter by default", "body": "Proposal.", "author": "Ana",
             "created_at": "2026-03-01", "status": "Cancelled", "resolved": 1},
            {"kind": "pr", "ref": "3301", "title": "Retry-After handling", "body": "Honors Retry-After.",
             "author": "Bo", "created_at": "2026-04-01", "status": "", "resolved": 1},
            {"kind": "review", "ref": "3301:9", "title": "Docstring placement", "body": "Document it in Retry.",
             "author": "Cy", "created_at": "2026-04-02", "status": "", "resolved": 1}]

    def composed(self, question, reply="RECORDS DO NOT ANSWER"):
        self.semantic()
        prompts = []

        def complete(self, purpose, system, prompt, **kw):
            prompts.append((system, prompt))
            labels = __import__('re').findall(r"(?:^|\n)(r\d+) \(", prompt)
            marker = "\nUSED_SOURCES: " + ",".join(labels) + "\n"
            return reply.replace("COVERAGE:", marker + "COVERAGE:") if "COVERAGE:" in reply else reply + marker
        self.patch(llm_mod.Client, "complete", complete)
        return ladder_mod._compose_joint(self.cfg, question, [dict(r) for r in self.ROWS]), prompts

    def test_the_joint_composer_leaves_it_out(self):
        # Measured live: given the cancelled proposal marked NOT ADOPTED,
        # the composer reversed which option it had rejected.
        out, prompts = self.composed(self.QUESTION, "Retry-After is honored per 3301.\nCOVERAGE: FULL")
        self.assertNotIn("RET-7", prompts[0][1])
        self.assertEqual(out[0], "Retry-After is honored per 3301.")
        self.assertIn("left out, not adopted: ticket RET-7 [Cancelled]", out[1])

    def test_a_question_about_history_sees_it_marked_as_history(self):
        out, prompts = self.composed("Why was jitter by default rejected before?")
        self.assertIsNone(out)
        system, prompt = prompts[0]
        self.assertIn("NOT ADOPTED (Cancelled): history only", prompt)
        self.assertIn("NOT ADOPTED", system)

    def test_an_answer_that_leaves_part_open_is_partial(self):
        out, _ = self.composed(self.QUESTION, "Retry-After is honored per 3301.\nCOVERAGE: PARTIAL")
        self.assertEqual(out[0], "Retry-After is honored per 3301.")
        self.assertTrue(out[2])
        # The live answer's own words, with no coverage line.
        live = ("The docstring documents it per 3301. However, none of the records show the changelog fragment "
                "itself; so the pattern of docstring-plus-fragment is supported, but whether a statement is needed "
                "is not settled.")
        out, _ = self.composed(self.QUESTION, live)
        self.assertTrue(out[2])


class ReversalEvidenceTests(LadderCase):
    def record(self):
        self.graph.upsert_intent("app", "pr", "14209", "Fix failed mutation teardown",
                                 "Record entries only after successful mutations.",
                                 "Ada", "2026-06-20T00:00:00Z")

    def ask_reversal(self):
        return run_task(self.graph, self.cfg, "Reconsider teardown", repo="app",
                        decisions=[{"question": "Should we revert #14209?", "category": "compat"}])

    def test_model_selection_cannot_promote_original_record_to_reversal_approval(self):
        self.record()
        self.semantic()
        _wire_fakes(self, json_map={"select": {"pick": "r1"}},
                    text_map={"compose": "Yes, revert it."})
        result = self.ask_reversal()
        self.assertFalse(result.resolved)
        self.assertFalse(result.assumed)
        self.assertIn("selected pr 14209", self.first(result).evidence)

    def test_joint_composer_cannot_promote_context_to_reversal_approval(self):
        self.record()
        self.graph.upsert_intent("app", "pr", "14210", "Failed mutation teardown follow-up",
                                 "The teardown fix prevents duplicate errors.",
                                 "Ada", "2026-06-21T00:00:00Z")
        self.semantic()
        _wire_fakes(self)
        with patch.object(ladder_mod, "_compose_joint", return_value=("Yes, revert it.", "14209, 14210")) as compose:
            result = self.ask_reversal()
        compose.assert_not_called()
        self.assertFalse(result.resolved)
        self.assertIn("retrieved context pr 14209", self.first(result).evidence)

    def test_signed_original_approval_is_not_approval_to_reverse_it(self):
        q = "Should we apply #14209?"
        task = self.graph.create_task("Original approval")
        self.graph.add_decision(task, q, "policy", "approved", source="human", answered_by="Ada",
                                answer="Yes, apply #14209.", embedding=llm_mod.embed(q), repo="app")
        result = self.ask_reversal()
        self.assertFalse(result.resolved)
        self.assertIn("not a signed answer to a reversal request", self.first(result).evidence)

    def test_signed_answer_to_the_same_reversal_question_is_reusable(self):
        q = "Should we revert #14209?"
        task = self.graph.create_task("Reversal approval")
        self.graph.add_decision(task, q, "policy", "approved", source="human", answered_by="Ada",
                                answer="Yes, revert #14209 because it breaks teardown.",
                                embedding=llm_mod.embed(q), repo="app")
        result = self.ask_reversal()
        self.assertTrue(result.resolved)
        self.assertEqual(self.first(result).answered_by, "Ada")

    def test_conflicting_record_cannot_replace_reversal_approval(self):
        q = "Should we revert #14209?"
        task = self.graph.create_task("Reversal approval")
        self.graph.add_decision(task, q, "policy", "approved", source="human", answered_by="Ada",
                                answer="Revert #14209 to restore the 7 day token lifetime.",
                                embedding=llm_mod.embed(q), repo="app")
        self.graph.upsert_intent("app", "pr", "14210", "Token lifetime",
                                 "The token lifetime is 30 days.", "Bob", "2026-06-21T00:00:00Z")
        rival = self.graph.intents_by_ref(["14210"], repo="app")[0]
        with patch.object(ladder_mod, "_record_contradicting", return_value=rival):
            result = self.ask_reversal()
        self.assertFalse(result.resolved)
        # The record states another figure than the signed approval: a
        # conflict for a person, with neither side served in its place.
        self.assertIn("serves neither and proposes neither", self.first(result).evidence)
        self.assertFalse(self.first(result).prediction)


class TermTests(LadderCase):
    def test_display_terms_restore_the_users_words(self):
        text = "who dropped the DeprecationWarning once from pyproject?"
        shown = _display_terms(text, _meaningful_terms(text))
        self.assertIn("dropped", shown)
        self.assertIn("deprecationwarning", shown)
        self.assertTrue(all(not s.endswith("dropp") for s in shown))

    def test_gap_detection_catches_hedges_about_the_records(self):
        for text in ("Neither record specifies whether a month-to-month plan is treated the same.",
                     "No record addresses credit notes.",
                     "That case is not covered by any record here.",
                     "The record does not explicitly state the window."):
            self.assertTrue(ladder_mod._states_a_gap(text), text)
        self.assertFalse(ladder_mod._states_a_gap("The cap is 24 hours per decision 91aee06f152d."))

    def test_contrast_terms_name_the_case_asked_about(self):
        self.assertEqual(ladder_mod._contrast_terms(
            "Same customer, but their contract is month-to-month rather than annual. Do the credits roll over?"),
            ["month"])
        self.assertEqual(ladder_mod._contrast_terms("What if the duplicate charge was $1,200 instead?"), [])
        self.assertEqual(ladder_mod._unmet_contrast(
            ["Is the fee waived for nonprofit customers rather than enterprise ones?"],
            ["Yes. The fee is waived for enterprise customers on annual terms."]), ["nonprofit"])
        self.assertEqual(ladder_mod._unmet_contrast(
            ["Is the fee waived for nonprofit customers rather than enterprise ones?"],
            ["Nonprofit and enterprise customers are both exempt."]), [])

    def test_cited_refs_finds_pr_numbers(self):
        self.assertEqual(_cited_refs("why did we drop 3.7 in PR #5072?"), ["5072"])
        self.assertEqual(_cited_refs("see #123 and pr 456, then #123 again"), ["123", "456"])
        self.assertEqual(_cited_refs("upgrade to python 3.12"), [])

    def test_cited_refs_hyphenated_and_variants(self):
        refs = _cited_refs("Does PR-4310's figure still hold after EHS-0987?")
        self.assertIn("PR-4310", refs)
        self.assertIn("EHS-0987", refs)
        variants = _ref_variants(["PR-4310"])
        self.assertIn("PR-4310", variants)
        self.assertIn("4310", variants)
        self.assertNotIn("utf-8", _cited_refs("encode as utf-8 please"))

    def test_canned_misfit_catches_which_release_questions(self):
        self.assertTrue(_canned_misfit("Which Flask release should first ship the __version__ warning?"))
        self.assertTrue(_canned_misfit("What version removes it?"))
        self.assertFalse(_canned_misfit("How should the rollout be staged?"))

    def test_canned_misfit_catches_non_sequitur_templates(self):
        self.assertTrue(_canned_misfit("Should the CHANGES.rst entry follow the same format used for the earlier "
                                       "no-cookie-domain change?", "rollout"))
        self.assertTrue(_canned_misfit("Should tests be updated to expect SameSite=Lax, or should a separate new "
                                       "test cover the default?", "ux"))
        self.assertFalse(_canned_misfit("How should the new default roll out to existing apps?", "rollout"))
        self.assertFalse(_canned_misfit("Should existing cookies keep their current behavior?", "compat"))

    def test_gap_narration_is_detected(self):
        self.assertTrue(ladder_mod._GAP_ASSUME_RE.search(
            "the precedent doesn't set a pattern for this at all, so no assumption about /healthz's "
            "treatment can be derived from it"))
        self.assertTrue(ladder_mod._GAP_ASSUME_RE.search(
            "that's an assumption gap to flag elsewhere, not something i can infer from this commit"))
        self.assertFalse(ladder_mod._GAP_ASSUME_RE.search("roll out in phased batches, matching the precedent"))

    def test_numeric_clash_detection(self):
        self.assertTrue(ladder_mod._numeric_clash("rated to 6800 m for splice work", "depth ceiling of 6200 m applies"))
        self.assertFalse(ladder_mod._numeric_clash("rated to 6200 m", "ceiling of 6200 m"))
        self.assertFalse(ladder_mod._numeric_clash("no figures here", "6200 m ceiling"))
        # A figure named only to deny or replace it is not one the text
        # states. Measured live on 5e967e4: "19 days, not 11 days" agreed
        # with a signed "11 days" because both contained 11.
        signed = "Keep retry error summaries for 11 days."
        self.assertTrue(ladder_mod._numeric_clash(signed, "Retain them for 19 days, not 11 days."))
        self.assertTrue(ladder_mod._numeric_clash(signed, "Retention changed from 11 days to 19 days."))
        self.assertTrue(ladder_mod._numeric_clash(signed, "Retain them for 19 days instead of 11 days."))
        self.assertFalse(ladder_mod._numeric_clash(signed, "Retain them for 11 days, not 19 days."))
        self.assertFalse(ladder_mod._numeric_clash("Keep them 19 days.", "Retention changed from 11 days to 19 days."))
        self.assertFalse(ladder_mod._numeric_clash("Retention was set to 19 days.", "Retain for 19 days."))

    def test_followup_reference_regex(self):
        for q in ["Is that same ceiling compatible with the splice depth?",
                  "You cited FLEET-210; does it cover night dives too?",
                  "Given your answer, which owner signs off on the exception?",
                  "If so, who approves the exception?",
                  "What if the duplicate charge was $1,200 instead?"]:
            self.assertTrue(ladder_mod._FOLLOWUP_REF_RE.search(q.lower()), q)
        for q in ["What is the depth ceiling for splice repairs?", "Who owns the tanker dispatch rotation?"]:
            self.assertFalse(ladder_mod._FOLLOWUP_REF_RE.search(q.lower()), q)

    def test_routing_question_regex(self):
        for q in ["Who should Raven actually route this to?",
                  "Whose call is it, given CODEOWNERS and blame disagree?",
                  "Who should own the odorization calibration going forward?",
                  "Who should sign off on the new exemption from now on?"]:
            self.assertTrue(ladder_mod._ROUTING_Q_RE.search(q.lower()), q)
        for q in ["Who decided to remove the legacy export button and why?",
                  "Who decided to approve the Class 1 exemption last year?",
                  "What is the odorization minimum for distribution mains?"]:
            self.assertFalse(ladder_mod._ROUTING_Q_RE.search(q.lower()), q)

    def test_accountability_and_status_regexes(self):
        for q in ["Who is accountable for the vessel-risk-profile corruption?",
                  "Whose responsibility is the detention workflow?",
                  "Who owns the odorization calibration service?",
                  "Who is the most active reviewer in this org?",
                  "Who is on the hook for the priority registry?",
                  # The present tense of the same question: how an agent
                  # asks it, and what a person means by "who do I ask".
                  "Who decides about pkg/services/sqlstore/migrations?",
                  "Who approves changes to the odorization service?",
                  "Who signs off on a schema migration here?",
                  "Who has the final say on the draindown cap?",
                  "Who should I ask about the detention workflow?",
                  "Who do we talk to about the priority registry?"]:
            self.assertTrue(ladder_mod._ACCOUNTABILITY_Q_RE.search(q.lower()), q)
        for q in ["Who decided to cap the draindown at 25 gpm?", "Who authored the mid-loop alarm change?",
                  "Who approved the Class 1 exemption?", "What is the draindown cap value?"]:
            self.assertFalse(ladder_mod._ACCOUNTABILITY_Q_RE.search(q.lower()), q)
        for q in ["Has the corruption been cleaned up, and who is accountable?",
                  "Is it done yet, and whose call is the rollout?"]:
            self.assertTrue(ladder_mod._STATUS_CLAUSE_RE.search(q.lower()), q)
        self.assertFalse(ladder_mod._STATUS_CLAUSE_RE.search("who is accountable for the registry?"))

    def test_guess_category_marks_retrospective_questions(self):
        self.assertEqual(ladder_mod.guess_category("Who decided to pause refunds, and why?"), "data-source")
        self.assertEqual(ladder_mod.guess_category("Whose call is it to change the batch size?"), "policy")
        self.assertEqual(ladder_mod.guess_category("Do we bill customer-run load tests?"), "policy")


class TwinTests(LadderCase):
    def test_open_decisions_dedup_across_tasks(self):
        q1 = "Which release should remove the deprecated __version__ attribute entirely?"
        r1 = run_task(self.graph, self.cfg, "deprecate __version__", decisions=[{"question": q1, "category": "policy"}])
        first_id = r1.open[0]["id"]
        q2 = "Which release should the deprecated __version__ attribute be removed in?"
        r2 = run_task(self.graph, self.cfg, "emit a warning for __version__",
                      decisions=[{"question": q2, "category": "policy"}])
        self.assertTrue(r2.open and r2.open[0]["id"] == first_id)
        self.assertIn("already open as decision", r2.open[0]["note"])
        n_open = self.graph.db.execute("SELECT count(*) c FROM decisions WHERE status='pending'").fetchone()["c"]
        self.assertEqual(n_open, 1)

    def test_open_twin_confirms_near_band_with_model(self):
        self.semantic(True)

        class FakeClient:
            def __init__(self, same):
                self.same, self.calls = same, 0

            def complete_json(self, purpose, system, prompt):
                self.calls += 1
                return {"same": self.same}

        q1 = "Should SESSION_COOKIE_SECURE also change its default in the same release?"
        t = self.graph.create_task("t1")
        self.graph.add_decision(t, q1, "policy", "pending", embedding=llm_mod.embed(q1), repo="flask")
        q2 = "Do we also flip SESSION_COOKIE_SECURE for session cookies when this ships?"
        emb2 = llm_mod.embed(q2)
        yes = FakeClient(True)
        twin = _open_twin(self.graph, Config(), yes, q2, emb2, "flask")
        self.assertIsNotNone(twin)
        self.assertEqual(twin.question, q1)
        self.assertEqual(yes.calls, 1)
        no = FakeClient(False)
        self.assertIsNone(_open_twin(self.graph, Config(), no, q2, emb2, "flask"))
        self.semantic(False)
        silent = FakeClient(True)
        self.assertIsNone(_open_twin(self.graph, Config(), silent, q2, emb2, "flask"))
        self.assertEqual(silent.calls, 0)

    def test_open_twin_found_despite_sibling_crowding(self):
        t1 = self.graph.create_task("add --json flag")
        siblings = [
            "Should the --json flag be added to flask routes or a new command?",
            "What fields should each route entry include in the json output?",
            "Should json output mode suppress the header row entirely?",
            "Should tests for the json output mode go in tests/test_cli.py?",
            "Does the json flag need to handle the --sort option interaction?",
            "Should json output pretty-print or emit compact json by default?",
        ]
        for s in siblings:
            self.graph.add_decision(t1, s, "policy", "pending", embedding=llm_mod.embed(s), repo="flask")
        q1 = "Does adding the --json flag require a CHANGES.rst entry?"
        twin_id = self.graph.add_decision(t1, q1, "ops", "pending", embedding=llm_mod.embed(q1), repo="flask")
        q2 = "Does adding JSON output mode require a CHANGES.rst entry and documentation updates?"
        tw = _open_twin(self.graph, Config(), None, q2, llm_mod.embed(q2), "flask")
        self.assertIsNotNone(tw)
        self.assertEqual(tw.id, twin_id)

    def test_new_refs_break_twin_absorption(self):
        t1 = self.graph.create_task("t1")
        q1 = "What is the worst-case webhook staleness during an outage?"
        self.graph.add_decision(t1, q1, "policy", "pending", embedding=llm_mod.embed(q1), repo="port")
        q2 = "Does the worst-case webhook staleness figure from PR-4310 still hold during an outage?"
        self.assertIsNone(_open_twin(self.graph, Config(), None, q2, llm_mod.embed(q2), "port"))

    def test_answering_closes_open_twins_in_other_tasks(self):
        q = "Should the /healthz config flag default to enabled or disabled?"
        t1 = self.graph.create_task("healthz v1")
        d1 = self.graph.add_decision(t1, q, "policy", "pending", embedding=llm_mod.embed(q), repo="acme",
                                     owner="David Lord")
        t2 = self.graph.create_task("healthz v2")
        q2 = "Should the /healthz config flag default to enabled, or disabled?"
        d2 = self.graph.add_decision(t2, q2, "policy", "pending", embedding=llm_mod.embed(q2), repo="acme")
        self.store.answer(d1, {"answer": "Disabled by default."})
        twin = self.graph.get_decision(d2)
        self.assertEqual(twin.status, "resolved")
        self.assertIn("settled by the same answer", twin.evidence)
        self.assertEqual(twin.answer, "Disabled by default.")

    def test_inbox_signoff_closes_open_twins(self):
        """The inbox's own sign-off path settles twins too, and never a
        decision bound to a managed provider call."""
        self.store.add_owner({"name": "Wes", "team": "Data", "patterns": "billing/*"})
        run = self.store.add_run({"title": "t", "repo": "acme/platform"})
        a = self.store.request({"run_id": run["id"], "question": "Should test traffic be billed?", "context": "c",
                                "path": "billing/usage.py"})
        b = self.store.request({"run_id": run["id"], "question": "Should test traffic be billed?", "context": "c2",
                                "path": "billing/usage.py"})
        self.store.answer(a["id"], {"answer": "Exclude it", "rationale": "internal"})
        self.assertEqual(self.store.get_decision(b["id"])["status"], "resolved")


class RoutingTests(LadderCase):
    def test_unscoped_questions_get_repo_wide_evidence(self):
        g = self.graph
        seed_changes(g, "flask", [("examples/tutorial/app.py", "David Lord", "author", i) for i in range(12)])
        picked = route(g, "flask", "What should python_requires be set to in pyproject.toml?")
        self.assertIsNotNone(picked)
        owner, evidence, _score = picked
        self.assertEqual(owner, "David Lord")
        joined = "; ".join(evidence)
        self.assertNotIn("examples/", joined)
        self.assertIn("repo-wide", joined)

    def test_repo_wide_route_prefers_recent_history(self):
        g = self.graph
        seed_changes(g, "flask", [("src/flask/app.py", "David Baumgold", "author", 5000 + i) for i in range(30)]
                     + [("src/flask/app.py", "David Lord", "author", i) for i in range(10)])
        picked = route(g, "flask", "What should python_requires be set to in pyproject.toml?")
        self.assertIsNotNone(picked)
        owner, evidence, score = picked
        self.assertEqual(owner, "David Lord")
        self.assertIn("recency weighted", evidence[0])
        self.assertIn("repo-wide", evidence[0])
        self.assertLessEqual(score, 1.0)

    def test_repo_wide_route_needs_a_real_sample(self):
        """One author of four commits is not the owner of everything."""
        g = self.graph
        seed_changes(g, "flask", [("src/flask/app.py", "Solo Dev", "author", i) for i in range(4)])
        self.assertIsNone(route(g, "flask", "Do we apply per-seat minimums under usage-based pricing?"))

    def test_thin_test_only_scope_routes_nobody(self):
        """Three changes under a test directory are not ownership: the
        route names nobody and says which area was too thin, rather than
        naming the test author or falling back to whoever carries the repo."""
        g = self.graph
        seed_changes(g, "flask", [("tests/type_check/typing_app.py", "Test Author", "author", i) for i in range(3)]
                     + [("src/flask/app.py", "David Lord", "author", i) for i in range(12)])
        notes: list[str] = []
        self.assertIsNone(route(g, "flask", "What should the URL path be for the type check?", notes=notes))
        self.assertIn("tests/type_check/", "; ".join(notes))

    def test_ownership_rows_alone_route_nobody(self):
        """A store seeded only by set_ownership holds no changes: the
        signal route names nobody and says why; the inbox's own pattern
        routing is what applies after it."""
        g = self.graph
        g.set_ownership("flask", "examples/", "David Lord", "blame", 0.9, "90% of the 114 commit touches under examples/")
        notes: list[str] = []
        self.assertIsNone(route(g, "flask", "Who owns the examples?", notes=notes))
        self.assertTrue(notes)

    def test_co_owner_note_names_signals_not_scores(self):
        g = self.graph
        seed_changes(g, "tq", [("triagequeue/queue.py", "Amara Osei", "author", i) for i in range(14)]
                     + [("triagequeue/queue.py", "Tomas Reyes", "author", 20)]
                     + [("triagequeue/queue.py", "Lee Park", "author", 30 + i) for i in range(5)])
        g.add_listing("tq", "codeowners", "triagequeue/", "@tomas")
        picked = route(g, "tq", "Who owns the triagequeue backlog policy?", path="triagequeue/queue.py")
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0], "Amara Osei")
        joined = "; ".join(picked[1])
        self.assertNotIn("scores nearly even", joined)
        self.assertNotIn("0.4", joined)
        if "Tomas" in joined:
            self.assertTrue("live majority" in joined or "live activity" in joined)

    def test_live_majority_overrides_stale_codeowners(self):
        g = self.graph
        seed_changes(g, "platform", METERING)
        g.add_listing("platform", "codeowners", "metering/", "@priya")
        picked = route(g, "platform", "Who owns the metering exporter?", path="metering/exporter.py")
        self.assertEqual(picked[0], "Yuki Tanaka")
        joined = "; ".join(picked[1])
        self.assertIn("CODEOWNERS lists Priya Natarajan", joined)
        self.assertIn("CODEOWNERS approval", joined)

    def test_person_seeking_escalation_labels_triage_contact(self):
        g = self.graph
        seed_changes(g, "hft", [("kill-switch/fast_path.py", "Marcus Chen", "author", i) for i in range(10)])
        for i in range(6):
            g.upsert_intent("hft", "commit", f"k{i}", f"kill switch {i}", "", "Marcus Chen", "2026-08-01T00:00:00Z")
        q = "Who was responsible for taking the kill-switch fast path away, and why did they decide that?"
        r = run_task(g, self.cfg, "trace the kill switch story", repo="hft",
                     decisions=[{"question": q, "category": "data-source"}])
        d = self.first(r)
        if d.status == "pending":
            self.assertIn("triage contact", d.owner_evidence)
            self.assertIn("NOT the person this question asks about", d.owner_evidence)


class RecordTests(LadderCase):
    def test_no_flag_platitude_for_release_questions(self):
        self.graph.upsert_intent("flask", "commit", "abc", "unrelated precedent", "", "Ada", "2026-01-01T00:00:00Z")
        q = "Which release should first ship the deprecation warning?"
        r = run_task(self.graph, self.cfg, "deprecate it", decisions=[{"question": q, "category": "rollout"}])
        self.assertFalse(r.assumed)
        self.assertNotIn("feature flag", self.first(r).answer or "")

    def test_retro_question_resolves_from_short_commit_title(self):
        g = self.graph
        g.upsert_intent("flask", "commit", "faef9a0f", "deprecate __version__ attribute", "", "David Lord",
                        "2026-01-05T00:00:00Z")
        g.upsert_intent("flask", "commit", "aaa", "remove old package pins", "", "Ada", "2026-01-01T00:00:00Z")
        g.upsert_intent("flask", "commit", "bbb", "clarify reasoning in docs", "", "Ada", "2026-01-02T00:00:00Z")
        q = "What reason was given for removing the __version__ attribute from the flask package?"
        r = run_task(g, self.cfg, "history question", repo="flask",
                     decisions=[{"question": q, "category": "data-source"}])
        d = self.first(r)
        self.assertIn(d.status, ("resolved", "partial"))
        self.assertIn("faef9a0f", d.evidence)

    def test_retro_why_resolves_partial_with_pointer(self):
        g = self.graph
        g.upsert_intent("flask", "pr", "5242", "deprecate __version__ attribute (#5242)", "", "David Lord",
                        "2026-01-05T00:00:00Z")
        g.upsert_intent("flask", "commit", "aaa", "remove old package pins", "", "Ada", "2026-01-01T00:00:00Z")
        g.upsert_intent("flask", "commit", "bbb", "clarify reasoning in docs", "", "Ada", "2026-01-02T00:00:00Z")
        q = "What reason was given for removing the __version__ attribute from the flask package?"
        r = run_task(g, self.cfg, "history question", repo="flask",
                     decisions=[{"question": q, "category": "data-source"}])
        d = self.first(r)
        self.assertIn(d.status, ("partial", "resolved"))
        self.assertTrue("5242" in (d.answer or "") or "5242" in d.evidence)
        self.assertFalse(r.open)
        from bridge.context_memory import edges
        self.assertEqual([(source['ref'], source['role']) for source in edges(g.db, d.id)], [('5242', 'context')])

    def test_retro_pointer_requires_topical_record(self):
        g = self.graph
        g.upsert_intent("app", "commit", "b1", "remove legacy billing cron shim",
                        "cleanup of legacy code paths, removed the shim", "Dana Cruz", "2026-01-05T00:00:00Z")
        g.upsert_intent("app", "ticket", "UI-9", "export button removal proposal",
                        "should the legacy export button be removed?", "Lee Park", "2026-02-01T00:00:00Z",
                        status="To Do", resolved=False)
        q = "Who decided to remove the legacy export button and what reason was given?"
        r = run_task(g, self.cfg, "retro", repo="app", decisions=[{"question": q, "category": "data-source"}])
        d = self.first(r)
        self.assertNotIn("closest recorded change", d.answer or "")
        self.assertNotEqual(d.answered_by, "Dana Cruz")

    def test_contested_by_newer_finds_open_symptom_ticket(self):
        g = self.graph
        g.upsert_intent("ops", "pr", "3240", "Debounce taxi route conflict check", "Debounce raised to 8 seconds.",
                        "Ada", "2026-06-01T00:00:00Z")
        g.upsert_intent("ops", "ticket", "OPS-1988", "Investigate intermittent taxi route flapping",
                        "Route flickering between two options, likely the conflict check debounce.", "Ben",
                        "2026-07-24T00:00:00Z")
        chosen = g.db.execute("SELECT * FROM intents WHERE ref='3240'").fetchone()
        hit = ladder_mod._contested_by_newer(g, "ops", chosen)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["ref"], "OPS-1988")

    def test_recency_question_downgrades_contested_record(self):
        g = self.graph
        g.upsert_intent("ops", "pr", "3240", "Debounce taxi route conflict check",
                        "Conflict check debounce raised to 8 seconds for taxi route suggestions.", "Ada",
                        "2026-06-01T00:00:00Z")
        g.upsert_intent("ops", "ticket", "OPS-1988", "Investigate intermittent taxi route flapping",
                        "Route flickering between two options every few seconds, likely the conflict check debounce.",
                        "Ben", "2026-07-24T00:00:00Z")
        q = "What debounce value for the taxi route conflict check is actually in effect today?"
        r = run_task(g, self.cfg, "check debounce", repo="ops", decisions=[{"question": q, "category": "definition"}])
        d = self.first(r)
        self.assertEqual(d.status, "pending")
        self.assertIn("OPS-1988", d.evidence)
        self.assertIn("contested by newer", d.evidence)

    def test_recency_question_confirms_when_nothing_newer(self):
        g = self.graph
        g.upsert_intent("ops", "pr", "3240", "Debounce taxi route conflict check",
                        "Conflict check debounce raised to 8 seconds for taxi route suggestions.", "Ada",
                        "2026-06-01T00:00:00Z")
        q = "What debounce value for the taxi route conflict check is actually in effect today?"
        r = run_task(g, self.cfg, "check debounce", repo="ops", decisions=[{"question": q, "category": "definition"}])
        d = self.first(r)
        self.assertEqual(d.status, "pending")
        self.assertEqual(d.kind, "new")
        self.assertIn("no newer indexed record revisits", d.evidence)

    def test_ref_named_record_bypasses_lexical_gates(self):
        g = self.graph
        g.upsert_intent("sat", "pr", "GSOPS-2075", "Slew rate limiter for antenna scheduling",
                        "Per-station slew budget of 4 deg/s applies to conflict resolution passes.", "Mei",
                        "2026-05-01T00:00:00Z")
        for i in range(3):
            g.upsert_intent("sat", "commit", f"n{i}", f"noise {i}", "", "Ada", "2026-01-01T00:00:00Z")
        q = "Under GSOPS-2075, what budget applies during conflict resolution passes?"
        r = run_task(g, self.cfg, "check the budget", repo="sat", decisions=[{"question": q, "category": "definition"}])
        d = self.first(r)
        self.assertEqual(d.status, "pending")
        self.assertIn("GSOPS-2075", d.evidence)

    def test_superseded_record_is_replaced_by_the_newer_one(self):
        g = self.graph
        g.upsert_intent("auth", "pr", "70", "Service token expiry set to 7 days",
                        "Service tokens expire after 7 days to match legacy rotation.", "Dana", "2026-04-01T00:00:00Z")
        g.upsert_intent("auth", "pr", "88", "Service token expiry capped at 24 hours",
                        "Security baseline caps service token expiry at 24 hours; the 7 day setting is retired.",
                        "Dana", "2026-05-12T00:00:00Z")
        q = "What service token expiry applies?"
        r = run_task(g, self.cfg, "token expiry", repo="auth", decisions=[{"question": q, "category": "definition"}])
        d = self.first(r)
        self.assertEqual(d.status, "pending")
        self.assertIn("24 hours", d.evidence)
        self.assertIn("88", d.evidence)

    def test_git_revert_commit_retires_the_record_it_names(self):
        """git's own Revert "X" commit says nothing about superseding; the
        word revert alone retires the record its subject names."""
        g = self.graph
        g.upsert_intent("auth", "pr", "70", "Service token expiry set to 7 days",
                        "Service tokens expire after 7 days to match legacy rotation.", "Dana", "2026-04-01T00:00:00Z")
        g.upsert_intent("auth", "commit", "c0ffee123456", 'Revert "Service token expiry set to 7 days"',
                        "This reverts commit 1234abcd. The 7 day expiry broke token rotation.",
                        "Dana", "2026-05-12T00:00:00Z")
        chosen = g.db.execute("SELECT * FROM intents WHERE ref='70'").fetchone()
        newer = ladder_mod._superseded_by_newer(g, "auth", chosen)
        self.assertIsNotNone(newer)
        self.assertEqual(newer["ref"], "c0ffee123456")

    def test_open_ticket_is_never_served_as_settled(self):
        g = self.graph
        g.upsert_intent("meter", "ticket", "T-430", "Change the metering export batch size from 500 to 5000",
                        "Proposal: raise the export batch size to 5000. Need a decision from Priya.", "Yuki",
                        "2026-08-12T00:00:00Z", status="To Do", resolved=False)
        q = "What metering export batch size should the exporter use?"
        r = run_task(g, self.cfg, "batch size", repo="meter", decisions=[{"question": q, "category": "definition"}])
        d = self.first(r)
        self.assertNotIn(d.status, ("resolved", "partial"))
        self.assertNotIn("5000", d.answer or "")


class OwnershipRungTests(LadderCase):
    def test_ownership_introspection_answered_from_graph(self):
        g = self.graph
        g.upsert_engineer("Amara Osei")
        g.upsert_engineer("Tomas Reyes")
        g.set_ownership("antctl", "antctl/", "Amara Osei", "blame", 0.75, "75% of touches under antctl/")
        g.set_ownership("antctl", "antctl/", "Tomas Reyes", "codeowners", 1.0, "CODEOWNERS lists @tomas")
        q = "Who holds the largest blame share for antctl, and does the CODEOWNERS entry point to someone else?"
        r = run_task(g, self.cfg, "ownership check", repo="antctl", decisions=[{"question": q, "category": "data-source"}])
        d = self.first(r)
        self.assertEqual(d.status, "resolved")
        self.assertIn("75%", d.answer)
        self.assertIn("CODEOWNERS-listed", d.answer)
        self.assertIn("ownership graph", d.evidence)
        q2 = "Should the CODEOWNERS entry for antctl be updated to Amara?"
        r2 = run_task(g, self.cfg, "ownership change", repo="antctl", decisions=[{"question": q2, "category": "policy"}])
        self.assertNotIn("ownership graph", self.first(r2).evidence or "")

    def test_ownership_rung_skips_compound_questions(self):
        g = self.graph
        g.upsert_engineer("Elena Vasquez")
        g.set_ownership("dud", "dudledger/", "Elena Vasquez", "blame", 0.6, "60% of touches under dudledger/")
        q = ("Has a fix been signed off for the sensor nuisance trips, and whose call is it, given CODEOWNERS "
             "points at a different person than blame does?")
        r = run_task(g, self.cfg, "sensor fix", repo="dud", decisions=[{"question": q, "category": "ops"}])
        self.assertNotIn("ownership graph", self.first(r).evidence or "")
        q2 = "Per ENG-95, who holds the blame share for dudledger?"
        r2 = run_task(g, self.cfg, "check", repo="dud", decisions=[{"question": q2, "category": "data-source"}])
        self.assertNotIn("ownership graph", self.first(r2).evidence or "")

    def test_ownership_answer_scopes_to_named_service(self):
        g = self.graph
        for svc, eng, w in [("fuel-load-calc/", "Ann A", 0.9), ("tanker-dispatch/", "Bob B", 0.8),
                            ("retardant-mixing/", "Cy C", 0.7), ("dudledger/", "Elena Vasquez", 0.6)]:
            g.set_ownership("wf", svc, eng, "blame", w, f"{w} under {svc}")
        ans = ladder_mod._ownership_answer(g, "wf", "Who holds the largest blame share for dudledger?")
        self.assertIn("Elena Vasquez", ans)
        self.assertIn("60%", ans)
        self.assertNotIn("fuel-load-calc", ans)
        self.assertEqual(ladder_mod._ownership_answer(g, "wf", "Who holds the largest blame share for zephyr?"), "")

    def test_accountability_question_resolves_to_graph_owner(self):
        g = self.graph
        seed_changes(g, "port", [("vessel-risk-profile/profile.py", "Elin Kowalski", "author", i) for i in range(12)]
                     + [("vessel-risk-profile/profile.py", "Naledi Mokoena", "author", 20 + i) for i in range(5)]
                     + [("vessel-risk-profile/profile.py", "Ola Berg", "author", 40 + i) for i in range(3)])
        g.add_listing("port", "codeowners", "vessel-risk-profile/", "@elin")
        q = "Who is accountable for the vessel-risk-profile service?"
        r = run_task(g, self.cfg, "accountability", repo="port", decisions=[{"question": q, "category": "ops"}])
        d = self.first(r)
        self.assertEqual(d.status, "resolved", d.evidence)
        self.assertIn("Elin Kowalski", d.answer)
        self.assertIn("ownership graph", d.evidence)

    def test_whose_call_to_change_something_is_still_accountability(self):
        g = self.graph
        seed_changes(g, "platform", METERING)
        g.add_listing("platform", "codeowners", "metering/", "@priya")
        q = "Whose call is it to change the export batch size in metering/exporter.py?"
        r = run_task(g, self.cfg, "batch size", repo="platform",
                     decisions=[{"question": q, "category": "policy", "path": "metering/exporter.py"}])
        d = self.first(r)
        self.assertEqual(d.status, "resolved", d.evidence)
        self.assertIn("Yuki Tanaka", d.answer)
        self.assertIn("Priya Natarajan", d.answer)

    def test_accountability_rung_skips_compound_status_questions(self):
        g = self.graph
        seed_changes(g, "port", [("vessel-risk-profile/profile.py", "Elin Kowalski", "author", i) for i in range(12)])
        q = "Has the vessel-risk-profile data corruption been cleaned up, and who is accountable for it?"
        r = run_task(g, self.cfg, "compound", repo="port", decisions=[{"question": q, "category": "ops"}])
        d = self.first(r)
        self.assertFalse(d.status == "resolved" and "resolved from the ownership graph" in (d.evidence or ""))


class ModelRungTests(LadderCase):
    def test_no_pattern_precedent_means_no_assumption(self):
        self.semantic(True)
        self.patch(ladder_mod, "_compose_assumption", lambda *a, **k: ladder_mod.ASSUME_GAP)
        self.patch(ladder_mod, "_selector_pick", lambda *a, **k: None)
        self.patch(ladder_mod, "_wide_select", lambda *a, **k: None)
        self.patch(ladder_mod, "_compose_answer", lambda *a, **k: None)
        self.patch(ladder_mod, "_open_twin", lambda *a, **k: None)
        self.patch(ladder_mod, "_compose_joint", lambda *a, **k: None)
        self.patch(llm_mod.Client, "complete_json", lambda self, purpose, system, prompt, **k: {"pick": "r1"})
        g = self.graph
        g.upsert_intent("acme", "commit", "abc", "healthz probe exempt from auth hooks",
                        "the /healthz probe endpoint skips hooks", "Ada", "2026-01-01T00:00:00Z")
        q = "Should the healthz probe endpoint be exempt from auth hooks?"
        r = run_task(g, Config(), "add healthz", repo="acme", decisions=[{"question": q, "category": "compat"}])
        self.assertFalse(r.assumed)
        d = self.first(r)
        self.assertNotEqual(d.status, "assumed")
        self.assertIn("sets no pattern", d.evidence)

    def test_contextualize_rewrites_only_with_context(self):
        g = self.graph
        t = g.create_task("t")
        g.add_decision(t, "What is the depth ceiling?", "policy", "resolved", answer="6200 m per FLEET-303", repo="sub")
        recent = g.recent_answered(repo="sub")
        self.assertTrue(recent and "6200" in recent[0].answer)
        self.patch(llm_mod.Client, "complete_json", lambda self, purpose, system, prompt, **k: {
            "needs_context": True, "question": "Does the 6200 m ceiling apply to splice work?"})
        rw = ladder_mod._contextualize(Config(), "Does that ceiling apply to splice work?", recent)
        self.assertIn("6200", rw)
        self.patch(llm_mod.Client, "complete_json", lambda self, purpose, system, prompt, **k: {
            "needs_context": False, "question": "anything"})
        self.assertEqual(ladder_mod._contextualize(Config(), "Does that ceiling apply?", recent), "")
        self.assertEqual(ladder_mod._contextualize(Config(), "Does that ceiling apply?", []), "")

    def test_expand_query_parses_and_bounds(self):
        self.patch(llm_mod.Client, "complete_json", lambda self, purpose, system, prompt, **k: {
            "phrases": ["silt filter purge", "sediment strainer", "", "x" * 80, "dup", "dup"]})
        out = ladder_mod._expand_query(Config(), "how often is the strainer flushed?")
        self.assertEqual(out, ["silt filter purge", "sediment strainer", "dup"])
        self.patch(llm_mod.Client, "complete_json", lambda self, purpose, system, prompt, **k: {"phrases": "nope"})
        self.assertEqual(ladder_mod._expand_query(Config(), "q"), [])

    def test_expansion_rescues_zero_overlap_paraphrase(self):
        self.semantic(True)
        g = self.graph
        g.upsert_intent("aq", "ticket", "AQ-77", "Silt purge cadence settled at 45 minutes",
                        "Purge the silt trap every 45 minutes during dredging; decided after the pump failures.",
                        "Rosa Diaz", "2026-02-01T00:00:00Z", status="Done")
        for i in range(4):
            g.upsert_intent("aq", "commit", f"c{i}", f"Routine berth telemetry patch {i}", "Unrelated maintenance merge.",
                            "Kai Ito", f"2026-03-0{i + 1}T00:00:00Z")
        q = "How frequently must accumulated material be cleared from the intake?"

        def fake_json(self, purpose, system, prompt, **k):
            if purpose == "expand":
                return {"phrases": ["silt purge cadence", "silt trap"]}
            if purpose == "select":
                m = re.search(r"\b(r\d+) \[ticket AQ-77", prompt)
                return {"pick": m.group(1)} if m else {"pick": "none"}
            return {"same": False}

        self.patch(llm_mod.Client, "complete_json", fake_json)
        self.patch(llm_mod.Client, "complete", lambda self, purpose, system, prompt, **k:
                   ("Every 45 minutes during dredging, per AQ-77." if purpose == "compose" else "UNRELATED"))
        r = run_task(g, Config(), "intake maintenance", repo="aq", decisions=[{"question": q, "category": "ops"}])
        d = self.first(r)
        self.assertEqual(d.status, "resolved", d.evidence)
        self.assertIn("45 minutes", d.answer)

    def test_no_canned_assumption_over_cited_refs(self):
        self.semantic(True)
        _wire_fakes(self)
        g = self.graph
        g.upsert_intent("sites", "ticket", "PAD-2555", "padlock log deprecation",
                        "padlock log entries replaced by RFID scans at all existing sites", "Ana Ruiz",
                        "2026-03-01T00:00:00Z", status="Done")
        q = "For the new satellite site, should we keep padlock entries during rollout, per PAD-2555?"
        r = run_task(g, Config(), "new site", repo="sites", decisions=[{"question": q, "category": "rollout"}])
        self.assertFalse(r.assumed)
        d = self.first(r)
        self.assertNotEqual(d.status, "assumed")
        self.assertIn("cites PAD-2555", d.evidence or "")

    def test_joint_composition_rescues_multihop(self):
        self.semantic(True)
        _wire_fakes(self, text_map={"compose_joint": "During storms the haul cap is 30 minutes per ticket FLT-2, "
                                                     "which narrows the 45 minute cap set by ticket FLT-1."})
        g = self.graph
        g.upsert_intent("fleet", "ticket", "FLT-1", "haul cap set at 45 minutes per tide",
                        "standard haul cap is 45 minutes per tide window", "Mo Farah", "2026-01-01T00:00:00Z", status="Done")
        g.upsert_intent("fleet", "ticket", "FLT-2", "storm exception narrows haul cap",
                        "during storm warnings the haul cap narrows to 30 minutes", "Mo Farah",
                        "2026-02-01T00:00:00Z", status="Done")
        q = "What haul cap applies during a storm tide window?"
        r = run_task(g, Config(), "storm ops", repo="fleet", decisions=[{"question": q, "category": "ops"}])
        d = self.first(r)
        self.assertEqual(d.status, "resolved", d.evidence)
        self.assertIn("30 minutes", d.answer)
        self.assertIn("composed across records", d.evidence)

    def test_settled_rival_conflict_leads_answer(self):
        self.semantic(True)
        _wire_fakes(self, json_map={"select": {"pick": "m1"}},
                    text_map={"conflict": "CONFLICT",
                              "compose": "Yes, dispatch sign off is mandatory for SIGMET-only reroutes."})
        g = self.graph
        g.upsert_intent("avops", "ticket", "AVOPS-455", "SIGMET-only auto reroutes proceed without dispatch sign off",
                        "ratified: reroutes on a SIGMET alone do not wait for dispatch sign off", "Priya Nair",
                        "2026-04-01T00:00:00Z", status="Done")
        t = g.create_task("prior")
        pq = "Does dispatch need to sign off before the system auto reroutes on a SIGMET?"
        g.add_decision(t, pq, "ops", "approved", source="human",
                       answer="Yes, dispatch sign off is mandatory for SIGMET-only reroutes.",
                       answered_by="Tunde Adebayo", embedding=llm_mod.embed(pq), repo="avops")
        q = "Must dispatch sign off before an auto reroute when only a SIGMET is active?"
        r = run_task(g, Config(), "reroute policy", repo="avops", decisions=[{"question": q, "category": "ops"}])
        d = self.first(r)
        self.assertEqual(d.status, "partial")
        self.assertTrue(d.answer.startswith("CONFLICTED"))
        self.assertIn("AVOPS-455", d.answer)
        from bridge.context_memory import edges
        self.assertEqual([(source['ref'], source['role']) for source in edges(g.db, d.id)], [('AVOPS-455', 'contradiction')])

    def test_followup_ref_in_prior_answer_reaches_memory(self):
        g = self.graph
        t = g.create_task("prior")
        pq = "What is the maximum dive depth for splice work?"
        g.add_decision(t, pq, "ops", "approved", source="human", answered_by="Mei",
                       answer="The ceiling is 6200 m, set by ticket FLEET-303 after the winch failure.",
                       embedding=llm_mod.embed(pq), repo="sub")
        q = "Was FLEET-303 the record that set that same ceiling?"
        r = run_task(g, self.cfg, "check", repo="sub", decisions=[{"question": q, "category": "ops"}])
        d = self.first(r)
        self.assertIn(d.status, ("resolved", "partial"), d.evidence)
        self.assertIn("6200", d.answer)

    def test_an_unsigned_record_derived_answer_is_not_memory(self):
        """A resolution nobody signed, derived from a record, is left out
        of memory: it adds nothing its record does not, and one unsigned
        reuse must not breed another."""
        g = self.graph
        t = g.create_task("prior")
        pq = "What is the maximum dive depth for splice work?"
        g.add_decision(t, pq, "ops", "resolved", source="record",
                       answer="The ceiling is 6200 m, set by ticket FLEET-303 after the winch failure.",
                       embedding=llm_mod.embed(pq), repo="sub")
        q = "Was FLEET-303 the record that set that same ceiling?"
        r = run_task(g, self.cfg, "check", repo="sub", decisions=[{"question": q, "category": "ops"}])
        d = self.first(r)
        self.assertEqual(d.status, "pending", d.evidence)
        self.assertIn("nothing similar answered before", d.evidence)

    def test_joint_composition_declines_routing_questions(self):
        self.semantic(True)
        g = self.graph
        g.upsert_intent("cal", "ticket", "ODOR-214", "calibration ledger perf ticket",
                        "Owen looked at calibration ledger performance", "Owen Fitzgerald", "2025-09-13T00:00:00Z", status="Done")
        g.upsert_intent("cal", "ticket", "ODOR-140", "calibration ledger integrity fix",
                        "future-dated entry integrity fix", "Tomas Vitkus", "2026-05-31T00:00:00Z", status="Done")
        rows = list(g.db.execute("SELECT * FROM intents"))
        called = {"n": 0}

        def fake_complete(self, purpose, system, prompt, **kw):
            if purpose == "compose_joint":
                called["n"] += 1
                return "Route to Owen Fitzgerald per ODOR-214."
            return "RECORD DOES NOT ANSWER"

        self.patch(llm_mod.Client, "complete", fake_complete)
        q = "A brand-new concern came up. Who should Raven route this to?"
        self.assertIsNone(ladder_mod._compose_joint(Config(), q, rows))
        self.assertEqual(called["n"], 0)

    def test_joint_composer_scrubs_listing_labels(self):
        self.semantic(True)
        _wire_fakes(self, text_map={"compose_joint": "The cap is 30 minutes (r2) per FLT-2, which narrows FLT-1 (r1)."})
        g = self.graph
        g.upsert_intent("f", "ticket", "FLT-1", "cap 45", "45 min cap", "A", "2026-01-01T00:00:00Z", status="Done")
        g.upsert_intent("f", "ticket", "FLT-2", "storm 30", "30 min storm", "A", "2026-02-01T00:00:00Z", status="Done")
        rows = list(g.db.execute("SELECT * FROM intents"))
        out = ladder_mod._compose_joint(Config(), "what cap applies in a storm?", rows)
        self.assertIsNotNone(out)
        ans = out[0]
        self.assertNotIn("(r1)", ans)
        self.assertNotIn("(r2)", ans)
        self.assertIn("30 minutes", ans)

    def test_near_memory_match_is_composed_for_this_question(self):
        """A neighbouring signed answer plus its carve-out are composed for
        the question actually asked, not served in half."""
        self.semantic(True)
        _wire_fakes(self, json_map={"select": {"pick": "m1"}},
                    text_map={"compose": "The record does not specify the add-on case; base credits roll over.",
                              "compose_joint": "2,500 carry over: base credits roll over per the first answer, "
                                               "and GPU burst credits do not per the second."})
        g = self.graph
        t = g.create_task("prior")
        q1 = "Should unused prepaid credits expire at annual contract renewal?"
        first_source = g.add_decision(t, q1, "policy", "approved", source="human", answered_by="Tomas Lindqvist",
                       answer="No. For annual contracts, unused prepaid credits roll over.", embedding=llm_mod.embed(q1), repo="acme")
        q2 = "Do rolled-over prepaid credits apply to add-on SKUs such as GPU burst?"
        second_source = g.add_decision(t, q2, "policy", "approved", source="human", answered_by="Tomas Lindqvist",
                       answer="No. Rollover covers the base platform SKU only.", embedding=llm_mod.embed(q2), repo="acme")
        q = "A customer on an annual plan has 4,000 unused prepaid credits at renewal, 1,500 bought for GPU burst. How many carry over?"
        r = run_task(g, Config(), "credits", repo="acme", decisions=[{"question": q, "category": "policy"}])
        d = self.first(r)
        self.assertEqual(d.status, "resolved", d.evidence)
        self.assertIn("2,500", d.answer)
        linked = [link for link in g.links_for([d.id])[d.id] if link['kind'] == 'derived']
        self.assertEqual({link['id'] for link in linked}, {first_source, second_source})
        self.assertTrue(all(link.get('source_version_id') for link in linked))


class TwinClosingTests(LadderCase):
    def test_open_proposal_is_not_closed_as_a_twin_by_the_model_band(self):
        """An open ticket proposing a CHANGE is not the same decision as a
        question about the current value; closing twins never uses the
        model-confirmed band, only the deterministic bar."""
        self.semantic(True)
        _wire_fakes(self, json_map={"same": {"same": True}, "select": {"pick": "m1"}},
                    text_map={"compose": "The batch size is 500 rows per request."})
        g = self.graph
        t = g.create_task("tickets")
        proposal = g.add_decision(t, "Change the metering export batch size from 500 to 5000?", "policy", "pending",
                                  embedding=llm_mod.embed("Change the metering export batch size from 500 to 5000?"),
                                  repo="meter", path="metering/exporter.py")
        prior = g.create_task("prior")
        pq = "What batch size should the metering exporter use?"
        g.add_decision(prior, pq, "definition", "approved", source="human", answer="500 rows per request.",
                       answered_by="Priya", embedding=llm_mod.embed(pq), repo="meter")
        run_task(g, Config(), "batch", repo="meter",
                 decisions=[{"question": "What batch size is the metering exporter supposed to use?", "category": "definition"}])
        self.assertEqual(g.get_decision(proposal).status, "pending")

    def test_accountability_rung_survives_a_value_memory_hit(self):
        g = self.graph
        seed_changes(g, "platform", METERING)
        g.add_listing("platform", "codeowners", "metering/", "@priya")
        prior = g.create_task("prior")
        pq = "What batch size is the metering exporter supposed to use?"
        g.add_decision(prior, pq, "definition", "resolved", source="memory", answer="500 rows per request.",
                       answered_by="Priya Natarajan", embedding=llm_mod.embed(pq), repo="platform")
        r = run_task(g, self.cfg, "batch", repo="platform",
                     decisions=[{"question": "Whose call is it to change the export batch size in metering/exporter.py?",
                                 "category": "policy", "path": "metering/exporter.py"}])
        d = self.first(r)
        self.assertEqual(d.status, "resolved", d.evidence)
        self.assertIn("accountable owner", d.answer)


class NoBackendTests(LadderCase):
    def test_semantic_off_without_key_or_cli(self):
        os.environ.pop("BRIDGE_SEMANTIC", None)
        self.patch(llm_mod, "find_claude", lambda: None)
        self.assertFalse(Config().semantic_retrieval)
        with self.assertRaises(llm_mod.NoAPIKey):
            llm_mod.Client(Config()).complete("x", "s", "p")

    def test_semantic_on_with_key(self):
        os.environ.pop("BRIDGE_SEMANTIC", None)
        os.environ["ANTHROPIC_API_KEY"] = "test-only-not-a-real-key"
        try:
            self.assertTrue(Config().semantic_retrieval)
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_deterministic_ladder_never_fabricates(self):
        """With no backend, an unsettled question routes; nothing is composed."""
        g = self.graph
        seed_changes(g, "acme", [("pricing/minimums.py", "Tomas Lindqvist", "author", i) for i in range(6)])
        g.add_listing("acme", "codeowners", "pricing/", "@tomas")
        q = "Under usage-based pricing, do we apply per-seat minimums?"
        r = run_task(g, self.cfg, "pricing", repo="acme", decisions=[{"question": q, "category": "policy", "path": "pricing/minimums.py"}])
        d = self.first(r)
        self.assertEqual(d.status, "pending")
        self.assertEqual(d.kind, "new")
        self.assertEqual(d.owner, "Tomas Lindqvist")
        self.assertEqual(d.answer, "")


class ThinkingModelTests(unittest.TestCase):
    """A model that thinks before it answers spends max_tokens on the
    thinking too. Measured live: the conformance read's 1200 tokens went
    to thinking on two of six decisions, no text came back, and the reads
    were silently missing."""

    def setUp(self):
        llm_mod._THINKS.clear()
        self.addCleanup(llm_mod._THINKS.clear)
        self.budgets = []

    def client(self, replies, model="claude-sonnet-5"):
        patcher = patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-only-not-a-real-key"})
        patcher.start()
        self.addCleanup(patcher.stop)
        client = llm_mod.Client(Config(model_api="anthropic", model=model))

        def post(url, headers, body, timeout=120):
            self.budgets.append(body["max_tokens"])
            return replies.pop(0)
        client._post_json = post
        return client

    def test_the_answer_lost_to_thinking_is_asked_again_with_room(self):
        spent = {"stop_reason": "max_tokens", "content": [{"type": "thinking", "thinking": ""}]}
        done = {"stop_reason": "end_turn", "content": [{"type": "thinking", "thinking": ""},
                                                       {"type": "text", "text": '{"verdict": "follows"}'}]}
        client = self.client([spent, done, dict(done)])
        self.assertEqual(client.complete_json("conformance", "s", "p", max_tokens=1200), {"verdict": "follows"})
        # Every later call to that model starts with the room.
        client.complete("conformance", "s", "p", max_tokens=1200)
        self.assertEqual(self.budgets, [1200, 1200 + llm_mod.THINKING_ROOM, 1200 + llm_mod.THINKING_ROOM])

    def test_a_model_that_does_not_think_keeps_its_budget(self):
        whole = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "a whole answer."}]}
        client = self.client([whole], model="claude-haiku-4-5-20251001")
        self.assertEqual(client.complete("brief", "s", "p", max_tokens=300), "a whole answer.")
        self.assertEqual(self.budgets, [300])
        self.assertNotIn("claude-haiku-4-5-20251001", llm_mod._THINKS)

    def test_an_answer_cut_off_is_asked_again_and_never_returned_cut(self):
        """Measured live: a composed answer ended mid-sentence and was stored
        as the answer."""
        cut = {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "Jitter applies to every wa"}]}
        whole = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Jitter applies to every wait."}]}
        client = self.client([cut, whole], model="claude-haiku-4-5-20251001")
        self.assertEqual(client.complete("compose", "s", "p", max_tokens=300), "Jitter applies to every wait.")
        self.assertEqual(self.budgets, [300, 600])
        client = self.client([dict(cut), dict(cut)], model="claude-haiku-4-5-20251001")
        with self.assertRaises(llm_mod.LLMError):
            client.complete("compose", "s", "p", max_tokens=300)


class DeepModeTests(LadderCase):
    """BRIDGE_DEEP=0 keeps the fast model points (the selector confirming
    a record, the area mapping) and skips the slow rungs (query
    expansion with its wide selection, the same-decision check on
    near-twins, follow-up rewriting), so a node comes back in seconds.
    The bench runs its model conditions this way."""

    NEAR = "Should the metering exporter batch size change from 500 to 5000 rows?"
    QUESTION = "What batch size should the metering exporter use per request?"

    def test_deep_retrieval_needs_semantic_retrieval(self):
        os.environ["BRIDGE_DEEP"] = "1"
        self.assertFalse(Config().semantic_retrieval)
        self.assertFalse(Config().deep_retrieval)
        self.semantic(True)
        self.assertTrue(Config().deep_retrieval)
        os.environ.pop("BRIDGE_DEEP", None)
        self.assertTrue(Config().deep_retrieval)
        os.environ["BRIDGE_DEEP"] = "0"
        self.assertTrue(Config().semantic_retrieval)
        self.assertFalse(Config().deep_retrieval)
        os.environ.pop("BRIDGE_SEMANTIC", None)
        os.environ["BRIDGE_DEEP"] = "1"
        self.patch(llm_mod, "find_claude", lambda: None)
        self.assertFalse(Config().semantic_retrieval)
        self.assertFalse(Config().deep_retrieval)

    def purposes(self, deep):
        """The model purposes one unsettled question costs: a record the
        selector rejects, a pending near-twin in the model-check band,
        and a question that names no path in the tree."""
        g = Store(Path(self.temp.name) / f"deep{int(deep)}.db").graph
        for p in ("usage/export.py", "usage/settings.py", "billing/invoice.py", "README.md"):
            g.upsert_artifact("meter", p)
        g.upsert_intent("meter", "ticket", "T-500", "Metering exporter batch size set to 500",
                        "The metering exporter sends 500 rows per request.", "Yuki", "2026-05-01T00:00:00Z",
                        status="Done")
        t = g.create_task("earlier")
        g.add_decision(t, self.NEAR, "policy", "pending", embedding=llm_mod.embed(self.NEAR), repo="meter")
        seen: list[str] = []

        def fake_json(self_, purpose, system, prompt, **kw):
            seen.append(purpose)
            if purpose == "area":
                return {"paths": ["usage/"], "confidence": "high", "why": "the exporter lives there"}
            if purpose == "expand":
                return {"phrases": ["export batch rows"]}
            if purpose in ("select", "precedent"):
                return {"pick": "none"}
            return {"same": False}

        self.patch(llm_mod.Client, "complete_json", fake_json)
        self.patch(llm_mod.Client, "complete", lambda self_, purpose, system, prompt, **kw:
                   "RECORDS DO NOT ANSWER" if purpose == "compose_joint" else "RECORD DOES NOT ANSWER")
        with patch.dict(os.environ, {"BRIDGE_DEEP": "1" if deep else "0"}):
            r = run_task(g, Config(), "batch", repo="meter",
                         decisions=[{"question": self.QUESTION, "category": "definition"}])
        self.assertEqual(g.decisions_for_task(r.task_id)[0].status, "pending")
        return seen

    def test_deep_off_keeps_the_selector_and_the_area_call_and_skips_the_rest(self):
        self.semantic(True)
        deep = self.purposes(True)
        self.assertIn("expand", deep)
        self.assertIn("same_decision", deep)
        fast = self.purposes(False)
        self.assertIn("select", fast)
        self.assertIn("area", fast)
        self.assertNotIn("expand", fast)
        self.assertNotIn("same_decision", fast)
        self.assertNotIn("followup", fast)
        self.assertEqual(fast.count("select"), 1)
        self.assertLess(len(fast), len(deep))

    def test_deep_off_never_rewrites_a_follow_up(self):
        self.semantic(True)
        g = self.graph
        t = g.create_task("prior")
        pq = "What is the depth ceiling for splice work?"
        g.add_decision(t, pq, "policy", "resolved", answer="6200 m per FLEET-303", repo="sub")
        seen: list[str] = []

        def fake_json(self_, purpose, system, prompt, **kw):
            seen.append(purpose)
            if purpose == "followup":
                return {"needs_context": True, "question": "Does the 6200 m ceiling apply to splice work?"}
            return {"pick": "none"}

        self.patch(llm_mod.Client, "complete_json", fake_json)
        self.patch(llm_mod.Client, "complete", lambda self_, purpose, system, prompt, **kw: "RECORD DOES NOT ANSWER")
        q = "Does that same ceiling apply to night dives?"
        with patch.dict(os.environ, {"BRIDGE_DEEP": "0"}):
            run_task(g, Config(), "check", repo="sub", decisions=[{"question": q, "category": "ops"}])
        self.assertNotIn("followup", seen)
        self.assertEqual(g.count_events("followup_context"), 0)
        with patch.dict(os.environ, {"BRIDGE_DEEP": "1"}):
            run_task(g, Config(), "check again", repo="sub", decisions=[{"question": q, "category": "ops"}])
        self.assertIn("followup", seen)
        self.assertEqual(g.count_events("followup_context"), 1)


class RetrospectiveMemoryTests(LadderCase):
    def _answered(self, run, question, answer, rationale, when, path="payments/refunds.py"):
        d = self.store.request({"run_id": run["id"], "question": question, "context": "Recorded in the payments inbox.", "path": path,
                                "owner_id": self.owner["id"]})
        self.store.answer(d["id"], {"answer": answer, "rationale": rationale})
        ts = f"{when}T12:00:00+00:00"
        with self.store.connect() as db:
            db.execute("UPDATE decisions SET created_at=?, updated_at=? WHERE id=?", (ts, ts, d["id"]))
        return d["id"]

    def setUp(self):
        super().setUp()
        self.owner = self.store.add_owner({"name": "Ruben Castillo", "team": "Payments", "patterns": "payments/*"})
        run = self.store.add_run({"title": "history", "agent": "t", "repo": "acme/platform"})
        self.older = self._answered(run, "Should automatic refunds for duplicate charges be paused?",
                                    "Yes. Pause automatic refunds; route every duplicate-charge refund to manual review.",
                                    "The March double-refund incident refunded 212 customers twice.", "2026-04-18")
        self.newer = self._answered(run, "Can automatic duplicate-charge refunds resume?",
                                    "Yes, partially. Resume automatic refunds for duplicates under $500.",
                                    "PR-120 fixed the root cause.", "2026-08-05")

    def test_who_decided_matches_the_decision_asked_about(self):
        """The asking machinery does not dilute overlap, and newest-wins does
        not swap a newer answer in for a question about the older decision."""
        q = "Who decided to pause automatic refunds for duplicate charges, and what was the reason?"
        r = run_task(self.graph, self.cfg, "history", repo="acme/platform",
                     decisions=[{"question": q, "path": "payments/refunds.py"}])
        d = self.first(r)
        self.assertIn(d.status, ("resolved", "partial"))
        self.assertIn(self.older, d.evidence)
        self.assertEqual(d.answered_by, "Ruben Castillo")
        self.assertIn("Pause automatic refunds", d.answer)

    def test_present_tense_question_still_prefers_the_newest_answer(self):
        run = self.store.add_run({"title": "later", "agent": "t", "repo": "acme/platform"})
        latest = self._answered(run, "Should automatic refunds for duplicate charges stay paused?",
                                "No. Resume automatic refunds for duplicates under $500.",
                                "The root cause is fixed.", "2026-09-01")
        q = "Should automatic refunds for duplicate charges be paused?"
        r = run_task(self.graph, self.cfg, "now", repo="acme/platform",
                     decisions=[{"question": q, "path": "payments/refunds.py"}])
        d = self.first(r)
        self.assertIn(d.status, ("resolved", "partial"))
        self.assertIn(latest, d.evidence)
        self.assertNotIn(self.older, d.evidence)


class ContrastGateTests(LadderCase):
    def test_answer_for_the_other_side_of_a_contrast_is_not_reused(self):
        """A rule scoped to annual contracts is not applied to a monthly one,
        whatever the selector and composer say."""
        self.semantic(True)
        _wire_fakes(self, json_map={"select": {"pick": "m1"}, "same": {"same": True}},
                    text_map={"compose": "Those base platform credits still roll over.",
                              "compose_joint": "Those base platform credits still roll over (decision x)."})
        g = self.graph
        t = g.create_task("prior")
        pq = "Do unused prepaid credits roll over at renewal on an annual contract?"
        g.add_decision(t, pq, "policy", "approved", source="human",
                       answer="Yes. For annual contracts, unused base platform credits roll over for the remaining term.",
                       answered_by="Tomas Lindqvist", embedding=llm_mod.embed(pq), repo="platform")
        q = ("The customer's contract is month-to-month rather than annual. Do the unused prepaid base platform "
             "credits still roll over at renewal on their contract?")
        r = run_task(g, Config(), "renewal", repo="platform", decisions=[{"question": q, "category": "policy"}])
        d = self.first(r)
        self.assertEqual(d.status, "pending", d.evidence)
        self.assertEqual(d.answer, "")
        self.assertIn("never mentions month", d.evidence)

    def test_contrast_met_by_the_record_still_composes(self):
        self.semantic(True)
        _wire_fakes(self, json_map={"select": {"pick": "m1"}, "same": {"same": True}},
                    text_map={"compose": "Monthly contracts get no rollover; credits expire at each renewal."})
        g = self.graph
        t = g.create_task("prior")
        pq = "Do unused prepaid credits roll over at renewal on an annual or monthly contract?"
        g.add_decision(t, pq, "policy", "approved", source="human",
                       answer="Annual contracts roll credits over; monthly contracts do not.",
                       answered_by="Tomas Lindqvist", embedding=llm_mod.embed(pq), repo="platform")
        q = ("The customer's contract is month-to-month rather than annual. Do the unused prepaid credits "
             "still roll over at renewal on their contract?")
        r = run_task(g, Config(), "renewal", repo="platform", decisions=[{"question": q, "category": "policy"}])
        d = self.first(r)
        self.assertIn(d.status, ("resolved", "partial"), d.evidence)
        self.assertIn("Monthly contracts get no rollover", d.answer)


if __name__ == "__main__":
    unittest.main()
