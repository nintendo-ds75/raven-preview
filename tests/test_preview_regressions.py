"""Regressions from the f48189b real-host run, with scope controls."""
from pathlib import Path

from fixtures import OfflineCase
from bridge import canvas, ladder
from bridge.config import Config
from bridge.store import Store


class PreviewRegressions(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "preview.db")
        self.graph = self.store.graph
        self.cfg = Config(model_api="none")
        self.repo = "acme/library"
        self.owner = self.store.add_owner({"name": "Mira Runtime", "patterns": "src/*", "team": "Runtime"})

    def seed(self, question, answer, rationale="Recorded policy", path="src/retry.py", **extra):
        run = self.store.add_run({"title": "Policy", "repo": self.repo})
        node = self.store.request({"run_id": run["id"], "question": question, "path": path, "context": "Policy discussion",
                                   "owner_id": self.owner["id"]})
        self.store.answer(node["id"], {"answer": answer, "rationale": rationale, **extra})
        return self.graph.get_decision(node["id"])

    def ask(self, question, path="src/retry.py", **extra):
        r = ladder.run_task(self.graph, self.cfg, "New task", repo=self.repo,
                            decisions=[{"question": question, "path": path, **extra}])
        return self.graph.decisions_for_task(r.task_id)[0]

    def test_primary_docs_decider_survives_incidental_runtime_path(self):
        with self.graph.transaction():
            mira = self.graph.add_person("Mira Runtime", email="mira@example.test")
            theo = self.graph.add_person("Theo Release", email="theo@example.test")
            self.graph.add_authority("path", "src/*", "decides", person_id=mira, repo=self.repo)
            self.graph.add_authority("path", "docs/*", "decides", person_id=theo, repo=self.repo)
            self.graph.add_authority("path", "src/retry.py", "approves", person_id=mira, repo=self.repo)
        task = canvas.start_task(self.store, self.cfg, {"title": "Add retry option", "repo": self.repo})
        node = canvas.add_node(self.store, self.cfg, {"task_id": task["task_id"],
            "question": "Should this feature ship a docstring and changelog or also a guide?",
            "category": "docs", "path": "docs/", "paths": "docs/,changelog/,src/retry.py"})
        self.assertEqual(node["owner"], "Theo Release", node)
        self.assertIn("Mira Runtime", node["required_signers"])

    def test_explicit_applicability_not_recording_path_controls_reuse(self):
        q = "What documentation must ship for a new Retry feature in release=library-next?"
        source = self.seed(q, "Ship a feature changelog fragment and Retry docstring, no separate guide.",
                           path="docs/user-guide.rst", applicability={"requires": {"release": "library-next"}})
        why, _ = ladder._scope_difference("What documentation should ship under changelog/ for this Retry feature?",
            "", "changelog/", source, {"release": "library-next"}, {}, self.repo)
        self.assertEqual(why, "")

    def test_explicit_path_boundary_is_not_erased_by_matching_release(self):
        source = self.seed("What docs should ship?", "Guide only", path="docs/user-guide.rst",
            applicability={"requires": {"release": "library-next"}, "paths": ["docs/"]})
        node = self.ask("What docs should ship?", path="changelog/", facts={"release": "library-next"})
        self.assertNotEqual(node.status, "resolved")
        self.assertFalse(node.authorized)

    def test_source_file_specific_question_stays_file_specific(self):
        source = self.seed("What should docs/user-guide.rst explain?", "Guide only", path="docs/user-guide.rst",
            applicability={"requires": {"release": "library-next"}})
        why, known = ladder._scope_difference("What should changelog/ explain?", "", "changelog/", source,
            {"release": "library-next"}, {"release": "library-next"}, self.repo)
        self.assertTrue(why)
        self.assertTrue(known)

    def test_attribution_by_id_cannot_substitute_generic_policy(self):
        source = self.seed("What is the retry_after_jitter API?", "Reject negative jitter with ValueError.",
                           "Negative delays hide configuration errors.")
        self.seed("Who decided the validation policy and why?", "Reject all invalid numeric options.")
        node = self.ask(f"Who decided this in decision {source.id}, and why?")
        self.assertEqual(node.source_id, source.id)
        self.assertIn("Mira Runtime", node.answer)
        self.assertIn("Negative delays hide configuration errors.", node.answer)
        self.assertFalse(node.authorized)

    def test_natural_attribution_searches_answer_and_rationale(self):
        # The actual API answer and query from the frozen real-host run.
        source = self.seed(
            "What is the API shape and default: a keyword-only-in-practice `retry_after_jitter: float = 0.0` "
            "appended last to Retry.__init__, defaulting to off?",
            "Use float seconds, default 0.0, appended as the last constructor parameter to preserve positional "
            "compatibility. Negative or non-finite numeric Retry options raise ValueError; bools are accepted as "
            "numeric 0 or 1. Apply this to the new option, without retrofitting existing options.",
            "Explicit synthetic oracle policy for this evaluation.")
        self.seed("How should numeric Retry options validate negative, non-finite and bool inputs?",
                  "Negative or non-finite values raise ValueError; bools are accepted as numeric 0 or 1.")
        node = self.ask("Who decided that negative retry_after_jitter values should raise ValueError, and why?")
        self.assertEqual(node.source_id, source.id, node)
        self.assertIn("Mira Runtime", node.answer)
        self.assertIn(source.rationale, node.answer)

    def test_explicit_id_lookup_ignores_model_selector_and_composer(self):
        from unittest.mock import patch
        source = self.seed("What is the jitter default?", "Default to zero.")
        with patch("bridge.config.Config.semantic_retrieval", property(lambda self: True)), \
             patch("bridge.llm.Client.complete", side_effect=AssertionError("No model needed for ID lookup")):
            node = self.ask(f"Who decided decision {source.id} and why?")
        self.assertEqual(node.source_id, source.id)
        self.assertIn("Mira Runtime", node.answer)

    def test_fact_scoped_docs_reuse_is_evidence_but_still_needs_signature(self):
        from unittest.mock import patch
        question = "What ships as documentation for backoff_deadline under changelog/: a feature fragment plus " \
                   "the Retry docstring, with an unassigned local fragment filename?"
        source = self.seed("What documentation must ship for a new Retry feature in release=library-next?",
            "For release=library-next, ship a feature changelog fragment and the Retry docstring. "
            "Use an unassigned local fragment filename. No separate user-guide page is needed.",
            path="docs/user-guide.rst", applicability={"requires": {"release": "library-next"}})
        # Exercise scope handling after a composer maps a general policy to
        # the new feature, without asking a live model to grade itself.
        with patch("bridge.config.Config.semantic_retrieval", property(lambda self: True)), \
             patch("bridge.ladder._compose_answer", return_value=source.answer), \
             patch("bridge.llm.rerank_memories", return_value=(source.id, "same release-wide documentation policy")), \
             patch("bridge.ladder._selector_pick", return_value=("memory", source)):
            node = self.ask(question, path="changelog/", facts={"release": "library-next"})
        self.assertEqual(node.status, "resolved", node)
        self.assertEqual(node.source_id, source.id)
        self.assertFalse(node.authorized)

    def test_unknown_or_other_repository_id_is_not_substituted(self):
        source = self.seed("Who decided numeric validation and why?", "Reject bad options.")
        for ref in ("deadbeef1234", source.id):
            if ref == source.id:
                self.graph.db.execute("UPDATE decisions SET repo=? WHERE id=?", ("private/other", ref))
            node = self.ask(f"Who decided numeric validation in decision {ref}, and why?")
            self.assertEqual(node.status, "pending")
            self.assertFalse(node.source_id)
            self.assertFalse(node.answer)

    def test_unsigned_attribution_is_not_a_human_decision(self):
        source = self.seed("What should the jitter API be?", "Use a float.")
        self.graph.db.execute("UPDATE decisions SET signoff='required', answered_by='' WHERE id=?", (source.id,))
        node = self.ask(f"Who decided decision {source.id} and why?")
        self.assertNotEqual(node.status, "resolved")
        self.assertFalse(node.authorized)
