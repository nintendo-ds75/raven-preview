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

from bridge import attention, canvas, meeting_precedent, meeting_records as mr, people_context, timeline  # noqa: E402
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
        # Filed as pricing: Dana decides pricing, and the words stand only
        # when Raven's own route would ask the speaker (DeciderTests).
        return canvas.add_node(self.store, cfg or self.cfg, {"task_id": self.task, "question": question, "context": CONTEXT,
                                                             "paths": "hw/riscv/virt.c", "category": "pricing",
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

    def test_the_words_shown_are_the_ones_that_decide_it(self):
        # Dry run 9: the settle quoted the note's turn ("I still need to land
        # the rate") while the answer came from another of Dana's turns. The
        # model copies the deciding words; they stand when they are verbatim
        # from a turn of the speaker's, and never when another person said
        # them or the model made them up.
        self.learned()
        part = "Northwind stays on their contract terms, no public rate for them until Noah clears it."
        verdict = dict(SETTLES)
        self.model(lambda payload: verdict)
        for picked, shown in [(part, part),
                              ("keep Northwind off the public rate until Legal clears the MFN clause", QUOTE),  # Elena's words
                              ("Northwind is never charged the public rate.", QUOTE),  # not in the transcript
                              ("", QUOTE)]:
            with self.subTest(picked=picked):
                verdict["quote"] = picked
                n = self.node(ModelOn(model_api="claude-cli"), question=QUESTION + f" ({len(picked)})")
                self.assertEqual(n["rationale"], shown)
                self.assertIn(f"said “{shown}”", self.store.get_decision(n["node_id"])["fit_reason"])

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
        # The prediction stands on the meeting, not on whatever earlier call
        # the ladder matched under its floor.
        row = self.store.get_decision(n["node_id"])
        self.assertEqual((row["source"], row["answered_via"], row["source_id"] or "", row["fit_reason"]),
                         ("meeting", "meeting", "", ""))

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


PW = "parcelwise/parcelwise-app"
PW_QUESTION = "What do we charge per assistant answer past the allowance?"
PW_CONTEXT = "No rate on record. Code will hold it as Decimal per plan in PLANS (extra_assistant_price), left unset until answered."
MARCUS_QUOTE = "Nothing charges when the merchant asks the question. It goes through the nightly job like everything else."
MARCUS_NOTE = "Marcus: nothing is charged per assistant answer at question time; assistant charges go through the nightly job."
PRIYA_QUOTE = "Past the allowance it's two cents an assistant answer, the same on every plan."
PRIYA_NOTE = "Priya: past the allowance each assistant answer is charged two cents, the same on every plan."
PW_TRANSCRIPT = "\n".join([
    "Priya Shah: Assistant pricing, then. The allowance first: how many answers a month before we charge?",
    "Marcus Lee: I'd rather charge from the first of the month than argue about it in February.",
    "Priya Shah: " + PRIYA_QUOTE,
    "Marcus Lee: " + MARCUS_QUOTE,
])


class DeciderTests(OfflineCase):
    """Whose words stand. Measured on the Parcelwise dry run (Nov 2): "What
    do we charge per assistant answer past the allowance?" (pricing,
    app/billing/plans.py) became Raven's guess routed to Marcus from his
    "nothing charges when the merchant asks" line, with owner evidence
    "spoke for app/billing/*". Marcus decides billing and app/billing/*;
    Priya decides pricing and app/billing/plans.py, the more specific path,
    and the ladder's own route had named her. The speaker's own rows were
    read instead of who Raven would ask."""

    def setUp(self):
        super().setUp()
        self.store = self.fresh_store("parcelwise-meeting.db")
        self.graph = g = self.store.graph
        self.addCleanup(g.close)
        mr.migrate(g.db)
        self.cfg = Config()
        with g.transaction():
            self.marcus = g.add_person("Marcus Lee", email="marcus@parcelwise.example")
            self.priya = g.add_person("Priya Shah", email="priya@parcelwise.example")
            for who, kind, scope in (("Marcus Lee", "category", "billing"), ("Marcus Lee", "path", "app/billing/*"),
                                     ("Priya Shah", "category", "pricing"), ("Priya Shah", "path", "app/billing/plans.py")):
                g.add_authority(kind, scope, "decides", person_id=self.marcus if who == "Marcus Lee" else self.priya,
                                repo=PW, source="config")
            for path in ("app/billing/plans.py", "app/billing/nightly.py", "app/assistant/answers.py"):
                g.upsert_artifact(PW, path)
        meeting = mr.create(self.store, {"repo": PW, "provider": "import", "namespace": "import", "platform": "zoom",
                                         "native_meeting_id": "assistant-pricing", "title": "Assistant pricing sync (founders), Oct 29"})
        self.mid = meeting["id"]
        mr.import_transcript(self.store, self.mid, PW, {"body": PW_TRANSCRIPT, "source_version": "import:" + self.mid,
                                                        "workspace_shared": True, "by": "Priya Shah", "complete": True})
        self.task = canvas.start_task(self.store, self.cfg, {
            "title": "Charge for assistant answers past the allowance", "repo": PW, "agent": "claude",
            "requester": "Sam Rivera <sam@parcelwise.example>", "paths": "app/billing/plans.py"})["task_id"]

    def learned(self, person, text, quote):
        return people_context.remember(self.store, person, text, "from a meeting", "unwritten", quote,
                                       "Assistant pricing sync (founders), Oct 29", artifact="meeting:" + self.mid)

    def node(self, cfg=None, question=PW_QUESTION, category="pricing", **extra):
        return canvas.add_node(self.store, cfg or self.cfg, {"task_id": self.task, "question": question, "context": PW_CONTEXT,
                                                             "paths": "app/billing/plans.py", "category": category, **extra})

    def model(self, reading):
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

    def test_a_speaker_who_decides_a_broader_path_does_not_take_the_call_from_the_category_decider(self):
        """Marcus decides app/billing/* and billing; Priya decides pricing
        and app/billing/plans.py. His words go to Priya as context."""
        self.learned(self.marcus, MARCUS_NOTE, MARCUS_QUOTE)
        n = self.node()
        self.assertEqual((n["status"], n["kind"], n["owner"], n["prediction"] or ""), ("pending", "new", "Priya Shah", ""))
        row = self.store.get_decision(n["node_id"])
        self.assertNotEqual((row["source"], row["answered_via"]), ("meeting", "meeting"))
        self.assertTrue(row["owner_evidence"].startswith("verified: Priya Shah decides for pricing decisions"), row["owner_evidence"])
        self.assertNotIn("spoke for", row["owner_evidence"])
        self.assertIn("meeting: Marcus Lee said in the Oct 29 Assistant pricing sync (founders): “" + MARCUS_QUOTE + "”; "
                      "Priya Shah decides pricing, so it is Priya's call", row["evidence"])
        # The transcript is pinned as a source, and the event is not a guess.
        self.assertEqual([(s["ref"].split(":")[0], s["role"]) for s in n["sources"]], [("meeting", "support")])
        self.assertEqual(self.graph.count_events("meeting_context", decision_id=n["node_id"]), 1)
        self.assertEqual(self.graph.count_events("meeting_precedent", decision_id=n["node_id"]), 0)
        # Priya is asked, with what Marcus said; Marcus has nothing to confirm.
        priya = attention.overview(self.store, Actor(id=self.priya, name="Priya Shah"))
        self.assertEqual([i["id"] for i in priya["needs_you"]], [n["node_id"]])
        self.assertFalse(priya["needs_you"][0].get("guess"))
        marcus = attention.overview(self.store, Actor(id=self.marcus, name="Marcus Lee"))
        self.assertEqual(marcus["needs_you"], [])
        from bridge.delivery import _found_for_owner
        self.assertIn(MARCUS_QUOTE, _found_for_owner(row["evidence"]))
        rows = timeline.rows(self.store, self.task)
        self.assertTrue(any(r["kind"] == "flagged" and "to Priya, who decides for pricing decisions" in r["text"] for r in rows), rows)
        self.assertFalse(any(r["kind"] == "guess" for r in rows), rows)

    def test_words_the_model_reads_as_settling_it_still_settle_nothing_when_the_speaker_does_not_decide(self):
        self.learned(self.marcus, MARCUS_NOTE, MARCUS_QUOTE)
        self.model({"settles": True, "bears_on": True, "quote": MARCUS_QUOTE,
                    "answer": "Nothing is charged when the merchant asks; it goes through the nightly job.", "why": "Marcus said so."})
        n = self.node(ModelOn(model_api="claude-cli"))
        self.assertEqual((n["status"], n["owner"]), ("pending", "Priya Shah"))
        row = self.store.get_decision(n["node_id"])
        self.assertNotEqual(row["source"], "meeting")
        self.assertIn("meeting: Marcus Lee said", row["evidence"])

    def test_the_category_deciders_own_words_are_ravens_guess_routed_to_her(self):
        self.learned(self.priya, PRIYA_NOTE, PRIYA_QUOTE)
        n = self.node()
        self.assertEqual((n["status"], n["kind"], n["owner"], n["prediction"]), ("predicted", "prediction", "Priya Shah", PRIYA_NOTE))
        self.assertIn("Raven would apply what Priya Shah said in the Oct 29 Assistant pricing sync (founders), but isn't sure", n["evidence"])
        self.assertIn("Priya confirms or corrects it", n["evidence"])
        row = self.store.get_decision(n["node_id"])
        self.assertEqual(row["answered_via"], "meeting")
        self.assertTrue(row["owner_evidence"].startswith("verified: Priya Shah decides for pricing decisions"), row["owner_evidence"])
        self.assertIn("; spoke for pricing in the Oct 29 Assistant pricing sync (founders)", row["owner_evidence"])
        self.assertEqual(self.graph.count_events("meeting_precedent", decision_id=n["node_id"]), 1)
        rows = timeline.rows(self.store, self.task)
        guess = [r for r in rows if r["kind"] == "guess"]
        self.assertEqual(len(guess), 1, rows)
        self.assertTrue(guess[0]["text"].startswith(f"Raven's guess on “{timeline.topic(PW_QUESTION)}” from what Priya said in the"),
                        guess[0]["text"])
        self.assertTrue(guess[0]["text"].endswith("; Priya confirms"), guess[0]["text"])

    def test_with_both_on_record_the_deciders_words_are_the_guess_not_the_other_persons(self):
        self.learned(self.marcus, MARCUS_NOTE, MARCUS_QUOTE)
        self.learned(self.priya, PRIYA_NOTE, PRIYA_QUOTE)
        n = self.node()
        self.assertEqual((n["status"], n["owner"], n["prediction"]), ("predicted", "Priya Shah", PRIYA_NOTE))

    def test_with_no_category_the_most_specific_path_decider_is_the_one_whose_words_stand(self):
        """Paths only: Marcus decides app/billing/*, Priya the file itself."""
        self.learned(self.marcus, MARCUS_NOTE, MARCUS_QUOTE)
        n = self.node(category="", question=PW_QUESTION + " (paths only)")
        self.assertEqual((n["status"], n["owner"]), ("pending", "Priya Shah"))
        row = self.store.get_decision(n["node_id"])
        self.assertIn("Priya Shah decides app/billing/plans.py, so it is Priya's call", row["evidence"])

    def test_with_no_category_the_file_deciders_own_words_are_the_guess(self):
        self.learned(self.priya, PRIYA_NOTE, PRIYA_QUOTE)
        n = self.node(category="")
        self.assertEqual((n["status"], n["owner"], n["prediction"]), ("predicted", "Priya Shah", PRIYA_NOTE))
        self.assertIn("spoke for app/billing/plans.py in the Oct 29", self.store.get_decision(n["node_id"])["owner_evidence"])

    def test_the_deciders_words_settle_it_with_the_route_as_the_reason(self):
        self.learned(self.priya, PRIYA_NOTE, PRIYA_QUOTE)
        self.model({"settles": True, "bears_on": True, "quote": PRIYA_QUOTE, "why": "Priya set the rate.",
                    "answer": "Two cents per assistant answer past the allowance, the same on every plan."})
        n = self.node(ModelOn(model_api="claude-cli"))
        self.assertEqual((n["status"], n["signoff"], n["answered_by"]), ("resolved", "rule", "Priya Shah"))
        row = self.store.get_decision(n["node_id"])
        self.assertIn("Priya Shah, who decides pricing, said “", row["fit_reason"])
        self.assertTrue(row["owner_evidence"].startswith("verified: Priya Shah decides for pricing decisions"), row["owner_evidence"])

    def test_the_route_not_the_speakers_rows_says_who_decides(self):
        """A question the route sends to git history, not to the speaker:
        the words travel as context, with who is asked."""
        self.learned(self.marcus, MARCUS_NOTE, MARCUS_QUOTE)
        why: list = []
        run = self.graph.get_task(self.task)
        out = meeting_precedent.consider(self.store, self.cfg, run, PW_QUESTION, context=PW_CONTEXT,
                                         paths=["app/billing/plans.py"], category="pricing", repo=PW, why=why)
        self.assertEqual(out[0], "context")
        self.assertEqual((out[1]["decider"]["name"], out[1]["decider"]["area"], out[1]["area"]), ("Priya Shah", "pricing", ""))
        self.assertTrue(any("Marcus Lee does not decide this; Priya Shah does (pricing)" in w for w in why), why)
        out = meeting_precedent.consider(self.store, self.cfg, run, PW_QUESTION, context=PW_CONTEXT,
                                         paths=["app/billing/nightly.py"], category="billing", repo=PW)
        self.assertEqual((out[0], out[1]["area"]), ("predict", "billing"))


class HelperTests(OfflineCase):
    def test_the_area_a_route_line_names(self):
        area = meeting_precedent._route_area
        self.assertEqual(area(["verified: Priya Shah decides for pricing decisions (this one speaks of pricing) (config)"]), "pricing")
        self.assertEqual(area(["verified: Priya Shah decides for app/billing/plans.py (matches app/billing/plans.py) (config)"]),
                         "app/billing/plans.py")
        self.assertEqual(area(["verified: Dana Price decides for hw/riscv/* (matches hw/riscv/virt.c) (config), as a member of Core"]),
                         "hw/riscv/*")
        self.assertEqual(area(["verified: Ana Ruiz decides repository-wide (config)"]), "this repository")
        self.assertEqual(area(["inferred first contact: authored pr 12 for app/x.py"]), "")

    def test_the_meeting_line_survives_a_rewrite_of_the_evidence(self):
        line = "meeting: Marcus Lee said in the Oct 29 sync: “Nothing charges; ever: fine”; Priya Shah decides pricing, so it is Priya's call"
        before = "memory: best match scored 0.32, under the 0.6 floor; " + line
        self.assertEqual(meeting_precedent.carry_context(before, "semantic: no candidate answers this"),
                         "semantic: no candidate answers this; " + line)
        self.assertEqual(meeting_precedent.carry_context(before, "x; " + line), "x; " + line)
        self.assertEqual(meeting_precedent.carry_context("memory: nothing", "x"), "x")
        self.assertEqual(meeting_precedent.carry_context(before, ""), line)
        # Delivery shows it to the owner first, ahead of a record excerpt.
        from bridge.delivery import _found_for_owner
        plain = "meeting: Marcus Lee said in the Oct 29 sync: “Nothing charges when the merchant asks”; Priya Shah decides pricing"
        self.assertEqual(_found_for_owner("memory: best match scored 0.32, under the 0.6 floor; records: retrieved pr 220: x; " + plain),
                         plain + "; records: retrieved pr 220: x")


class WhenTests(OfflineCase):
    def test_the_meeting_date_comes_from_its_title_else_the_record(self):
        self.assertEqual(meeting_precedent.meeting_when({"title": "Pilot pricing sync, Oct 29", "at": "2026-11-02T10:00:00+00:00"}),
                         ("Oct 29", "2026-10-29"))
        self.assertEqual(meeting_precedent.meeting_when({"title": "Bloom check-in 2026-10-29", "at": ""}), ("Oct 29", "2026-10-29"))
        self.assertEqual(meeting_precedent.meeting_when({"title": "Planning", "at": "2026-11-02T10:00:00+00:00"}), ("Nov 2", "2026-11-02"))
        self.assertEqual(meeting_precedent._short_title("Pilot pricing sync (Dana, Elena), Oct 29"), "Pilot pricing sync (Dana, Elena)")

    def test_a_month_inside_a_persons_name_is_not_a_date(self):
        """Parcelwise dry run: "Marcus" read as "Mar", so the title lost the
        name and the date fell back to the import day."""
        at = "2026-11-02T10:00:00+00:00"
        title = "Assistant pricing sync (Priya, Marcus), Oct 29"
        self.assertEqual(meeting_precedent.meeting_when({"title": title, "at": at}), ("Oct 29", "2026-10-29"))
        self.assertEqual(meeting_precedent._short_title(title), "Assistant pricing sync (Priya, Marcus)")
        names = "Sync with Mary, June, Augusto, Novak, Decker, Julian and Sept"
        self.assertEqual(meeting_precedent.meeting_when({"title": names, "at": at}), ("Nov 2", "2026-11-02"))
        self.assertEqual(meeting_precedent._short_title(names), names)
        self.assertEqual(meeting_precedent._short_title("May Chen 1:1"), "May Chen 1:1")
        self.assertEqual(meeting_precedent.meeting_when({"title": "Marcus and June, 2026-10-29", "at": at}),
                         ("Oct 29", "2026-10-29"))

    def test_a_date_is_a_whole_month_word_next_to_a_day(self):
        at = "2026-11-02T10:00:00+00:00"
        for title, when in (("Sept. 3 pricing call", ("Sep 3", "2026-09-03")),
                            ("3 September pricing call", ("Sep 3", "2026-09-03")),
                            ("29 Oct billing", ("Oct 29", "2026-10-29")),
                            ("Oct. 29th sync", ("Oct 29", "2026-10-29")),
                            ("Q3 review Jun 1, 2025", ("Jun 1", "2025-06-01")),
                            ("Dec 2025 planning", ("Nov 2", "2026-11-02"))):
            self.assertEqual(meeting_precedent.meeting_when({"title": title, "at": at}), when, title)
        self.assertEqual(meeting_precedent._short_title("Q3 review Jun 1, 2025"), "Q3 review")
        self.assertEqual(meeting_precedent._short_title("Dec 2025 planning"), "Dec 2025 planning")
