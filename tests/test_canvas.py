"""The canvas protocol: kickoff triage, nodes with parents, idempotent
writes, agent-settled nodes marked for sign-off, follow-up questions
people add, sign-off and correction, and the tree read back, over MCP
and REST alike."""

import json
import os
import sqlite3
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import OfflineCase  # noqa: E402
from review_source_fixtures import source_conformance

from bridge import canvas  # noqa: E402
from bridge.config import Config  # noqa: E402
from bridge.mcp import dispatch  # noqa: E402
from fixtures import ready_server as make_server  # noqa: E402
from bridge.store import Invalid  # noqa: E402


class CanvasCase(OfflineCase):
    """Fixture only: the qemu-like repository ingested, a copy per test."""

    def setUp(self):
        super().setUp()
        self.store = self.warm_store("qemulike", "canvas.db")
        self.cfg = Config()

    def start(self, title, **extra):
        return canvas.start_task(self.store, self.cfg, {"title": title, "repo": "qemulike", "agent": "test",
                                                        "requester": "Tamsin Reed <tamsin@synthco.example>",
                                                        **extra})

    def sign(self, node_id, by, **extra):
        """Sign (or correct) a node at the revision currently on it, the
        way the inbox does."""
        current = canvas.node_view(self.store, node_id)
        return canvas.sign_off(self.store, node_id, {"by": by, "expected_updated_at": current["updated_at"], **extra})


class CandidateTests(CanvasCase):
    """Raven cannot see a question the agent never writes down. What it
    can do is say which of its own signals usually mean somebody has to
    decide, and let the agent pick. These are prompts, not decisions."""

    def test_a_quoted_question_is_cut_at_a_word_and_marked(self):
        """Measured live on eb9d22d: stored text ended inside a word with no
        mark. A quote shown to a person is whole, or cut at a word with an
        ellipsis."""
        question = ("What documentation must ship for a new Retry feature in release=library-next, and does it "
                    "cover the changelog fragment, the Retry docstring and the user guide section on backoff?")
        shown = canvas._clip(question, 120)
        self.assertTrue(shown.endswith("…"), shown)
        kept = shown[:-1]
        self.assertTrue(question.startswith(kept), kept)
        self.assertEqual(question[len(kept)], " ", f"cut inside a word: {kept[-20:]!r}")
        self.assertEqual(canvas._clip("short question?", 120), "short question?")

    def test_the_kickoff_names_another_deciders_area_these_files_usually_change_with(self):
        """Measured live on 63eb671: the jitter task named a changelog
        fragment and never asked the person who decides changelog/*; nothing
        at kickoff said changes to retry.py come with one."""
        graph = self.store.graph
        theo = graph.add_person("Theo Release", email="theo@example.test")
        graph.add_authority("path", "changelog/*", "decides", person_id=theo, repo="qemulike")
        nora = graph.add_person("Nora Docs", email="nora@example.test")
        graph.add_authority("path", "docs/devel/*", "decides", person_id=nora, repo="qemulike")
        for i in range(8):
            graph.add_change("qemulike", f"c0ffee{i:02d}", f"2026-09-{i + 10:02d}T00:00:00+00:00",
                             ["hw/riscv/virt.c", f"changelog/{5000 + i}.feature.rst", "tests/test_virt.py"],
                             [("Oriel Vance", "oriel@example.test", "author")])
        t = self.start("Change the default interrupt controller in hw/riscv/virt.c", paths="hw/riscv/virt.c",
                       goal="Switch the default to the aplic. This changes the default for every board user.")
        history = [c for c in t["candidates"] if c["source"] == "the history"]
        self.assertEqual(len(history), 1, t["candidates"])
        self.assertEqual(history[0]["owner"], "Theo Release")
        self.assertEqual(history[0]["paths"], "changelog/")
        self.assertEqual(history[0]["question"], "What does this change add or name under changelog/*, which Theo "
                                                 "Release decides?")
        self.assertTrue(history[0]["why"].startswith("8 of the last 21 changes to these files also touched "
                                                     "changelog/*"), history[0]["why"])
        self.assertIn("bridge_finish_task (uncovered)", t["next"])
        # A file history never changes with these is not named, unless the
        # task names it; a typo asks nobody anything.
        self.assertNotIn("Nora Docs", str(t["candidates"]))
        both = self.start("Change the default interrupt controller in hw/riscv/virt.c",
                          paths="hw/riscv/virt.c docs/devel/riscv.rst",
                          goal="Switch the default to the aplic and say so in the developer docs.")
        named = [c for c in both["candidates"] if c["owner"] == "Nora Docs"]
        self.assertEqual(len(named), 1, both["candidates"])
        self.assertEqual(named[0]["why"], "the task names docs/devel/riscv.rst, and Nora Docs decides docs/devel/*; "
                                          "write what the change settles there as a node for Nora Docs")
        typo = self.start("Fix a typo in hw/riscv/virt.c", paths="hw/riscv/virt.c",
                          goal="One spelling correction in a comment, no behavioral change.")
        self.assertEqual(typo["candidates"], [])

    def test_the_kickoff_names_decisions_the_task_may_contain(self):
        t = self.start("Remove the legacy virtio-net header", paths="hw/net/virtio-net.c",
                       goal="Drop the legacy header. It breaks compat for old guests and changes the default.")
        topics = [c["question"] for c in t["candidates"]]
        self.assertTrue(topics, t["why"])
        self.assertTrue(any("keep working" in q for q in topics), topics)
        self.assertTrue(any("removed outright" in q or "deprecated" in q for q in topics), topics)
        for c in t["candidates"]:
            self.assertTrue(c["why"], c)
            self.assertIn(c["source"], ("the task's own words", "a prior decision", "the listings"))
        self.assertIn("candidates", t["next"])

    def test_decisions_a_model_read_out_of_the_task_are_named_too(self):
        """The templates stand for Raven's own signals and are general
        by construction. Measured on the held-out Grafana tasks they
        named 2 of the 8 decisions those changes turned on; read closely,
        4 to 6. A candidate is a prompt, so a wrong one costs one read."""
        read = [{"question": "Which tables does the migration have to cover?",
                 "from": "the legacy virtio-net header"}]
        with patch("bridge.config.Config.semantic_retrieval", property(lambda self: True)), \
             patch("bridge.llm.name_decisions", return_value=read) as namer:
            t = self.start("Remove the legacy virtio-net header", paths="hw/net/virtio-net.c",
                           goal="Drop the legacy header. It breaks compat for old guests.")
            self.assertTrue(canvas.wait_for_background(10))
            t = canvas.start_task(self.store, Config(), {"task_id": t["task_id"]})
        self.assertEqual(namer.call_count, 1)
        first = t["candidates"][0]
        self.assertEqual(first["question"], "Which tables does the migration have to cover?")
        self.assertEqual(first["source"], "the task read closely")
        self.assertIn("the legacy virtio-net header", first["why"])
        # The templates are still there: read closely adds, never replaces.
        self.assertTrue(any(c["source"] == "the task's own words" for c in t["candidates"]), t["candidates"])
        # And a later read costs nothing: it rides on the stored discovery.
        again = canvas.get_tree(self.store, t["task_id"])
        self.assertIn("Raven sees", again["next"])

    def test_without_a_model_the_templates_are_the_whole_list(self):
        t = self.start("Remove the legacy virtio-net header", paths="hw/net/virtio-net.c",
                       goal="Drop the legacy header. It breaks compat for old guests.")
        self.assertNotIn("named_decisions", t["discovery"])
        self.assertTrue(t["candidates"])
        self.assertTrue(all(c["source"] != "the task read closely" for c in t["candidates"]))

    def test_an_empty_tree_repeats_them_where_the_agent_looks(self):
        t = self.start("Remove the legacy virtio-net header", paths="hw/net/virtio-net.c",
                       goal="Drop the legacy header. It breaks compat for old guests.")
        tree = canvas.get_tree(self.store, t["task_id"])
        self.assertEqual(tree["nodes"], [])
        self.assertIn("Raven sees", tree["next"])
        self.assertIn("bridge_add_node", tree["next"])

    def test_a_task_with_nothing_to_go_on_proposes_nothing(self):
        t = self.start("Fix the typo in the release notes")
        self.assertEqual(t["candidates"], [])
        self.assertNotIn("candidates", t["next"])

    def test_a_prior_decision_becomes_a_candidate_to_re_examine(self):
        first = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        node = canvas.add_node(self.store, self.cfg, {
            "task_id": first["task_id"], "client_ref": "p",
            "question": "Should hw/riscv/virt.c change the default interrupt controller?",
            "paths": "hw/riscv/virt.c"})
        row = self.store.get_decision(node["node_id"])
        self.store.answer(node["node_id"], {"answer": "Keep the plic as the default", "rationale": "boards ship it",
                                            "signed_by": "Oriel Vance",
                                            "expected_updated_at": row["updated_at"]})
        again = self.start("Change the default interrupt controller on hw/riscv/virt.c",
                           paths="hw/riscv/virt.c", client_key="second",
                           goal="Make the aplic the default for the virt board.")
        priors = [c for c in again["candidates"] if c["source"] == "a prior decision"]
        self.assertTrue(priors, again["candidates"])
        self.assertIn("still hold", priors[0]["question"])
        self.assertIn(node["node_id"], priors[0]["why"])


class FinishTests(CanvasCase):
    """Finishing says every decision was authorized. It does not say the
    change does what was authorized, and a reviewer read it as though it
    did, on a task whose tests could not even run."""

    def finished(self, **extra):
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        node = canvas.add_node(self.store, self.cfg, {
            "task_id": t["task_id"], "client_ref": "n",
            "question": "Should hw/riscv/virt.c change the default interrupt controller?",
            "paths": "hw/riscv/virt.c"})
        row = self.store.get_decision(node["node_id"])
        self.store.answer(node["node_id"], {"answer": "Use the aplic", "rationale": "boards are ready",
                                            "signed_by": "Oriel Vance",
                                            "expected_updated_at": row["updated_at"]})
        return node, canvas.finish_task(self.store, {"task_id": t["task_id"], **extra})

    def test_finishing_says_what_it_does_and_does_not_cover(self):
        node, done = self.finished()
        self.assertEqual(done["status"], "completed")
        self.assertFalse(done["verified"])
        self.assertEqual([a["node_id"] for a in done["authorized"]], [node["node_id"]])
        self.assertEqual(done["authorized"][0]["signed_by"], "Oriel Vance")
        self.assertIn("Authorized is not verified", done["caveat"])
        self.assertIn("did not read the diff", done["caveat"])
        self.assertIn("reported no checks", done["caveat"])

    def test_finish_retains_source_catalog_in_saved_review_and_proof_without_rereading(self):
        from bridge import llm, proof
        sources = llm._approved_sources('Use the aplic')
        reading = {'verdict': 'unclear', 'why': 'A code check remains unresolved.', 'requirements': [
            {'needs': 'Use the aplic', 'source_id': sources[0]['id'], 'source_status': 'bound', 'state': 'unclear'}],
            'approved_sources': sources, 'approved_context': {'question': 'Original question',
                'other_decisions': [{'question': '  q? ', 'answer': '  exact sibling\n'}]},
            'source_issues': ['requirements[0]: unresolved fixture'],
            'source_issues_omitted': 2, 'incomplete': True}
        with patch.object(llm, 'check_conformance', return_value=reading):
            node, done = self.finished(diff='--- a/hw/riscv/virt.c\n+++ b/hw/riscv/virt.c\n+  use_aplic = true;')
        current = done['follows'][0]
        self.assertEqual(current['approved_sources'], sources)
        self.assertEqual(current['approved_context'], reading['approved_context'])
        self.assertEqual(current['source_issues'], reading['source_issues'])
        self.assertEqual(current['source_issues_omitted'], 2)
        task_id = self.store.get_decision(node['node_id'])['run_id']
        with patch.object(llm, 'check_conformance') as model:
            tree = canvas.get_tree(self.store, task_id)
            bundle = proof.export(self.store, {'task_id': task_id})['bundle']
        model.assert_not_called()
        self.assertEqual(tree['review']['follows'][0]['approved_sources'], sources)
        self.assertEqual(bundle['payload']['review']['follows'][0]['approved_sources'], sources)
        self.assertEqual(bundle['payload']['review']['follows'][0]['approved_context'], reading['approved_context'])

    def test_the_diff_is_read_against_each_signed_answer_and_reported(self):
        """Raven gates authorization and has never been able to say
        whether the code follows it. Measured on Grafana: an owner
        corrected an answer to three states, the agent reported that its
        change matched, and the diff carried one of them."""
        read = {"verdict": "departs", "why": "the diff sets the default to the old controller"}
        with patch("bridge.llm.check_conformance", return_value=read):
            node, done = self.finished(diff="--- a/hw/riscv/virt.c\n+++ b/hw/riscv/virt.c\n+  use_plic = true;")
        self.assertEqual([f["verdict"] for f in done["follows"]], ["departs"])
        self.assertEqual(done["follows"][0]["node_id"], node["node_id"])
        self.assertIn("1 depart from what was signed", done["caveat"])
        self.assertIn("authorizes nothing", done["caveat"])
        # A departure is reported, never a gate: the task still finished.
        self.assertEqual(done["status"], "completed")
        self.assertFalse(done["verified"])
        events = [e for e in self.store.state()["events"] if e["kind"] == "conformance_read"]
        self.assertEqual(len(events), 1)

    def test_each_signed_answer_is_read_with_the_tasks_other_answers_beside_it(self):
        """Measured live on urllib3: three signed answers on one task, each
        read alone, and the one about negative values called the decimal
        parsing that a sibling answer authorized "not authorized"."""
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        nodes = []
        for ref, question, answer in (("a", "Should hw/riscv/virt.c change the default interrupt controller?", "Use the aplic"),
                                      ("b", "Should the plic stay available on hw/riscv/virt.c?", "Keep it behind a property")):
            node = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "client_ref": ref,
                                                          "question": question, "paths": "hw/riscv/virt.c"})
            row = self.store.get_decision(node["node_id"])
            self.store.answer(node["node_id"], {"answer": answer, "rationale": "r", "signed_by": "Oriel Vance",
                                                "expected_updated_at": row["updated_at"]})
            nodes.append(node)
        seen = []

        def read(cfg, question, answer, diff, others=()):
            seen.append((answer, [a for _, a in others]))
            return {"verdict": "follows", "why": "", "requirements": []}
        with patch("bridge.llm.check_conformance", side_effect=read):
            canvas.finish_task(self.store, {"task_id": t["task_id"], "diff": "+  use_aplic = true;"})
        self.assertEqual(sorted(seen), [("Keep it behind a property", ["Use the aplic"]),
                                        ("Use the aplic", ["Keep it behind a property"])])

    def test_a_signed_answer_the_model_gave_no_reading_for_is_named(self):
        """Measured live: the model gave no reading for one of six signed
        answers, the finish counted five, and the agent's summary said all
        six followed."""
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        ids = []
        for ref, question, answer in (("a", "Should hw/riscv/virt.c change the default interrupt controller?", "Use the aplic"),
                                      ("b", "Should the plic stay available on hw/riscv/virt.c?", "Keep it behind a property")):
            node = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "client_ref": ref,
                                                          "question": question, "paths": "hw/riscv/virt.c"})
            row = self.store.get_decision(node["node_id"])
            self.store.answer(node["node_id"], {"answer": answer, "rationale": "r", "signed_by": "Oriel Vance",
                                                "expected_updated_at": row["updated_at"]})
            ids.append(node["node_id"])

        def read(cfg, question, answer, diff, others=()):
            return {"verdict": "follows", "why": ""} if answer == "Use the aplic" else {}
        with patch("bridge.llm.check_conformance", side_effect=read):
            done = canvas.finish_task(self.store, {"task_id": t["task_id"], "diff": "+  use_aplic = true;"})
        self.assertEqual([f["node_id"] for f in done["follows"]], [ids[0]])
        self.assertEqual([u["node_id"] for u in done["unread"]], [ids[1]])
        self.assertIn(f"no reading for 1 ({ids[1]})", done["caveat"])
        self.assertIn("do not report it as followed", done["caveat"])

    def test_the_decisions_are_read_at_once(self):
        """Measured live: six signed decisions read one after another took
        about 44 seconds at the finish."""
        import threading
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        for ref, question, answer in (("a", "Should hw/riscv/virt.c change the default interrupt controller?", "Use the aplic"),
                                      ("b", "Should the plic stay available on hw/riscv/virt.c?", "Keep it behind a property")):
            node = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "client_ref": ref,
                                                          "question": question, "paths": "hw/riscv/virt.c"})
            row = self.store.get_decision(node["node_id"])
            self.store.answer(node["node_id"], {"answer": answer, "rationale": "r", "signed_by": "Oriel Vance",
                                                "expected_updated_at": row["updated_at"]})
        together = threading.Barrier(2, timeout=10)

        def read(cfg, question, answer, diff, others=()):
            together.wait()  # only returns once both reads are running
            return {"verdict": "follows", "why": answer}
        with patch("bridge.llm.check_conformance", side_effect=read):
            done = canvas.finish_task(self.store, {"task_id": t["task_id"], "diff": "+  use_aplic = true;"})
        self.assertEqual([f["why"] for f in done["follows"]], ["Use the aplic", "Keep it behind a property"])

    def test_a_reason_is_whole_or_cut_at_a_word_and_says_so(self):
        """Measured live on a397f1c: five of seven reasons came back cut at
        300 characters, mid-word ("used by sleep_for_r"), with no mark."""
        from bridge import llm
        env = patch.dict(os.environ, {"BRIDGE_SEMANTIC": "1", "BRIDGE_CLAUDE_BIN": "/nonexistent/claude"})
        env.start()
        self.addCleanup(env.stop)
        medium = ("The diff adds retry_after_jitter as a trailing keyword argument defaulting to 0.0, validates it "
                  "in __init__, and draws a fresh offset in _jittered_retry_after used by sleep_for_retry only when "
                  "retry_after_jitter > 0 inside _jittered_retry_after, so the default path is unchanged and the "
                  "tests assert both the default and the jittered path with a seeded random source.")
        long = " ".join(["The requirement about copying the setting through new() is met by the change to new()."] * 30)
        needs = "every " * 120
        replies = [{"status": "complete", "requirements": [{"needs": "default stays 0.0", "kind": "must", "found": "honored", "at": "default = 0.0"}], "why": medium},
                   {"status": "complete", "requirements": [{"needs": needs, "kind": "must", "found": "honored", "at": "default = 0.0"}], "why": long}]
        with patch.object(llm.Client, "complete_json", lambda self, purpose, *a, **k: replies.pop(0)
                          if purpose == "conformance" else ({"status": "complete", "conditions": []}
                          if purpose == "conditions" else {"status": "complete", "checks": []})):
            whole = source_conformance(Config(), "q", "a", "+default = 0.0")
            cut = source_conformance(Config(), "q", needs, "+default = 0.0")
        self.assertGreater(len(medium), 300)
        self.assertEqual(whole["why"], medium)
        self.assertTrue(cut["why"].endswith("… [cut: the requirements list what was read]"), cut["why"][-80:])
        kept = cut["why"].split(" … [cut:")[0]
        self.assertTrue(long.startswith(kept) and long[len(kept)] == " ", kept[-60:])
        self.assertTrue(cut["requirements"][0]["needs"].endswith("[cut: full text is in approved_sources]"))

    def test_the_finish_returns_the_requirements_each_reading_rests_on(self):
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        node = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "paths": "hw/riscv/virt.c",
                                                      "question": "Should hw/riscv/virt.c change the default interrupt controller?"})
        row = self.store.get_decision(node["node_id"])
        self.store.answer(node["node_id"], {"answer": "Use the aplic", "rationale": "r", "signed_by": "Oriel Vance",
                                            "expected_updated_at": row["updated_at"]})
        reqs = [{"needs": "aplic is the default", "kind": "must", "found": "honored", "state": "ok"}]
        with patch("bridge.llm.check_conformance", return_value={"verdict": "follows", "why": "w", "requirements": reqs}):
            done = canvas.finish_task(self.store, {"task_id": t["task_id"], "diff": "+  use_aplic = true;"})
        self.assertEqual(done["follows"][0]["requirements"], reqs)

    def test_a_reading_with_no_reason_names_what_it_rests_on(self):
        from bridge import llm
        env = patch.dict(os.environ, {"BRIDGE_SEMANTIC": "1", "BRIDGE_CLAUDE_BIN": "/nonexistent/claude"})
        env.start()
        self.addCleanup(env.stop)
        reply = {"status": "complete", "requirements": [{"needs": "negatives stay invalid", "kind": "must", "found": "honored",
                                   "at": "if value < 0: raise InvalidHeader(value)"},
                                  {"needs": "zero stays the default", "kind": "must", "found": "honored",
                                   "at": "default: float = 0"}], "why": ""}
        diff = "+    default: float = 0\n+    if value < 0: raise InvalidHeader(value)\n"
        with patch.object(llm.Client, "complete_json",
                          lambda self, purpose, *a, **k: reply if purpose == "conformance" else ({"status": "complete", "conditions": []} if purpose == "conditions" else {"status": "complete", "checks": []})):
            read = source_conformance(Config(), "Should -1 be accepted?", "Keep negatives invalid", diff)
        self.assertEqual(read["verdict"], "follows")
        self.assertEqual(read["why"], "Read as doing each thing it requires: Keep negatives invalid")

    def test_the_conformance_prompt_lists_the_other_answers_and_not_its_own(self):
        from bridge import llm
        prompts = []

        def complete_json(self, purpose, system, prompt, **kw):
            if purpose == "conditions":
                return {"status": "complete", "conditions": []}
            prompts.append(prompt)
            if purpose == "counterexample":
                return {"status": "complete", "checks": [{"n": 1, "counterexample": "", "at": "", "not_shown": ""}]}
            return {"status": "complete", "requirements": [{"needs": "negatives stay invalid", "kind": "must", "found": "present",
                                      "at": "if value < 0: raise InvalidHeader(value)"}], "why": ""}
        env = patch.dict(os.environ, {"BRIDGE_SEMANTIC": "1", "BRIDGE_CLAUDE_BIN": "/nonexistent/claude"})
        env.start()
        self.addCleanup(env.stop)
        with patch.object(llm.Client, "complete_json", complete_json):
            read = source_conformance(Config(), "Should -1 be accepted?", "Keep negatives invalid",
                                         "+    if value < 0: raise InvalidHeader(value)\n",
                                         (("Should 0.5 be accepted?", "Accept unsigned decimals, rounded up"),))
        self.assertEqual(read["verdict"], "follows")
        self.assertIn("OTHER DECISIONS ON THIS TASK", prompts[0])
        self.assertIn("Accept unsigned decimals, rounded up", prompts[0])
        self.assertEqual(prompts[0].count("Keep negatives invalid"), 1)
        # The search for counterexamples knows what the sibling answers
        # authorized, so their changes are not reported as breaking this one.
        self.assertIn("Accept unsigned decimals, rounded up", prompts[1])
        self.assertIn("1. ORIGINAL SOURCE", prompts[1])
        self.assertIn("at: if value < 0: raise InvalidHeader(value)", prompts[1])
        self.assertNotIn("negatives stay invalid", prompts[1])

    def test_a_rule_the_diff_keeps_reads_as_kept_however_it_is_worded(self):
        """Measured on the hard end-to-end run: "POST/PATCH must not be
        retried after connect" came back as a must_not the diff showed,
        and a change that kept the rule read as departing from it."""
        from bridge import llm
        replies = []

        def complete_json(self, purpose, system, prompt, **kw):
            if purpose == "conditions":
                return {"status": "complete", "conditions": []}
            if purpose == "counterexample":
                return {"status": "complete", "checks": []}
            return {"status": "complete", "requirements": replies.pop(0), "why": ""}
        env = patch.dict(os.environ, {"BRIDGE_SEMANTIC": "1", "BRIDGE_CLAUDE_BIN": "/nonexistent/claude"})
        env.start()
        self.addCleanup(env.stop)
        rule = "POST/PATCH are never retried after the socket connected"
        kept = "if read is False or method is None or not self._is_method_retryable(method):"
        replies.extend([
            [{"needs": rule, "kind": "must_not", "found": "honored", "at": kept}],
            [{"needs": rule, "kind": "must_not", "found": "violated"}],
            [{"needs": rule, "kind": "must", "found": "violated"}],
            [{"needs": rule, "kind": "must_not", "found": "unseen"}],
            [{"needs": rule, "kind": "must", "found": "unseen"}],
            [{"needs": "a regression test for POST after connect", "kind": "must", "found": "missing"}],
            # Missing for something ruled out: the forbidden thing, or the rule against it.
            [{"needs": rule, "kind": "must_not", "found": "missing"}],
        ])
        with patch.object(llm.Client, "complete_json", complete_json):
            got = [source_conformance(Config(), "What is safe to retry?", "Connect-phase only",
                                         f"         {kept}\n")["verdict"] for _ in range(7)]
        # Ungrounded negative labels no longer establish a departure.
        self.assertEqual(got, ["follows", "unclear", "unclear", "follows", "unclear", "unclear", "unclear"])
        self.assertIn('"honored" or "missing" or "violated" or "unseen"', llm.CONFORMANCE_SYSTEM)

    # The hunk of the real eb9d22d patch the false reading was about. The
    # budget is checked before every positive sleep; two lines above that,
    # an unchanged line returns early when the backoff is zero.
    DEADLINE_DIFF = (
        "@@ -560,6 +560,24 @@ class Retry:\n"
        "+    def _sleep_within_deadline(self, duration: float) -> None:\n"
        "+        remaining = self._deadline_time_remaining()\n"
        "+        if remaining is not None and duration >= remaining:\n"
        "+            raise MaxRetryError(None, None, ResponseError(\"backoff_deadline would be exceeded\"))\n"
        "+\n"
        "+        time.sleep(duration)\n"
        "@@ -380,7 +584,7 @@ class Retry:\n"
        "         backoff = self.get_backoff_time()\n"
        "         if backoff <= 0:\n"
        "             return\n"
        "-        time.sleep(backoff)\n"
        "+        self._sleep_within_deadline(backoff)\n")

    def _conformance_env(self):
        env = patch.dict(os.environ, {"BRIDGE_SEMANTIC": "1", "BRIDGE_CLAUDE_BIN": "/nonexistent/claude"})
        env.start()
        self.addCleanup(env.stop)

    def test_missing_callable_outside_diff_is_uncertain_not_a_departure(self):
        from bridge import llm
        self._conformance_env()
        diff = ('diff --git a/retry.py b/retry.py\n'
                '@@ -1,2 +1,3 @@ def new(self):\n'
                '+    params["jitter"] = self.jitter\n'
                '     params.update(kw)\n'
                'diff --git a/tests/test_retry.py b/tests/test_retry.py\n'
                '+assert retry.increment().jitter == 4\n')
        raw = {'status': 'complete', 'requirements':[{'needs':'Thread jitter through `increment()`',
               'kind':'must','found':'missing','at':'params.update(kw)'}],
               'why':'increment() does not copy jitter.'}
        with patch.object(llm.Client,'complete_json',return_value=raw):
            read = source_conformance(Config(),'Copy the option?', 'Carry jitter through increment().',diff)
        self.assertEqual(read['verdict'],'unclear')
        self.assertIn('increment',read['requirements'][0]['note'])
        self.assertNotIn('does not copy',read['why'])

    def test_missing_callable_shown_in_hunk_context_remains_a_departure(self):
        from bridge import llm
        self._conformance_env()
        diff = ('diff --git a/retry.py b/retry.py\n'
                '@@ -1,2 +1,3 @@ def increment(self):\n'
                '+    params["total"] = total\n'
                '     params.update(kw)\n')
        raw = {'status': 'complete', 'requirements':[{'needs':'Thread jitter through `increment()`',
               'kind':'must','found':'missing','at':'params.update(kw)',
               'allegation': {'kind': 'behavioral', 'authorized': 'Carry jitter through increment().',
                   'input': 'A retry object has jitter 4; call increment().',
                   'sequence': ['Build the copied params.', 'Pass them to the new retry object.'],
                   'expected': 'The copied jitter is 4.', 'observed': 'The copied params omit jitter.'}}]}
        with patch.object(llm.Client,'complete_json',return_value=raw):
            read = source_conformance(Config(),'Copy the option?', 'Carry jitter through increment().',diff)
        self.assertEqual(read['verdict'],'departs')

    def test_honored_needs_a_line_that_is_in_what_the_diff_leaves(self):
        """Measured live on eb9d22d: "when time left is zero or negative, no
        further retry is permitted" read as honored with nothing located,
        and the change let the next attempt through."""
        from bridge import llm
        self._conformance_env()
        need = "when time left is zero or negative, no further retry is permitted"
        cases = [
            ("if remaining is not None and duration >= remaining:", "must", "follows", ""),
            # With its marker and its own spacing, still the line.
            ("+        if remaining is not None and duration >= remaining:", "must", "follows", ""),
            # Elided in the middle, the pieces in order.
            ("remaining = self._deadline_time_remaining() ... raise MaxRetryError(", "must", "follows", ""),
            # A paraphrase is not a quote.
            ("if the remaining budget is spent, raise MaxRetryError", "must", "unclear",
             "the line quoted for it is not in the diff"),
            # Only in a removed line: what the change took out, not what it does.
            ("time.sleep(backoff)", "must", "unclear", "the line quoted for it is not in the diff"),
            ("", "must", "unclear", "read as honored with no line of the diff to show for it"),
            # Something ruled out and nowhere to be seen is the diff not doing it.
            ("", "must_not", "follows", "no line of the diff quoted for it; read as the diff not doing it"),
        ]
        for at, kind, want, note in cases:
            reply = {"status": "complete", "requirements": [{"needs": need, "kind": kind, "at": at, "found": "honored"}], "why": "w"}
            with patch.object(llm.Client, "complete_json",
                              lambda self, purpose, *a, **k: reply if purpose == "conformance" else ({"status": "complete", "conditions": []} if purpose == "conditions" else {"status": "complete", "checks": []})):
                read = source_conformance(Config(), "q", "a", self.DEADLINE_DIFF)
            self.assertEqual(read["verdict"], want, at)
            self.assertEqual(read["requirements"][0].get("note", ""), note, at)

    def test_a_file_requirement_is_honored_by_the_diffs_own_file_header(self):
        """Measured live on 63eb671: "use a descriptive unassigned filename"
        quoted the diff's own `diff --git` header for the new changelog
        fragment, the line was in the diff, and the check said it was not."""
        from bridge import llm
        self._conformance_env()
        diff = ("diff --git a/changelog/backoff-deadline.feature.rst b/changelog/backoff-deadline.feature.rst\n"
                "new file mode 100644\n--- /dev/null\n+++ b/changelog/backoff-deadline.feature.rst\n"
                "@@ -0,0 +1 @@\n+Added an optional ``backoff_deadline`` parameter to ``Retry``.\n")
        header = "diff --git a/changelog/backoff-deadline.feature.rst b/changelog/backoff-deadline.feature.rst"
        cases = [
            ("Use a descriptive unassigned local filename for this feature", "must", header, "follows"),
            ("Use a descriptive unassigned local filename for this feature", "must",
             "changelog/backoff-deadline.feature.rst", "follows"),
            # A file the diff does not have is still not there.
            ("Use a descriptive unassigned local filename for this feature", "must",
             "diff --git a/changelog/5277.feature.rst b/changelog/5277.feature.rst", "unclear"),
            # A header is not evidence for what the code does.
            ("Negative values raise ValueError", "must", header, "unclear"),
        ]
        for needs, kind, at, want in cases:
            reply = {"status": "complete", "requirements": [{"needs": needs, "kind": kind, "at": at, "found": "honored"}], "why": "w"}
            with patch.object(llm.Client, "complete_json",
                              lambda self, purpose, *a, **k: reply if purpose == "conformance" else ({"status": "complete", "conditions": []} if purpose == "conditions" else {"status": "complete", "checks": []})):
                read = source_conformance(Config(), "q", needs, diff)
            self.assertEqual(read["verdict"], want, (needs, at))
            self.assertNotIn("note", read["requirements"][0]) if want == "follows" else None

    def test_a_counterexample_makes_a_met_requirement_unclear_and_leads_the_reason(self):
        """The eb9d22d case end to end: read as honored at the new check, and
        broken by the unchanged early return two lines above it."""
        from bridge import llm
        self._conformance_env()
        need = "When time left is zero or negative, no further retry is permitted"
        first = {"status": "complete", "requirements": [
            {"needs": need, "kind": "must", "found": "honored",
             "at": "if remaining is not None and duration >= remaining:"},
            {"needs": "Use the existing MaxRetryError exhaustion path", "kind": "must", "found": "honored",
             "at": "raise MaxRetryError(None, None, ResponseError(\"backoff_deadline would be exceeded\"))"}],
            "why": "The diff raises MaxRetryError when the wait would not fit."}
        broke = {"status": "complete", "checks": [
            {"n": 1, "counterexample": "sleep() with a zero backoff after the budget ran out returns at the early "
                                       "return and never reaches _sleep_within_deadline, so the next attempt goes ahead",
             "at": "if backoff <= 0:", "not_shown": "the connection pool loop that calls increment() and sleep()",
             "allegation": {"kind": "behavioral", "authorized": "when time left is zero or negative, no further retry is permitted",
                 "input": "sleep() with a zero backoff after the budget ran out",
                 "sequence": ["Enter sleep with backoff 0.", "Take the early return before the deadline check."],
                 "expected": "Refuse the further retry.", "observed": "sleep returns without checking the exhausted deadline."}},
            {"n": 2, "counterexample": "", "at": "", "not_shown": ""}]}
        prompts = {}

        def complete_json(self, purpose, system, prompt, **kw):
            if purpose == "conditions":
                return {"status": "complete", "conditions": []}
            prompts[purpose] = (system, prompt)
            return first if purpose == "conformance" else ({"status": "complete", "conditions": []} if purpose == "conditions" else broke)
        with patch.object(llm.Client, "complete_json", complete_json):
            read = source_conformance(Config(), "When the budget is spent, how does that surface?",
                                         "Through MaxRetryError; when time left is zero or negative, no further "
                                         "retry is permitted.", self.DEADLINE_DIFF)
        self.assertEqual(read["verdict"], "unclear")
        spent, path = read["requirements"]
        self.assertEqual(spent["state"], "unclear")
        self.assertEqual(spent["counterexample"]["at"], "if backoff <= 0:")
        self.assertTrue(spent["counterexample"]["located"])
        self.assertEqual(path["state"], "unclear")  # The whole shared source is unresolved.
        self.assertNotIn("counterexample", path)
        grounded_need = spent['needs']
        self.assertTrue(read["why"].startswith(f'Possible counterexample to "{grounded_need}": sleep() with a zero backoff'),
                        read["why"])
        self.assertIn("(at `if backoff <= 0:`)", read["why"])
        self.assertIn("The first reading:", read["why"])
        self.assertIn("The diff raises MaxRetryError", read["why"])
        self.assertIn("the connection pool loop that calls increment() and sleep()", read["unexamined"])
        self.assertTrue(read["incomplete"])  # The retained legacy critic lacks source bindings.
        # The search is told to walk the paths around the quoted line, context lines included.
        system, prompt = prompts["counterexample"]
        self.assertIn("every early return", system)
        self.assertIn("Context lines count", system)
        self.assertIn("1. ORIGINAL SOURCE", prompt)
        self.assertIn(need.lower(), prompt.lower())
        self.assertIn("at: if remaining is not None and duration >= remaining:", prompt)
        # The path it missed is put in front of it: the guarded exits the
        # diff shows, unchanged lines included, and never a removed line.
        self.assertIn("EARLY EXITS THE DIFF SHOWS (added or unchanged):\n1. if backoff <= 0: -> return\n", prompt)
        self.assertIn("A later check that would catch it afterwards does not rescue a path", system)

    def test_a_counterexample_whose_line_is_not_in_the_changes_code_is_listed_not_counted(self):
        """Measured live on the conformance panel: a path through a branch
        the diff does not show, pinned to a test's name, turned a correct
        reading unclear."""
        from bridge import llm
        self._conformance_env()
        diff = (self.DEADLINE_DIFF + "diff --git a/test/test_retry.py b/test/test_retry.py\n"
                "+++ b/test/test_retry.py\n@@ -1,0 +1,2 @@\n+def test_spent_budget_refuses_the_sleep(self):\n"
                "+    assert refused\n")
        first = {"status": "complete", "requirements": [{"needs": "n", "kind": "must", "found": "honored",
                                   "at": "if remaining is not None and duration >= remaining:"}], "why": "w"}
        for at in ("def test_spent_budget_refuses_the_sleep(self):", "elif error: other -= 1"):
            broke = {"status": "complete", "checks": [{"n": 1, "counterexample": "an SSLError goes to the other branch", "at": at}]}
            with patch.object(llm.Client, "complete_json",
                              lambda self, purpose, *a, **k: first if purpose == "conformance" else ({"status": "complete", "conditions": []} if purpose == "conditions" else broke)):
                read = source_conformance(Config(), "q", "a", diff)
            self.assertEqual(read["verdict"], "unclear", at)
            self.assertNotIn("counterexample", read["requirements"][0])
            self.assertTrue(any("grounded witness" in limitation for limitation in read["unexamined"]))
            self.assertIn("inconclusive", read["why"])

    def test_an_early_exit_judged_to_let_a_requirement_through_is_a_counterexample(self):
        """Measured live: asked for counterexamples in general, the search
        named the zero-backoff return in one run of four. Each listed exit
        now gets its own verdict."""
        from bridge import llm
        self._conformance_env()
        need = "When time left is zero or negative, no further retry is permitted"
        first = {"status": "complete", "requirements": [
            {"needs": "Use the existing MaxRetryError exhaustion path", "kind": "must", "found": "honored",
             "at": "raise MaxRetryError(None, None, ResponseError(\"backoff_deadline would be exceeded\"))"},
            {"needs": need, "kind": "must", "found": "honored",
             "at": "if remaining is not None and duration >= remaining:"}], "why": "w"}
        judged = {"status": "complete", "checks": [{"n": 1, "counterexample": ""}, {"n": 2, "counterexample": ""}],
                  "exits": [{"exit": 1, "breaks": 1, "how": "sleep() with backoff 0 after the budget ran out returns "
                                                            "here and the next attempt goes ahead",
                             "allegation": {"kind": "behavioral", "authorized": need,
                                 "input": "sleep() with backoff 0 after the budget ran out",
                                 "sequence": ["Enter sleep.", "Take the early return before checking the deadline."],
                                 "expected": "Refuse further retries.", "observed": "sleep returns without checking the spent budget."}},
                            {"exit": 9, "breaks": 1, "how": "no such exit"},
                            {"exit": 1, "breaks": 0, "how": ""}]}
        with patch.object(llm.Client, "complete_json",
                          lambda self, purpose, *a, **k: first if purpose == "conformance" else ({"status": "complete", "conditions": []} if purpose == "conditions" else judged)):
            read = source_conformance(Config(), "q", need, self.DEADLINE_DIFF)
        self.assertEqual(read["verdict"], "unclear")
        spent, path = read["requirements"]
        self.assertNotIn("counterexample", path)
        self.assertTrue(spent["counterexample"]["located"])
        self.assertEqual(spent["counterexample"]["at"], "if backoff <= 0:")
        self.assertEqual(spent["counterexample"]["allegation"], judged["exits"][0]["allegation"])
        self.assertTrue(read["why"].startswith(f'Possible counterexample to "{need}"'), read["why"])

    def test_a_failed_counterexample_search_cannot_report_follows(self):
        from bridge import llm
        self._conformance_env()
        first = {"status": "complete", "requirements": [{"needs": "n", "kind": "must", "found": "honored",
                                   "at": "if remaining is not None and duration >= remaining:"}], "why": "w"}

        def complete_json(self, purpose, system, prompt, **kw):
            if purpose == "conditions":
                return {"status": "complete", "conditions": []}
            if purpose == "counterexample":
                raise llm.LLMError("timed out")
            return first
        with patch.object(llm.Client, "complete_json", complete_json):
            read = source_conformance(Config(), "q", "a", self.DEADLINE_DIFF)
        self.assertEqual(read["verdict"], "unclear")
        self.assertIn("counterexample search did not complete", read["why"])
        self.assertEqual(read["unexamined"], ["no search for counterexamples: the model did not answer"])

    def test_what_a_long_diff_leaves_unread_is_named(self):
        from bridge import llm
        self._conformance_env()
        diff = self.DEADLINE_DIFF + "+# padding\n" * 3000
        reply = {"status": "complete", "requirements": [{"needs": "n", "kind": "must", "found": "unseen"}], "why": "w"}
        with patch.object(llm.Client, "complete_json", lambda self, *a, **k: reply):
            read = source_conformance(Config(), "q", "a", diff)
        self.assertEqual(read["unexamined"], [f"the diff past its first {llm.DIFF_READ} characters ({len(diff)} given)"])

    def test_the_finish_names_counterexamples_and_calls_the_reading_a_model_reading(self):
        """Measured live on eb9d22d: the finish said "6 are in it" and the
        host reported that all six followed."""
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        node = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "paths": "hw/riscv/virt.c",
                                                      "question": "Should hw/riscv/virt.c change the default interrupt controller?"})
        row = self.store.get_decision(node["node_id"])
        self.store.answer(node["node_id"], {"answer": "Use the aplic", "rationale": "r", "signed_by": "Oriel Vance",
                                            "expected_updated_at": row["updated_at"]})
        reqs = [{"needs": "aplic is the default", "kind": "must", "found": "honored", "state": "unclear",
                 "at": "use_aplic = true;",
                 "counterexample": {"what": "a machine built with -M virt,aia=none still takes the plic path",
                                    "at": "if (!s->aia) {", "located": True}}]
        read = {"verdict": "unclear", "why": "Possible counterexample", "requirements": reqs,
                "unexamined": ["virt_machine_init, which the diff does not show"]}
        with patch("bridge.llm.check_conformance", return_value=read):
            done = canvas.finish_task(self.store, {"task_id": t["task_id"], "diff": "+  use_aplic = true;"})
        self.assertEqual(done["follows"][0]["unexamined"], ["virt_machine_init, which the diff does not show"])
        caveat = done["caveat"]
        self.assertIn("A model read the diff you supplied against 1 of them", caveat)
        self.assertIn(f"possible counterexample for 1 ({node['node_id']})", caveat)
        self.assertIn("Check each one before you report the change as following it", caveat)
        self.assertIn(f"depend on code the diff does not show ({node['node_id']})", caveat)
        self.assertIn("not a test run", caveat)
        self.assertNotIn("are in it", caveat)
        event = [e for e in self.store.state()["events"] if e["kind"] == "conformance_read"][0]
        self.assertEqual(json.loads(event["detail"])["read"][0]["counterexamples"], 1)

    def test_a_slow_reading_is_kept_and_read_later_instead_of_lost(self):
        """Measured live on 5e967e4: three finish calls timed out at 60 seconds
        while the diff was read, the hosts never saw the departure it found,
        and a retry would have read it all again."""
        import threading
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        node = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "paths": "hw/riscv/virt.c",
                                                      "question": "Should hw/riscv/virt.c change the default interrupt controller?"})
        row = self.store.get_decision(node["node_id"])
        self.store.answer(node["node_id"], {"answer": "Use the aplic", "rationale": "r", "signed_by": "Oriel Vance",
                                            "expected_updated_at": row["updated_at"]})
        release, calls = threading.Event(), []

        def slow(cfg, question, answer, diff, others=()):
            calls.append(question)
            release.wait(10)
            return {"verdict": "departs", "why": "the default stays the plic", "requirements": []}
        diff = "+  use_plic = true;"
        with patch("bridge.llm.check_conformance", side_effect=slow), patch.object(canvas, "FINISH_WAIT", 0.3):
            first = canvas.finish_task(self.store, {"task_id": t["task_id"], "diff": diff})
            self.assertEqual(first["status"], "completed")
            self.assertEqual(first["review"]["status"], "running")
            self.assertEqual(first["follows"], [])
            self.assertIn("still reading the diff", first["caveat"])
            self.assertIn("read it on bridge_get_tree (review)", first["caveat"])
            running = canvas.get_tree(self.store, t["task_id"])["review"]
            self.assertEqual((running["id"], running["status"]), (first["review"]["id"], "running"))
            # A retry while it runs waits on the same reading; it does not start another.
            again = canvas.finish_task(self.store, {"task_id": t["task_id"], "diff": diff})
            self.assertEqual(again["review"]["id"], first["review"]["id"])
            release.set()
            for _ in range(100):
                done = canvas.get_tree(self.store, t["task_id"])["review"]
                if done["status"] == "done":
                    break
                time.sleep(0.05)
        self.assertEqual(done["status"], "done")
        self.assertEqual([f["verdict"] for f in done["follows"]], ["departs"])
        # Read back, not read again: the model was asked once.
        later = canvas.finish_task(self.store, {"task_id": t["task_id"], "diff": diff})
        self.assertEqual([f["verdict"] for f in later["follows"]], ["departs"])
        self.assertEqual(later["review"]["status"], "done")
        self.assertIn("1 depart from what was signed", later["caveat"])
        self.assertEqual(len(calls), 1)
        # Another diff is another reading.
        with patch("bridge.llm.check_conformance", return_value={"verdict": "follows", "why": "w", "requirements": []}):
            changed = canvas.finish_task(self.store, {"task_id": t["task_id"], "diff": "+  use_aplic = true;"})
        self.assertNotEqual(changed["review"]["id"], first["review"]["id"])
        self.assertEqual([f["verdict"] for f in changed["follows"]], ["follows"])

    def test_a_kept_reading_is_stale_once_a_signed_answer_it_read_changes(self):
        """Measured live on 63eb671: after the task finished, the owner
        reversed a signed answer and it was signed again, and the tree went
        on showing the old reading as done, the change following it."""
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        node = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "paths": "hw/riscv/virt.c",
                                                      "question": "Should hw/riscv/virt.c change the default interrupt controller?"})
        row = self.store.get_decision(node["node_id"])
        self.store.answer(node["node_id"], {"answer": "Use the aplic", "rationale": "r", "signed_by": "Oriel Vance",
                                            "expected_updated_at": row["updated_at"]})
        diff = "+  use_aplic = true;"
        follows = {"verdict": "follows", "why": "the aplic is the default", "requirements": []}
        with patch("bridge.llm.check_conformance", return_value=follows):
            done = canvas.finish_task(self.store, {"task_id": t["task_id"], "diff": diff})
        tree = canvas.get_tree(self.store, t["task_id"])
        self.assertEqual(tree["review"]["status"], "done")
        self.assertEqual(tree["next"], "no node waits on anyone")
        # The owner reverses the answer after the finish.
        row = self.store.get_decision(node["node_id"])
        self.store.answer(node["node_id"], {"answer": "Keep the plic as the default", "rationale": "r",
                                            "signed_by": "Oriel Vance", "expected_updated_at": row["updated_at"]})
        tree = canvas.get_tree(self.store, t["task_id"])
        review = tree["review"]
        self.assertEqual((review["status"], review["read_status"], review["id"]), ("stale", "done", done["review"]["id"]))
        self.assertEqual([(x["node_id"], x["why"]) for x in review["stale"]],
                         [(node["node_id"], "its signed answer changed after this reading")])
        self.assertTrue(review["follows"][0]["stale"])
        self.assertIn(f"read an earlier answer to 1 decision ({node['node_id']}): call bridge_finish_task again",
                      tree["next"])
        # Read again against the answers as they stand: current, not stale.
        departs = {"verdict": "departs", "why": "the diff makes the aplic the default", "requirements": []}
        with patch("bridge.llm.check_conformance", return_value=departs):
            again = canvas.finish_task(self.store, {"task_id": t["task_id"], "diff": diff})
        self.assertNotEqual(again["review"]["id"], done["review"]["id"])
        self.assertEqual([f["verdict"] for f in again["follows"]], ["departs"])
        tree = canvas.get_tree(self.store, t["task_id"])
        self.assertEqual(tree["review"]["status"], "done")
        self.assertNotIn("stale", tree["review"])
        # A decision signed after the reading (the task reopened) is one it
        # does not cover.
        self.store.update_run(t["task_id"], {"status": "working"})
        later = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "paths": "hw/riscv/virt.c",
                                                       "question": "Should hw/riscv/virt.c keep the legacy PLIC property?"})
        row = self.store.get_decision(later["node_id"])
        self.store.answer(later["node_id"], {"answer": "Keep it", "rationale": "r", "signed_by": "Oriel Vance",
                                             "expected_updated_at": row["updated_at"]})
        stale = canvas.get_tree(self.store, t["task_id"])["review"]
        self.assertEqual(stale["status"], "stale")
        self.assertEqual([(x["node_id"], x["why"]) for x in stale["stale"]],
                         [(later["node_id"], "it was signed after this reading, which does not cover it")])

    def test_a_reading_kept_before_revisions_were_recorded_is_checked_by_time(self):
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        node = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "paths": "hw/riscv/virt.c",
                                                      "question": "Should hw/riscv/virt.c change the default interrupt controller?"})
        row = self.store.get_decision(node["node_id"])
        self.store.answer(node["node_id"], {"answer": "Use the aplic", "rationale": "r", "signed_by": "Oriel Vance",
                                            "expected_updated_at": row["updated_at"]})
        with self.store.graph.transaction():
            self.store.graph.append_event("conformance_read", {
                "task_id": t["task_id"], "review_id": "legacy000001", "diff_hash": "d", "status": "done",
                "follows": [{"node_id": node["node_id"], "verdict": "follows", "why": "w", "requirements": []}]})
        self.assertEqual(canvas.get_tree(self.store, t["task_id"])["review"]["status"], "done")
        time.sleep(0.01)
        row = self.store.get_decision(node["node_id"])
        self.store.answer(node["node_id"], {"answer": "Keep the plic", "rationale": "r", "signed_by": "Oriel Vance",
                                            "expected_updated_at": row["updated_at"]})
        self.assertEqual(canvas.get_tree(self.store, t["task_id"])["review"]["status"], "stale")

    def test_each_decision_reads_its_own_files_first(self):
        from bridge import llm
        diff = ("diff --git a/changelog/1.rst b/changelog/1.rst\n+note\n"
                "diff --git a/test/test_virt.py b/test/test_virt.py\n+def test_x(): pass\n"
                "diff --git a/hw/riscv/virt.c b/hw/riscv/virt.c\n+  use_aplic = true;\n")
        focused = llm.focus_diff(diff, ["hw/riscv/virt.c"])
        self.assertTrue(focused.startswith("diff --git a/hw/riscv/virt.c"))
        self.assertLess(focused.index("changelog/1.rst"), focused.index("test/test_virt.py"))
        self.assertEqual(llm._unread_files(focused, len(focused.split("diff --git a/test")[0]) + 5),
                         ["test/test_virt.py (in part)"])

    def test_the_finish_names_changed_files_whose_decider_was_asked_nothing(self):
        """Measured live on 5e967e4: the agent wrote a user-guide section and
        picked a changelog fragment name, the docs decider was never asked,
        and nothing at the finish said so."""
        graph = self.store.graph
        docs = graph.add_person("Theo Release", email="theo@example.test")
        graph.add_authority("path", "docs/*", "decides", person_id=docs, repo="qemulike")
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        node = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "paths": "hw/riscv/virt.c",
                                                      "question": "Should hw/riscv/virt.c change the default interrupt controller?"})
        row = self.store.get_decision(node["node_id"])
        self.store.answer(node["node_id"], {"answer": "Use the aplic", "rationale": "r", "signed_by": "Oriel Vance",
                                            "expected_updated_at": row["updated_at"]})
        diff = ("diff --git a/hw/riscv/virt.c b/hw/riscv/virt.c\n+  use_aplic = true;\n"
                "diff --git a/docs/about.rst b/docs/about.rst\n+The board now uses the aplic by default.\n"
                "diff --git a/tests/test_virt.py b/tests/test_virt.py\n+def test_aplic(): pass\n")
        with patch("bridge.llm.check_conformance", return_value={"verdict": "follows", "why": "w", "requirements": []}):
            done = canvas.finish_task(self.store, {"task_id": t["task_id"], "diff": diff})
        self.assertEqual(done["uncovered"], [{"path": "docs/about.rst", "decider": "Theo Release", "scope": "docs/*"}])
        self.assertIn("The diff changes 1 file whose decider was asked nothing on this task: docs/about.rst "
                      "(Theo Release decides docs/*)", done["caveat"])
        # A decision about that area covers it, whoever it went to.
        t2 = self.start("Document the aplic default", paths="docs/about.rst")
        docs_node = canvas.add_node(self.store, self.cfg, {"task_id": t2["task_id"], "paths": "docs/about.rst",
                                                           "question": "Should docs/about.rst say the aplic is the default?"})
        row = self.store.get_decision(docs_node["node_id"])
        self.store.answer(docs_node["node_id"], {"answer": "Yes", "rationale": "r", "signed_by": "Theo Release",
                                                 "expected_updated_at": row["updated_at"]})
        with patch("bridge.llm.check_conformance", return_value={"verdict": "follows", "why": "w", "requirements": []}):
            covered = canvas.finish_task(self.store, {"task_id": t2["task_id"],
                                                      "diff": "diff --git a/docs/about.rst b/docs/about.rst\n+x\n"})
        self.assertEqual(covered["uncovered"], [])

    def engaged_with_docs(self):
        """An engaged task on hw/riscv/virt.c, one signed decision, and a
        docs decider nobody on the task was asked."""
        graph = self.store.graph
        docs = graph.add_person("Theo Release", email="theo@example.test")
        graph.add_authority("path", "docs/*", "decides", person_id=docs, repo="qemulike")
        t = self.start("Change the default interrupt controller in hw/riscv/virt.c", paths="hw/riscv/virt.c",
                       goal="Switch the default to the aplic. This changes the default for every board user.")
        self.assertEqual(t["verdict"], "engage", t["why"])
        node = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "paths": "hw/riscv/virt.c",
                                                      "question": "Should hw/riscv/virt.c change the default interrupt controller?"})
        row = self.store.get_decision(node["node_id"])
        self.store.answer(node["node_id"], {"answer": "Use the aplic", "rationale": "r", "signed_by": "Oriel Vance",
                                            "expected_updated_at": row["updated_at"]})
        diff = ("diff --git a/hw/riscv/virt.c b/hw/riscv/virt.c\n+  use_aplic = true;\n"
                "diff --git a/docs/about.rst b/docs/about.rst\n+The board now uses the aplic by default.\n"
                "diff --git a/tests/test_virt.py b/tests/test_virt.py\n+def test_aplic(): pass\n")
        return t, diff

    def test_an_engaged_task_does_not_finish_past_a_file_whose_decider_was_asked_nothing(self):
        """Measured live on 63eb671: the finish named the changelog fragment
        the agent had numbered itself and its decider, the host reported the
        number as a guess and stopped, and by then the task was finished: the
        decision could no longer be written."""
        t, diff = self.engaged_with_docs()
        with patch("bridge.llm.check_conformance", return_value={"verdict": "follows", "why": "w", "requirements": []}):
            with self.assertRaises(Invalid) as refused:
                canvas.finish_task(self.store, {"task_id": t["task_id"], "diff": diff})
            said = str(refused.exception)
            self.assertIn("1 file whose decider was asked nothing on this task: docs/about.rst (Theo Release decides "
                          "docs/*)", said)
            self.assertIn("bridge_add_node (paths=<the file>)", said)
            self.assertIn("uncovered set to one line per file", said)
            self.assertNotEqual(self.store.graph.get_task(t["task_id"])["status"], "completed")
            # A word is not a reason.
            with self.assertRaises(Invalid):
                canvas.finish_task(self.store, {"task_id": t["task_id"], "diff": diff,
                                                "uncovered": "docs/about.rst: fine"})
            reason = "the sentence restates the signed aplic default and settles nothing new"
            done = canvas.finish_task(self.store, {"task_id": t["task_id"], "diff": diff,
                                                   "uncovered": f"- docs/about.rst: {reason}"})
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["uncovered"], [{"path": "docs/about.rst", "decider": "Theo Release", "scope": "docs/*",
                                              "reason": reason}])
        self.assertIn("The agent says these changes settle nothing, so their deciders were not asked: docs/about.rst "
                      f"(Theo Release): {reason}", done["caveat"])
        self.assertNotIn("whose decider was asked nothing", done["caveat"])

    def test_a_node_for_the_file_lets_the_engaged_task_finish(self):
        t, diff = self.engaged_with_docs()
        docs_node = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "paths": "docs/about.rst",
                                                           "question": "Should docs/about.rst say the aplic is the default?"})
        self.assertEqual(canvas.node_view(self.store, docs_node["node_id"])["owner"], "Theo Release")
        row = self.store.get_decision(docs_node["node_id"])
        self.store.answer(docs_node["node_id"], {"answer": "Yes, one sentence", "rationale": "r",
                                                 "signed_by": "Theo Release", "expected_updated_at": row["updated_at"]})
        with patch("bridge.llm.check_conformance", return_value={"verdict": "follows", "why": "w", "requirements": []}):
            done = canvas.finish_task(self.store, {"task_id": t["task_id"], "diff": diff})
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["uncovered"], [])

    def test_a_typo_fix_is_not_asked_about_the_file_it_corrects(self):
        """Measured live on 63eb671: the typo task passed triage as a narrow
        edit, and its finish still said the docs decider was asked nothing,
        and told the agent to pass the diff it had passed."""
        graph = self.store.graph
        docs = graph.add_person("Theo Release", email="theo@example.test")
        graph.add_authority("path", "docs/*", "decides", person_id=docs, repo="qemulike")
        t = self.start("Fix a typo in docs/about.rst", paths="docs/about.rst",
                       goal="Keep it to one spelling or grammar correction with no behavioral change.")
        self.assertEqual(t["verdict"], "pass", t["why"])
        done = canvas.finish_task(self.store, {"task_id": t["task_id"], "diff": (
            "diff --git a/docs/about.rst b/docs/about.rst\n-it is highly recommend\n+it is highly recommended\n")})
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["uncovered"], [])
        self.assertNotIn("asked nothing", done["caveat"])
        self.assertIn("No decision on this task was signed, so there was nothing to read the diff you supplied "
                      "against", done["caveat"])
        self.assertNotIn("pass `diff`", done["caveat"])

    def test_a_reason_is_taken_from_the_line_that_names_the_file(self):
        uncovered = [{"path": "docs/index.rst"}, {"path": "docs/api/index.rst"}, {"path": "changelog/5277.feature.rst"}]
        got = canvas._justifications("docs/api/index.rst: a cross reference only, nothing is settled\n"
                                     "* 5277.feature.rst: ok\n"
                                     "docs/index.rst: the toctree gains the new page and nothing else", uncovered)
        self.assertEqual(got, {"docs/api/index.rst": "a cross reference only, nothing is settled",
                               "docs/index.rst": "the toctree gains the new page and nothing else"})
        # One file, one reason, however it is written.
        self.assertEqual(canvas._justifications("only reflows a paragraph of prose", uncovered[:1]),
                         {"docs/index.rst": "only reflows a paragraph of prose"})

    def test_without_a_diff_the_finish_says_it_read_nothing(self):
        _, done = self.finished()
        self.assertEqual(done["follows"], [])
        self.assertIn("did not read the diff", done["caveat"])
        self.assertIn("pass `diff`", done["caveat"])

    def test_a_diff_with_no_model_to_read_it_is_not_asked_for_again(self):
        """Measured on the Docker install, which has no model by default:
        the agent passed its diff and was told to pass its diff."""
        with patch("bridge.llm.check_conformance", return_value=None):
            _, done = self.finished(diff="--- a/hw/riscv/virt.c\n+++ b/hw/riscv/virt.c\n+  use_aplic = true;")
        self.assertEqual(done["follows"], [])
        self.assertIn("did not read the diff you supplied", done["caveat"])
        self.assertIn("ANTHROPIC_API_KEY", done["caveat"])
        self.assertNotIn("pass `diff`", done["caveat"])

    def test_a_long_test_log_is_kept_cut_not_refused(self):
        """Measured live: a host's finish was refused for a 2000-character
        checks string, and it had to shorten its own claim and retry."""
        log = "pytest test/test_retry.py -q: " + "." * 2500 + " 196 passed"
        _, done = self.finished(checks=log)
        self.assertEqual(done["status"], "completed")
        self.assertTrue(done["checks"].startswith("pytest test/test_retry.py -q: ...."))
        self.assertIn(f"[cut by Raven: {len(log)} characters given, the first 2000 kept]", done["checks"])

    def test_the_checks_an_agent_ran_are_recorded_as_its_claim(self):
        _, done = self.finished(checks="go test ./pkg/... passed, 412 tests")
        self.assertEqual(done["checks"], "go test ./pkg/... passed, 412 tests")
        self.assertIn("what the agent says it ran: go test", done["caveat"])
        self.assertFalse(done["verified"])
        events = [e for e in self.store.state()["events"] if e["kind"] == "checks_reported"]
        self.assertEqual(len(events), 1)
        self.assertIn("412 tests", events[0]["detail"])


class ModelTriageTests(CanvasCase):
    """The fast model advises the kickoff verdict when a key is set. The
    two mistakes it can make are not the same size: engaging a task for
    nothing costs an agent one read, and passing one wrongly is a
    decision nobody was ever asked about. So it may raise a verdict and
    not lower one."""

    def kickoff(self, title, advice, paths="hw/net/virtio-net.c"):
        env = patch.dict(os.environ, {"BRIDGE_SEMANTIC": "1", "BRIDGE_CLAUDE_BIN": "/nonexistent/claude"})
        env.start()
        self.addCleanup(env.stop)
        with patch("bridge.llm.model_triage", return_value=advice):
            result = canvas.start_task(self.store, Config(), {
                "title": title, "repo": "qemulike", "agent": "test", "paths": paths,
                "requester": "Tamsin Reed <tamsin@synthco.example>"})
            self.assertTrue(canvas.wait_for_background(10))
            return canvas.start_task(self.store, Config(), {"task_id": result["task_id"]})

    def test_the_model_may_engage_a_task_the_rules_passed(self):
        t = self.kickoff("Add the imsics compatible string to hw/riscv/virt.c",
                         {"verdict": "engage", "why": "this quietly changes a default guests depend on"},
                         paths="hw/riscv/virt.c")
        self.assertEqual(t["verdict"], "engage")
        self.assertIn("quietly changes a default", t["why"])
        self.assertEqual(t["discovery"]["verdict_source"], "model")
        self.assertEqual(t["discovery"]["verdict_rules"], "pass")

    def test_the_model_may_not_pass_a_task_the_rules_engaged(self):
        t = self.kickoff("Remove the legacy virtio-net header, breaking compat for old guests",
                         {"verdict": "pass", "why": "routine cleanup in an owned area"})
        self.assertEqual(t["verdict"], "engage", t["why"])
        self.assertIn("the model would have passed this", t["why"])
        self.assertIn("routine cleanup", t["why"])
        # The record keeps both readings, and says which one stands.
        self.assertEqual(t["discovery"]["verdict_source"], "rules")
        self.assertEqual(t["discovery"]["verdict_model"], "pass")

    def test_the_model_cannot_pass_a_task_nobody_has_placed(self):
        """With no paths and no area the model read the same empty digest;
        its pass knows no more than the rules did."""
        t = self.kickoff("Add a lease expiry", {"verdict": "pass", "why": "routine hardening"}, paths="")
        self.assertEqual(t["verdict"], "unplaced", t["why"])
        self.assertEqual(t["discovery"]["verdict_model"], "pass")
        self.assertNotIn("engages anyway", t["why"])
        engaged = self.kickoff("Add a timer lease expiry", {"verdict": "engage", "why": "leases change for guests"},
                               paths="")
        self.assertEqual(engaged["verdict"], "engage")

    def test_agreement_is_recorded_as_agreement(self):
        t = self.kickoff("Remove the legacy virtio-net header, breaking compat for old guests",
                         {"verdict": "engage", "why": "old guests break"})
        self.assertEqual(t["verdict"], "engage")
        self.assertIn("the model agrees: old guests break", t["why"])


class KickoffTests(CanvasCase):
    def test_a_task_bridge_cannot_place_is_unplaced_and_judged_once_the_agent_names_the_files(self):
        """Kicked off before any work, as the protocol asks, an agent has no
        paths yet. Measured on a real task, a pass here sent the agent off
        to settle a token lifetime alone in an area whose owner was on the
        map. Unplaced says Raven has judged nothing; the second call with
        the files is judged."""
        goal = "The machine's timers never expire their leases. Add an expiry to them."
        t = self.start("Add a lease expiry", goal=goal, client_key="lease-1")
        self.assertEqual(t["verdict"], "unplaced", t["why"])
        self.assertIn("not a pass", t["why"])
        self.assertIn("again with the same title, repo and client_key", t["next"])
        placed = self.start("Add a lease expiry", goal=goal, client_key="lease-1", paths="hw/riscv/virt.c")
        self.assertEqual(placed["task_id"], t["task_id"])
        self.assertEqual(placed["verdict"], "engage", placed["why"])
        self.assertIn("Oriel Vance", placed["why"])
        self.assertIn("expire", placed["why"])
        self.assertEqual(self.store.graph.count_events("task_placed"), 1)
        # A retry of the placed call is a retry: the same task, not judged again.
        again = self.start("Add a lease expiry", goal=goal, client_key="lease-1", paths="hw/riscv/virt.c")
        self.assertTrue(again["repeated"])
        self.assertEqual(again["verdict"], "engage")
        self.assertEqual(self.store.graph.count_events("task_placed"), 1)

    def test_a_narrow_typo_fix_passes_whatever_is_decided_or_pending_nearby(self):
        """Measured live on eb9d22d: a one-word grammar fix engaged on a
        pending docs decision, a prior one and the word "release" in the
        task's facts; the agent asked an owner which typo to fix and waited
        four and a half minutes."""
        owner = self.store.graph.add_person("Docs Owner", email="docs@example.test")
        self.store.graph.add_authority("path", "docs/*", "decides", person_id=owner, repo="qemulike")
        earlier = self.start("Document the release process", paths="docs/about.rst")
        for ref, question in (("a", "What documentation must ship for a new machine type in release=next?"),
                              ("b", "Should docs/about.rst describe the release cadence?")):
            node = canvas.add_node(self.store, self.cfg, {"task_id": earlier["task_id"], "client_ref": ref,
                                                          "question": question, "paths": "docs/about.rst"})
            if ref == "a":
                row = self.store.get_decision(node["node_id"])
                self.store.answer(node["node_id"], {"answer": "A changelog entry and the machine page",
                                                    "rationale": "r", "signed_by": "Docs Owner",
                                                    "expected_updated_at": row["updated_at"]})
        goal = ("Fix a typo in docs/about.rst. Keep it to one spelling or grammar correction with no behavioral "
                "change. facts: release=next.")
        t = self.start("Fix a typo in docs/about.rst", goal=goal, paths="docs/about.rst")
        self.assertEqual(t["verdict"], "pass", t["why"])
        self.assertTrue(t["why"].startswith('the task is a typo fix and says "no behavioral change"'), t["why"])
        self.assertEqual(t["candidates"], [])
        self.assertIn("make the edit yourself", t["next"])
        self.assertIn("do not ask anyone to choose", t["next"])
        # A change to the same file that decides something still engages.
        c = self.start("Drop the release cadence section from docs/about.rst",
                       goal="Remove the release cadence section from docs/about.rst; we no longer publish one.",
                       paths="docs/about.rst")
        self.assertEqual(c["verdict"], "engage", c["why"])
        # So does a typo fix that asks a question.
        q = self.start("Fix a typo in docs/about.rst", goal="Fix a typo in docs/about.rst. Should the page also say "
                       "which releases it covers?", paths="docs/about.rst", client_key="typo-question")
        self.assertEqual(q["verdict"], "engage", q["why"])

    def test_a_fact_key_is_not_the_task_speaking_of_it(self):
        signals = canvas.judgment_signals({}, "Tidy the board setup",
                                          "Tidy hw/riscv/virt.c. facts: release=next, customer=acme.")
        self.assertEqual(signals, [])
        self.assertEqual([w for w, _ in canvas.judgment_signals({}, "Tidy the board setup",
                                                                "Change the default in hw/riscv/virt.c for "
                                                                "release=next")], ["default"])

    def test_the_kickoff_names_a_decider_the_map_records_inside_the_area(self):
        """Measured live on urllib3: the task mapped to a directory, the map
        named a decider for a file inside it, and the kickoff said nobody
        was known and the questions would go unrouted."""
        graph = self.store.graph
        owner = graph.add_person("Retry Policy Owner", email="retry@example.test")
        graph.add_authority("path", "hw/riscv/virt.c", "decides", person_id=owner, repo="qemulike")
        found = canvas._deciders_inside(graph, "qemulike", ["hw/riscv/"])
        self.assertEqual([p["name"] for p in found], ["Retry Policy Owner"])
        self.assertEqual(found[0]["inside"], "hw/riscv/virt.c")
        self.assertEqual(canvas._deciders_inside(graph, "qemulike", ["hw/net/"]), [])
        verdict, why = canvas.triage({"areas": [{"path": "hw/riscv/"}], "people": found},
                                     "Change the virt board default", "Change the default interrupt controller.")
        self.assertEqual(verdict, "engage")
        self.assertIn("Retry Policy Owner, who decides for hw/riscv/virt.c in this area", why)
        self.assertNotIn("does not know who owns this", why)

    def test_a_second_look_never_passes_an_engaged_task(self):
        t = self.start("Remove the legacy virtio-net header, breaking compat for old guests",
                       paths="hw/net/virtio-net.c", client_key="engaged-1")
        self.assertEqual(t["verdict"], "engage", t["why"])
        again = self.start("Remove the legacy virtio-net header, breaking compat for old guests",
                           paths="hw/riscv/virt.c", client_key="engaged-1")
        self.assertTrue(again["repeated"])
        self.assertEqual(again["verdict"], "engage")

    def test_a_pass_in_an_owned_area_names_the_owner_for_what_the_task_left_open(self):
        t = self.start("Add the imsics compatible string to hw/riscv/virt.c", paths="hw/riscv/virt.c")
        self.assertEqual(t["verdict"], "pass", t["why"])
        self.assertIn("a default, a limit, a lifetime", t["next"])
        self.assertIn("for Oriel Vance", t["next"])

    def test_a_routine_change_in_an_owned_area_is_passed(self):
        t = self.start("Add the imsics compatible string to hw/riscv/virt.c", paths="hw/riscv/virt.c")
        self.assertEqual(t["verdict"], "pass", t["why"])
        self.assertIn("Oriel Vance", t["why"])
        self.assertEqual(t["discovery"]["people"][0]["name"], "Oriel Vance")
        self.assertTrue(any(a["path"] == "hw/riscv/virt.c" for a in t["discovery"]["areas"]))

    def test_a_change_that_speaks_of_policy_engages_the_owner(self):
        t = self.start("Remove the legacy virtio-net header, breaking compat for old guests",
                       paths="hw/net/virtio-net.c")
        self.assertEqual(t["verdict"], "engage", t["why"])
        self.assertIn("Kwame Asante", t["why"])
        self.assertIn("compat", t["why"])

    def test_a_change_across_two_owners_engages(self):
        t = self.start("Wire the riscv virt board to the net device model", paths="hw/riscv/virt.c, hw/net/virtio-net.c")
        self.assertEqual(t["verdict"], "engage", t["why"])
        self.assertIn("different people", t["why"])

    def test_a_pending_question_on_the_same_paths_engages(self):
        first = self.start("Rework hw/riscv/boot.c firmware loading, dropping the old path", paths="hw/riscv/boot.c")
        node = canvas.add_node(self.store, self.cfg, {"task_id": first["task_id"],
                                                     "question": "Should hw/riscv/boot.c keep loading the old firmware layout?",
                                                     "paths": "hw/riscv/boot.c"})
        self.assertEqual(node["status"], "pending")
        second = self.start("Tidy hw/riscv/boot.c comments", paths="hw/riscv/boot.c")
        self.assertEqual(second["verdict"], "engage", second["why"])
        self.assertIn("already pending", second["why"])

    def test_an_area_with_no_owner_still_engages(self):
        """Whether a change is a call for somebody is a property of the
        change. Not knowing who to ask is a gap in the map, and passing
        on it is how a compatibility question gets decided by an agent
        because the repository never named a person."""
        bare = self.warm_store("qemulike", "nobody.db")
        # A repository Raven has read but that names nobody: the tree is
        # there, so the task resolves to an area, and every people signal
        # is gone.
        for table in ("ownership", "listings", "authority", "change_people", "blame_lines", "engineers"):
            bare.graph.db.execute(f"DELETE FROM {table}")
        bare.graph._memo.clear()
        t = canvas.start_task(bare, self.cfg, {
            "title": "Remove the legacy virtio-net header", "repo": "qemulike", "agent": "test",
            "paths": "hw/net/virtio-net.c", "requester": "Tamsin Reed <tamsin@synthco.example>",
            "goal": "Drop the legacy header; it breaks compat for old guests."})
        self.assertEqual(t["discovery"]["people"], [])
        self.assertEqual(t["verdict"], "engage", t["why"])
        self.assertIn("Raven does not know who owns this", t["why"])
        self.assertIn("unrouted queue", t["why"])

    def test_a_task_in_no_known_area_is_not_engaged_by_a_stray_word(self):
        """The other half: a task Raven cannot place at all is not made
        into a question by a stray policy word. It is unplaced: Raven
        asks for the files rather than judging without them."""
        t = self.start("Fix the typo in the release notes")
        self.assertEqual(t["discovery"]["areas"], [])
        self.assertEqual(t["verdict"], "unplaced", t["why"])

    def test_a_repository_bridge_has_never_read_is_not_a_repository_with_nothing_in_it(self):
        """The two read identically: no area, no prior decision, a
        confident pass. Measured on Grafana, a host passed its own
        worktree directory as `repo` and the sibling task passed on a
        decision the same Raven had already answered."""
        t = self.start("Add a second legacy-handling feature toggle", repo="/tmp/agent-work/repo2",
                       paths="hw/net/virtio-net.c",
                       goal="Add a second toggle that controls what the frontend does, the same way as the "
                            "other legacy-handling toggles.")
        self.assertEqual(t["verdict"], "pass", t["why"])
        self.assertIn("no graph for 'repo2'", t["why"])
        self.assertIn("qemulike", t["why"])
        self.assertIn("read no repository", t["next"])
        self.assertEqual(t["discovery"]["unknown_repo"], "repo2")
        known = self.start("Add a second legacy-handling feature toggle", paths="hw/net/virtio-net.c",
                           goal="Add a second toggle that controls what the frontend does, the same way as the "
                                "other legacy-handling toggles.")
        self.assertNotIn("unknown_repo", known["discovery"])
        self.assertNotIn("no graph for", known["why"])

    def test_the_same_key_may_name_the_repository_properly_and_be_looked_at_again(self):
        """The disclosure tells a host to call again with the repository's
        own name. The key rule used to refuse that call, so the advice
        could not be taken. Correcting an identity that matched no graph
        is not renaming the task; anything else still is."""
        first = self.start("Drop the legacy virtio-net header", repo="/tmp/agent-work/repo2",
                           client_key="k-repo", paths="hw/net/virtio-net.c",
                           goal="Drop the legacy header. It breaks compat for old guests.")
        self.assertEqual(first["verdict"], "pass", first["why"])
        again = self.start("Drop the legacy virtio-net header", repo="qemulike",
                           client_key="k-repo", paths="hw/net/virtio-net.c",
                           goal="Drop the legacy header. It breaks compat for old guests.")
        self.assertEqual(again["task_id"], first["task_id"])
        self.assertEqual(again["verdict"], "engage", again["why"])
        self.assertNotIn("no graph for", again["why"])
        # A different repository Raven does know is still a different task.
        with self.assertRaises(Invalid) as refused:
            self.start("Drop the legacy virtio-net header", repo="other/platform", client_key="k-repo",
                       paths="hw/net/virtio-net.c", goal="Drop the legacy header.")
        self.assertIn("already names the task", str(refused.exception))
        self.assertIn("different task, use a new key", str(refused.exception))

    def test_an_agent_that_restarts_gets_its_task_back_under_its_own_new_title(self):
        """Measured live: a host killed while it waited came back with the
        same client_key and the same task as given, titled in its own new
        words, and was refused and told to take a new key; a second task
        beside the tree is what that advice makes."""
        goal = "Clients get InvalidHeader for Retry-After values like 2.0 and +1. Handle them consistently."
        first = self.start("Fix InvalidHeader for nonstandard Retry-After values", client_key="k-restart",
                           paths="hw/net/virtio-net.c", goal=goal)
        again = self.start("Handle non-standard Retry-After values from CDN 503 responses", client_key="k-restart",
                           paths="hw/net/virtio-net.c", goal="  " + goal.replace(" ", "  ") + "\n")
        self.assertEqual(again["task_id"], first["task_id"])
        self.assertTrue(again["repeated"])
        self.assertEqual(again["title"], "Fix InvalidHeader for nonstandard Retry-After values")
        # Neither the title nor the task as given: another task, refused, and
        # the refusal says how to get the first one back.
        with self.assertRaises(Invalid) as refused:
            self.start("Rework the interrupt map", client_key="k-restart", goal="Move the PLIC base.")
        self.assertIn("'Fix InvalidHeader for nonstandard Retry-After values'", str(refused.exception))
        self.assertIn("call again with that title or the goal as it was given", str(refused.exception))

    def test_how_the_work_goes_through_bridge_is_not_a_reason_and_a_behaviour_change_is(self):
        """Measured live on urllib3: the rules engaged on "obtain needed
        policy decisions through Raven", the host's process, and missed
        "without unexpectedly changing existing retry behavior", the task."""
        goal = ("Some upstream services send nonstandard Retry-After values, and callers get InvalidHeader. "
                "Improve interoperability without unexpectedly changing existing retry behavior. "
                "Investigate the implementation, obtain needed policy decisions through Raven, implement a narrow "
                "change, and add and run regression tests.")
        words = [w for w, _ in canvas.judgment_signals({"areas": []}, "Accept odd Retry-After values", goal)]
        self.assertEqual(words, ["changing existing retry behavior"])
        process_only = "Fix the flaky retry test. Get any policy decisions via Raven before merging."
        self.assertEqual(canvas.judgment_signals({"areas": []}, "Fix the flaky retry test", process_only), [])

    def test_the_hosts_own_workflow_instructions_do_not_decide_the_verdict(self):
        """A host appends its house rules to the goal it passes. Measured
        on Grafana: a brief Raven passed on engaged once the host's
        boilerplate mentioning "authorization" and "release" was
        appended, so the verdict was evidence about the host and not
        about the work. What the requester wrote decides it."""
        plain = "Bump the pinned toolchain in scripts/ to the version the build already uses."
        rules = ("\n\nWorkflow instructions:\n"
                 "1. Call bridge_start_task before you begin.\n"
                 "2. You are authorized to change files under scripts/.\n"
                 "3. Do not cut a release; the maintainers handle releases.\n")
        loose = "\n\nYou are authorized to change files. Do not cut a release; the maintainers handle releases."
        first = self.start("Bump the pinned toolchain", paths="hw/net/virtio-net.c", goal=plain)
        self.assertEqual(first["verdict"], "pass", first["why"])
        for i, extra in enumerate((rules, loose)):
            t = self.start(f"Bump the pinned toolchain {i}", paths="hw/net/virtio-net.c", goal=plain + extra)
            self.assertEqual(t["verdict"], "pass", t["why"])
            self.assertNotIn("authorized", t["why"])
            self.assertNotIn("release", t["why"])

    def test_an_engage_reason_quotes_the_words_it_read(self):
        """A reason nobody can check against the task is not a reason.
        The verdict names the sentence its words came from."""
        t = self.start("Update the virtio-net device header", paths="hw/net/virtio-net.c",
                       goal="Drop the legacy header. It breaks compat for old guests.")
        self.assertEqual(t["verdict"], "engage", t["why"])
        self.assertIn('in "Drop the legacy header."', t["why"])

    def test_a_new_operator_visible_switch_is_somebodys_call(self):
        """A feature toggle ships in some state and somebody decides
        which. Raven passed on a Grafana brief whose whole subject was
        registering one."""
        t = self.start("Report or block reads of the legacy frontend feature-toggle map",
                       paths="hw/net/virtio-net.c",
                       goal="Register a feature toggle in hw/net/virtio-net.c that controls what the "
                            "frontend does when something reads the legacy map.")
        self.assertEqual(t["verdict"], "engage", t["why"])
        self.assertTrue(any("what does it ship as" in c["question"] for c in t["candidates"]), t["candidates"])

    def test_a_pass_formed_from_a_title_alone_says_so(self):
        """The same task engages on the requester's wording and passes on
        a one-line title an agent wrote itself, so a pass with no goal
        says which of the two it read."""
        thin = self.start("Update the virtio-net device header", paths="hw/net/virtio-net.c")
        self.assertEqual(thin["verdict"], "pass", thin["why"])
        self.assertIn("formed from a one-line title with no goal", thin["why"])
        full = self.start("Update the virtio-net device header", paths="hw/net/virtio-net.c",
                          goal="Drop the legacy header. It breaks compat for old guests, which we need a "
                               "call on before shipping.")
        self.assertEqual(full["verdict"], "engage", full["why"])
        self.assertNotIn("one-line title", full["why"])

    def test_a_task_with_no_known_area_says_so_honestly(self):
        t = self.start("Fix the typo in the release notes")
        self.assertEqual(t["verdict"], "unplaced")
        self.assertIn("names no area", t["why"])
        self.assertIn("not a pass", t["why"])

    def test_a_task_that_names_no_area_does_not_inherit_every_pending_question(self):
        """A pending node elsewhere in the repository is not "on these
        paths" when the new task resolves to no area at all."""
        first = self.start("Rework hw/riscv/boot.c firmware loading, dropping the old path", paths="hw/riscv/boot.c")
        canvas.add_node(self.store, self.cfg, {"task_id": first["task_id"],
                                               "question": "Should hw/riscv/boot.c keep loading the old firmware layout?",
                                               "paths": "hw/riscv/boot.c"})
        t = self.start("Fix the typo in the release notes")
        self.assertEqual(t["discovery"]["areas"], [])
        self.assertEqual(t["discovery"]["pending"], [])
        self.assertEqual(t["verdict"], "unplaced", t["why"])


class NodeTests(CanvasCase):
    def test_a_quote_the_host_escaped_twice_reaches_the_owner_as_a_quote(self):
        """Measured live: Claude Code sent 'like \\"0.5\\"' and the owner's
        card showed the backslashes; a regex in the context keeps its own."""
        t = self.start("Rework hw/riscv/virt.c interrupt routing, changing the default", paths="hw/riscv/virt.c")
        n = canvas.add_node(self.store, self.cfg, {
            "task_id": t["task_id"], "paths": "hw/riscv/virt.c",
            "question": 'Should hw/riscv/virt.c accept values like \\"aplic\\" in the default?',
            "context": 'Today it matches ^\\s*plic\\s*$ only.', "options": 'keep \\"plic\\" | switch'})
        row = self.store.get_decision(n["node_id"])
        self.assertEqual(row["question"], 'Should hw/riscv/virt.c accept values like "aplic" in the default?')
        self.assertIn('^\\s*plic\\s*$', row["context"])
        self.assertEqual(n["options"], ['keep "plic"', "switch"])

    def test_an_option_keeps_its_commas_and_no_option_is_dropped(self):
        """Measured live on a397f1c: three options came apart at their
        commas, and the fragments past the fourth, a whole option among
        them, were dropped."""
        t = self.start("Rework hw/riscv/virt.c interrupt routing, changing the default", paths="hw/riscv/virt.c")
        options = ["clamp after jitter: min(retry_after_max, delay + jitter), cap is never exceeded",
                   "jitter after clamp: delay may exceed retry_after_max by up to the jitter amount",
                   "raise the effective ceiling to retry_after_max + retry_after_jitter; document it"]
        n = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "paths": "hw/riscv/virt.c",
                                                  "question": "Should hw/riscv/virt.c clamp the jittered wait?",
                                                  "options": " | ".join(options)})
        self.assertEqual(n["options"], options)
        listed = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "paths": "hw/riscv/virt.c",
                                                       "question": "Should hw/riscv/virt.c log the clamp?",
                                                       "options": json.dumps(["yes, at debug", "no"])})
        self.assertEqual(listed["options"], ["yes, at debug", "no"])
        with self.assertRaises(Invalid) as refused:
            canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "paths": "hw/riscv/virt.c",
                                                  "question": "Which of nine?", "options": " | ".join("abcdefghi")})
        self.assertIn("at most 8, 9 given", str(refused.exception))

    def test_nodes_route_to_a_person_and_never_to_the_requester(self):
        t = self.start("Rework hw/riscv/virt.c interrupt routing, changing the default", paths="hw/riscv/virt.c")
        n = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "client_ref": "n1",
                                                  "question": "Should hw/riscv/virt.c change the default interrupt controller?",
                                                  "paths": "hw/riscv/virt.c", "options": "aplic | plic"})
        self.assertEqual(n["status"], "pending")
        self.assertEqual(n["owner"], "Oriel Vance")
        self.assertEqual(n["depth"], 0)
        self.assertEqual(n["options"], ["aplic", "plic"])
        self.assertIn("waiting on Oriel Vance", n["next"])
        again = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "client_ref": "n1",
                                                      "question": "a different wording, same client_ref"})
        self.assertEqual(again["node_id"], n["node_id"])
        self.assertTrue(again["repeated"])
        same_q = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"],
                                                       "question": "Should hw/riscv/virt.c change the default interrupt controller?"})
        self.assertEqual(same_q["node_id"], n["node_id"])
        child = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "parent_id": n["node_id"],
                                                      "question": "Should the aplic be the default for the virt board only?",
                                                      "paths": "hw/riscv/virt.c"})
        self.assertEqual(child["parent_id"], n["node_id"])
        self.assertEqual(child["depth"], 1)
        with self.assertRaises(Invalid):
            canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "parent_id": "nope", "question": "q?"})
        tree = canvas.get_tree(self.store, t["task_id"])
        self.assertEqual([x["node_id"] for x in tree["nodes"]], [n["node_id"]])
        self.assertEqual(tree["nodes"][0]["children"][0]["node_id"], child["node_id"])
        self.assertEqual(tree["counts"]["pending"], 2)
        self.assertIn("waiting on Oriel Vance", tree["next"])
        with self.assertRaises(Invalid):
            canvas.finish_task(self.store, {"task_id": t["task_id"]})

    def test_a_twin_in_another_task_binds_its_own_stub_and_leaves_the_first_node_alone(self):
        """Task B asks what task A already has open: B's node is the stub,
        status duplicate, pointing at A's node, and A's node keeps its
        parent, client_ref and depth. Retrying B's client_ref returns the
        stub, and A's node is not asked for judgment a second time."""
        a = self.start("Rework hw/riscv/virt.c interrupt routing, changing the default", paths="hw/riscv/virt.c")
        root = canvas.add_node(self.store, self.cfg, {"task_id": a["task_id"], "client_ref": "A-0",
                                                     "question": "Should hw/riscv/virt.c change the default interrupt controller?",
                                                     "paths": "hw/riscv/virt.c"})
        child = canvas.add_node(self.store, self.cfg, {"task_id": a["task_id"], "client_ref": "A-1",
                                                      "parent_id": root["node_id"],
                                                      "question": "Should the aplic be the default for the virt board only?",
                                                      "paths": "hw/riscv/virt.c"})
        self.assertEqual(child["status"], "pending")
        self.assertEqual((child["parent_id"], child["client_ref"], child["depth"]), (root["node_id"], "A-1", 1))
        b = self.start("Tidy the hw/riscv/virt.c interrupt defaults", paths="hw/riscv/virt.c")
        twin = canvas.add_node(self.store, self.cfg, {"task_id": b["task_id"], "client_ref": "B-1",
                                                     "question": "Should the aplic be the default for the virt board only?",
                                                     "paths": "hw/riscv/virt.c"})
        self.assertEqual(twin["status"], "duplicate")
        self.assertEqual(twin["duplicate_of"], child["node_id"])
        self.assertNotEqual(twin["node_id"], child["node_id"])
        self.assertEqual((twin["task_id"], twin["parent_id"], twin["client_ref"], twin["depth"]),
                         (b["task_id"], "", "B-1", 0))
        self.assertIn(child["node_id"], twin["next"])
        kept = canvas.node_view(self.store, child["node_id"])
        self.assertEqual((kept["task_id"], kept["parent_id"], kept["client_ref"], kept["depth"], kept["status"]),
                         (a["task_id"], root["node_id"], "A-1", 1, "pending"))
        self.assertEqual(self.store.graph.count_events("judgment_requested", decision_id=child["node_id"]), 1)
        again = canvas.add_node(self.store, self.cfg, {"task_id": b["task_id"], "client_ref": "B-1",
                                                      "question": "a different wording, same client_ref"})
        self.assertEqual(again["node_id"], twin["node_id"])
        self.assertTrue(again["repeated"])
        self.assertEqual((again["status"], again["duplicate_of"]), ("duplicate", child["node_id"]))
        tree = canvas.get_tree(self.store, a["task_id"])
        self.assertEqual([x["node_id"] for x in tree["nodes"]], [root["node_id"]])
        self.assertEqual([x["node_id"] for x in tree["nodes"][0]["children"]], [child["node_id"]])
        self.assertEqual([x["node_id"] for x in canvas.get_tree(self.store, b["task_id"])["nodes"]], [twin["node_id"]])

    def test_a_parent_id_that_is_a_prefix_of_a_real_id_is_refused(self):
        t = self.start("Rework hw/riscv/virt.c interrupt routing, changing the default", paths="hw/riscv/virt.c")
        n = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"],
                                                  "question": "Should hw/riscv/virt.c change the default interrupt controller?",
                                                  "paths": "hw/riscv/virt.c"})
        prefix = n["node_id"][:6]
        self.assertIsNotNone(self.store.graph.get_decision(prefix))
        with self.assertRaises(Invalid):
            canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "parent_id": prefix, "question": "q?"})
        with self.assertRaises(Invalid):
            canvas.settle_node(self.store, {"task_id": t["task_id"], "node_id": prefix, "answer": "x"})
        with self.assertRaises(Invalid):
            canvas.sign_off(self.store, prefix, {"by": "Oriel Vance", "answer": "x", "expected_updated_at": "x"})
        self.assertEqual(canvas.get_tree(self.store, t["task_id"])["counts"]["pending"], 1)

    def test_the_ask_reports_the_route_that_produced_its_owner(self):
        """The ranked candidates on the row are the ladder's own route,
        best first: the head is the owner, with evidence and a score, and
        a node carries the same list."""
        from bridge.ladder import ask
        run = self.store.add_run({"title": "t", "agent": "test", "repo": "qemulike"})
        row = ask(self.store, self.cfg, run["id"], "Should hw/riscv/virt.c add the imsics compatible string?",
                  context="c", requester="Tamsin Reed <tamsin@synthco.example>")
        self.assertEqual(row["owner_name"], "Oriel Vance")
        self.assertEqual(row["ranked"][0]["owner"], row["owner_name"])
        self.assertTrue(row["ranked"][0]["evidence"])
        self.assertGreater(row["ranked"][0]["score"], 0)
        self.assertLessEqual(len(row["ranked"]), 5)
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        n = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "paths": "hw/riscv/virt.c",
                                                  "question": "Should hw/riscv/virt.c change the default interrupt controller?"})
        self.assertEqual(n["ranked"][0]["owner"], n["owner"])

    def test_an_explicit_owner_on_a_node_overrides_the_route_and_must_exist(self):
        """The inbox form picks an owner by hand; the node goes to that
        person, not the one the signals name, and a missing owner is
        refused before anything is written under that id."""
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        picked = self.store.add_owner({"name": "Pat Picked", "team": "Boards", "patterns": "docs/*"})
        n = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "owner_id": picked["id"],
                                                  "question": "Should hw/riscv/virt.c change the default interrupt controller?",
                                                  "paths": "hw/riscv/virt.c"})
        self.assertEqual((n["status"], n["owner"]), ("pending", "Pat Picked"))
        with self.assertRaises(Invalid):
            canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "owner_id": "missing",
                                                  "question": "Should the aplic be the default for the virt board only?"})

    def test_a_settled_node_is_marked_for_signoff_and_a_person_signs_or_corrects(self):
        t = self.start("Refactor hw/net/e1000.c checksum offload, removing the old path", paths="hw/net/e1000.c")
        n = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"],
                                                  "question": "Should hw/net/e1000.c keep the software checksum fallback?",
                                                  "paths": "hw/net/e1000.c"})
        settled = canvas.settle_node(self.store, {"task_id": t["task_id"], "node_id": n["node_id"],
                                                  "answer": "Keep it behind a property", "rationale": "old guests need it"})
        self.assertEqual(settled["status"], "resolved")
        self.assertEqual(settled["signoff"], "required")
        self.assertEqual(settled["kind"], "agent")
        self.assertFalse(settled["authorized"])
        self.assertTrue(settled["blocking"])
        self.assertIn("do not ship", settled["next"])
        # An unsigned answer is evidence, not authorization: the task
        # cannot finish until a person signs it, and the refusal says so.
        with self.assertRaises(Invalid) as refused:
            canvas.finish_task(self.store, {"task_id": t["task_id"]})
        self.assertIn(n["node_id"], str(refused.exception))
        self.assertIn("sign-off wanted from Kwame Asante", str(refused.exception))
        # A signature names the revision it covers; a stale one is refused.
        with self.assertRaises(Invalid):
            canvas.sign_off(self.store, n["node_id"], {"by": "Kwame Asante"})
        with self.assertRaises(Invalid):
            canvas.sign_off(self.store, n["node_id"], {"by": "Kwame Asante", "expected_updated_at": "2000-01-01T00:00:00+00:00"})
        signed = self.sign(n["node_id"], "Kwame Asante")
        self.assertEqual(signed["signoff"], "signed")
        self.assertEqual(signed["signed_by"], "Kwame Asante")
        self.assertTrue(signed["authorized"])
        self.assertFalse(signed["blocking"])
        done = canvas.finish_task(self.store, {"task_id": t["task_id"]})
        self.assertEqual(done["status"], "completed")
        corrected = self.sign(n["node_id"], "Kwame Asante", answer="Drop it; no guest needs it", rationale="measured")
        self.assertEqual(corrected["status"], "answered")
        self.assertEqual(corrected["answered_by"], "Kwame Asante")
        self.assertEqual(corrected["answer"], "Drop it; no guest needs it")
        self.assertTrue(corrected["authorized"])

    def test_follow_up_questions_land_as_suggested_children_the_agent_adopts(self):
        t = self.start("Change hw/riscv/virt.c memory map defaults", paths="hw/riscv/virt.c")
        n = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"],
                                                  "question": "Should hw/riscv/virt.c move the flash base address?",
                                                  "paths": "hw/riscv/virt.c"})
        self.assertEqual(n["status"], "pending")
        self.store.answer(n["node_id"], {"answer": "Yes, to 0x20000000", "rationale": "matches the spec"})
        added = canvas.add_followups(self.store, self.cfg, n["node_id"],
                                     {"questions": "Should the DTB advertise the new base?\nShould old firmware keep booting?",
                                      "by": "Oriel Vance"})
        self.assertEqual(len(added["nodes"]), 2)
        self.assertTrue(all(x["status"] == "suggested" and x["origin"] == "human" and x["parent_id"] == n["node_id"]
                            for x in added["nodes"]))
        tree = canvas.get_tree(self.store, t["task_id"])
        self.assertEqual(tree["nodes"][0]["status"], "answered")
        self.assertEqual(len(tree["followups"]), 2)
        self.assertIn("follow-up questions added by people", tree["next"])
        adopted = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "adopt": added["nodes"][0]["node_id"],
                                                        "question": added["nodes"][0]["question"], "paths": "hw/riscv/virt.c"})
        self.assertEqual(adopted["origin"], "human")
        self.assertEqual(adopted["parent_id"], n["node_id"])
        self.assertEqual(adopted["depth"], 1)
        tree = canvas.get_tree(self.store, t["task_id"])
        self.assertEqual(len(tree["followups"]), 1)
        statuses = {x["node_id"]: x["status"] for x in tree["nodes"][0]["children"]}
        self.assertEqual(statuses[added["nodes"][0]["node_id"]], "adopted")

    def test_a_suggested_or_duplicate_node_is_not_signed_off(self):
        t = self.start("Change hw/riscv/virt.c memory map defaults", paths="hw/riscv/virt.c")
        n = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"],
                                                  "question": "Should hw/riscv/virt.c move the flash base address?",
                                                  "paths": "hw/riscv/virt.c"})
        # A twin asked while the first is still open is a duplicate; the
        # twin it points at carries the answer, so it is never signed itself.
        other = self.start("Move hw/riscv/virt.c flash base for the new boards", paths="hw/riscv/virt.c")
        twin = canvas.add_node(self.store, self.cfg, {"task_id": other["task_id"],
                                                     "question": "Should hw/riscv/virt.c move the flash base address?",
                                                     "paths": "hw/riscv/virt.c"})
        self.assertEqual(twin["status"], "duplicate", twin)
        with self.assertRaises(Invalid) as caught:
            self.sign(twin["node_id"], "Oriel Vance")
        self.assertIn("twin carries the answer", str(caught.exception))
        self.store.answer(n["node_id"], {"answer": "Yes, to 0x20000000", "rationale": "matches the spec"})
        # The duplicate reads through: the canonical decision's answer, its
        # signer and the next step are what the stub now shows.
        seen = canvas.node_view(self.store, twin["node_id"])
        self.assertEqual((seen["status"], seen["answer"], seen["answered_by"]),
                         ("duplicate", "Yes, to 0x20000000", "Oriel Vance"))
        self.assertTrue(seen["authorized"])
        self.assertFalse(seen["blocking"])
        self.assertEqual(seen["resolution"]["status"], "answered")
        self.assertIn("answered", seen["next"])
        added = canvas.add_followups(self.store, self.cfg, n["node_id"],
                                     {"questions": "Should the DTB advertise the new base?", "by": "Oriel Vance"})
        with self.assertRaises(Invalid) as caught:
            self.sign(added["nodes"][0]["node_id"], "Oriel Vance")
        self.assertIn("suggested", str(caught.exception))

    def test_a_child_written_after_the_answer_but_before_a_correction_is_flagged(self):
        """The answer time that counts is the last time the parent's answer
        changed, so a sign-off correction after the child was written
        flags the child even though the first answer came earlier."""
        t = self.start("Refactor hw/net/e1000.c checksum offload, removing the old path", paths="hw/net/e1000.c")
        n = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"],
                                                  "question": "Should hw/net/e1000.c keep the software checksum fallback?",
                                                  "paths": "hw/net/e1000.c"})
        canvas.settle_node(self.store, {"task_id": t["task_id"], "node_id": n["node_id"],
                                        "answer": "Keep it behind a property", "rationale": "old guests need it"})
        child = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "parent_id": n["node_id"],
                                                      "question": "Which property name does the fallback use?",
                                                      "paths": "hw/net/e1000.c"})
        tree = canvas.get_tree(self.store, t["task_id"])
        self.assertFalse(tree["nodes"][0]["children"][0]["parent_changed_after"])
        # The child's clock must be strictly earlier than the correction.
        self.store.graph.db.execute("UPDATE decisions SET created_at = '2000-01-01T00:00:00Z' WHERE id = ?",
                                    (child["node_id"],))
        self.sign(n["node_id"], "Kwame Asante", answer="Drop it; no guest needs it", rationale="measured")
        tree = canvas.get_tree(self.store, t["task_id"])
        self.assertTrue(tree["nodes"][0]["children"][0]["parent_changed_after"])
        self.assertIn("written before their parent was answered", tree["next"])

    def test_a_child_written_before_its_parent_was_answered_is_flagged(self):
        t = self.start("Change hw/riscv/virt.c memory map defaults", paths="hw/riscv/virt.c")
        n = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"],
                                                  "question": "Should hw/riscv/virt.c move the flash base address?",
                                                  "paths": "hw/riscv/virt.c"})
        child = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "parent_id": n["node_id"],
                                                      "question": "Should the DTB advertise the moved base?",
                                                      "paths": "hw/riscv/virt.c"})
        self.store.answer(n["node_id"], {"answer": "No, keep it", "rationale": "compat"})
        tree = canvas.get_tree(self.store, t["task_id"])
        self.assertTrue(tree["nodes"][0]["children"][0]["parent_changed_after"])
        self.assertIn("written before their parent was answered", tree["next"])
        self.assertEqual(tree["nodes"][0]["children"][0]["node_id"], child["node_id"])
        # The person then answers the child, after the parent: that answer
        # is the later word, and the tree stops asking for a re-read.
        self.store.answer(child["node_id"], {"answer": "Yes, from the same property", "rationale": "one source"})
        tree = canvas.get_tree(self.store, t["task_id"])
        self.assertFalse(tree["nodes"][0]["children"][0]["parent_changed_after"])
        self.assertNotIn("written before their parent was answered", tree["next"])

    def test_an_adopted_follow_up_is_not_flagged_for_a_re_read(self):
        """The follow-up a person added lives on as the node the agent
        adopted it into; only that node can be stale. Measured live: the
        adopted placeholder was flagged too, and the agent was told to
        re-read two nodes where one existed."""
        t = self.start("Change hw/riscv/virt.c memory map defaults", paths="hw/riscv/virt.c")
        n = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"],
                                                  "question": "Should hw/riscv/virt.c move the flash base address?",
                                                  "paths": "hw/riscv/virt.c"})
        added = canvas.add_followups(self.store, self.cfg, n["node_id"],
                                     {"questions": "Should the DTB advertise the moved base?", "by": "Oriel Vance"})
        placeholder = added["nodes"][0]["node_id"]
        adopter = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "adopt": placeholder,
                                                        "question": "Should the DTB advertise the moved base?",
                                                        "paths": "hw/riscv/virt.c"})
        self.store.graph.db.execute("UPDATE decisions SET created_at = '2000-01-01T00:00:00Z' WHERE id IN (?, ?)",
                                    (placeholder, adopter["node_id"]))
        self.store.answer(n["node_id"], {"answer": "No, keep it", "rationale": "compat"})
        tree = canvas.get_tree(self.store, t["task_id"])
        by_id = {x["node_id"]: x for x in _flatten(tree["nodes"])}
        self.assertEqual(by_id[placeholder]["status"], "adopted")
        self.assertFalse(by_id[placeholder]["parent_changed_after"])
        self.assertTrue(by_id[adopter["node_id"]]["parent_changed_after"])
        self.assertIn("1 node written before their parent was answered", tree["next"])


def _flatten(nodes):
    out = []
    for n in nodes:
        out.append(n)
        out.extend(_flatten(n["children"]))
    return out


class FactClashTests(CanvasCase):
    def test_a_fact_the_question_words_differently_is_named_and_the_stated_one_scopes(self):
        """Measured live on 5e967e4: the question said release=library-next,
        the task's facts said release=1.26.x, and the owner's brief followed
        the question."""
        t = self.start("Document the new retry option", paths="docs/about.rst", facts="release=1.26.x")
        node = canvas.add_node(self.store, self.cfg, {
            "task_id": t["task_id"], "paths": "docs/about.rst",
            "question": "What documentation must ship for a new feature in release=library-next?"})
        self.assertEqual(node["facts"]["release"], "1.26.x")
        context = self.store.get_decision(node["node_id"])["context"]
        self.assertIn("Facts: this task states release=1.26.x; the question's wording says release=library-next, "
                      "which is not this task's release", context)
        plain = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "paths": "docs/about.rst",
                                                       "question": "What documentation must ship in release=1.26.x?"})
        self.assertNotIn("Facts:", self.store.get_decision(plain["node_id"])["context"])


class PartialAnswerTests(CanvasCase):
    def test_an_answer_the_evidence_supports_only_in_part_says_so_to_the_agent(self):
        """Measured live on eb9d22d: stored as partial, shown to the agent as
        plain resolved, with only the evidence saying "in part"."""
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        node = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "paths": "hw/riscv/virt.c",
                                                      "question": "Which interrupt controller and which default IRQ count?"})
        with self.store.graph.transaction():
            self.store.graph.update_decision(node["node_id"], status="partial", kind="evidence",
                                             answer="The records settle the controller (aplic); they do not say the IRQ count.",
                                             evidence="composed across records: pr 14209; answers this only in part")
        view = canvas.node_view(self.store, node["node_id"])
        self.assertEqual(view["status"], "resolved")
        self.assertTrue(view["partial"])
        self.assertIn("answered only in part", view["next"])
        self.assertIn("Do not fill the open part yourself", view["next"])
        other = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "paths": "hw/riscv/virt.c",
                                                       "question": "Should hw/riscv/virt.c keep the legacy PLIC?"})
        self.assertFalse(canvas.node_view(self.store, other["node_id"])["partial"])


class TreeReadTests(CanvasCase):
    def ten_nodes(self):
        t = self.start("Rework hw/riscv/virt.c interrupt routing, changing the default", paths="hw/riscv/virt.c")
        subjects = ["interrupt controller default", "flash base address", "hart count ceiling", "serial console port",
                    "PCIe window size", "RTC device model", "virtio-mmio slot count", "ACLINT timer frequency",
                    "DTB reservation size", "boot ROM alignment"]
        parent = ""
        for i, subject in enumerate(subjects):
            n = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "parent_id": parent, "client_ref": f"n{i}",
                                                      "question": f"Should hw/riscv/virt.c change the {subject}?",
                                                      "paths": "hw/riscv/virt.c"})
            if i == 0:
                root = n
            if i % 3 == 0:
                parent = n["node_id"]
        # The root is pending (nothing was open before it); the rest are
        # pending or twins of it, as the deduper sees fit.
        self.assertEqual(root["status"], "pending")
        canvas.settle_node(self.store, {"task_id": t["task_id"], "node_id": root["node_id"], "answer": "yes"})
        return t

    def test_the_tree_reads_ten_nodes_with_one_query_on_decisions(self):
        """Read before every irreversible step: one statement on the
        decisions table, on the graph's own connection, no matter how
        many nodes the task has."""
        t = self.ten_nodes()
        statements = []
        self.store.graph.db.set_trace_callback(statements.append)
        self.addCleanup(self.store.graph.db.set_trace_callback, None)
        with patch.object(sqlite3, "connect", side_effect=AssertionError("get_tree opened a connection")):
            tree = canvas.get_tree(self.store, t["task_id"])
        self.assertEqual(sum(1 for s in statements if "FROM decisions" in s), 1)
        self.assertEqual(len(_flatten(tree["nodes"])), 10)
        self.assertEqual(len(tree["nodes"]), 1)
        self.assertEqual((tree["counts"]["resolved"], tree["counts"]["signoff_required"]), (1, 1))
        self.assertEqual(tree["counts"].get("pending", 0) + tree["counts"].get("duplicate", 0), 9)
        self.assertIn("sign-off wanted", tree["next"])
        self.assertIn("bridge_finish_task is refused", tree["next"])
        self.assertNotIn("discovery", tree)
        self.assertEqual(tree["verdict"], t["verdict"])
        self.assertEqual(tree["verdict_why"], t["why"])

    def test_a_node_reads_the_same_alone_and_on_the_tree(self):
        t = self.ten_nodes()
        flat = _flatten(canvas.get_tree(self.store, t["task_id"])["nodes"])
        self.assertEqual(len(flat), 10)
        for n in flat:
            alone = canvas.node_view(self.store, n["node_id"])
            self.assertEqual({k: v for k, v in n.items() if k not in ("children", "parent_changed_after", "parent")},
                             {k: v for k, v in alone.items() if k != "parent"})
            if n.get('parent'):
                self.assertEqual(n['parent'], {key: alone['parent'][key] for key in n['parent']})
        with self.assertRaises(Invalid):
            canvas.node_view(self.store, "nope")


class ClientRefTests(CanvasCase):
    """A client_ref is the agent's own name for one node, and its promise
    is that writing it twice writes one node. That has to hold when the
    second write starts before the first has finished, which is what a
    retry after a timeout looks like."""

    QUESTION = "Should hw/riscv/virt.c change the default interrupt controller?"

    def add(self, task_id, **extra):
        return canvas.add_node(self.store, self.cfg, {"task_id": task_id, "client_ref": "n1",
                                                      "question": self.QUESTION,
                                                      "paths": "hw/riscv/virt.c", **extra})

    def test_two_overlapping_writes_of_one_ref_make_one_node(self):
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        out, errors = [], []
        start = threading.Barrier(2)

        def write():
            start.wait()
            try:
                out.append(self.add(t["task_id"])["node_id"])
            except Exception as error:  # noqa: BLE001 - recorded, asserted below
                errors.append(f"{type(error).__name__}: {error}")

        threads = [threading.Thread(target=write) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(out)), 1, f"one client_ref made two nodes: {out}")
        self.assertEqual(len(self.store.graph.blocking_nodes(t["task_id"])), 1)
        self.assertEqual(self.store.list_decisions()["total"], 1)

    def test_a_retry_after_the_first_write_returns_the_same_node(self):
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        first = self.add(t["task_id"])
        again = self.add(t["task_id"], question="different words, same ref")
        self.assertEqual(again["node_id"], first["node_id"])
        self.assertTrue(again["repeated"])

    def test_a_write_that_produced_nothing_gives_the_ref_back(self):
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        with patch("bridge.ladder.ask", side_effect=RuntimeError("the ladder fell over")):
            with self.assertRaises(RuntimeError):
                self.add(t["task_id"])
        self.assertEqual(self.store.list_decisions()["total"], 0)
        node = self.add(t["task_id"])
        self.assertFalse(node.get("repeated"))
        self.assertEqual(self.store.list_decisions()["total"], 1)

    def test_a_write_that_died_holding_the_ref_is_taken_over(self):
        """The process was killed between the claim and the node. The
        ref is not held forever: after the same window an abandoned
        draft is recovered in, the next write takes it."""
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        graph = self.store.graph
        self.assertEqual(graph.claim_node_ref(t["task_id"], "n1"), (True, ""))
        # Still in flight: the ref is not free, and a writer that will not
        # wait is told so rather than writing a second node.
        self.assertEqual(graph.claim_node_ref(t["task_id"], "n1"), (False, ""))
        with self.assertRaises(Invalid) as refused:
            with patch.object(canvas, "_await_node_ref", return_value=""):
                self.add(t["task_id"])
        self.assertIn("being written right now", str(refused.exception))
        self.assertEqual(self.store.list_decisions()["total"], 0)
        # Past the window it is an abandoned claim, and the write proceeds.
        self.assertEqual(graph.claim_node_ref(t["task_id"], "n1", stale=0), (True, ""))
        graph.release_node_ref(t["task_id"], "n1")
        node = self.add(t["task_id"])
        self.assertEqual(node["question"], self.QUESTION)
        self.assertEqual(self.store.list_decisions()["total"], 1)

    def test_a_node_that_could_not_be_placed_still_answers_its_ref(self):
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        with patch.object(canvas, "_place_node", side_effect=Invalid("placement blew up")):
            with self.assertRaises(Invalid):
                self.add(t["task_id"])
        self.assertEqual(self.store.list_decisions()["total"], 1, "the decision was lost")
        again = self.add(t["task_id"])
        self.assertTrue(again["repeated"])
        self.assertEqual(self.store.list_decisions()["total"], 1, "the retry wrote a second node")


class BriefTests(CanvasCase):
    """The owner's brief is written by a model when one is configured,
    which every other test in this suite turns off. That is a whole
    branch of add_node nothing else runs."""

    def test_the_brief_is_written_from_the_question_and_the_agents_context(self):
        seen = {}

        def spy(cfg, question, context, task_title, evidence=None, options=None, **kw):
            seen.update(question=question, context=context, title=task_title,
                        evidence=list(evidence or []), options=list(options or []))
            return "Oriel: pick the interrupt controller for the virt board."

        # Semantic on, but no backend to reach: the other model rungs fall
        # through to their deterministic ones instead of shelling out to
        # whatever CLI happens to be on this machine's PATH.
        env = patch.dict(os.environ, {"BRIDGE_SEMANTIC": "1", "BRIDGE_CLAUDE_BIN": "/nonexistent/claude"})
        env.start()
        self.addCleanup(env.stop)
        with patch("bridge.llm.compose_brief", spy):
            t = self.start("Rework hw/riscv/virt.c interrupt routing, changing the default",
                           paths="hw/riscv/virt.c")
            node = canvas.add_node(self.store, Config(), {
                "task_id": t["task_id"], "client_ref": "b",
                "question": "Should hw/riscv/virt.c change the default interrupt controller?",
                "context": "The aplic is ready; the plic is what boards ship today.",
                "paths": "hw/riscv/virt.c", "options": "aplic | plic"})
            self.assertTrue(canvas.wait_for_background(10))
            node = canvas.node_view(self.store, node["node_id"])
        self.assertEqual(node["status"], "pending")
        self.assertEqual(node["brief"], "Oriel: pick the interrupt controller for the virt board.")
        # The agent's own words, not the paths and options Raven appends
        # to the stored context for scope and retrieval.
        self.assertEqual(seen["context"], "The aplic is ready; the plic is what boards ship today.")
        self.assertEqual(seen["options"], ["aplic", "plic"])
        # Who owns it and how it is approved are stated by the message itself,
        # never handed to the model to restate. Measured live: routing notes came
        # back as "approval comes from git trailers in the commit".
        self.assertEqual(seen["evidence"], [])
        self.assertIn("interrupt controller", seen["question"])

    def test_the_brief_prompt_carries_no_routing_or_approval_facts(self):
        from bridge import llm
        prompts = []

        def complete_json(self, purpose, system, prompt, **kw):
            if purpose == "conditions":
                return {"conditions": []}
            prompts.append((system, prompt))
            return {"claims": []} if purpose == "brief_check" else {"brief": "Pick the header list."}
        env = patch.dict(os.environ, {"BRIDGE_SEMANTIC": "1", "BRIDGE_CLAUDE_BIN": "/nonexistent/claude"})
        env.start()
        self.addCleanup(env.stop)
        with patch.object(llm.Client, "complete_json", complete_json):
            brief = llm.compose_brief(Config(), "Which headers join the list?", "No registered standard exists.",
                                      "Redirect headers", ["verified: Library Owner decides repository-wide",
                                                           "GitHub not synced: approvals come from git trailers"],
                                      ["X-Api-Key", "X-Auth-Token"])
        self.assertEqual(brief, "Pick the header list.")
        system, prompt = prompts[0]
        self.assertNotIn("git trailers", prompt)
        self.assertNotIn("Library Owner", prompt)
        self.assertIn("No registered standard exists.", prompt)
        self.assertIn("Never say who approves", system)

    # The live case on a397f1c, word for word: the agent's context, its
    # options, and the brief that reversed both outcomes.
    CAP_CONTEXT = (
        "parse_retry_after() clamps parsed seconds to self.retry_after_max (default DEFAULT_RETRY_AFTER_MAX = 21600, "
        "documented as \"Any Retry-After headers larger than this value will be limited to this value\"). If jitter "
        "is added after the clamp, a wait can exceed retry_after_max, which contradicts the documented meaning of the "
        "cap. If jitter is added before the clamp, then at or near the cap the jitter collapses to zero (all workers "
        "at the cap wake together again) \u2014 which is exactly the thundering-herd case the task is about, though "
        "only for headers above 6 hours. Note the precedent in get_backoff_time(): jitter is added to backoff_value "
        "and then min(backoff_max, ...) is applied, i.e. the existing code clamps the jittered value to the max.")
    CAP_OPTIONS = ["clamp after jitter: min(retry_after_max, delay + jitter) \u2014 matches get_backoff_time "
                   "precedent, cap is never exceeded",
                   "jitter after clamp: delay may exceed retry_after_max by up to the jitter amount",
                   "raise the effective ceiling to retry_after_max + retry_after_jitter and document it"]
    CAP_QUESTION = ("How does Retry-After jitter interact with the retry_after_max cap: is the jittered wait clamped "
                    "to retry_after_max, or may jitter push the wait above the cap?")
    CAP_REVERSED = (
        "When adding random jitter to retry waits from Retry-After headers, the code must decide whether jitter can "
        "push the total wait time above the retry_after_max cap. The cap is documented as a hard limit on wait times "
        "from headers, but adding jitter after clamping defeats jitter's purpose near the cap (all workers wake "
        "simultaneously), while adding jitter before clamping lets it exceed the documented limit. The existing "
        "backoff retry logic clamps the jittered value to its max, setting a precedent for one approach.")

    def brief(self, text, claims):
        from bridge import llm
        calls = []

        def complete_json(self, purpose, system, prompt, **kw):
            if purpose == "conditions":
                return {"conditions": []}
            calls.append(purpose)
            if purpose == "brief":
                return {"brief": text}
            if isinstance(claims, Exception):
                raise claims
            return claims
        env = patch.dict(os.environ, {"BRIDGE_SEMANTIC": "1", "BRIDGE_CLAUDE_BIN": "/nonexistent/claude"})
        env.start()
        self.addCleanup(env.stop)
        why: list = []
        with patch.object(llm.Client, "complete_json", complete_json):
            out = llm.compose_brief(Config(), self.CAP_QUESTION, self.CAP_CONTEXT, "Retry-After jitter",
                                    options=self.CAP_OPTIONS, why=why)
        return out, why, calls

    def test_a_brief_that_reverses_the_outcomes_is_withheld(self):
        """Measured live on a397f1c: the brief said jitter after the clamp
        collapses at the cap and jitter before it exceeds the cap; the
        agent had written the opposite of both. Caught without a model."""
        out, why, calls = self.brief(self.CAP_REVERSED, {"claims": [{"claim": "x", "verdict": "same"}]})
        self.assertEqual(out, "")
        self.assertIn('ties "jitter is added after the clamp" to what the agent said follows from '
                      '"jitter is added before the clamp"', why[0])
        self.assertEqual(calls, ["brief"])

    def test_a_brief_the_second_reading_finds_wrong_is_withheld(self):
        faithful = ("The owner decides whether a jittered Retry-After wait may go above retry_after_max. If jitter is "
                    "added after the clamp, a wait can exceed retry_after_max; if before, jitter collapses near the cap.")
        kept, _, calls = self.brief(faithful, {"claims": [{"claim": "after the clamp can exceed", "verdict": "same"}]})
        self.assertEqual(kept, faithful)
        self.assertEqual(calls, ["brief", "brief_check"])
        for claims in ({"claims": [{"claim": "before the clamp keeps the default", "verdict": "unsupported"}]},
                       {"claims": [{"claim": "after the clamp collapses", "verdict": "swapped"}]},
                       {"no": "claims"}):
            out, why, _ = self.brief(faithful, claims)
            self.assertEqual(out, "", claims)
            self.assertTrue(why, claims)
        from bridge.llm import LLMError
        out, why, _ = self.brief(faithful, LLMError("down"))
        self.assertEqual((out, why), ("", ["the brief could not be checked against the agent's words"]))

    def test_a_withheld_brief_leaves_the_agents_words_to_the_owner(self):
        from bridge import delivery, llm
        env = patch.dict(os.environ, {"BRIDGE_SEMANTIC": "1", "BRIDGE_CLAUDE_BIN": "/nonexistent/claude"})
        env.start()
        self.addCleanup(env.stop)

        def complete_json(self, purpose, system, prompt, **kw):
            if purpose == "conditions":
                return {"conditions": []}
            return {"brief": BriefTests.CAP_REVERSED} if purpose == "brief" else {"claims": []}
        with patch.object(llm.Client, "complete_json", complete_json):
            t = self.start("Rework hw/riscv/virt.c interrupt routing, changing the default", paths="hw/riscv/virt.c")
            node = canvas.add_node(self.store, Config(), {
                "task_id": t["task_id"], "question": "Should hw/riscv/virt.c clamp the jittered wait at the cap?",
                "context": self.CAP_CONTEXT, "paths": "hw/riscv/virt.c", "options": " | ".join(self.CAP_OPTIONS)})
            self.assertTrue(canvas.wait_for_background(10))
            node = canvas.node_view(self.store, node["node_id"])
        self.assertEqual(node["brief"], "")
        self.assertEqual(self.store.graph.count_events("brief_withheld", decision_id=node["node_id"]), 1)
        row = dict(self.store.get_decision(node["node_id"]))
        text = delivery.render(row, "ask", "Oriel Vance", "")["text"]
        self.assertIn("If jitter is added before the clamp, then at or near the cap the jitter collapses to zero", text)
        self.assertNotIn("defeats jitter's purpose", text)


class UnroutedSignoffTests(CanvasCase):
    """A node Raven answered but could not route waits on a person who
    was never named. It is the operator's to place: it shows in the
    unrouted count, and assigning it works."""

    def orphan(self):
        """A node the record or a default settled, with no owner: the
        agent asked about an area this repository knows nothing about."""
        t = self.start("Decide the rollout of an unrelated surface")
        node = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "client_ref": "o",
                                                      "question": "Should the new export surface ship behind a flag?",
                                                      "category": "rollout"})
        canvas.settle_node(self.store, {"task_id": t["task_id"], "node_id": node["node_id"],
                                        "answer": "behind a flag, default off"})
        view = canvas.node_view(self.store, node["node_id"])
        self.assertEqual(view["owner"], "")
        self.assertTrue(view["blocking"])
        self.assertEqual(view["signoff"], "required")
        return t, view

    def test_a_node_with_no_owner_is_counted_as_unrouted(self):
        self.orphan()
        counts = self.store.state()["counts"]
        self.assertEqual(counts["unrouted"], 1, "a node waiting on nobody was not in the unrouted queue")
        self.assertEqual(counts["signoff_required"], 1)

    def test_the_operator_can_assign_a_node_that_waits_for_sign_off(self):
        t, view = self.orphan()
        owner = self.store.add_owner({"name": "Oriel Vance", "team": "RISC-V", "patterns": "hw/riscv/*"})
        row = self.store.assign(view["node_id"], {"owner_id": owner["id"]})
        self.assertEqual(row["owner_id"], owner["id"])
        self.assertEqual(canvas.node_view(self.store, view["node_id"])["owner"], "Oriel Vance")
        self.assertEqual(self.store.state()["counts"]["unrouted"], 0)
        # The answer it was assigned over is still there to sign.
        self.assertEqual(row["answer"], "behind a flag, default off")
        self.sign(view["node_id"], "Oriel Vance")
        self.assertEqual(canvas.finish_task(self.store, {"task_id": t["task_id"]})["status"], "completed")

    def test_assigning_still_needs_a_real_owner_and_a_live_decision(self):
        _, view = self.orphan()
        with self.assertRaises(Invalid):
            self.store.assign(view["node_id"], {"owner_id": "missing"})
        self.sign(view["node_id"], "Oriel Vance")
        owner = self.store.add_owner({"name": "Kwame Asante", "team": "Net", "patterns": "hw/net/*"})
        with self.assertRaises(Invalid) as refused:
            self.store.assign(view["node_id"], {"owner_id": owner["id"]})
        self.assertIn("waiting for sign-off", str(refused.exception))


class HalfWrittenNodeTests(CanvasCase):
    """A node is written in two steps: the ladder routes it, then the
    canvas gives it its place on the tree. Between them it is a draft:
    no reader sees it, the finish gate still counts it, nothing loses
    it, and a write that never returned is recovered and says so."""

    def mid_write(self, task_id, observe):
        """Run `observe()` at the moment the node is routed but not yet
        placed, from inside add_node itself."""
        real = canvas._place_node
        seen = {}

        def spy(*args, **kwargs):
            seen["drafts"] = list(args[-1])
            seen["observed"] = observe()
            return real(*args, **kwargs)

        with patch.object(canvas, "_place_node", spy):
            node = canvas.add_node(self.store, self.cfg, {"task_id": task_id, "client_ref": "half",
                                                          "question": "Should the RTC device model change?",
                                                          "paths": "hw/riscv/virt.c"})
        return node, seen

    def test_a_reader_sees_no_node_while_one_is_being_written(self):
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")

        def look():
            state = self.store.state()
            return {"tree": len(_flatten(canvas.get_tree(self.store, t["task_id"])["nodes"])),
                    "inbox": self.store.inbox()["total"],
                    "state": len(state["decisions"]),
                    "needs_you": state["counts"]["needs_you"],
                    "listed": self.store.list_decisions()["total"]}

        empty = {"tree": 0, "inbox": 0, "state": 0, "needs_you": 0, "listed": 0}
        self.assertEqual(look(), empty)
        node, seen = self.mid_write(t["task_id"], look)
        self.assertEqual(seen["observed"], empty, "a node still being written was visible to a reader")
        self.assertEqual(seen["drafts"], [node["node_id"]])
        # And it is all there the moment add_node returns.
        self.assertEqual(look(), {"tree": 1, "inbox": 1, "state": 1, "needs_you": 1, "listed": 1})
        self.assertTrue(node["owner"])

    def test_finishing_is_refused_while_a_node_is_being_written(self):
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")

        def try_finish():
            try:
                return canvas.finish_task(self.store, {"task_id": t["task_id"]})["status"]
            except Invalid as error:
                return str(error)

        _, seen = self.mid_write(t["task_id"], try_finish)
        self.assertIn("being written right now", seen["observed"])
        self.assertNotEqual(self.store.list_runs()["items"][0]["status"], "completed")

    def test_a_node_whose_placement_fails_is_published_not_lost(self):
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        with patch.object(canvas, "_place_node", side_effect=Invalid("placement blew up")):
            with self.assertRaises(Invalid):
                canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "client_ref": "boom",
                                                       "question": "Should the boot ROM alignment change?",
                                                       "paths": "hw/riscv/virt.c"})
        listed = self.store.list_decisions()
        self.assertEqual(listed["total"], 1, "a decision somebody asked was left invisible")
        self.assertEqual(self.store.inbox()["total"], 1)

    def test_a_write_that_never_returned_is_recovered_and_says_so(self):
        """The process died between the routing and the placement. The
        draft is swept back into view with the reason, once, and the
        task cannot finish over it."""
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        # SystemExit is not an Exception: add_node's publish-on-failure
        # does not run, which is what a killed process leaves behind.
        with patch.object(canvas, "_place_node", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "client_ref": "killed",
                                                       "question": "Should the hart count ceiling change?",
                                                       "paths": "hw/riscv/virt.c"})
        self.assertEqual(self.store.inbox()["total"], 0)
        self.assertEqual(self.store.graph.get_setting("drafts_pending"), "1")
        # Within the window it is still a write in flight, not a loss.
        self.assertEqual(self.store.graph.expire_drafts(), [])
        recovered = self.store.graph.expire_drafts(seconds=0)
        self.assertEqual(len(recovered), 1)
        row = self.store.get_decision(recovered[0])
        self.assertIn("did not finish", row["routing_reason"])
        self.assertEqual(self.store.inbox()["total"], 1)
        # Swept once: the marker is cleared and a second sweep is free.
        self.assertEqual(self.store.graph.get_setting("drafts_pending"), "")
        self.assertEqual(self.store.graph.expire_drafts(seconds=0), [])
        with self.assertRaises(Invalid) as refused:
            canvas.finish_task(self.store, {"task_id": t["task_id"]})
        self.assertIn("still wait", str(refused.exception))

    def test_a_draft_is_not_memory_and_is_not_a_twin(self):
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        question = "Should hw/riscv/virt.c change the RTC device model?"
        with patch.object(canvas, "_place_node", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "client_ref": "ghost",
                                                       "question": question, "paths": "hw/riscv/virt.c"})
        self.assertEqual(self.store.graph.memory_search(question, limit=5), [])
        # The same question asked again is a question of its own, not a
        # duplicate of a node nobody ever saw.
        again = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "client_ref": "again",
                                                       "question": question, "paths": "hw/riscv/virt.c"})
        self.assertEqual(again["status"], "pending")
        self.assertEqual(again["duplicate_of"], "")
        self.assertEqual(again["related"], [], "a node nobody ever saw was linked as a relative")

    def test_finishing_sweeps_before_it_takes_the_write_lock(self):
        """The sweeps write on the graph's own connection. Run inside the
        finish transaction they would wait on a lock this thread holds,
        for the full busy timeout, and then fail."""
        t = self.start("Rework hw/riscv/virt.c interrupt routing", paths="hw/riscv/virt.c")
        with self.store.graph.transaction():
            self.store.graph.set_setting("rule_next_expiry", "2000-01-01T00:00:00Z")
        started = time.monotonic()
        self.assertEqual(canvas.finish_task(self.store, {"task_id": t["task_id"]})["status"], "completed")
        self.assertLess(time.monotonic() - started, 5.0, "the finish waited on its own write lock")
        self.assertEqual(self.store.graph.get_setting("rule_next_expiry"), "")


class RecordGateTests(CanvasCase):
    def test_a_proposal_is_not_settled_by_a_record_that_only_shares_area_words(self):
        """A commit about an interrupt loop in target/arm decided nothing
        about adding non-maskable interrupts; the node routes to a
        person, and a sibling with the same words is not resolved from
        the first node either."""
        g = self.store.graph
        g.upsert_intent("qemulike", "commit", "abc123", "hw/riscv/virt: fix interrupt number in the loop",
                        "Interrupt number in the loop should be base irq plus loop index; add the missing check "
                        "and support the target board.", "Tamsin Reed", "2026-06-20T00:00:00+00:00")
        t = self.start("Add non-maskable interrupt support to the riscv virt board", paths="hw/riscv/virt.c")
        n = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"],
                                                  "question": "Should hw/riscv/virt.c add support for Non-maskable Interrupt?",
                                                  "paths": "hw/riscv/virt.c"})
        self.assertEqual(n["status"], "pending", n)
        self.assertEqual(n["owner"], "Oriel Vance")
        sibling = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "parent_id": n["node_id"],
                                                        "question": "Should hw/riscv/virt.c add NMI support in riscv_cpu_exec_interrupt()?",
                                                        "paths": "hw/riscv/virt.c"})
        self.assertIn(sibling["status"], ("pending", "duplicate"), sibling)

    def test_a_record_whose_title_names_the_proposal_is_context_without_a_model(self):
        g = self.store.graph
        g.upsert_intent("qemulike", "commit", "def456", "hw/riscv/virt: add non-maskable interrupt support",
                        "Add NMI support to the virt board: the ACLINT gets a non-maskable interrupt line.",
                        "Tamsin Reed", "2026-06-20T00:00:00+00:00")
        t = self.start("Add non-maskable interrupt support to the riscv virt board", paths="hw/riscv/virt.c")
        n = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"],
                                                  "question": "Should hw/riscv/virt.c add support for the non-maskable interrupt?",
                                                  "paths": "hw/riscv/virt.c"})
        self.assertEqual(n["status"], "pending", n)
        self.assertEqual(n["answer"], "")
        # The relevant record travels to the owner, but retrieval alone
        # is not a verified answer to the proposal.
        self.assertEqual(n["owner"], "Oriel Vance", n["owner_evidence"])
        self.assertEqual(n["ranked"][0]["owner"], "Oriel Vance")
        self.assertIn("def456", n["evidence"])
        self.assertIn("as context", n["evidence"])

    def test_reverting_what_is_on_record_is_a_new_decision(self):
        g = self.store.graph
        g.upsert_intent("qemulike", "commit", "aaa111", "hw/net/virtio-net: assume presence of the legacy header",
                        "The legacy header is always present now.", "Tamsin Reed", "2026-06-20T00:00:00+00:00")
        t = self.start("Undo the legacy header assumption in hw/net/virtio-net.c", paths="hw/net/virtio-net.c")
        n = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"],
                                                  "question": "Should we revert \"hw/net/virtio-net: assume presence of the legacy header\"?",
                                                  "paths": "hw/net/virtio-net.c"})
        self.assertEqual(n["status"], "pending", n)
        self.assertEqual(n["owner"], "Kwame Asante")
        self.assertIn("retrieved context commit aaa111", n["evidence"])
        self.assertIn("legacy header is always present", n["evidence"])
        self.assertIn("not approval", n["evidence"])

    def test_undo_and_rollback_terms_do_not_hide_cited_evidence(self):
        body = "Record restoration entries only after a successful mutation; failed mutations leave no entry."
        self.store.graph.upsert_intent("qemulike", "pr", "14209", "Fix failed mutation teardown",
                                       body, "Tamsin Reed", "2026-06-20T00:00:00+00:00")
        questions = (
            "Does the existing setattr fix in #14209 establish how failed mutations should interact with monkeypatch undo?",
            "Does #14209 establish when to record restoration entries?",
            "Does #14209 establish when to record undo entries?",
            "What does #14209 say about transaction rollback bookkeeping?",
            "What does #14209 say about MonkeyPatch.undo()?",
        )
        for question in questions:
            with self.subTest(question=question):
                t = self.start("Explain failed mutation teardown", paths="hw/net/virtio-net.c")
                n = canvas.add_node(self.store, self.cfg, {"task_id": t["task_id"], "question": question})
                self.assertIn(n["status"], ("pending", "unrouted", "duplicate"), n)
                canonical = self.store.graph.get_decision(n["duplicate_of"]) if n["status"] == "duplicate" else None
                evidence = canonical.evidence if canonical is not None else n["evidence"]
                self.assertIn("14209", evidence)
                self.assertIn(body, evidence)
                self.assertFalse(n["answer"])
                self.assertNotIn("reverse a change", n["evidence"])

    def test_reversal_retrieves_cited_context_without_approving_it(self):
        self.store.graph.upsert_intent("qemulike", "pr", "14209", "Fix failed mutation teardown",
                                       "Record entries after successful mutations.",
                                       "Tamsin Reed", "2026-06-20T00:00:00+00:00")
        for action in ("revert", "undo", "roll back", "back out"):
            with self.subTest(action=action):
                t = self.start("Reconsider teardown behavior", paths="hw/net/virtio-net.c")
                n = canvas.add_node(self.store, self.cfg, {
                    "task_id": t["task_id"], "question": f"Should we {action} #14209?",
                    "category": "compat",  # A low-stakes default must not approve reversal either.
                })
                self.assertIn(n["status"], ("pending", "duplicate"), n)
                if n["status"] == "pending":
                    self.assertIn("retrieved context pr 14209", n["evidence"])
                    self.assertIn("not approval", n["evidence"])
                    self.assertEqual(n["answer"], "")

    def test_reversal_without_records_does_not_claim_a_record_exists(self):
        t = self.start("Reconsider absent change", paths="hw/net/virtio-net.c")
        n = canvas.add_node(self.store, self.cfg, {
            "task_id": t["task_id"], "question": "Should we revert #999999?",
        })
        self.assertEqual(n["status"], "pending", n)
        self.assertNotIn("is on record", n["evidence"])
        self.assertNotIn("retrieved context", n["evidence"])


class ProtocolTests(CanvasCase):
    def test_mcp_tools_carry_the_protocol(self):
        init = dispatch(self.store, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertIn("bridge_start_task the moment a task is kicked off", init["result"]["instructions"])
        listed = dispatch(self.store, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = [t["name"] for t in listed["result"]["tools"]]
        for name in ("bridge_start_task", "bridge_add_node", "bridge_settle_node", "bridge_get_tree", "bridge_finish_task"):
            self.assertIn(name, names)

        def call(name, args):
            r = dispatch(self.store, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                      "params": {"name": name, "arguments": args}})
            self.assertFalse(r["result"]["isError"], r["result"]["content"][0]["text"])
            return json.loads(r["result"]["content"][0]["text"])

        t = call("bridge_start_task", {"title": "Drop the legacy header from hw/net/virtio-net.c, breaking compat",
                                       "repo": "acme/qemulike", "requester": "Tamsin Reed", "paths": "hw/net/virtio-net.c"})
        self.assertEqual(t["verdict"], "engage")
        n = call("bridge_add_node", {"task_id": t["task_id"], "question": "Should hw/net/virtio-net.c drop the legacy header?",
                                     "context": "old guests", "paths": "hw/net/virtio-net.c", "client_ref": "a"})
        self.assertEqual(n["owner"], "Kwame Asante")
        tree = call("bridge_get_tree", {"task_id": t["task_id"]})
        self.assertEqual(tree["counts"]["pending"], 1)
        s = call("bridge_settle_node", {"task_id": t["task_id"], "node_id": n["node_id"], "answer": "keep it"})
        self.assertEqual(s["signoff"], "required")
        self.assertFalse(s["authorized"])
        refused = dispatch(self.store, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                        "params": {"name": "bridge_finish_task", "arguments": {"task_id": t["task_id"]}}})
        self.assertTrue(refused["result"]["isError"])
        self.assertIn("sign-off wanted from Kwame Asante", refused["result"]["content"][0]["text"])
        self.sign(n["node_id"], "Kwame Asante")
        # Kwame signed after the agent last read the tree: it has not seen
        # what he signed, so it reads before it finishes.
        unread = dispatch(self.store, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                       "params": {"name": "bridge_finish_task", "arguments": {"task_id": t["task_id"]}}})
        self.assertTrue(unread["result"]["isError"])
        self.assertIn("after you last read it", unread["result"]["content"][0]["text"])
        self.assertIn("Kwame Asante", unread["result"]["content"][0]["text"])
        call("bridge_get_tree", {"task_id": t["task_id"]})
        done = call("bridge_finish_task", {"task_id": t["task_id"]})
        self.assertEqual(done["status"], "completed")

    def test_an_answer_the_agent_never_read_holds_the_finish(self):
        """Measured on a real task: the owner answered in 37 seconds and the
        agent built the opposite for fifteen edits before it read the tree.
        Nothing made it read before finishing, and with no model to read
        the diff the finish would have said only that the decision was
        authorized. The finish now waits until the agent has read what
        people did."""
        def call(name, args):
            r = dispatch(self.store, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                      "params": {"name": name, "arguments": args}})
            return r["result"]["isError"], r["result"]["content"][0]["text"]

        _, text = call("bridge_start_task", {"title": "Drop the legacy header from hw/net/virtio-net.c, breaking compat",
                                             "repo": "acme/qemulike", "paths": "hw/net/virtio-net.c"})
        task = json.loads(text)["task_id"]
        _, text = call("bridge_add_node", {"task_id": task, "question": "Should hw/net/virtio-net.c drop the legacy header?",
                                           "paths": "hw/net/virtio-net.c", "client_ref": "a"})
        node = json.loads(text)
        self.assertEqual(node["status"], "pending")
        self.assertIn("do not settle this in code yourself", node["next"])
        _, text = call("bridge_add_node", {"task_id": task, "question": "Should hw/net/virtio-net.c keep the old guest quirk?",
                                           "paths": "hw/net/virtio-net.c", "client_ref": "b"})
        other = json.loads(text)
        row = self.store.get_decision(node["node_id"])
        self.store.answer(node["node_id"], {"answer": "Keep it one more release", "rationale": "old guests",
                                            "signed_by": "Kwame Asante", "expected_updated_at": row["updated_at"]})
        row = self.store.get_decision(other["node_id"])
        self.store.answer(other["node_id"], {"answer": "Drop the quirk", "rationale": "nobody runs it",
                                             "signed_by": "Kwame Asante", "expected_updated_at": row["updated_at"]})
        refused, text = call("bridge_finish_task", {"task_id": task})
        self.assertTrue(refused)
        self.assertIn(node["node_id"], text)
        self.assertIn("Read bridge_get_tree", text)
        # A wait on one node shows that node only: not a read of the tree.
        call("bridge_wait", {"task_id": task, "node_id": node["node_id"], "timeout": "0"})
        refused, text = call("bridge_finish_task", {"task_id": task})
        self.assertTrue(refused)
        self.assertIn(other["node_id"], text)
        # An empty whole-task wait cannot acknowledge answers that arrived
        # before the call and were not returned. A full tree can.
        _, text = call("bridge_wait", {"task_id": task, "timeout": "0"})
        self.assertFalse(json.loads(text)['read_acknowledged'])
        refused, text = call("bridge_finish_task", {"task_id": task})
        self.assertTrue(refused)
        call('bridge_get_tree', {'task_id': task})
        refused, text = call("bridge_finish_task", {"task_id": task})
        self.assertFalse(refused, text)
        self.assertEqual(json.loads(text)["status"], "completed")

    def test_rest_routes_mirror_the_tools(self):
        server = make_server(self.store, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        port = server.server_port
        base = f"http://127.0.0.1:{port}"

        def get(path):
            with urlopen(Request(base + path, headers={"Host": f"127.0.0.1:{port}"})) as r:
                return json.loads(r.read())

        csrf = get("/api/state")["csrf_token"]

        def post(path, data):
            req = Request(base + path, data=json.dumps(data).encode(), method="POST",
                          headers={"Content-Type": "application/json", "X-Bridge-CSRF": csrf, "Host": f"127.0.0.1:{port}"})
            with urlopen(req) as r:
                return json.loads(r.read())

        t = post("/api/tasks/start", {"title": "Remove the old riscv boot path from hw/riscv/boot.c", "repo": "qemulike",
                                      "requester": "Tamsin Reed", "paths": "hw/riscv/boot.c"})
        self.assertEqual(t["verdict"], "engage")
        n = post(f"/api/tasks/{t['task_id']}/nodes", {"question": "Should hw/riscv/boot.c drop the old boot path?",
                                                       "paths": "hw/riscv/boot.c"})
        self.assertEqual(n["owner"], "Oriel Vance")
        # A human answer over HTTP names the revision it reviewed.
        with self.assertRaises(HTTPError) as stale:
            post(f"/api/decisions/{n['node_id']}/answer", {"answer": "Yes", "rationale": "nobody uses it"})
        self.assertEqual(stale.exception.code, 400)
        post(f"/api/decisions/{n['node_id']}/answer", {"answer": "Yes", "rationale": "nobody uses it",
                                                        "expected_updated_at": n["updated_at"]})
        f = post(f"/api/decisions/{n['node_id']}/followups", {"questions": ["Should the docs say so?"], "by": "Oriel Vance"})
        self.assertEqual(f["nodes"][0]["status"], "suggested")
        # The card names the records its question cites, with their status.
        self.assertEqual(get(f"/api/decisions/{n['node_id']}")["records_named"], "")
        tree = get(f"/api/tasks/{t['task_id']}/tree")
        self.assertEqual(tree["nodes"][0]["status"], "answered")
        self.assertEqual(len(tree["followups"]), 1)
        with self.assertRaises(HTTPError) as caught:
            post(f"/api/tasks/{t['task_id']}/settle", {"node_id": f["nodes"][0]["node_id"], "answer": "x"})
        self.assertEqual(caught.exception.code, 400)

    def test_overlapping_writes_of_one_ref_over_http_make_one_node(self):
        """The same race as in process, over the transport a shared Raven
        actually serves: two MCP calls in flight at once with one
        client_ref."""
        server = make_server(self.store, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        port = server.server_port
        base = f"http://127.0.0.1:{port}"
        headers = {"Content-Type": "application/json", "Host": f"127.0.0.1:{port}"}

        def call(name, **args):
            req = Request(base + "/mcp", method="POST", headers=headers,
                          data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": name, "arguments": args}}).encode())
            with urlopen(req) as r:
                result = json.loads(r.read())["result"]
            text = result["content"][0]["text"]
            if result.get("isError"):
                raise Invalid(text)
            return json.loads(text)

        task = call("bridge_start_task", title="Rework hw/riscv/virt.c interrupt routing", repo="qemulike",
                    requester="Tamsin Reed", paths="hw/riscv/virt.c", client_key="http-race")
        out, errors = [], []
        start = threading.Barrier(2)

        def write():
            start.wait()
            try:
                out.append(call("bridge_add_node", task_id=task["task_id"], client_ref="n1",
                                question="Should hw/riscv/virt.c change the default interrupt controller?",
                                paths="hw/riscv/virt.c")["node_id"])
            except Exception as error:  # noqa: BLE001 - recorded, asserted below
                errors.append(f"{type(error).__name__}: {error}")

        threads = [threading.Thread(target=write) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(out)), 1, f"one client_ref made two nodes over HTTP: {out}")
        tree = call("bridge_get_tree", task_id=task["task_id"])
        self.assertEqual(len(tree["nodes"]), 1)


if __name__ == "__main__":
    unittest.main()
