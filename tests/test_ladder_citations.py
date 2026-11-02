"""Three things seen on the Harborline dry run (run 10) that made a settled
call cite the wrong record, show Raven's own commentary as a person's words,
or describe a preference as a retirement. All offline."""

import json
import unittest

from fixtures import OfflineCase

from bridge.config import Config
from bridge.graph import split_gaps

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
