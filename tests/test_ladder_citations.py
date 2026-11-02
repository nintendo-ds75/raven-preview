"""Three things seen on the Harborline dry run (run 10) that made a settled
call cite the wrong record, show Raven's own commentary as a person's words,
or describe a preference as a retirement. All offline."""

import json
import unittest

from fixtures import OfflineCase

from bridge.config import Config
from bridge.graph import split_gaps
from bridge.ladder import run_task

REPO = "harborline/harborline-app"


class CitationCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store("citations.db")
        self.graph = self.store.graph
        self.cfg = Config()
        self.raj = self.store.add_owner({"name": "Raj Patel", "team": "Usage", "patterns": "services/usage/*"})
        self.dana = self.store.add_owner({"name": "Dana Whitfield", "team": "Pricing", "patterns": "app/billing/*"})
        self.graph.set_setting("auto_rules", "1")

    def answered(self, owner, question, answer, when, path, rationale="Recorded in the inbox.", everywhere=True):
        run = self.store.add_run({"title": f"seed {when}", "agent": "t", "repo": REPO})
        d = self.store.request({"run_id": run["id"], "question": question, "context": f"Paths: {path}",
                                "path": path, "owner_id": owner["id"]})
        self.store.answer(d["id"], {"answer": answer, "rationale": rationale, "signed_by": owner["name"],
                                    "source": f"inbox: {owner['name']}"})
        if everywhere:
            self.store.make_rule(d["id"], {"by": owner["name"], "scope": "any",
                                           "expected_updated_at": self.store.get_decision(d["id"])["updated_at"]})
        ts = f"{when}T12:00:00+00:00"
        with self.store.connect() as db:
            db.execute("UPDATE decisions SET created_at=?, updated_at=? WHERE id=?", (ts, ts, d["id"]))
        self.store.update_run(run["id"], {"status": "completed"})
        return d["id"]

    def first(self, result):
        return self.graph.decisions_for_task(result.task_id)[0]

    def events(self, kind, decision_id):
        with self.store.connect() as db:
            return [json.loads(r["detail"]) for r in db.execute(
                "SELECT detail FROM events WHERE kind=? AND decision_id=? ORDER BY id", (kind, decision_id))]


class DirectRungPrefersTheQuestionAskedTests(CitationCase):
    """Run 10: "What counts as one Pilot use?" settled from Raj's Sep 17 call
    about retries ("Does a retried Pilot chat turn count as a second use, and
    does a failed generation count?") at "direct, similarity 1.00", while his
    Dec 11 call "What counts as one billable use of Pilot: a request, a
    completed response, or tokens?" is the question itself. Every meaningful
    term of the short question (count, pilot, use) sits inside both longer
    questions, so term containment scored both 1.00 and the newer one won on
    age. The question asked in its own words wins over a superset match."""

    UNIT = "What counts as one billable use of Pilot: a request, a completed response, or tokens?"
    RETRY = "Does a retried Pilot chat turn count as a second use, and does a failed generation count?"

    def setUp(self):
        super().setUp()
        self.unit = self.answered(self.raj, self.UNIT,
                                  "One completed Pilot response is one use (BILL-201, PR #7). Tokens, requests, "
                                  "streamed chunks and prompts that never completed are not units of use.",
                                  "2025-12-11", "services/usage/", rationale="Settled with Billing on BILL-201.")
        self.retry = self.answered(self.raj, self.RETRY,
                                   "A client retry of the same turn, with the same request id, counts once; a "
                                   "generation that never completed counts zero (BILL-266, PR #47).",
                                   "2026-09-17", "services/usage/", rationale="BILL-266, PR #47.")

    def ask(self, question):
        r = run_task(self.graph, self.cfg, "Start charging for Pilot", repo=REPO,
                     decisions=[{"question": question, "category": "definition", "path": "services/usage/events.py"}])
        return self.first(r)

    def test_the_question_in_its_own_words_beats_a_newer_superset_match(self):
        d = self.ask("What counts as one Pilot use?")
        self.assertIn(d.status, ("resolved", "partial"), d.evidence)
        self.assertEqual(d.source_id, self.unit, d.evidence)
        self.assertIn("One completed Pilot response is one use", d.answer)
        self.assertNotIn(self.retry, d.evidence)
        self.assertIn("direct", d.evidence)

    def test_a_superset_match_that_does_not_repeat_the_question_is_not_direct(self):
        """With only the retry call on record, the short question is a near
        match to it at best: it asks about more than this question does and
        does not repeat its words. Never "direct, similarity 1.00"."""
        with self.store.connect() as db:
            db.execute("UPDATE decisions SET status='withdrawn' WHERE id=?", (self.unit,))
        d = self.ask("What counts as one Pilot use?")
        self.assertNotIn("direct, decision", d.evidence)
        self.assertNotIn("similarity 1.00", d.evidence)

    def test_the_retry_question_still_finds_the_retry_call(self):
        d = self.ask("How are Pilot retries and failed attempts counted for billing?")
        self.assertIn(d.status, ("resolved", "partial"), d.evidence)
        self.assertEqual(d.source_id, self.retry, d.evidence)


class PreferenceIsNotRetirementTests(CitationCase):
    """Run 10: three conflict_resolved events said "kept 8531bfc34012, retired
    5bdec5f18755, rule newest answer wins" about Dana's Sep 24 call, which is
    still approved and not superseded; it was only passed over for one
    question. The event says what happened, and "retired" is reserved for a
    decision that really was superseded."""

    def setUp(self):
        super().setUp()
        self.older = self.answered(self.dana, "Which plans are charged per use for Pilot: Enterprise only, or Team "
                                   "and Starter too?", "Enterprise only. Team and Starter are not metered.",
                                   "2026-09-24", "app/billing/plans.py")
        self.newer = self.answered(self.dana, "Which plans are charged per use for Pilot?",
                                   "Only the Enterprise plan is charged per use (metered) for Pilot; it is billed in "
                                   "arrears on the monthly invoice, same as seats.", "2026-10-29", "app/billing/plans.py")

    def test_the_event_says_the_newer_answer_was_preferred_and_both_stand(self):
        r = run_task(self.graph, self.cfg, "Start charging for Pilot", repo=REPO,
                     decisions=[{"question": "Which plans are charged per use for Pilot?", "category": "policy",
                                 "path": "app/billing/plans.py"}])
        d = self.first(r)
        self.assertEqual(d.source_id, self.newer, d.evidence)
        events = self.events("conflict_resolved", d.id)
        self.assertTrue(events, "the preference is logged")
        for detail in events:
            self.assertEqual(detail["preferred"], self.newer)
            self.assertEqual(detail["over"], [self.older])
            self.assertNotIn("retired", detail)
            self.assertNotIn("kept", detail)
            self.assertIn("both stand", detail["summary"])
            self.assertNotIn("retired", detail["summary"])
            self.assertNotIn("superseded", detail["summary"])
        for did in (self.older, self.newer):
            row = self.store.get_decision(did)
            self.assertEqual(row["status"], "approved")
            self.assertEqual(row["superseded_by"], "")


class GapsAreRavensNoteTests(CitationCase):
    """Run 10: the Northwind composition ended with "The records do not
    establish: Has Legal reviewed...", and the paging partial quoted "The
    earlier answer does not establish: Why is the cursor-based paging..." as
    Raj's words. What a source leaves open is Raven's note, kept out of the
    answer field and stored beside it."""

    def test_split_gaps_takes_the_composer_sentence_out_of_the_answer(self):
        text = ("Pilot usage reads are paged by cursor, never by offset (ADR-0002). The earlier answer does not "
                "establish: Why is the cursor-based paging method specifically necessary; what is the causal "
                "mechanism that requires readers to walk every page.")
        answer, gaps = split_gaps(text)
        self.assertEqual(answer, "Pilot usage reads are paged by cursor, never by offset (ADR-0002).")
        self.assertEqual(gaps, ["Why is the cursor-based paging method specifically necessary",
                                "what is the causal mechanism that requires readers to walk every page"])
        answer, gaps = split_gaps("Northwind is not charged today. The records do not establish: Has Legal reviewed "
                                  "the Pilot rate for Northwind; Will the rate be quoted, and when.")
        self.assertEqual(answer, "Northwind is not charged today.")
        self.assertEqual(gaps, ["Has Legal reviewed the Pilot rate for Northwind", "Will the rate be quoted, and when"])
        # A trailing note the record rung adds after the sentence survives.
        answer, gaps = split_gaps("Debounce is 8 seconds. The record does not establish: whether it applies to "
                                  "arrivals. (note: ticket OPS-9, filed after this change, reports the same subject.)")
        self.assertEqual(answer, "Debounce is 8 seconds. (note: ticket OPS-9, filed after this change, reports the "
                                 "same subject.)")
        self.assertEqual(gaps, ["whether it applies to arrivals"])
        self.assertEqual(split_gaps("Plain answer."), ("Plain answer.", []))
        self.assertEqual(split_gaps(""), ("", []))

    def test_published_evidence_stores_the_gaps_beside_a_clean_answer(self):
        run = self.store.add_run({"title": "t", "agent": "t", "repo": REPO})
        did = self.graph.add_decision(run["id"], "How are Pilot usage reads paged?", "data-source", "pending",
                                      repo=REPO, path="services/usage/reads.py")
        self.graph.publish_evidence(did, [], status="partial", source="record",
                                    answer="Paged by cursor. The record does not establish: why every page is walked.",
                                    kind="evidence")
        row = self.store.get_decision(did)
        self.assertEqual(row["answer"], "Paged by cursor.")
        self.assertEqual(json.loads(row["gaps"]), ["why every page is walked"])
        d = self.graph.decisions_for_task(run["id"])[0]
        self.assertEqual(d.gaps, ["why every page is walked"])
        self.assertEqual(d.answer, "Paged by cursor.")
        # A whole answer leaves the field empty again.
        self.graph.publish_evidence(did, [], status="resolved", source="record", answer="Paged by cursor.",
                                    kind="evidence")
        self.assertEqual(self.store.get_decision(did)["gaps"], "")


if __name__ == "__main__":
    unittest.main()
