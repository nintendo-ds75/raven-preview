"""Near-duplicate questions on one task reach the person once. Measured
on the Harborline dry run (run 6): "What per-use rate does Enterprise pay
for Pilot?" and "How many cents does Enterprise pay per completed Pilot
response?" both reached Dana as two calls, and the pair about the free
allowance did the same."""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import OfflineCase  # noqa: E402

from bridge import attention, canvas, dedupe  # noqa: E402
from bridge.authz import Actor  # noqa: E402
from bridge.config import Config  # noqa: E402
from bridge.llm import LLMError  # noqa: E402

RATE = "What per-use rate does Enterprise pay for Pilot?"
CENTS = "How many cents does Enterprise pay per completed Pilot response?"
FREE = "How many Pilot uses does Enterprise get free each month before charges start?"
ALLOWANCE = "Is there a free monthly Pilot allowance for Enterprise, and how large?"
DATE = "From which date is Pilot usage charged for Enterprise orgs?"


class DedupeCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.warm_store("qemulike", "dedupe.db")
        self.cfg = Config()
        self.task = canvas.start_task(self.store, self.cfg, {
            "title": "Charge Enterprise for Pilot by use", "repo": "qemulike", "agent": "claude",
            "requester": "Tamsin Reed <tamsin@synthco.example>", "paths": "hw/riscv/virt.c"})["task_id"]

    def node(self, question, **extra):
        return canvas.add_node(self.store, self.cfg, {"task_id": self.task, "question": question,
                                                      "paths": "hw/riscv/virt.c", "category": "policy", **extra})

    def with_model(self, same):
        """A configuration with the deep rungs on and a model whose
        same-decision reading is `same`: a verdict for every pair, or a
        function of (Q1, Q2). The first ladder pass stays model-free, as
        it is in production."""
        self.cfg = ModelOn()
        verdict = same if callable(same) else (lambda a, b: same)

        def read(self_, purpose, system, prompt, **kw):
            if purpose != "same_decision":
                raise LLMError("offline: only the same-decision reading is faked here")
            q1, q2 = (line.split(": ", 1)[1] for line in prompt.splitlines()[:2])
            return {"same": verdict(q1, q2)}
        patcher = patch("bridge.llm.Client.complete_json", autospec=True, side_effect=read)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        # The background model pass is the ladder's own concern; this suite
        # reads what the add_node reply says.
        quiet = patch("bridge.canvas._start_background", lambda *a, **k: None)
        quiet.start()
        self.addCleanup(quiet.stop)
        return mock


class ModelOn(Config):
    """As production runs with a model: the semantic and deep rungs on, so
    add_node's first ladder pass is model-free and the same-decision
    reading is the one model call the fold makes."""
    @property
    def semantic_retrieval(self):
        return not self.deterministic

    @property
    def deep_retrieval(self):
        return not self.deterministic


class SameTaskTwinTests(DedupeCase):
    def test_the_same_words_in_another_order_fold_without_a_model(self):
        first = self.node("Should the aplic be the default interrupt controller for the virt board?")
        self.assertEqual(first["status"], "pending")
        second = self.node("Should the default interrupt controller for the virt board be the aplic?")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["duplicate_of"], first["node_id"])
        self.assertNotEqual(second["node_id"], first["node_id"])
        self.assertIn(f"folded into node {first['node_id']}", second["next"])
        self.assertEqual(second["owner"], first["owner"])
        # The owner is asked once: the stub is nobody's question to answer.
        self.assertEqual(self.store.graph.count_events("dedup_open", decision_id=second["node_id"]), 1)
        self.assertFalse(self.store.graph.db.execute("SELECT 1 FROM notifications WHERE decision_id=?",
                                                     (second["node_id"],)).fetchone())
        tree = canvas.get_tree(self.store, self.task)
        self.assertEqual(tree["counts"]["pending"], 1)
        self.assertEqual(tree["counts"]["duplicate"], 1)
        self.assertIn("1 waiting on", tree["next"])

    def test_one_answer_settles_both(self):
        first = self.node("Should the aplic be the default interrupt controller for the virt board?")
        second = self.node("Should the default interrupt controller for the virt board be the aplic?")
        self.store.answer(first["node_id"], {"answer": "Yes, the aplic is the default.", "signed_by": first["owner"],
                                             "source": "inbox: " + first["owner"]})
        stub = canvas.node_view(self.store, second["node_id"])
        self.assertEqual(stub["answer"], "Yes, the aplic is the default.")
        self.assertTrue(stub["authorized"])
        self.assertFalse(stub["blocking"])

    def test_the_run_6_pairs_fold_when_the_model_reads_them_as_one_decision(self):
        """Hybrid similarity 0.67 and 0.43: under the deterministic bar, so
        the ladder's open-twin check never folded them; the second pair was
        not even a candidate under the 0.45 floor."""
        pairs = {frozenset((RATE, CENTS)), frozenset((FREE, ALLOWANCE))}
        read = self.with_model(lambda a, b: frozenset((a, b)) in pairs)
        rate = self.node(RATE)
        cents = self.node(CENTS)
        self.assertEqual((cents["status"], cents["duplicate_of"]), ("duplicate", rate["node_id"]))
        self.assertIn(f"folded into node {rate['node_id']}", cents["next"])
        free = self.node(FREE)
        allowance = self.node(ALLOWANCE)
        self.assertEqual((allowance["status"], allowance["duplicate_of"]), ("duplicate", free["node_id"]))
        self.assertEqual(read.call_count, 3)  # cents vs rate; allowance vs free and vs rate
        pending = [n for n in canvas.get_tree(self.store, self.task)["nodes"] if n["status"] == "pending"]
        self.assertEqual(sorted(n["question"] for n in pending), sorted([RATE, FREE]))

    def test_an_unsure_model_leaves_both_questions_with_the_person(self):
        self.with_model(False)
        rate = self.node(RATE)
        cents = self.node(CENTS)
        self.assertEqual(cents["status"], "pending")
        self.assertNotEqual(cents["node_id"], rate["node_id"])

    def test_without_a_model_a_near_match_is_asked_as_its_own_node(self):
        rate = self.node(RATE)
        cents = self.node(CENTS)
        self.assertEqual(cents["status"], "pending")
        self.assertNotEqual(cents["node_id"], rate["node_id"])

    def test_distinct_questions_are_never_merged(self):
        """The rate and the start date share the subject, not the ask: far
        enough apart that the model is not even consulted."""
        read = self.with_model(True)
        rate = self.node(RATE)
        date = self.node(DATE)
        self.assertEqual(date["status"], "pending")
        self.assertNotEqual(date["node_id"], rate["node_id"])
        self.assertEqual(read.call_count, 0)

    def test_another_named_customer_or_category_is_another_decision(self):
        """Even a model reading them as one decision does not merge them:
        the scope guards run first."""
        self.with_model(lambda a, b: a == b)
        alpha = self.node("Should the Pilot charge be waived for this customer?", facts="customer=alpha")
        beta = self.node("Should the Pilot charge be waived for this customer?", facts="customer=beta",
                         context="Customer Beta asked in their renewal.")
        self.assertEqual(beta["status"], "pending")
        self.assertNotEqual(beta["node_id"], alpha["node_id"])

    def test_the_same_words_under_another_category_fold_only_on_the_models_reading(self):
        rollout = canvas.add_node(self.store, self.cfg, {"task_id": self.task, "question": "When does the Pilot charge start for Enterprise?",
                                                         "paths": "hw/riscv/virt.c", "category": "rollout"})
        policy = canvas.add_node(self.store, self.cfg, {"task_id": self.task, "question": "Does the Pilot charge start for Enterprise now?",
                                                        "paths": "hw/riscv/virt.c", "category": "policy"})
        self.assertEqual(policy["status"], "pending")
        self.assertNotEqual(policy["node_id"], rollout["node_id"])
        read = self.with_model(True)
        again = canvas.add_node(self.store, self.cfg, {"task_id": self.task, "question": "Is the Pilot charge for Enterprise starting now?",
                                                       "paths": "hw/riscv/virt.c", "category": "definition"})
        self.assertEqual(again["status"], "duplicate")
        self.assertIn(again["duplicate_of"], (rollout["node_id"], policy["node_id"]))
        self.assertGreaterEqual(read.call_count, 1)

    def test_the_shorter_questions_words_all_in_the_longer_one_is_not_the_same_words(self):
        """Measured live on dry run 8: "Which plans are charged per use for
        Pilot?" folded into the allowance question at 0.80, because every
        question on the canvas says Pilot, charge and per use. Words alone
        fold only the same term set; this pair waits for a model's reading,
        and a model that reads them as two decisions keeps them apart."""
        plans = "Which plans are charged per use for Pilot?"
        allowance = ("How many Pilot uses are included each month before charges start, and is that per seat or "
                     "per organisation?")
        self.assertGreaterEqual(dedupe._similarity(plans, allowance), dedupe.SURE)
        self.assertLess(dedupe._jaccard(plans, allowance), dedupe.SURE)
        first = self.node(allowance)
        second = self.node(plans)
        self.assertEqual(second["status"], "pending")
        self.assertNotEqual(second["node_id"], first["node_id"])
        self.with_model(False)
        third = self.node("Which plans are charged for Pilot per use?")
        self.assertEqual(third["status"], "duplicate")
        self.assertEqual(third["duplicate_of"], second["node_id"], "the same words in another order fold without a model")

    def test_a_different_figure_is_a_different_question(self):
        self.with_model(True)
        a = self.node("Should Pilot retain usage rows for 30 days?")
        b = self.node("Should Pilot retain usage rows for 90 days?")
        self.assertEqual(b["status"], "pending")
        self.assertNotEqual(a["node_id"], b["node_id"])

    def test_similarity_matches_the_ladders_hybrid(self):
        self.assertGreaterEqual(dedupe._similarity(RATE, CENTS), dedupe.BAND)
        self.assertLess(dedupe._similarity(RATE, CENTS), dedupe.SURE)
        self.assertGreaterEqual(dedupe._similarity(FREE, ALLOWANCE), dedupe.BAND)
        self.assertLess(dedupe._similarity(RATE, DATE), dedupe.BAND)


class CountsTests(DedupeCase):
    """A folded question is not another call on Being built, For you or
    /api/attention."""

    def test_the_stub_is_not_counted_as_a_call(self):
        first = self.node("Should the aplic be the default interrupt controller for the virt board?")
        second = self.node("Should the default interrupt controller for the virt board be the aplic?")
        owner = Actor(id=self.store.add_person({"name": first["owner"]})["id"], name=first["owner"])
        view = attention.overview(self.store, owner)
        self.assertEqual([item["id"] for item in view["needs_you"]], [first["node_id"]])
        self.assertEqual(view["counts"]["needs_you"], 1)
        self.assertEqual(view["counts"]["asked_elsewhere"], 0)
        self.assertNotIn("other call", view["needs_you"][0]["meanwhile"])
        other = Actor(id=self.store.add_person({"name": "Someone Else"})["id"], name="Someone Else")
        self.assertEqual(attention.overview(self.store, other)["counts"]["asked_elsewhere"], 1)
        state = self.store.state()
        stub = next(d for d in state["decisions"] if d["id"] == second["node_id"])
        self.assertEqual((stub["status"], stub["superseded_by"]), ("duplicate", first["node_id"]))
