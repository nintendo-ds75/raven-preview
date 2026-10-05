"""Reusable rules: the one way an answer resolves a later question
without a fresh signature. An owner declares a signed answer a rule,
with the phrases the question must mention and an expiry; a matching
question comes back authorized (signoff rule), anything else stays
request-specific. Ending the rule, its expiry, an unmet condition and a
correction each take that authorization away."""

import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from fixtures import OfflineCase

from bridge import canvas
from bridge.config import Config
from bridge.delivery import handle_slack_event
from fixtures import ready_server as make_server
from bridge.store import Invalid, Store
from test_delivery import FakeSlack

CFG = Config(model_api="none")
REPO = "acme/ledger"
QUESTION = "Round the overage charge to whole cents?"


class RuleCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "rules.db")
        self.graph = self.store.graph
        self.slack = FakeSlack()
        self.delivery = self.store.connect_delivery(self.slack, base_url="https://bridge.acme.test")
        self.store.add_owner({"name": "Priya Natarajan", "team": "Billing", "patterns": "billing/*"})
        with self.graph.transaction():
            self.priya = self.graph.add_person("Priya Natarajan", email="priya@acme.example", slack_id="UPRI")
        # These tests exercise what a rule does once the organization has
        # turned automatic rules on; Raven's default keeps them off.
        self.store.update_settings({"auto_rules": True})

    def task(self, key, title="Add usage-based pricing for overage"):
        return canvas.start_task(self.store, CFG, {"title": title, "repo": REPO, "paths": "billing/rates.py",
                                                   "client_key": key, "requester": "wes@acme.example"})["task_id"]

    def node(self, task, context="Globex is on the enterprise plan", ref="n1"):
        return canvas.add_node(self.store, CFG, {"task_id": task, "question": QUESTION, "context": context,
                                                 "paths": "billing/rates.py", "client_ref": ref})

    def signed_answer(self):
        """Task A: Priya answers the question; her answer is signed."""
        task = self.task("a")
        node = self.node(task)
        self.assertEqual(node["status"], "pending")
        row = self.store.get_decision(node["node_id"])
        self.store.answer(node["node_id"], {"answer": "Round half up to whole cents", "rationale": "invoice totals",
                                            "signed_by": "Priya Natarajan", "expected_updated_at": row["updated_at"]})
        canvas.finish_task(self.store, {"task_id": task})
        return node["node_id"]

    def rule(self, decision_id, **extra):
        row = self.store.get_decision(decision_id)
        return self.store.make_rule(decision_id, {"by": "Priya Natarajan", "expected_updated_at": row["updated_at"], **extra})


class RuleTests(RuleCase):
    def test_a_signed_answer_is_evidence_until_it_is_a_rule(self):
        source = self.signed_answer()
        before = self.node(self.task("b"), ref="b1")
        self.assertEqual(before["status"], "resolved")
        self.assertEqual(before["signoff"], "required")
        self.assertFalse(before["authorized"])
        self.assertTrue(before["blocking"])
        made = self.rule(source, conditions="enterprise")
        self.assertTrue(made["reusable"])
        self.assertEqual(made["rule_conditions"], "enterprise")
        self.assertIn("Rule made by Priya Natarajan", made["notice"])
        task = self.task("c")
        self.delivery.deliver_now()
        self.slack.messages.clear()
        covered = self.node(task, ref="c1")
        self.assertEqual(covered["status"], "resolved")
        self.assertEqual(covered["signoff"], "rule")
        self.assertTrue(covered["authorized"])
        self.assertFalse(covered["blocking"])
        self.assertEqual(covered["signed_by"], "Priya Natarajan")
        self.assertIn("covered by the rule Priya Natarajan made", covered["evidence"])
        self.assertIn("enterprise", covered["evidence"])
        self.assertIn("covered by a reusable rule", covered["next"])
        self.assertEqual(self.delivery.deliver_now(), 0)
        done = canvas.finish_task(self.store, {"task_id": task})
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["counts"]["blocking"], 0)
        # The finish says the rule covered it, not a signature on this task.
        self.assertEqual(done["authorized"][0]["signed_by"], "Priya Natarajan")
        self.assertTrue(done["authorized"][0]["by_rule"])
        self.assertIn("1 by a reusable rule made earlier, not signed on this task", done["caveat"])
        tree = canvas.get_tree(self.store, task)
        self.assertTrue(tree["nodes"][0]["reusable"] is False)
        self.assertEqual(self.store.get_decision(covered["node_id"])["source_id"], source)

    def test_conditions_too_long_to_keep_whole_are_refused_not_cut(self):
        """A condition cut short ("customer=ac") is a different condition,
        and the rule would apply where nobody said it should."""
        source = self.signed_answer()
        long = "; ".join(f"customer=account-{i:03d}" for i in range(30))
        self.assertGreater(len(long), 500)
        with self.assertRaises(Invalid) as refused:
            self.rule(source, conditions=long)
        self.assertIn("500 characters", str(refused.exception))
        self.assertFalse(self.store.get_decision(source)["reusable"])

    def test_a_condition_denied_anywhere_is_not_met_by_a_mention_elsewhere(self):
        """Measured live on 5e967e4: the question asked about "the enterprise
        plan policy", the context said the customer is not on it, and the rule
        authorized because the question's own mention was not negated."""
        from bridge.graph import phrase_denied
        source = self.signed_answer()
        self.rule(source, conditions="enterprise plan")
        mixed = canvas.add_node(self.store, CFG, {
            "task_id": self.task("m"), "question": "Round the enterprise plan overage charge to whole cents?",
            "context": "Globex is not on the enterprise plan; this request is for the starter plan, so that "
                       "condition is not satisfied.", "paths": "billing/rates.py", "client_ref": "m1"})
        self.assertEqual(mixed["signoff"], "required", mixed["evidence"])
        self.assertFalse(mixed["authorized"])
        self.assertIn('denies "enterprise plan" somewhere', mixed["evidence"])
        self.assertTrue(phrase_denied("numeric retry option",
                                      "Validate this numeric retry option. This is not a numeric retry option."))
        self.assertFalse(phrase_denied("enterprise plan", "Globex is on the enterprise plan."))

    def test_structured_rule_fact_denied_in_prose_does_not_authorize(self):
        source = self.signed_answer()
        self.rule(source, conditions="customer=probe")
        for i, context in enumerate(("The condition customer=probe does not hold for this request.",
                                     "This is not customer=probe.",
                                     "customer=probe is false for this request.")):
            with self.subTest(context=context):
                node = canvas.add_node(self.store, CFG, {
                    "task_id": self.task("fact-denial-" + str(i)), "question": QUESTION,
                    "context": context, "paths": "billing/rates.py", "facts": "customer=probe"})
                self.assertFalse(node["authorized"], node)
                self.assertEqual(node["signoff"], "required")
        good = canvas.add_node(self.store, CFG, {
            "task_id": self.task("fact-confirmed"), "question": QUESTION,
            "context": "customer=probe is confirmed; it does not change the rounding rule.",
            "paths": "billing/rates.py", "facts": "customer=probe"})
        self.assertTrue(good["authorized"], good)

    def test_a_rule_does_not_stand_in_for_an_approver_who_did_not_sign_its_source(self):
        """Measured live on 5e967e4: a rule its owner made authorized a node on
        a file another person must approve, and the task finished without
        them."""
        source = self.signed_answer()
        self.rule(source, conditions="enterprise")
        with self.graph.transaction():
            wes = self.graph.add_person("Wes Chen", email="wes@acme.example", slack_id="UWES")
            self.graph.add_authority("path", "billing/*", "approves", person_id=wes, repo=REPO, source="config",
                                     asserted_by="test", accepted=True)
        task = self.task("r")
        node = self.node(task, context="Globex is on the enterprise plan", ref="r1")
        self.assertEqual(node["required_signers"], ["Wes Chen"])
        self.assertEqual(node["signoff"], "required")
        self.assertFalse(node["authorized"])
        self.assertIn("the rule does not stand in for Wes Chen", node["evidence"])
        self.delivery.deliver_now()
        self.assertTrue(any(m["channel"] == "DUWES" and "Sign-off wanted" in m["text"] for m in self.slack.messages))
        with self.assertRaises(Invalid):
            canvas.finish_task(self.store, {"task_id": task})
        row = self.store.get_decision(node["node_id"])
        signed = canvas.sign_off(self.store, node["node_id"], {"by": "Wes Chen", "expected_updated_at": row["updated_at"]})
        self.assertTrue(signed["authorized"])

    def test_conditions_and_expiry_gate_the_rule(self):
        source = self.signed_answer()
        self.rule(source, conditions="enterprise plan; overage")
        unmet = self.node(self.task("d"), context="Globex is on the starter plan", ref="d1")
        self.assertEqual(unmet["status"], "resolved")
        self.assertEqual(unmet["signoff"], "required")
        self.assertFalse(unmet["authorized"])
        met = self.node(self.task("e"), context="Globex is on the enterprise plan and the overage is billed monthly", ref="e1")
        self.assertEqual(met["signoff"], "rule")
        with self.assertRaises(Invalid):
            self.rule(source, expires="2001-01-01")
        with self.assertRaises(Invalid):
            self.rule(source, expires="next week")
        later = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
        made = self.rule(source, conditions="", expires=later)
        self.assertTrue(made["rule_expires"].startswith(later))
        fresh = self.node(self.task("f"), context="anything at all", ref="f1")
        self.assertEqual(fresh["signoff"], "rule")
        with self.graph.transaction():
            self.graph.db.execute("UPDATE decisions SET rule_expires=? WHERE id=?",
                                  ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), source))
        expired = self.node(self.task("g"), ref="g1")
        self.assertEqual(expired["signoff"], "required")
        self.assertFalse(expired["authorized"])

    def test_ending_the_rule_restores_sign_off(self):
        source = self.signed_answer()
        self.rule(source)
        self.assertEqual(self.node(self.task("h"), ref="h1")["signoff"], "rule")
        ended = self.rule(source, end=True)
        self.assertFalse(ended["reusable"])
        self.assertIn("No longer a rule", ended["notice"])
        self.assertEqual(self.node(self.task("i"), ref="i1")["signoff"], "required")
        kinds = [r["kind"] for r in self.graph.db.execute("SELECT kind FROM events WHERE decision_id=? ORDER BY id", (source,))]
        self.assertIn("rule_made", kinds)
        self.assertIn("rule_ended", kinds)
        with self.assertRaises(Invalid):
            self.rule(source, end=True)

    def test_only_a_signed_answer_can_be_a_rule(self):
        task = self.task("j")
        pending = self.node(task)
        with self.assertRaises(Invalid):
            self.rule(pending["node_id"])
        settled = canvas.settle_node(self.store, {"task_id": task, "node_id": pending["node_id"],
                                                  "answer": "Round down", "rationale": "simpler"})
        self.assertEqual(settled["signoff"], "required")
        with self.assertRaises(Invalid):
            self.rule(pending["node_id"])
        stale = self.store.get_decision(pending["node_id"])
        canvas.sign_off(self.store, pending["node_id"], {"by": "Priya Natarajan", "expected_updated_at": stale["updated_at"]})
        with self.assertRaises(Invalid):
            self.store.make_rule(pending["node_id"], {"by": "Priya Natarajan", "expected_updated_at": stale["updated_at"]})
        made = self.rule(pending["node_id"])
        self.assertTrue(made["reusable"])

    def test_correcting_the_rule_puts_what_it_covered_in_doubt(self):
        source = self.signed_answer()
        self.rule(source)
        task = self.task("k")
        covered = self.node(task, ref="k1")
        self.assertTrue(covered["authorized"])
        row = self.store.get_decision(source)
        canvas.sign_off(self.store, source, {"by": "Priya Natarajan", "answer": "Round to the nearest cent, half even",
                                             "rationale": "finance asked", "expected_updated_at": row["updated_at"]})
        again = canvas.node_view(self.store, covered["node_id"])
        self.assertTrue(again["needs_review"])
        self.assertFalse(again["authorized"])
        self.assertEqual(again["signoff"], "required")
        with self.assertRaises(Invalid):
            canvas.finish_task(self.store, {"task_id": task})
        # The corrected answer is still the rule: a new question gets the new text.
        newer = self.node(self.task("l"), ref="l1")
        self.assertEqual(newer["signoff"], "rule")
        self.assertIn("half even", newer["answer"])


class AutoRulesOffTests(RuleCase):
    def test_with_automatic_rules_off_a_matching_rule_still_wants_a_signature(self):
        self.store.update_settings({"auto_rules": False})
        source = self.signed_answer()
        made = self.rule(source, conditions="enterprise")
        self.assertIn("automatic rule authorization is off", made["notice"])
        covered = self.node(self.task("z"), ref="z1")
        self.assertEqual(covered["status"], "resolved")
        self.assertEqual(covered["signoff"], "required")
        self.assertFalse(covered["authorized"])
        self.assertTrue(covered["blocking"])
        self.assertIn("automatic rule authorization is off", covered["evidence"])
        with self.assertRaises(Invalid):
            canvas.finish_task(self.store, {"task_id": covered["task_id"]})


class RuleChannelTests(RuleCase):
    def test_a_rule_from_slack_and_over_http(self):
        task = self.task("m")
        node = self.node(task)
        self.delivery.deliver_now()
        ask = self.slack.messages[-1]
        def reply(text, eid):
            return handle_slack_event(self.delivery, {
                "type": "event_callback", "event_id": eid,
                "event": {"type": "message", "channel": "DUPRI", "thread_ts": ask["ts"],
                          "user": "UPRI", "text": text}})
        reply("Round half up to whole cents because invoice totals are shown that way", "e1")
        self.assertIn("Recorded as Priya Natarajan's answer", self.slack.messages[-1]["text"])
        reply("rule if enterprise plan until 2030-01-01", "e2")
        self.assertIn("Rule made by Priya Natarajan", self.slack.messages[-1]["text"])
        row = self.store.get_decision(node["node_id"])
        self.assertTrue(row["reusable"])
        self.assertEqual(row["rule_conditions"], "enterprise plan")
        self.assertTrue(row["rule_expires"].startswith("2030-01-01"))
        self.assertEqual(row["rule_by"], "Priya Natarajan")

        server = make_server(self.store, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))
        url = f"http://127.0.0.1:{server.server_port}"
        with urlopen(url + "/api/state") as resp:
            csrf = json.load(resp)["csrf_token"]

        def post(path, body):
            req = Request(url + path, data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json", "X-Bridge-CSRF": csrf})
            with urlopen(req) as resp:
                return json.load(resp)
        ended = post(f"/api/decisions/{node['node_id']}/rule", {"end": True, "by": "Priya Natarajan",
                                                                "expected_updated_at": row["updated_at"]})
        self.assertFalse(ended["reusable"])
        current = self.store.get_decision(node["node_id"])
        made = post(f"/api/decisions/{node['node_id']}/rule", {"by": "Priya Natarajan", "conditions": "overage",
                                                               "expected_updated_at": current["updated_at"]})
        self.assertTrue(made["reusable"])
        self.assertEqual(made["rule_conditions"], "overage")
        self.assertEqual(made["rule_expires"], "")


if __name__ == "__main__":
    unittest.main()
