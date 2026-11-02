"""What people say in meetings settles calls. Measured on the Harborline
dry run (run 6): the Zoom transcript ("keep Northwind off the public rate
until Legal clears the MFN clause") was learned as notes on Dana's page,
and the agent's "Is Northwind charged for Pilot, and at what rate?" was
routed to a person all the same."""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import OfflineCase  # noqa: E402

from bridge import attention, canvas, meeting_precedent, meeting_records as mr, people_context  # noqa: E402
from bridge.authz import Actor  # noqa: E402
from bridge.config import Config  # noqa: E402
from bridge.llm import LLMError  # noqa: E402

REPO = "qemulike"
QUOTE = "Agreed. Northwind stays on their contract terms, no public rate for them until Noah clears it."
NOTE = "Northwind must stay on contract terms with no public rate until Legal clears the MFN clause."
TRANSCRIPT = "\n".join([
    "Dana Price: Okay, aplic pricing. Elena, you saw the October number?",
    "Elena Ross: I did. If we publish a per-use rate, Northwind's most-favoured-nation clause means that rate is "
    "their rate too. So keep Northwind off the public rate until Legal clears the MFN clause.",
    "Dana Price: " + QUOTE,
    "Elena Ross: And Globex stays on the old plan until their renewal.",
    "Dana Price: Fine, Globex keeps the old plan until renewal.",
])
QUESTION = "Is Northwind charged the public rate for the aplic?"
CONTEXT = "Pricing: Northwind's contract has an MFN clause (app/contracts). Code: hw/riscv/virt.c reads the public rate."


class ModelOn(Config):
    @property
    def semantic_retrieval(self):
        return not self.deterministic

    @property
    def deep_retrieval(self):
        return not self.deterministic

    def has_backend(self):
        return not self.deterministic


class MeetingCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.warm_store(REPO, "meeting-precedent.db")
        self.graph = self.store.graph
        mr.migrate(self.graph.db)
        self.cfg = Config()
        self.dana = self.store.add_person({"name": "Dana Price"})["id"]
        self.elena = self.store.add_person({"name": "Elena Ross"})["id"]
        self.graph.add_authority("category", "pricing", "decides", person_id=self.dana, repo=REPO)
        meeting = mr.create(self.store, {"repo": REPO, "provider": "import", "namespace": "import", "platform": "zoom",
                                         "native_meeting_id": "pricing-sync", "title": "Pricing sync (Dana, Elena), Oct 29"})
        self.mid = meeting["id"]
        mr.import_transcript(self.store, self.mid, REPO, {"body": TRANSCRIPT, "source_version": "import:" + self.mid,
                                                          "workspace_shared": True, "by": "Dana Price", "complete": True})
        self.task = canvas.start_task(self.store, self.cfg, {
            "title": "Charge for the aplic by use", "repo": REPO, "agent": "claude",
            "requester": "Tamsin Reed <tamsin@synthco.example>", "paths": "hw/riscv/virt.c"})["task_id"]

    def learned(self, text=NOTE, quote=QUOTE, person=None):
        """What meeting_learn keeps: a note on the person's page, quoting
        the transcript, pointing at the meeting."""
        return people_context.remember(self.store, person or self.dana, text, "from a meeting", "unwritten", quote,
                                       "Pricing sync (Dana, Elena), Oct 29", artifact="meeting:" + self.mid)

    def node(self, cfg=None, question=QUESTION, **extra):
        return canvas.add_node(self.store, cfg or self.cfg, {"task_id": self.task, "question": question, "context": CONTEXT,
                                                             "paths": "hw/riscv/virt.c", "category": "policy",
                                                             "facts": "customer=northwind", **extra})

    def model(self, reading):
        """A model whose meeting reading is `reading` (a dict, or a function
        of the payload); every other purpose is offline."""
        import json

        def read(self_, purpose, system, prompt, **kw):
            if purpose != "meeting_precedent":
                raise LLMError("offline")
            return reading(json.loads(prompt)) if callable(reading) else reading
        patcher = patch("bridge.llm.Client.complete_json", autospec=True, side_effect=read)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        quiet = patch("bridge.canvas._start_background", lambda *a, **k: None)
        quiet.start()
        self.addCleanup(quiet.stop)
        return mock


SETTLES = {"settles": True, "bears_on": True, "why": "Dana decided Northwind is kept off the public rate pending Legal.",
           "answer": "No: Northwind stays on its contract terms, with no public rate, until Noah clears the MFN clause."}


class SettleTests(MeetingCase):
    def test_the_speakers_words_settle_the_call_the_way_an_earlier_decision_does(self):
        self.learned()
        read = self.model(SETTLES)
        n = self.node(ModelOn(model_api="claude-cli"))
        self.assertEqual((n["status"], n["kind"], n["signoff"], n["answered_by"]), ("resolved", "evidence", "rule", "Dana Price"))
        self.assertEqual(n["answer"], SETTLES["answer"])
        self.assertEqual(n["rationale"], QUOTE)
        self.assertTrue(n["authorized"])
        self.assertFalse(n["blocking"])
        self.assertNotIn("Still reading", n["next"])
        row = self.store.get_decision(n["node_id"])
        self.assertEqual((row["source"], row["answered_via"], row["source_revision"][:10]), ("meeting", "meeting", "2026-10-29"))
        self.assertTrue(row["fit_reason"].startswith("From the Oct 29 Pricing sync (Dana, Elena): Dana Price, who decides pricing, said “"), row["fit_reason"])
        self.assertIn("nobody was asked", row["fit_reason"])
        # The transcript chunk is the node's source.
        self.assertEqual([(s["ref"].split(":")[0], s["role"]) for s in n["sources"]], [("meeting", "support")])
        self.assertEqual(read.call_count, 1)
        payload = read.call_args.args[3]
        self.assertIn(QUOTE, payload)
        self.assertIn(QUESTION, payload)
        # Nobody was asked: no judgment request, no sign-off wanted.
        self.assertEqual(self.graph.count_events("judgment_requested", decision_id=n["node_id"]), 0)
        self.assertEqual(self.graph.count_events("meeting_precedent", decision_id=n["node_id"]), 1)
        tree = canvas.get_tree(self.store, self.task)
        self.assertEqual(tree["counts"]["resolved"], 1)
        self.assertEqual(tree["counts"]["signoff_required"], 0)
        self.assertEqual(tree["counts"]["blocking"], 0)

    def test_for_you_shows_dana_what_raven_settled_from_her_words(self):
        self.learned()
        self.model(SETTLES)
        n = self.node(ModelOn(model_api="claude-cli"))
        view = attention.overview(self.store, Actor(id=self.dana, name="Dana Price"))
        self.assertEqual(view["needs_you"], [])
        self.assertEqual([h["id"] for h in view["handled"]], [n["node_id"]])
        call = view["handled"][0]["call"]
        self.assertEqual((call["answered_via"], call["by"], call["answer"], call["when"][:10]),
                         ("meeting", "Dana Price", QUOTE, "2026-10-29"))
        self.assertIn("Oct 29 Pricing sync", call["task"])
        elena = attention.overview(self.store, Actor(id=self.elena, name="Elena Ross"))
        self.assertEqual(elena["handled"], [])

    def test_the_speaker_not_the_page_the_note_sits_on_must_hold_the_authority(self):
        """The note was filed under Elena, who does not decide pricing;
        Dana spoke the words, and Dana does."""
        self.learned(person=self.elena)
        self.model(SETTLES)
        n = self.node(ModelOn(model_api="claude-cli"))
        self.assertEqual((n["status"], n["answered_by"]), ("resolved", "Dana Price"))

    def test_a_path_the_speaker_decides_counts_as_their_area_too(self):
        self.graph.db.execute("DELETE FROM authority WHERE person_id=?", (self.dana,))
        self.graph.add_authority("path", "hw/riscv/*", "decides", person_id=self.dana, repo=REPO)
        self.learned()
        self.model(SETTLES)
        n = self.node(ModelOn(model_api="claude-cli"))
        self.assertEqual(n["status"], "resolved")
        self.assertIn("who decides hw/riscv/*", self.store.get_decision(n["node_id"])["fit_reason"])


class SafeguardTests(MeetingCase):
    def test_without_a_model_the_words_reach_the_speaker_as_ravens_guess(self):
        self.learned()
        n = self.node()
        self.assertEqual((n["status"], n["kind"], n["owner"], n["signoff"]), ("predicted", "prediction", "Dana Price", "required"))
        self.assertEqual(n["prediction"], NOTE)
        self.assertIn("Raven would apply what Dana Price said in the Oct 29 Pricing sync (Dana, Elena), but isn't sure", n["evidence"])
        self.assertIn(QUOTE, n["evidence"])
        self.assertFalse(n["authorized"])
        self.assertEqual(self.store.get_decision(n["node_id"])["answered_via"], "meeting")
        view = attention.overview(self.store, Actor(id=self.dana, name="Dana Price"))
        self.assertEqual([i["id"] for i in view["needs_you"]], [n["node_id"]])
        self.assertEqual(view["needs_you"][0]["guess"], NOTE)

    def test_an_unsure_model_asks_the_speaker_instead(self):
        self.learned()
        self.model({"settles": False, "bears_on": True, "answer": "",
                    "why": "Dana spoke of the public rate, not of whether Northwind is charged at all."})
        n = self.node(ModelOn(model_api="claude-cli"))
        self.assertEqual((n["status"], n["owner"], n["prediction"]), ("predicted", "Dana Price", NOTE))
        self.assertIn("Raven would apply what Dana Price said", n["evidence"])
        self.assertIn("The ladder left it open", n["evidence"])

    def test_words_the_model_reads_as_not_about_this_question_leave_the_ladders_route(self):
        self.learned()
        self.model({"settles": False, "bears_on": False, "answer": "", "why": "Another matter."})
        n = self.node(ModelOn(model_api="claude-cli"))
        self.assertEqual(n["status"], "pending")
        self.assertNotEqual(self.store.get_decision(n["node_id"])["source"], "meeting")

    def test_an_answer_with_a_figure_the_speaker_did_not_say_is_not_applied(self):
        self.learned()
        self.model({**SETTLES, "answer": "Northwind pays 12 cents per use until Legal clears the MFN clause."})
        n = self.node(ModelOn(model_api="claude-cli"))
        self.assertEqual(n["status"], "predicted")

    def test_a_quote_no_longer_verbatim_in_the_transcript_is_not_applied(self):
        self.learned(quote="Northwind stays off the public rate for good.")
        self.model(SETTLES)
        n = self.node(ModelOn(model_api="claude-cli"))
        self.assertNotEqual(n["status"], "resolved")
        self.assertNotEqual(self.store.get_decision(n["node_id"])["source"], "meeting")

    def test_a_speaker_without_authority_over_the_area_is_not_applied(self):
        self.graph.db.execute("DELETE FROM authority WHERE person_id=?", (self.dana,))
        self.learned()
        self.model(SETTLES)
        n = self.node(ModelOn(model_api="claude-cli"))
        self.assertNotEqual(self.store.get_decision(n["node_id"])["source"], "meeting")

    def test_words_about_another_customer_are_not_applied(self):
        self.learned(text="Globex must stay on the old plan until its renewal.",
                     quote="Fine, Globex keeps the old plan until renewal.")
        self.model(SETTLES)
        n = self.node(ModelOn(model_api="claude-cli"), question="Is Northwind charged the public rate for the aplic?")
        self.assertNotEqual(self.store.get_decision(n["node_id"])["source"], "meeting")

    def test_a_question_the_meeting_did_not_touch_runs_the_ladder_as_before(self):
        self.learned()
        read = self.model(SETTLES)
        n = self.node(ModelOn(model_api="claude-cli"), question="Should the aplic be the default interrupt controller for the virt board?")
        self.assertNotEqual(self.store.get_decision(n["node_id"])["source"], "meeting")
        self.assertEqual(read.call_count, 0)


class WhenTests(OfflineCase):
    def test_the_meeting_date_comes_from_its_title_else_the_record(self):
        self.assertEqual(meeting_precedent.meeting_when({"title": "Pilot pricing sync, Oct 29", "at": "2026-11-02T10:00:00+00:00"}),
                         ("Oct 29", "2026-10-29"))
        self.assertEqual(meeting_precedent.meeting_when({"title": "Bloom check-in 2026-10-29", "at": ""}), ("Oct 29", "2026-10-29"))
        self.assertEqual(meeting_precedent.meeting_when({"title": "Planning", "at": "2026-11-02T10:00:00+00:00"}), ("Nov 2", "2026-11-02"))
        self.assertEqual(meeting_precedent._short_title("Pilot pricing sync (Dana, Elena), Oct 29"), "Pilot pricing sync (Dana, Elena)")
