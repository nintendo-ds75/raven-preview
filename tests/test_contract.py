"""The decision contract, as invariants.

Every case here reproduces a behaviour the readiness review found on the
prototype (reviews/audit_probes.py) and asserts the contract that
replaced it: evidence is not authorization, an unsigned answer is a
prediction for everyone else, a decision is its question plus its scope,
a duplicate reads through to the decision it points at, a correction
reaches every answer derived from the corrected one, a repository is
identified by owner and name, a signature is bound to the revision it
covers, a kickoff key names one task, and actionable work is never
hidden by a payload cap."""

import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fixtures import OfflineCase

from bridge import canvas
from bridge.config import Config
from bridge.store import Invalid, Store, answer_hash, repo_key

CFG = Config(model_api="none")


class ContractCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "contract.db")
        self.wes = self.store.add_owner({"name": "Wes", "team": "Billing", "patterns": "billing/*"})

    def start(self, title="Review billing behavior", repo="acme/platform", **extra):
        return canvas.start_task(self.store, CFG, {"title": title, "repo": repo, "paths": "billing/usage.py", **extra})["task_id"]

    def node(self, task, question="Should we waive the usage charge?", context="Customer Alpha", **extra):
        return canvas.add_node(self.store, CFG, {"task_id": task, "question": question, "context": context,
                                                 "paths": "billing/usage.py", **extra})

    def settle(self, task, node_id, answer, rationale="agent inference, not reviewed"):
        return canvas.settle_node(self.store, {"task_id": task, "node_id": node_id, "answer": answer,
                                               "rationale": rationale})

    def sign(self, node_id, by="Wes", **extra):
        current = canvas.node_view(self.store, node_id)
        return canvas.sign_off(self.store, node_id, {"by": by, "expected_updated_at": current["updated_at"], **extra})

    def answer(self, node_id, text, **extra):
        return self.store.answer(node_id, {"answer": text, "rationale": "reviewed", **extra})


class AuthorizationTests(ContractCase):
    def test_an_unsigned_answer_never_completes_a_task(self):
        task = self.start()
        n = self.node(task)
        settled = self.settle(task, n["node_id"], "Waive the charge.")
        self.assertEqual((settled["status"], settled["signoff"]), ("resolved", "required"))
        self.assertFalse(settled["authorized"])
        self.assertTrue(settled["blocking"])
        self.assertTrue(self.store.get_decision(n["node_id"])["approval_pending"])
        with self.assertRaises(Invalid) as refused:
            canvas.finish_task(self.store, {"task_id": task})
        self.assertIn(n["node_id"], str(refused.exception))
        self.assertIn("evidence, not sign-off", str(refused.exception))
        tree = canvas.get_tree(self.store, task)
        self.assertEqual(tree["counts"]["blocking"], 1)
        self.assertIn("do not ship", tree["next"])
        self.sign(n["node_id"])
        self.assertFalse(self.store.get_decision(n["node_id"])["approval_pending"])
        self.assertEqual(canvas.finish_task(self.store, {"task_id": task})["status"], "completed")

    def test_the_agent_is_told_not_to_ship_on_evidence(self):
        task = self.start()
        n = self.node(task)
        settled = self.settle(task, n["node_id"], "Waive the charge.")
        self.assertIn("do not ship", settled["next"])
        self.assertNotIn("proceed on this answer", settled["next"])

    def test_a_human_answer_is_the_signed_revision(self):
        task = self.start()
        n = self.node(task)
        row = self.answer(n["node_id"], "Bill it.")
        self.assertEqual((row["status"], row["signoff"], row["signed_by"]), ("approved", "signed", "Wes"))
        self.assertEqual(row["signed_hash"], answer_hash("Bill it."))
        self.assertEqual(row["signed_revision"], row["updated_at"])
        self.assertTrue(row["authorized"])
        self.assertFalse(row["approval_pending"])


class SourceTrustTests(ContractCase):
    def test_an_unsigned_answer_is_reused_only_as_a_prediction(self):
        a = self.start()
        original = self.node(a)
        c = self.start(title="A third task")
        third = self.node(c, context="Customer Gamma; separate contract")
        self.assertEqual(third["status"], "pending")
        self.settle(a, original["node_id"], "Waive the charge.")
        b = self.start(title="Bill a different customer")
        reused = self.node(b)
        self.assertEqual((reused["status"], reused["kind"]), ("predicted", "prediction"))
        self.assertEqual(reused["answer"], "Waive the charge.")
        self.assertIn("no human has signed", reused["evidence"])
        self.assertIn("prediction", reused["next"])
        self.assertFalse(reused["authorized"])
        self.assertTrue(reused["blocking"])
        self.assertEqual([ln["id"] for ln in reused["related"] if ln["kind"] == "derived"], [original["node_id"]])
        with self.assertRaises(Invalid):
            canvas.finish_task(self.store, {"task_id": b})
        # The unsigned reuse closes no open question elsewhere.
        self.assertEqual(canvas.node_view(self.store, third["node_id"])["status"], "pending")

    def test_a_signed_answer_for_another_scope_is_a_prediction_here(self):
        a = self.start()
        alpha = self.node(a, context="Customer Alpha; one-off exception")
        self.answer(alpha["node_id"], "Waive Alpha's charge.")
        b = self.start(title="Gamma's charge")
        gamma = self.node(b, context="Customer Gamma; separate contract")
        self.assertEqual((gamma["status"], gamma["kind"]), ("predicted", "prediction"))
        self.assertIn("another scope", gamma["evidence"])
        self.assertEqual(gamma["answer"], "Waive Alpha's charge.")
        # Only names tell them apart, so it is suspected, not known, and each side is named as itself.
        self.assertIn(f"which may be another scope (this one names gamma, decision {alpha['node_id']} names alpha)",
                      gamma["evidence"])

    def test_incidental_words_are_not_another_scope(self):
        """Measured live on main 6d90c3d: a follow-up about the same release
        and file was called another scope because one context said
        "follow-up" and "urllib3", the other "273" and "APIs"."""
        a = self.start(repo="urllib3/urllib3")
        question = "Which extra API credential headers belong in the default redirect removal list?"
        original = self.node(a, question=question,
                             context="Only X-Api-Key, see line 273; APIs that send other headers configure them")
        self.answer(original["node_id"], "Only X-Api-Key joins the list")
        b = self.start(title="Follow-up guide", repo="urllib3/urllib3")
        reused = self.node(b, question="For this release, " + question[0].lower() + question[1:],
                           context="A follow-up guide for the same urllib3 release; keep the earlier compatibility scope.")
        self.assertEqual((reused["status"], reused["kind"]), ("resolved", "evidence"), reused["evidence"])
        self.assertNotIn("another scope", reused["evidence"])
        self.assertFalse(reused["authorized"])

    def test_a_question_about_no_file_is_not_scoped_by_the_file_it_was_asked_from(self):
        """Measured live on eb9d22d: "What documentation must ship for a new
        Retry feature?" answered from docs/user-guide.rst read as another
        scope when the same question came from changelog/."""
        a = self.start(repo="urllib3/urllib3")
        question = "What documentation must ship for a new Retry feature in this release?"
        original = self.node(a, question=question, context="Retry docs", paths="billing/docs/user-guide.rst")
        self.answer(original["node_id"], "A feature changelog fragment and the Retry docstring")
        b = self.start(title="Changelog", repo="urllib3/urllib3")
        reused = self.node(b, question=question, context="Retry docs", paths="billing/changelog/5280.feature.rst")
        self.assertEqual((reused["status"], reused["kind"]), ("resolved", "evidence"), reused["evidence"])
        self.assertNotIn("another scope", reused["evidence"])
        # A question about its own file is scoped by it.
        c = self.start(title="Guide", repo="urllib3/urllib3")
        own = "Should billing/docs/user-guide.rst keep the retry section?"
        first = self.node(c, question=own, context="Retry docs", paths="billing/docs/user-guide.rst")
        self.answer(first["node_id"], "Keep it")
        d = self.start(title="Guide elsewhere", repo="urllib3/urllib3")
        other = self.node(d, question=own, context="Retry docs", paths="billing/docs/advanced-usage.rst")
        self.assertIn("this one is about billing/docs/advanced-usage.rst", other["evidence"])

    def test_stated_facts_decide_the_scope(self):
        a = self.start()
        first = self.node(a, context="Exception approved by Priya", facts="customer=acme")
        self.answer(first["node_id"], "Waive it.")
        b = self.start(title="Another customer")
        other = self.node(b, context="Exception approved by Priya", facts="customer=globex")
        self.assertEqual(other["status"], "predicted")
        self.assertIn(f"answered this for another scope, decision {first['node_id']} (this one states "
                      f"customer=globex, decision {first['node_id']} states customer=acme)", other["evidence"])
        c = self.start(title="Same customer, other words")
        same = self.node(c, context="Exception for the renewal, per Dana", facts="customer=acme")
        self.assertEqual((same["status"], same["kind"]), ("resolved", "evidence"), same["evidence"])

    def test_facts_stated_for_the_task_hold_for_every_node(self):
        """Measured live on 35a5f84: the host wrote release=library on one
        node and the ticket's release=library-next on the next, so a
        signed answer for the same release could not carry over."""
        a = self.start(facts="release=library-next")
        first = self.node(a, context="Exception approved by Priya")
        self.assertEqual(self.store.graph.get_task(a)["facts"], '{"release": "library-next"}')
        self.assertEqual(first["facts"], {"release": "library-next"})
        self.answer(first["node_id"], "Waive it.")
        b = self.start(title="Same release, other words", facts="release=library-next")
        same = self.node(b, context="Exception for the renewal, per Dana")
        self.assertEqual((same["status"], same["kind"]), ("resolved", "evidence"), same["evidence"])
        # A node's own value still wins, and says so.
        other = self.node(b, question="Should we waive the usage charge next release?", context="Exception",
                          facts="release=library-later")
        self.assertEqual(other["facts"], {"release": "library-later"})
        self.assertEqual(canvas.get_tree(self.store, b)["facts"], {"release": "library-next"})

    def test_a_signed_answer_is_reused_as_evidence_that_still_wants_signoff(self):
        a = self.start()
        original = self.node(a)
        self.answer(original["node_id"], "Waive the charge.")
        b = self.start(title="Bill a different customer")
        reused = self.node(b)
        self.assertEqual((reused["status"], reused["kind"]), ("resolved", "evidence"))
        self.assertEqual(reused["answered_by"], "Wes")
        self.assertEqual(reused["signoff"], "required")
        self.assertFalse(reused["authorized"])
        self.assertTrue(reused["blocking"])
        row = self.store.get_decision(reused["node_id"])
        self.assertEqual(row["source_id"], original["node_id"])
        self.assertTrue(row["source_revision"])

    def test_search_marks_what_a_person_signed(self):
        a = self.start()
        n = self.node(a)
        self.settle(a, n["node_id"], "Waive the charge.")
        m = self.node(a, question="Should trial accounts be metered?", context="Trial cohort")
        self.answer(m["node_id"], "No, trials are not metered.")
        matches = {x["question"]: x["signed"] for x in self.store.search("waive the usage charge")["matches"]}
        self.assertFalse(matches["Should we waive the usage charge?"])
        matches = {x["question"]: x["signed"] for x in self.store.search("trial accounts metered")["matches"]}
        self.assertTrue(matches["Should trial accounts be metered?"])


class DecisionIdentityTests(ContractCase):
    def test_the_same_question_in_another_scope_is_another_node(self):
        task = self.start()
        alpha = self.node(task, context="Customer Alpha", client_ref="alpha")
        beta = self.node(task, context="Customer Beta", client_ref="beta")
        self.assertNotEqual(alpha["node_id"], beta["node_id"])
        self.assertFalse(beta.get("repeated"))
        self.assertEqual(self.store.get_decision(beta["node_id"])["context"].split("\n")[0], "Customer Beta")
        self.assertEqual([ln["id"] for ln in beta["related"] if ln["kind"] == "related"], [alpha["node_id"]])
        # Same question, same scope, no client_ref of its own: the node
        # already there. A new client_ref is the agent naming a new
        # decision, and is honored.
        again = self.node(task, context="Customer Alpha")
        self.assertEqual(again["node_id"], alpha["node_id"])
        self.assertTrue(again["repeated"])
        named = self.node(task, context="Customer Alpha", client_ref="alpha-2")
        self.assertNotEqual(named["node_id"], alpha["node_id"])

    def test_a_bare_retry_returns_the_node_it_repeats(self):
        task = self.start()
        first = canvas.add_node(self.store, CFG, {"task_id": task, "question": "Should we waive the usage charge?",
                                                  "context": "Customer Alpha", "paths": "billing/usage.py"})
        bare = canvas.add_node(self.store, CFG, {"task_id": task, "question": "Should we waive the usage charge?"})
        self.assertEqual(bare["node_id"], first["node_id"])
        self.assertTrue(bare["repeated"])

    def test_across_tasks_a_different_scope_is_related_never_merged(self):
        a, b = self.start(), self.start()
        first = self.node(a, context="Customer Alpha; one-off exception")
        second = self.node(b, context="Customer Beta; different contract")
        self.assertEqual(first["status"], "pending")
        self.assertEqual(second["status"], "pending")
        self.assertEqual(second["duplicate_of"], "")
        self.assertIn(first["node_id"], [ln["id"] for ln in second["related"]])
        self.assertIn("another scope", second["evidence"])
        self.answer(first["node_id"], "Waive only Alpha's charge.")
        self.assertEqual(canvas.node_view(self.store, second["node_id"])["status"], "pending")

    def test_the_same_scope_across_tasks_is_a_duplicate_that_reads_through(self):
        a, b = self.start(), self.start()
        first = self.node(a)
        second = self.node(b)
        self.assertEqual((second["status"], second["duplicate_of"]), ("duplicate", first["node_id"]))
        self.assertTrue(second["blocking"])
        self.assertEqual(second["resolution"]["status"], "pending")
        with self.assertRaises(Invalid) as refused:
            canvas.finish_task(self.store, {"task_id": b})
        self.assertIn(f"same decision as node {first['node_id']}", str(refused.exception))
        self.assertIn("waiting on Wes", str(refused.exception))
        self.answer(first["node_id"], "Waive only Alpha's charge.")
        seen = canvas.node_view(self.store, second["node_id"])
        self.assertEqual((seen["answer"], seen["answered_by"], seen["authorized"], seen["blocking"]),
                         ("Waive only Alpha's charge.", "Wes", True, False))
        self.assertNotIn("waiting", seen["next"])
        tree = canvas.get_tree(self.store, b)
        self.assertEqual(tree["nodes"][0]["answer"], "Waive only Alpha's charge.")
        self.assertEqual(tree["counts"]["blocking"], 0)
        self.assertEqual(canvas.finish_task(self.store, {"task_id": b})["status"], "completed")
        self.assertEqual(self.store.get_decision(second["node_id"])["canonical"]["status"], "approved")


class CorrectionTests(ContractCase):
    def test_a_correction_reaches_every_derived_answer(self):
        a = self.start()
        original = self.node(a)
        self.answer(original["node_id"], "Waive the charge.")
        b = self.start()
        reused = self.node(b)
        self.assertEqual(reused["answer"], "Waive the charge.")
        self.sign(reused["node_id"])
        self.assertTrue(canvas.node_view(self.store, reused["node_id"])["authorized"])
        self.answer(original["node_id"], "Bill the charge. The waiver was incorrect.",
                    expected_updated_at=self.store.get_decision(original["node_id"])["updated_at"])
        dependent = self.store.get_decision(reused["node_id"])
        self.assertEqual(dependent["needs_review"], 1)
        self.assertIn(original["node_id"], dependent["review_reason"])
        self.assertEqual(dependent["signoff"], "required")
        self.assertTrue(dependent["approval_pending"])
        tree = canvas.get_tree(self.store, b)
        self.assertTrue(tree["needs_review"])
        self.assertEqual(tree["counts"]["needs_review"], 1)
        self.assertIn("review", tree["next"])
        self.assertIn("needs review", tree["nodes"][0]["next"])
        with self.assertRaises(Invalid) as refused:
            canvas.finish_task(self.store, {"task_id": b})
        self.assertIn("needs review", str(refused.exception))
        kinds = [e["kind"] for e in self.store.get_decision(reused["node_id"])["events"]]
        self.assertIn("dependent_flagged", kinds)
        # A person confirms or corrects the dependent; either clears the flag.
        review = self.store.get_decision(reused["node_id"])['source_revalidation']
        corrected = self.sign(reused["node_id"], answer="Bill the charge here too.", rationale="follows the correction",
                              source_evidence=review['pins'], source_decision_pins=review['decision_pins'])
        self.assertEqual(corrected["needs_review"], False)
        self.assertTrue(corrected["authorized"])
        self.assertEqual(canvas.finish_task(self.store, {"task_id": b})["status"], "completed")

    def test_a_correction_of_an_unsigned_answer_also_propagates(self):
        a = self.start()
        original = self.node(a)
        self.settle(a, original["node_id"], "Waive the charge.")
        b = self.start()
        predicted = self.node(b)
        self.assertEqual(predicted["status"], "predicted")
        self.sign(original["node_id"], answer="Bill the charge.", rationale="policy")
        dependent = self.store.get_decision(predicted["node_id"])
        self.assertEqual(dependent["needs_review"], 1)

    def test_a_signature_alone_flags_nothing(self):
        a = self.start()
        original = self.node(a)
        self.answer(original["node_id"], "Waive the charge.")
        b = self.start()
        reused = self.node(b)
        self.sign(original["node_id"])
        self.assertEqual(self.store.get_decision(reused["node_id"])["needs_review"], 0)


class RepositoryIdentityTests(ContractCase):
    def test_repo_key_keeps_the_owner(self):
        self.assertEqual(repo_key("acme/platform"), "acme/platform")
        self.assertEqual(repo_key("other-company/platform"), "other-company/platform")
        self.assertEqual(repo_key("https://github.com/acme/platform.git"), "acme/platform")
        self.assertEqual(repo_key("git@github.com:acme/platform.git"), "acme/platform")
        self.assertEqual(repo_key("/src/platform"), "platform")
        self.assertEqual(repo_key("platform"), "platform")
        self.assertEqual(repo_key(""), "")

    def test_repositories_with_the_same_name_do_not_share_memory(self):
        a = self.start(repo="acme/platform")
        first = self.node(a)
        self.answer(first["node_id"], "Use Acme's negotiated exception.")
        b = self.start(repo="other-company/platform")
        second = self.node(b)
        self.assertEqual(second["status"], "pending", second)
        self.assertEqual(second["answer"], "")
        self.assertEqual(self.store.get_decision(second["node_id"])["repo"], "other-company/platform")

    def test_a_hosted_identity_finds_the_bare_checkout_it_was_ingested_from(self):
        g = self.store.graph
        g.upsert_artifact("platform", "billing/usage.py")
        self.assertEqual(g.resolve_repo("acme/platform"), "platform")
        self.assertEqual(g.resolve_repo("platform"), "platform")
        g.upsert_artifact("other-company/platform", "billing/usage.py")
        # Two graphs could answer to the bare name: nothing is guessed.
        self.assertEqual(g.resolve_repo("acme/platform"), "acme/platform")
        self.assertEqual(g.resolve_repo("other-company/platform"), "other-company/platform")


class SignoffBindingTests(ContractCase):
    def test_a_signature_covers_the_revision_reviewed(self):
        task = self.start()
        n = self.node(task)
        old = self.settle(task, n["node_id"], "Waive the charge.")
        self.settle(task, n["node_id"], "Bill every charge.")
        with self.assertRaises(Invalid) as stale:
            canvas.sign_off(self.store, n["node_id"], {"by": "Wes", "expected_updated_at": old["updated_at"]})
        self.assertIn("changed while you were reviewing", str(stale.exception))
        self.assertEqual(canvas.node_view(self.store, n["node_id"])["signoff"], "required")
        signed = self.sign(n["node_id"])
        row = self.store.get_decision(n["node_id"])
        self.assertEqual(signed["answer"], "Bill every charge.")
        self.assertEqual(row["signed_revision"], row["updated_at"])
        self.assertEqual(row["signed_hash"], answer_hash("Bill every charge."))

    def test_a_signed_node_is_not_resettled_by_the_agent(self):
        task = self.start()
        n = self.node(task)
        self.settle(task, n["node_id"], "Waive the charge.")
        self.sign(n["node_id"])
        with self.assertRaises(Invalid):
            self.settle(task, n["node_id"], "Bill every charge.")


class KickoffTests(ContractCase):
    def test_a_client_key_names_one_task(self):
        first = canvas.start_task(self.store, CFG, {"title": "Review billing", "repo": "acme/platform",
                                                    "paths": "billing/usage.py", "client_key": "k1"})
        again = canvas.start_task(self.store, CFG, {"title": "Review billing", "repo": "acme/platform",
                                                    "paths": "billing/usage.py", "client_key": "k1"})
        self.assertEqual(again["task_id"], first["task_id"])
        self.assertTrue(again["repeated"])
        self.assertEqual(again["verdict"], first["verdict"])
        with self.assertRaises(Invalid):
            canvas.start_task(self.store, CFG, {"title": "Another task", "repo": "acme/platform", "client_key": "k1"})
        self.assertEqual(self.store.graph.db.execute("SELECT count(*) FROM runs").fetchone()[0], 1)

    def test_racing_kickoffs_with_one_key_make_one_task(self):
        gate = threading.Barrier(4)

        def kick(_):
            gate.wait()
            store = Store(self.store.path)
            try:
                return canvas.start_task(store, CFG, {"title": "Raced", "repo": "acme/platform", "client_key": "race"})["task_id"]
            finally:
                store.graph.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(kick, range(4)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.store.graph.db.execute("SELECT count(*) FROM runs WHERE client_key='race'").fetchone()[0], 1)


class InboxTests(ContractCase):
    def test_old_pending_work_is_never_hidden_by_the_cap(self):
        a = self.start()
        pending = self.node(a)
        for i in range(501):
            self.store.graph.add_decision(a, f"Routine resolved item {i}", "policy", "approved", answer="Done",
                                          answered_by="Wes", repo="acme/platform")
        state = self.store.state()
        self.assertTrue(any(d["id"] == pending["node_id"] for d in state["decisions"]))
        self.assertEqual(state["counts"]["pending"], 1)
        self.assertEqual(state["counts"]["needs_you"], 1)
        page = self.store.inbox(page=1, size=10)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["id"], pending["node_id"])

    def test_counts_name_what_waits_on_people(self):
        a = self.start()
        open_q = self.node(a)
        settled = self.node(a, question="Should trials be metered?", context="Trial cohort")
        self.settle(a, settled["node_id"], "No.")
        counts = self.store.state()["counts"]
        self.assertEqual((counts["pending"], counts["signoff_required"], counts["needs_you"]), (1, 1, 2))
        self.store.graph.db.execute("UPDATE decisions SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                                    (open_q["node_id"],))
        self.assertEqual(self.store.state()["counts"]["overdue"], 1)


class TwinClosingTests(ContractCase):
    def test_a_human_answer_settles_open_twins_as_derived_evidence(self):
        a, b = self.start(), self.start(title="Later task")
        first = self.node(a)
        # The same decision, asked again before the first was answered,
        # lands as a duplicate; a pending twin in its own right is one
        # whose scope matches but was routed separately.
        second = self.node(b)
        self.assertEqual(second["status"], "duplicate")
        self.answer(first["node_id"], "Waive the charge.")
        seen = canvas.node_view(self.store, second["node_id"])
        self.assertTrue(seen["authorized"])
        self.assertEqual(json.loads(json.dumps(seen["resolution"]))["status"], "answered")


if __name__ == "__main__":
    unittest.main()
