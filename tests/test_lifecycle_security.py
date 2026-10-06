"""Late forms and delayed inference cannot revive or replace human state."""
import json
import os
from unittest.mock import patch

from bridge import briefing, canvas
from bridge.authz import Actor
from bridge.config import Config
from bridge.store import Invalid
from test_auth import BOOTSTRAP, SharedServer
from test_delivery import DeliveryCase
from test_security_isolation import DecisionCase


class AbandonedCoreTests(DecisionCase):
    def setUp(self):
        super().setUp()
        self.node = self.decision(self.alice)
        self.task_id = self.node["run_id"]
        canvas.abandon_task(self.store, self.task_id, "Accidental duplicate")

    def unchanged(self):
        self.assertEqual(self.graph.get_task(self.task_id)["status"], "abandoned")
        self.assertEqual(self.store.get_decision(self.node["id"])["status"], "withdrawn")

    def test_agent_settlement_and_run_status_cannot_reopen(self):
        with self.assertRaises(Invalid):
            canvas.settle_node(self.store, {"task_id": self.task_id, "node_id": self.node["id"], "answer": "Revive"})
        for status in ("working", "completed"):
            with self.assertRaises(Invalid):
                self.store.update_run(self.task_id, {"status": status})
        with self.assertRaises(Invalid):
            self.store.request({"run_id": self.task_id, "question": "Another?", "context": "Late retry"})
        self.unchanged()

    def test_answers_signoffs_and_followups_cannot_revive_withdrawn_nodes(self):
        current = self.store.get_decision(self.node["id"])
        actor = Actor.person(self.alice, kind="session")
        with self.assertRaises(Invalid):
            self.store.answer(self.node["id"], {"answer": "Revive", "expected_updated_at": current["updated_at"]}, actor=actor)
        for answer in ("", "Correction after withdrawal"):
            with self.assertRaises(Invalid):
                canvas.sign_off(self.store, self.node["id"], {"answer": answer,
                    "expected_updated_at": current["updated_at"]}, actor=actor)
        with self.assertRaises(Invalid):
            canvas.add_followups(self.store, Config(model_api="none"), self.node["id"],
                                 {"questions": ["Revive the task?"], "required": True}, actor=actor)
        self.unchanged()
        self.assertEqual(self.graph.db.execute("SELECT count(*) n FROM decisions WHERE run_id=?",
                                             (self.task_id,)).fetchone()["n"], 1)

    def test_resume_reads_the_archive_but_cannot_change_its_facts(self):
        resumed = canvas.start_task(self.store, Config(model_api="none"), {"task_id": self.task_id})
        self.assertEqual(resumed["status"], "abandoned")
        with self.assertRaises(Invalid):
            canvas.start_task(self.store, Config(model_api="none"), {"task_id": self.task_id, "facts": "customer=other"})
        self.assertEqual(self.graph.get_task(self.task_id)["facts"], "")
        self.unchanged()


class CompletedCorrectionTests(DecisionCase):
    def test_completed_tasks_still_allow_the_authorized_owner_to_correct(self):
        node = self.decision(self.alice)
        self.answer(node, self.alice)
        canvas.finish_task(self.store, {"task_id": node["run_id"]})
        row = self.store.get_decision(node["id"])
        corrected = canvas.sign_off(self.store, node["id"], {"answer": "Use the corrected policy",
            "expected_updated_at": row["updated_at"]}, actor=Actor.person(self.alice, kind="session"))
        self.assertTrue(corrected["authorized"])
        self.assertEqual(corrected["answer"], "Use the corrected policy")


class HumanParticipationTests(DecisionCase):
    def test_human_context_and_interview_drafts_prevent_abandonment(self):
        from bridge import interview
        actor = Actor.person(self.alice, kind="session")
        for action in ("note", "interview"):
            node = self.decision(self.alice, title="Task with " + action)
            if action == "note":
                canvas.add_note(self.store, node["run_id"], {"text": "I am reviewing this now"}, actor=actor)
            else:
                interview.create(self.store, node["run_id"], {"decision_id": node["id"], "client_key": "review"}, actor)
            with self.assertRaises(Invalid):
                canvas.abandon_task(self.store, node["run_id"], "An agent thought it was accidental")
            self.assertNotEqual(self.graph.get_task(node["run_id"])["status"], "abandoned")


class LateHttpFormsTests(SharedServer):
    def setUp(self):
        super().setUp()
        self.person = self.post("/api/people", {"name": "Review Owner"}, token=BOOTSTRAP)
        self.token = self.post("/api/tokens", {"person_id": self.person["id"], "kind": "human"}, token=BOOTSTRAP)["token"]
        self.run = self.store.add_run({"title": "Accidental task", "repo": "acme/service"})
        with self.store.graph.transaction():
            self.did = self.store.graph.add_decision(self.run["id"], "Approve this policy?", "policy", "pending",
                                                    owner=self.person["name"], repo="acme/service")
            self.link = briefing.mint(self.store.graph, self.person["id"], self.run["id"], self.did)
        self.old_revision = self.store.get_decision(self.did)["updated_at"]
        canvas.abandon_task(self.store, self.run["id"], "Created twice")

    def test_late_http_answer_signoff_and_status_forms_are_refused(self):
        withdrawn = self.store.get_decision(self.did)
        for revision in (self.old_revision, withdrawn["updated_at"]):
            for action in ("answer", "signoff"):
                self.assertEqual(self.status_of("POST", f"/api/decisions/{self.did}/{action}",
                    {"answer": "Late approval", "expected_updated_at": revision}, token=self.token), 400)
        self.assertEqual(self.status_of("POST", f"/api/runs/{self.run['id']}/status",
                                        {"status": "working"}, token=self.token), 400)
        self.assertEqual(self.store.graph.get_task(self.run["id"])["status"], "abandoned")
        self.assertEqual(self.store.get_decision(self.did), withdrawn)

    def test_late_task_link_forms_are_refused_even_after_refresh(self):
        withdrawn = self.store.get_decision(self.did)
        for revision in (self.old_revision, withdrawn["updated_at"]):
            status, _, _ = self.raw("POST", "/api/brief/answer",
                {"Content-Type": "application/json", "X-Raven-Link": self.link},
                json.dumps({"decision_id": self.did, "answer": "Late answer",
                            "expected_updated_at": revision}).encode())
            self.assertEqual(status, 400)
        self.assertEqual(self.store.graph.get_task(self.run["id"])["status"], "abandoned")
        self.assertEqual(self.store.get_decision(self.did), withdrawn)


class DelayedWorkerTests(DeliveryCase):
    def scheduled_node(self):
        task = self.task()
        pending = []
        with patch.dict(os.environ, BRIDGE_SEMANTIC="1"), patch("bridge.canvas._start_background",
                side_effect=lambda *args: pending.append(args)):
            node = canvas.add_node(self.store, Config(model_api="none"), {"task_id": task,
                "question": "Should billing exclude the internal load test?", "paths": "billing/usage.py"})
        self.assertEqual(len(pending), 1)
        return node, pending[0]

    def assert_preserved_after_start(self, node, scheduled):
        before = self.store.get_decision(node["node_id"])
        with patch("bridge.ladder.ask", side_effect=AssertionError("Stale inference must not begin")) as ask:
            scheduled[1](*scheduled[2:])
            ask.assert_not_called()
        after = self.store.get_decision(node["node_id"])
        for key in ("status", "answer", "signed_by", "signatures", "signed_hash", "updated_at"):
            self.assertEqual(after[key], before[key], key)
        self.assertFalse(after["model_pending"])

    def test_an_answer_before_the_worker_starts_is_not_its_new_baseline(self):
        node, scheduled = self.scheduled_node()
        self.store.answer(node["node_id"], {"answer": "Exclude internal traffic only", "rationale": "Test traffic",
            "expected_updated_at": node["updated_at"]}, actor=Actor.person(self.graph.get_person(self.wes)))
        self.assert_preserved_after_start(node, scheduled)

    def test_a_signature_before_the_worker_starts_is_not_overwritten(self):
        node, scheduled = self.scheduled_node()
        settled = canvas.settle_node(self.store, {"task_id": node["task_id"], "node_id": node["node_id"],
                                                 "answer": "Keep test traffic unbilled"})
        canvas.sign_off(self.store, node["node_id"], {"expected_updated_at": settled["updated_at"]},
                        actor=Actor.person(self.graph.get_person(self.wes)))
        self.assert_preserved_after_start(node, scheduled)


class TemporarySignerTests(DeliveryCase):
    def test_reusing_an_answer_does_not_promote_a_temporary_substitute(self):
        task = self.task()
        node = self.node(task)
        self.graph.append_event("route_learning_optout", {"task_id": task, "decision_id": node["node_id"]})
        self.store.answer(node["node_id"], {"answer": "Keep the existing policy", "rationale": "Temporary cover",
            "expected_updated_at": node["updated_at"]}, actor=Actor.person(self.graph.get_person(self.wes)))
        row = self.store.get_decision(node["node_id"])
        with patch("bridge.routing.rank_for_decision", return_value=[("Marisol Vega", ["authored current code"], 1.0)]):
            ranked = canvas._route_signer(self.graph, self.graph.get_task(task), row["question"], row["context"],
                [row["path"]], "", None, row["category"], json.loads(row["facts"] or "{}"), node["node_id"])
        self.assertEqual(ranked[0][0], "Marisol Vega")
