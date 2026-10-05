"""Reaching the person: the outbox, Slack messages, replies in the
thread, hand-offs that teach routing, and what the answer teaches.

A fake transport stands in for Slack: it records what would be posted
and can be told to fail, so sends, retries, backoff, dedupe, fallback
and the thread replies are all exercised without the network."""

import hashlib
import hmac
import json
import os
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

from fixtures import OfflineCase

from bridge import canvas
from bridge.config import Config
from bridge.delivery import handle_slack_event, render, verify_slack_signature
from fixtures import ready_server as make_server
from bridge.store import Store

CFG = Config(model_api="none")


class FakeSlack:
    name = "slack"

    def __init__(self):
        self.messages = []
        self.fail = False
        self.opened = []
        self.counter = 0

    def open_dm(self, user_id):
        self.opened.append(user_id)
        return "D" + user_id

    def post_message(self, channel, text, blocks=None, thread_ts=""):
        if self.fail:
            raise RuntimeError("Slack chat.postMessage failed: ratelimited")
        self.counter += 1
        ts = f"1700000000.{self.counter:06d}"
        self.messages.append({"channel": channel, "text": text, "blocks": blocks, "thread_ts": thread_ts, "ts": ts})
        return ts


class DeliveryCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "delivery.db")
        self.graph = self.store.graph
        self.slack = FakeSlack()
        self.delivery = self.store.connect_delivery(self.slack, base_url="https://bridge.acme.test")
        with self.graph.transaction():
            self.wes = self.graph.add_person("Wes Chen", email="wes@acme.example", slack_id="UWES")
            self.marisol = self.graph.add_person("Marisol Vega", email="marisol@acme.example", slack_id="UMAR")
            self.priya = self.graph.add_person("Priya Natarajan", email="priya@acme.example", slack_id="UPRI")
            self.graph.add_authority("path", "billing/*", "decides", person_id=self.wes)

    def task(self, title="Add usage-based pricing", requester="Priya Natarajan"):
        return canvas.start_task(self.store, CFG, {"title": title, "repo": "acme/platform", "requester": requester,
                                                   "paths": "billing/usage.py"})["task_id"]

    def node(self, task, question="Should we bill the usage spike, or exclude it as a load test?", **extra):
        return canvas.add_node(self.store, CFG, {"task_id": task, "question": question, "paths": "billing/usage.py",
                                                 "context": "Usage jumped 11x on enterprise-two during the rollout.",
                                                 "options": "Bill it | Exclude the load test", **extra})

    def reply(self, message, user, text, event_id=""):
        return self.delivery.receive(message["channel"], message["ts"], user, text, event_id=event_id)


class QuietTests(DeliveryCase):
    """A reviewer counted eleven outbound messages for three decisions,
    two of them the same message twice, and a reply refused because a
    hand-on had moved the node. One decision is one thread, one thing
    said is one message, and being handed something is not a reason to
    reject the answer you send back."""

    def test_the_same_message_is_not_sent_twice(self):
        n = self.node(self.task())
        self.assertEqual(self.delivery.deliver_now(), 1)
        # Touched without changing what Wes would read.
        self.graph.db.execute("UPDATE decisions SET updated_at=? WHERE id=?",
                              ("2026-01-01T00:00:00+00:00", n["node_id"]))
        self.assertIsNone(self.store.notify(n["node_id"], "ask"))
        self.assertEqual(self.delivery.deliver_now(), 0)
        self.assertEqual(len(self.slack.messages), 1)

    def test_one_decision_is_one_thread(self):
        t = self.task()
        n = self.node(t)
        self.delivery.deliver_now()
        first = self.slack.messages[0]
        canvas.settle_node(self.store, {"task_id": t, "node_id": n["node_id"], "answer": "Bill it.",
                                        "rationale": "policy"})
        self.assertEqual(self.delivery.deliver_now(), 1)
        later = self.slack.messages[-1]
        self.assertEqual(later["thread_ts"], first["ts"], "the second word went to a new place")
        # And a reply in that thread finds the newest thing Bridge said.
        note = self.delivery.notification_for_thread(first["channel"], first["ts"])
        self.assertEqual(note["kind"], "signoff")

    def test_being_handed_a_node_does_not_make_your_answer_stale(self):
        t = self.task()
        n = self.node(t)
        self.delivery.deliver_now()
        # Bridge resolved it and wants a signature, then it is handed on.
        canvas.settle_node(self.store, {"task_id": t, "node_id": n["node_id"], "answer": "Bill it.",
                                        "rationale": "policy"})
        self.store.refer(n["node_id"], {"person": self.marisol, "by": "Wes Chen"})
        self.delivery.deliver_now()
        handed = next(m for m in self.slack.messages if "Handed to you, Marisol" in m["text"])
        reply = self.reply(handed, "UMAR", "answer: exclude it because it was our own load test")
        self.assertNotIn("Not recorded", reply)
        row = self.store.get_decision(n["node_id"])
        self.assertEqual(row["answered_by"], "Marisol Vega")

    def test_a_reading_is_offered_not_applied_and_the_person_confirms_it(self):
        """A person who writes what they decided, in their own words, is
        read back to rather than refused. The reading is never applied on
        the model's word: what makes it a decision is their `yes`."""
        n = self.node(self.task())
        self.delivery.deliver_now()
        asked = self.slack.messages[0]
        read = {"kind": "answer", "answer": "Exclude the spike.", "rationale": "it was our own load test", "to": ""}
        with patch("bridge.llm.read_reply", return_value=read):
            offered = self.reply(asked, "UWES", "just exclude that spike, it was our own load test")
        self.assertIn("Not recorded yet", offered)
        self.assertIn("Exclude the spike.", offered)
        self.assertEqual(self.store.get_decision(n["node_id"])["status"], "pending")
        # Nothing the model said is on the record until the person says so.
        applied = self.reply(asked, "UWES", "yes", event_id="confirm-1")
        self.assertIn("Recorded as Wes Chen's answer", applied)
        row = self.store.get_decision(n["node_id"])
        self.assertEqual((row["answer"], row["answered_by"]), ("Exclude the spike.", "Wes Chen"))

    def test_a_reading_the_person_declines_records_nothing(self):
        n = self.node(self.task())
        self.delivery.deliver_now()
        asked = self.slack.messages[0]
        read = {"kind": "answer", "answer": "Bill it in full.", "rationale": "", "to": ""}
        with patch("bridge.llm.read_reply", return_value=read):
            self.reply(asked, "UWES", "leaning towards billing it but not sure")
        dropped = self.reply(asked, "UWES", "no", event_id="decline-1")
        self.assertIn("Dropped", dropped)
        self.assertEqual(self.store.get_decision(n["node_id"])["status"], "pending")
        # And a later bare `yes` has nothing to take.
        self.assertIn("Not recorded", self.reply(asked, "UWES", "yes", event_id="decline-2"))

    def test_an_acknowledgement_never_becomes_a_signature(self):
        """Measured on the reply panel: the model read a bare "ok" as
        agreeing with the answer on the table, which would put somebody's
        name on a decision they only said they had seen."""
        n = self.node(self.task())
        canvas.settle_node(self.store, {"task_id": self.store.get_decision(n["node_id"])["run_id"],
                                        "node_id": n["node_id"], "answer": "Bill it.", "rationale": "policy"})
        self.delivery.deliver_now()
        asked = self.slack.messages[-1]
        agreeable = {"kind": "signoff", "answer": "", "rationale": "", "to": ""}
        for ack in ("ok", "okay", "sure", "thanks", "got it", "noted"):
            with patch("bridge.llm.read_reply", return_value=agreeable) as called:
                said = self.reply(asked, "UWES", ack, event_id=f"ack-{ack}")
            self.assertEqual(called.call_count, 0, f"{ack!r} should never reach the model")
            self.assertIn("Not recorded", said)
            self.assertNotEqual(self.store.get_decision(n["node_id"])["status"], "approved")

    def co_signed(self):
        """Wes answers in his thread and Marisol, a required approver,
        signs in hers: the sequence measured live on 63eb671."""
        with self.graph.transaction():
            self.graph.add_authority("path", "billing/usage.py", "approves", person_id=self.marisol)
        n = self.node(self.task())
        self.delivery.deliver_now()
        asked = next(m for m in self.slack.messages if m["channel"] == "DUWES")
        self.assertIn("Recorded as Wes Chen's answer",
                      self.reply(asked, "UWES", "answer: Exclude the load test because it was our own", event_id="a"))
        self.delivery.deliver_now()
        wanted = next(m for m in self.slack.messages if m["channel"] == "DUMAR" and "Sign-off wanted" in m["text"])
        self.assertIn("Signed off by Marisol Vega", self.reply(wanted, "UMAR", "sign off", event_id="s"))
        self.delivery.deliver_now()
        row = self.store.get_decision(n["node_id"])
        self.assertEqual((row["signoff"], row["signed_by"]), ("signed", "Wes Chen, Marisol Vega"))
        return n, asked, wanted

    def test_a_co_signature_does_not_make_the_answerers_thread_stale(self):
        """Measured live on 63eb671: Mira answered, Theo signed, and Mira's
        `rule if violet diagnostics until 2027-12-31` in her own thread was
        refused as stale, with a fresh "Sign-off wanted from Mira Runtime"
        though both had signed the answer as it stood."""
        n, asked, _ = self.co_signed()
        before = len(self.slack.messages)
        made = self.reply(asked, "UWES", "rule if load test until 2027-12-31", event_id="r")
        self.assertIn("Rule made by Wes Chen", made)
        row = self.store.get_decision(n["node_id"])
        self.assertEqual((row["reusable"], row["rule_conditions"]), (1, "load test"))
        self.delivery.deliver_now()
        self.assertFalse([m for m in self.slack.messages[before:] if "Sign-off wanted from Wes Chen" in m["text"]],
                         self.slack.messages[before:])

    def test_a_rule_is_refused_on_an_answer_the_person_has_not_signed(self):
        """A rule request applies to the answer as it stands: one changed
        after this person signed is not theirs to make reusable."""
        n, asked, wanted = self.co_signed()
        current = self.store.get_decision(n["node_id"])
        canvas.sign_off(self.store, n["node_id"], {"by": "Marisol Vega", "answer": "Bill it at half rate",
                                                    "rationale": "finance asked", "expected_updated_at": current["updated_at"]})
        self.delivery.deliver_now()
        before = len(self.slack.messages)
        refused = self.reply(asked, "UWES", "rule if load test", event_id="r2")
        self.assertIn("Not recorded", refused)
        self.assertIn("Bill it at half rate", refused)
        self.assertFalse(self.store.get_decision(n["node_id"])["reusable"])
        # Marisol signed the answer as it stands: nobody asks her again.
        self.reply(wanted, "UMAR", "rule if load test", event_id="r3")
        self.delivery.deliver_now()
        self.assertFalse([m for m in self.slack.messages[before:] if "Sign-off wanted from Marisol Vega" in m["text"]])

    def test_a_reading_read_back_survives_somebody_else_signing(self):
        """A reading is about what the decision says. Another approver
        signing between the read-back and the `yes` changes nothing the
        person agreed to."""
        with self.graph.transaction():
            ola = self.graph.add_person("Ola Ops", email="ola@acme.example", slack_id="UOLA")
            self.graph.add_authority("path", "billing/usage.py", "approves", person_id=self.marisol)
            self.graph.add_authority("path", "billing/usage.py", "approves", person_id=ola)
        n = self.node(self.task())
        self.delivery.deliver_now()
        asked = next(m for m in self.slack.messages if m["channel"] == "DUWES")
        self.reply(asked, "UWES", "answer: Exclude the load test because it was our own", event_id="a")
        self.delivery.deliver_now()
        to_marisol = next(m for m in self.slack.messages if m["channel"] == "DUMAR" and "Sign-off wanted" in m["text"])
        to_ola = next(m for m in self.slack.messages if m["channel"] == "DUOLA" and "Sign-off wanted" in m["text"])
        agrees = {"kind": "signoff", "answer": "", "rationale": "", "to": ""}
        with patch("bridge.llm.read_reply", return_value=agrees):
            self.assertIn("Not recorded yet", self.reply(to_marisol, "UMAR", "looks right to me", event_id="m1"))
        self.assertIn("Signed off by Ola Ops", self.reply(to_ola, "UOLA", "sign off", event_id="o1"))
        self.assertIn("Signed off by Marisol Vega", self.reply(to_marisol, "UMAR", "yes", event_id="m2"))
        row = self.store.get_decision(n["node_id"])
        self.assertEqual(row["signoff"], "signed")
        self.assertIn("Marisol Vega", row["signed_by"])

    def test_a_record_the_question_names_is_stated_with_its_status(self):
        """Measured live on 63eb671: a question asked "as proposed in
        NET-102", and nothing in its message said NET-102 was cancelled."""
        from bridge.store import repo_key
        repo = self.graph.resolve_repo(repo_key("acme/platform"))
        with self.graph.transaction():
            self.graph.upsert_intent(repo, "ticket", "NET-102", "Rejected proposal subtract jitter",
                                     "REJECTED, superseded by NET-201. Do not implement.", "Wes Chen",
                                     "2026-09-29T12:00:00Z", status="Cancelled", resolved=False)
            self.graph.upsert_intent(repo, "ticket", "NET-201", "Retry-After jitter hard total ceiling",
                                     "Settled.", "Wes Chen", "2026-09-29T12:00:00Z", status="Done")
        self.node(self.task(), question="Should usage spikes be billed as proposed in NET-102?",
                  context="NET-201 settled the ceiling; UTF-8 names nothing.")
        self.delivery.deliver_now()
        text = self.slack.messages[-1]["text"]
        self.assertIn("Records it names: NET-102 [Cancelled] \u201cRejected proposal subtract jitter\u201d: not in force, "
                      "so it is history, not current policy; NET-201 [Done] \u201cRetry-After jitter hard total "
                      "ceiling\u201d.", text)

    def test_a_reply_about_a_question_that_changed_is_still_refused(self):
        n = self.node(self.task())
        self.delivery.deliver_now()
        asked = self.slack.messages[0]
        self.graph.db.execute("UPDATE decisions SET question=? WHERE id=?",
                              ("Something else entirely?", n["node_id"]))
        reply = self.reply(asked, "UWES", "answer: bill it because policy")
        self.assertIn("Not recorded", reply)
        self.assertIn("the question changed", reply)


class OutboxTests(DeliveryCase):
    def test_a_routed_question_reaches_its_owner_in_slack(self):
        n = self.node(self.task())
        self.assertEqual((n["status"], n["owner"]), ("pending", "Wes Chen"))
        queued = self.delivery.list("queued")
        self.assertEqual([(q["kind"], q["person_name"], q["destination"]) for q in queued], [("ask", "Wes Chen", "UWES")])
        self.assertEqual(self.delivery.deliver_now(), 1)
        self.assertEqual(self.slack.opened, ["UWES"])
        message = self.slack.messages[0]
        self.assertEqual(message["channel"], "DUWES")
        text = message["text"]
        self.assertIn("A decision needs you, Wes Chen", text)
        self.assertIn("Should we bill the usage spike", text)
        self.assertIn("Task: Add usage-based pricing", text)
        self.assertIn("enterprise-two", text)
        self.assertIn("Why you: verified: Wes Chen decides for billing/*", text)
        self.assertIn("Options: Bill it | Exclude the load test", text)
        self.assertIn("not me @person", text)
        self.assertIn(f"https://bridge.acme.test/#inbox (decision {n['node_id']})", text)
        sent = self.delivery.list("sent")[0]
        self.assertEqual(sent["external_ref"], f"DUWES:{message['ts']}")
        # Nothing goes twice for one state; a second call sends nothing.
        self.assertEqual(self.delivery.deliver_now(), 0)
        self.assertIsNone(self.store.notify(n["node_id"], "ask"))

    def test_a_signoff_wanted_and_a_review_reach_the_owner(self):
        t = self.task()
        n = self.node(t)
        self.delivery.deliver_now()
        canvas.settle_node(self.store, {"task_id": t, "node_id": n["node_id"], "answer": "Bill it.", "rationale": "policy"})
        self.assertEqual(self.delivery.deliver_now(), 1)
        self.assertIn("Sign-off wanted from Wes Chen", self.slack.messages[-1]["text"])
        self.assertIn("Answer on the table (the agent settled this itself, unconfirmed): Bill it.",
                      self.slack.messages[-1]["text"])
        self.assertIn("Reply `sign off`", self.slack.messages[-1]["text"])

    def test_no_slack_id_falls_back_to_the_channel_or_fails_visibly(self):
        with self.graph.transaction():
            nobody = self.graph.add_person("No Slack", email="ns@acme.example")
            self.graph.add_authority("category", "pricing", "decides", person_id=nobody)
        n = self.node(self.task(), question="Do we apply per-seat minimums under usage-based pricing?")
        self.assertEqual(n["owner"], "No Slack")
        failed = self.delivery.list("failed")
        self.assertEqual(len(failed), 1)
        self.assertIn("no Slack id", failed[0]["last_error"])
        self.assertEqual(self.delivery.deliver_now(), 0)
        # A fallback channel catches it on retry, with a note naming the person.
        self.store.update_settings({"slack_fallback_channel": "C-BRIDGE"})
        self.delivery.retry(failed[0]["id"])
        self.assertEqual(self.delivery.deliver_now(), 1)
        self.assertEqual(self.slack.messages[-1]["channel"], "C-BRIDGE")
        self.assertIn("No Slack has no Slack id in Bridge; posted here instead", self.slack.messages[-1]["text"])

    def test_a_failed_send_backs_off_and_stays_visible(self):
        self.node(self.task())
        self.slack.fail = True
        self.assertEqual(self.delivery.deliver_now(), 0)
        row = self.delivery.list()[0]
        self.assertEqual((row["state"], row["attempts"]), ("queued", 1))
        self.assertIn("ratelimited", row["last_error"])
        self.assertGreater(row["next_attempt"], time.time() + 10)
        self.slack.fail = False
        self.assertEqual(self.delivery.deliver_now(), 0)  # not due yet
        self.graph.db.execute("UPDATE notifications SET next_attempt=0")
        self.assertEqual(self.delivery.deliver_now(), 1)
        self.assertEqual(self.delivery.list("sent")[0]["attempts"], 2)

    def test_a_newer_state_supersedes_what_has_not_gone_out(self):
        n = self.node(self.task())
        # Reassigned before the ask went out: the ask is superseded, one message goes.
        self.store.assign(n["node_id"], {"owner_id": self.graph.owner_id_for("Marisol Vega"), "by": "Wes Chen"})
        states = {(r["kind"], r["person_name"]): r["state"] for r in self.delivery.list()}
        self.assertEqual(states[("ask", "Wes Chen")], "queued")
        self.assertEqual(states[("reassigned", "Marisol Vega")], "queued")
        self.assertEqual(self.delivery.deliver_now(), 2)
        self.assertEqual(self.slack.opened, ["UWES", "UMAR"])

    def test_the_requester_hears_the_answer_when_a_team_asks_for_it(self):
        """Off by default: the requester is usually the agent, which reads
        the answer off the tree. A team that wants the copy turns it on."""
        self.store.update_settings({"notify_requester": True})
        n = self.node(self.task(requester="Priya Natarajan"))
        self.delivery.deliver_now()
        self.store.answer(n["node_id"], {"answer": "Bill it.", "rationale": "policy"})
        self.assertEqual(self.delivery.deliver_now(), 1)
        self.assertEqual(self.slack.messages[-1]["channel"], "DUPRI")
        self.assertIn("Your question was answered, Priya Natarajan", self.slack.messages[-1]["text"])
        self.assertIn("Bill it.", self.slack.messages[-1]["text"])

    def test_the_requester_is_not_told_by_default(self):
        n = self.node(self.task(requester="Priya Natarajan"))
        self.delivery.deliver_now()
        before = len(self.slack.messages)
        self.store.answer(n["node_id"], {"answer": "Bill it.", "rationale": "policy"})
        self.assertEqual(self.delivery.deliver_now(), 0)
        self.assertEqual(len(self.slack.messages), before)

    def test_nothing_is_queued_without_a_channel(self):
        store = Store(Path(self.temp.name) / "silent.db")
        store.add_owner({"name": "Wes", "team": "Billing", "patterns": "billing/*"})
        t = canvas.start_task(store, CFG, {"title": "t", "repo": "acme/platform", "paths": "billing/usage.py"})["task_id"]
        canvas.add_node(store, CFG, {"task_id": t, "question": "Should we bill it?", "paths": "billing/usage.py"})
        self.assertEqual(store.delivery.list(), [])


class ReplyTests(DeliveryCase):
    def test_an_answer_in_the_thread_is_recorded_as_the_owner(self):
        n = self.node(self.task())
        self.delivery.deliver_now()
        message = self.slack.messages[0]
        ack = self.reply(message, "UWES", "Exclude the load test because the accounts share the internal tag", event_id="Ev1")
        self.assertIn("Recorded as Wes Chen's answer", ack)
        self.assertIn("bridge_get_tree", ack)
        row = self.store.get_decision(n["node_id"])
        self.assertEqual((row["status"], row["answer"], row["rationale"], row["signed_by"]),
                         ("approved", "Exclude the load test", "the accounts share the internal tag", "Wes Chen"))
        self.assertIn("slack: Wes Chen", [e["detail"] for e in row["events"] if e["kind"] == "owner_approved"][0])
        # The same event delivered twice does nothing twice.
        self.assertEqual(self.reply(message, "UWES", "Bill it after all", event_id="Ev1"), "")
        self.assertEqual(self.store.get_decision(n["node_id"])["answer"], "Exclude the load test")

    def test_a_reply_from_someone_unknown_records_nothing(self):
        n = self.node(self.task())
        self.delivery.deliver_now()
        ack = self.reply(self.slack.messages[0], "USTRANGER", "Bill it")
        self.assertIn("could not verify you as an active member", ack)
        self.assertEqual(self.store.get_decision(n["node_id"])["status"], "pending")

    def test_sign_off_and_correction_from_the_thread(self):
        t = self.task()
        n = self.node(t)
        canvas.settle_node(self.store, {"task_id": t, "node_id": n["node_id"], "answer": "Bill it.", "rationale": "agent"})
        self.delivery.deliver_now()
        wanted = [m for m in self.slack.messages if "Sign-off wanted" in m["text"]][0]
        ack = self.reply(wanted, "UWES", "sign off")
        self.assertIn("Signed off by Wes Chen", ack)
        row = self.store.get_decision(n["node_id"])
        self.assertEqual((row["signoff"], row["signed_by"]), ("signed", "Wes Chen"))
        m = self.node(t, question="Should trial accounts be metered?", context="Trial cohort", paths="billing/meter.py")
        canvas.settle_node(self.store, {"task_id": t, "node_id": m["node_id"], "answer": "Yes.", "rationale": "agent"})
        self.delivery.deliver_now()
        wanted = [x for x in self.slack.messages if "Sign-off wanted" in x["text"] and "trial" in x["text"]][0]
        ack = self.reply(wanted, "UWES", "No, trials are never metered because they have not paid")
        self.assertIn("Corrected and signed by Wes Chen", ack)
        row = self.store.get_decision(m["node_id"])
        self.assertEqual((row["status"], row["answer"], row["rationale"]), ("approved", "No, trials are never metered", "they have not paid"))

    def test_a_stale_thread_is_refused_not_overwritten(self):
        n = self.node(self.task())
        self.delivery.deliver_now()
        message = self.slack.messages[0]
        self.store.answer(n["node_id"], {"answer": "Bill it.", "rationale": "policy"})
        # The thread's revision is stale, but a reply is an explicit correction
        # by the owner: recorded as a signed correction, never applied blind.
        ack = self.reply(message, "UWES", "Actually exclude it because the tag is internal")
        self.assertIn("Corrected and signed by Wes Chen", ack)
        self.assertEqual(self.store.get_decision(n["node_id"])["answer"], "Actually exclude it")

    def test_an_answer_to_sign_is_shown_whole_in_slack(self):
        """Measured live: answers were cut at 600 characters with no mark, in
        the message a person replies `sign off` to, and the whole message
        was cut at Slack's section limit."""
        from bridge.delivery import _sections
        row = {"id": "d1", "question": "Q?", "kind": "evidence", "status": "resolved"}
        medium = ("Jitter applies only when the caller opts in. " * 30).strip()
        text = render({**row, "answer": medium}, "signoff", "Ana", "")["text"]
        self.assertIn(medium, text)
        self.assertNotIn("cut here", text)
        long = ("Jitter applies only when the caller opts in. " * 80).strip()
        text = render({**row, "answer": long}, "signoff", "Ana", "")["text"]
        self.assertIn(long, text)
        self.assertNotIn("read the rest in the inbox", text)
        message = "\n".join(f"line {i}: " + "word " * 60 for i in range(40))
        parts = _sections(message, 2900)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 2900 for p in parts))
        self.assertEqual("\n".join(parts), message)

    def test_a_reply_read_back_for_confirmation_is_never_cut(self):
        """The person confirms the reading as their answer; it was cut at 700
        characters."""
        from bridge import llm
        long = ("Keep jitter off by default and let callers opt in per pool. " * 20).strip()

        def complete_json(self, purpose, system, prompt, **kw):
            return {"kind": "answer", "confident": True, "answer": replies[0], "rationale": "because"}
        env = patch.dict(os.environ, {"BRIDGE_SEMANTIC": "1", "BRIDGE_CLAUDE_BIN": "/nonexistent/claude"})
        env.start()
        self.addCleanup(env.stop)
        with patch.object(llm.Client, "complete_json", complete_json):
            replies = [long]
            self.assertEqual(llm.read_reply(Config(), "Jitter?", "", long)["answer"], long)
            replies[0] = long * 4
            self.assertEqual(llm.read_reply(Config(), "Jitter?", "", long), {})

    def test_the_message_says_what_bridge_cites_not_how_the_search_went(self):
        from bridge.delivery import _found_for_owner
        noise = ("memory: best match scored 0.28, under the 0.6 floor; records: candidates matched background words "
                 "but not the question's focus terms (may, tls, security); semantic: no candidate truly answers this")
        self.assertEqual(_found_for_owner(noise), "")
        cited = (noise + "; memory: decision 0123456789ab is context, not a signed answer here; "
                 "records: retrieved context slack C1:1.2: partner accounts are billed at list rate")
        found = _found_for_owner(cited)
        self.assertIn("decision 0123456789ab is context", found)
        self.assertIn("retrieved context slack C1:1.2", found)
        self.assertNotIn("scored", found)
        row = {"id": "x", "question": "Q?", "evidence": noise, "owner_evidence": "verified: Wes decides for billing/*"}
        self.assertNotIn("What Bridge found", render(row, "ask", "Wes", "")["text"])

    def test_not_me_hands_it_on_and_teaches_routing(self):
        n = self.node(self.task())
        self.delivery.deliver_now()
        ack = self.reply(self.slack.messages[0], "UWES", "not me <@UMAR>")
        self.assertIn("Handed to Marisol Vega", ack)
        self.assertIn("billing", ack)
        row = self.store.get_decision(n["node_id"])
        self.assertEqual(row["owner_name"], "Marisol Vega")
        self.assertIn("Referred to Marisol Vega by Wes Chen", row["routing_reason"])
        learned = [a for a in self.store.authority() if a["source"] == "referral"]
        self.assertEqual([(a["who"], a["scope_kind"], a["scope"], a["accepted"]) for a in learned],
                         [("Marisol Vega", "category", "billing", 0)])
        # The People page shows the reply with the name, not Slack's mention markup.
        self.assertEqual(learned[0]["note"], "not me @Marisol Vega")
        self.assertIn("route_learned", [e["kind"] for e in row["events"]])
        # Marisol hears about it, and the next billing decision goes to her as a candidate.
        self.assertEqual(self.delivery.deliver_now(), 1)
        self.assertEqual(self.slack.messages[-1]["channel"], "DUMAR")
        self.assertIn("Handed to you, Marisol Vega", self.slack.messages[-1]["text"])
        # Why her: the hand-on, in Wes's words, not the authority that made it his.
        self.assertIn('Why you: handed on by Wes Chen: "not me @Marisol Vega"; once you answer, Bridge routes '
                      "billing decisions", self.slack.messages[-1]["text"])
        self.assertNotIn("verified: Wes Chen", self.slack.messages[-1]["text"])
        # Her answer accepts the referral; from then on she decides billing.
        ack = self.reply(self.slack.messages[-1], "UMAR", "Exclude it because it was our load test")
        self.assertIn("Recorded as Marisol Vega's answer", ack)
        accepted = [a for a in self.store.authority() if a["source"] == "referral"][0]
        self.assertEqual(accepted["accepted"], 1)
        m = self.node(self.task(title="Another billing task"), question="Should overage be invoiced monthly?",
                      context="enterprise-three")
        self.assertEqual(m["owner"], "Marisol Vega", m["owner_evidence"])
        self.assertTrue(m["owner_evidence"].startswith("verified: Marisol Vega decides for billing decisions"))
        self.assertIn("referral, asserted by Wes Chen", m["owner_evidence"])


class LearningTests(DeliveryCase):
    def test_an_answer_on_an_inferred_route_makes_the_answerer_a_candidate(self):
        # Priya has no authority; the question reaches her by path patterns
        # (the legacy owner form) and her answer teaches that she knows billing.
        with self.graph.transaction():
            self.graph.end_authority(self.store.authority()[0]["id"])
        self.store.add_owner({"name": "Priya Natarajan", "team": "Billing", "patterns": "metering/*"})
        priya_row = self.store.authority()[0]
        with self.graph.transaction():
            self.graph.end_authority(priya_row["id"])
        t = self.task()
        n = self.node(t)
        self.assertEqual(n["status"], "unrouted")
        self.store.assign(n["node_id"], {"owner_id": self.graph.owner_id_for("Priya Natarajan")})
        self.store.answer(n["node_id"], {"answer": "Bill it.", "rationale": "policy"})
        learned = [a for a in self.store.authority() if a["source"] == "answer"]
        self.assertEqual([(a["who"], a["scope"], a["role"]) for a in learned], [("Priya Natarajan", "billing", "knows")])


class RenderTests(unittest.TestCase):
    def test_render_carries_what_the_owner_needs(self):
        row = {"id": "d1", "question": "Bill it?", "run_title": "Pricing", "repo": "acme/platform", "brief": "Two accounts spiked.",
               "context": "ctx", "owner_evidence": "verified: Wes decides for billing/* (config); area: x", "evidence": "memory: none",
               "options": json.dumps(["a", "b"]), "answer": "", "answered_by": "", "kind": "new"}
        out = render(row, "ask", "Wes", "https://b", "")
        self.assertIn("Two accounts spiked.", out["text"])
        self.assertIn("Why you: verified: Wes decides for billing/* (config); area: x", out["text"])
        self.assertIn("Options: a | b", out["text"])
        self.assertEqual(out["blocks"][0]["type"], "section")

    def test_a_conflict_leads_the_message_whole_and_the_brief_denies_nothing(self):
        """Measured live on 63eb671: the brief said no current policy was
        given, while the two conflicting policies were quoted lower down
        in the same message, cut at 400 characters."""
        evidence = ("memory: best match scored 0.4, under the 0.6 floor; conflict: decision 1a97e2922e18, signed by "
                    "Mira Runtime, says \"Keep retry error summaries for 11 days.\", and ticket CONFLICT-1 [Done] says "
                    "\"Retry error summary retention decision Approved current policy: retain retry error summaries "
                    "for 19 days, not 11 days.\"; they state different figures, so Bridge serves neither and "
                    "proposes neither: a person decides which stands; related: decision 145c6f563eef asks the same "
                    "question in another scope")
        row = {"id": "d1", "question": "What retention period applies to retry error summaries?", "evidence": evidence,
               "brief": "The system needs to determine what retention period should apply to retry error summaries, "
                        "but no context has been provided about current policies, regulatory requirements, or "
                        "technical constraints that would inform this decision. Guidance is needed on how long these "
                        "summaries should be kept."}
        lines = render(row, "ask", "Mira", "")["text"].split("\n")
        conflict = next(i for i, ln in enumerate(lines) if ln.startswith("*Conflict:* Decision 1a97e2922e18"))
        brief = next(i for i, ln in enumerate(lines) if ln.startswith("The system needs"))
        self.assertLess(conflict, brief)
        self.assertIn("19 days, not 11 days", lines[conflict])
        self.assertTrue(lines[conflict].endswith("a person decides which stands"), lines[conflict])
        self.assertEqual(lines[brief], "The system needs to determine what retention period should apply to retry "
                                       "error summaries. Guidance is needed on how long these summaries should be kept.")
        found = next(ln for ln in lines if ln.startswith("What Bridge found"))
        self.assertNotIn("conflict", found)
        self.assertIn("decision 145c6f563eef", found)

    def test_a_brief_never_says_nothing_was_provided(self):
        from bridge.llm import drop_absence
        cases = {
            "A decision is needed about which Retry metrics suffix normal customers should use. No context was "
            "provided to explain why this choice is needed or what options are available.":
                "A decision is needed about which Retry metrics suffix normal customers should use.",
            "The system needs a retry observation sample rate for customer integrations, but no context was provided "
            "about current rates, requirements, or constraints. A decision is needed on what sample rate to apply.":
                "The system needs a retry observation sample rate for customer integrations. A decision is needed on "
                "what sample rate to apply.",
            "Pick the metrics suffix. The context provided does not specify the available options.":
                "Pick the metrics suffix.",
            "No context was provided.": "",
            "The context says seconds are used elsewhere, so pick a unit.":
                "The context says seconds are used elsewhere, so pick a unit.",
            "The question is whether retry telemetry should retain request tokens. The material gives no further "
            "detail on why it came up.": "The question is whether retry telemetry should retain request tokens.",
            "The owner chooses between amber and violet. Violet gives no reason to change.":
                "The owner chooses between amber and violet. Violet gives no reason to change.",
        }
        for brief, want in cases.items():
            self.assertEqual(drop_absence(brief), want, brief)

    def test_slack_signature(self):
        secret, body, ts = "s3cr3t", b'{"type":"url_verification"}', str(int(time.time()))
        sig = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_slack_signature(secret, ts, body, sig))
        self.assertFalse(verify_slack_signature(secret, ts, body + b" ", sig))
        self.assertFalse(verify_slack_signature("other", ts, body, sig))
        self.assertFalse(verify_slack_signature(secret, str(int(time.time()) - 900), body, sig))


class WebhookTests(DeliveryCase):
    def test_slack_events_reach_the_decision_over_http(self):
        secret = "signing-secret"
        server = make_server(self.store, port=0, slack_signing_secret=secret)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        port = server.server_port

        def post(event, sign=True):
            body = json.dumps(event).encode()
            ts = str(int(time.time()))
            headers = {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json", "X-Slack-Request-Timestamp": ts}
            if sign:
                headers["X-Slack-Signature"] = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body,
                                                                hashlib.sha256).hexdigest()
            req = Request(f"http://127.0.0.1:{port}/webhooks/slack", data=body, method="POST", headers=headers)
            try:
                with urlopen(req) as r:
                    return r.status, json.loads(r.read())
            except Exception as error:  # HTTPError
                return error.code, {}

        self.assertEqual(post({"type": "url_verification", "challenge": "abc"}), (200, {"challenge": "abc"}))
        self.assertEqual(post({"type": "url_verification", "challenge": "abc"}, sign=False)[0], 401)
        n = self.node(self.task())
        self.delivery.deliver_now()
        message = self.slack.messages[0]
        status, _ = post({"type": "event_callback", "event_id": "Ev9", "event": {
            "type": "message", "channel": message["channel"], "thread_ts": message["ts"], "user": "UWES",
            "text": "Bill it because the tag is not ours"}})
        self.assertEqual(status, 200)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            row = self.store.get_decision(n["node_id"])
            if row['status'] == 'approved' and any('Recorded' in m['text'] for m in self.slack.messages):
                break
            time.sleep(.02)
        self.assertEqual((row["status"], row["answer"]), ("approved", "Bill it"))
        # Bridge acknowledged in the thread.
        self.assertTrue(any(m["thread_ts"] == message["ts"] and "Recorded" in m["text"] for m in self.slack.messages))
        self.assertEqual(handle_slack_event(self.delivery, {"type": "event_callback", "event": {"type": "message", "bot_id": "B1"}}), {})


class SlackApiBaseTests(unittest.TestCase):
    """SLACK_API_BASE points the transport at a Slack-compatible Web API:
    an egress proxy, or the stand-in an end-to-end run delivers to."""

    def calls(self, transport):
        seen = []

        class Reply:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(self.body).encode()

        def fake(req, timeout=0):
            seen.append(req.full_url)
            return Reply({"ok": True, "ts": "1.0", "channel": {"id": "D1"}})
        with patch("bridge.delivery.urllib.request.urlopen", side_effect=fake):
            transport.post_message("C1", "hello")
        return seen

    def test_the_default_is_slack(self):
        from bridge.delivery import SlackTransport
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("SLACK_API_BASE", None)
            self.assertEqual(self.calls(SlackTransport("xoxb-test")), ["https://slack.com/api/chat.postMessage"])

    def test_an_api_base_from_the_environment_is_used(self):
        from bridge.delivery import SlackTransport
        with patch.dict("os.environ", {"SLACK_API_BASE": "http://slack.test:8090/api/"}):
            self.assertEqual(self.calls(SlackTransport("xoxb-test")), ["http://slack.test:8090/api/chat.postMessage"])


if __name__ == "__main__":
    unittest.main()
