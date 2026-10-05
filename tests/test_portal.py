"""The wider promise, in one team's hands: how an owner has decided
before, shown as a prediction they confirm; what a decision depends on;
several required approvers who each must sign; notes people add to a
running task; overdue questions in their own view, with reminders to
the owner and the coordinator; the trace of a task; records from
outside git, by API and from a Slack channel; Teams as a channel;
evidence that says where an area came from."""

import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from fixtures import OfflineCase

from bridge import canvas
from bridge.authz import Actor
from bridge.config import Config
from bridge.delivery import TeamsTransport, handle_slack_event
from fixtures import ready_server as make_server
from bridge.store import Invalid, Store
from test_delivery import FakeSlack

CFG = Config(model_api="none")
REPO = "acme/ledger"


class PortalCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "portal.db")
        self.graph = self.store.graph
        self.slack = FakeSlack()
        self.delivery = self.store.connect_delivery(self.slack, base_url="https://bridge.acme.test")
        with self.graph.transaction():
            self.priya = self.graph.add_person("Priya Natarajan", email="priya@acme.example", slack_id="UPRI")
            self.wes = self.graph.add_person("Wes Chen", email="wes@acme.example", slack_id="UWES")
            self.dana = self.graph.add_person("Dana Ortiz", email="dana@acme.example", slack_id="UDAN")
            self.graph.add_authority("path", "billing/*", "decides", person_id=self.priya, repo=REPO,
                                     source="config", asserted_by="test", accepted=True)

    def task(self, key, title="Add usage-based pricing for overage"):
        return canvas.start_task(self.store, CFG, {"title": title, "repo": REPO, "paths": "billing/rates.py",
                                                   "client_key": key, "requester": "wes@acme.example"})["task_id"]

    def node(self, task, question, ref, context="", **extra):
        return canvas.add_node(self.store, CFG, {"task_id": task, "question": question, "context": context,
                                                 "paths": "billing/rates.py", "client_ref": ref, **extra})

    def answer(self, node_id, text, by="Priya Natarajan"):
        row = self.store.get_decision(node_id)
        return self.store.answer(node_id, {"answer": text, "rationale": "policy", "signed_by": by,
                                           "expected_updated_at": row["updated_at"]})


class HowTheyDecideTests(PortalCase):
    def test_the_owners_earlier_answers_become_a_prediction_to_confirm(self):
        first = self.node(self.task("a"), "Which rate applies to Globex usage above the included quota on an annual contract?", "a1")
        self.answer(first["node_id"], "The enterprise rate card: 0.02 per unit over quota")
        # The other side of a condition the answer does not mention: not
        # reused as evidence, but the owner's pattern is shown to them.
        later = self.node(self.task("b"), "Which rate applies to Initech usage above the included quota on a month-to-month rather than an annual contract?", "b1",
                          context="Initech is on the enterprise plan too")
        self.assertEqual(later["status"], "pending")
        self.assertFalse(later["authorized"])
        self.assertTrue(later["blocking"])
        self.assertEqual(later["prediction"], "The enterprise rate card: 0.02 per unit over quota")
        self.assertIn("how they decide: Priya Natarajan answered", later["evidence"])
        self.assertIn(first["node_id"], later["evidence"])
        self.assertIn("a prediction for Priya Natarajan to confirm", later["evidence"])
        self.assertEqual(self.store.get_decision(later["node_id"])["source_id"], first["node_id"])
        # The owner hears the prediction with the question, as a prediction.
        self.delivery.deliver_now()
        ask = next(m for m in self.slack.messages if "Initech" in m["text"])
        self.assertIn("How you decided before: The enterprise rate card", ask["text"])
        self.assertIn("confirm or correct it", ask["text"])
        # Somebody else's answers never speak for this owner.
        from bridge import llm
        from bridge.ladder import _how_they_decide
        question = "Which rate applies to Umbrella usage above the included quota?"
        self.assertEqual(_how_they_decide(self.graph, CFG, REPO, question, "", llm.embed(question), "Wes Chen"), ("", "", ""))
        proposal, source, why = _how_they_decide(self.graph, CFG, REPO, question, "", llm.embed(question), "Priya Natarajan")
        self.assertEqual(source, first["node_id"])
        self.assertIn("0.02 per unit", proposal)
        self.assertIn("how they decide", why)


class ScopedPredictionTests(PortalCase):
    """Measured live on eb9d22d: an answer declared for customer=acme, one
    that excluded customer=globex, and an expired one each came back as a
    confident prediction for a request outside them, and a question about
    a default value got a proposal about documentation."""

    def signed_for(self, question, answer, applicability, ref):
        node = self.node(self.task(ref), question, ref)
        row = self.store.get_decision(node["node_id"])
        self.store.answer(node["node_id"], {"answer": answer, "rationale": "policy", "signed_by": "Priya Natarajan",
                                            "applicability": applicability, "expected_updated_at": row["updated_at"]})
        return node["node_id"]

    def test_an_answer_that_excludes_this_request_is_never_proposed_for_it(self):
        from bridge import llm
        from bridge.ladder import _how_they_decide
        source = self.signed_for("Which metrics suffix should normal customers use for billing?",
                                 "Use billing_normal_total.", {"excludes": {"customer": "globex"}}, "x1")
        question = "Which metrics suffix should normal customers use for billing?"
        emb = llm.embed(question)
        self.assertEqual(_how_they_decide(self.graph, CFG, REPO, question, "", emb, "Priya Natarajan",
                                          path="billing/rates.py", facts={"customer": "globex"}), ("", "", ""))
        proposal, used, _why = _how_they_decide(self.graph, CFG, REPO, question, "", emb, "Priya Natarajan",
                                                path="billing/rates.py", facts={"customer": "initech"})
        self.assertEqual((proposal, used), ("Use billing_normal_total.", source))

    def test_a_prediction_from_another_scope_carries_that_scope_to_the_owner(self):
        source = self.signed_for("What usage sample rate applies to customer integrations?",
                                 "Use a 7 percent sample rate.", {"requires": {"customer": "acme"}}, "r1")
        later = self.node(self.task("r2"), "What usage sample rate applies to customer integrations?", "r2",
                          facts={"customer": "globex"})
        self.assertEqual(later["status"], "pending")
        self.assertEqual(later["prediction"], "Use a 7 percent sample rate.")
        scope = (f"prediction scope: decision {source}, which it leans on, needs customer=acme and this request "
                 "states customer=globex, so it is an analogy here, not the same decision")
        self.assertIn(scope, later["evidence"])
        self.delivery.deliver_now()
        ask = next(m for m in self.slack.messages if "sample rate" in m["text"] and "How you decided before" in m["text"])
        self.assertIn("Scope: " + scope.split("prediction scope: ", 1)[1] + ".", ask["text"])

    def test_a_proposal_that_does_not_answer_the_question_is_not_made(self):
        import os
        from unittest.mock import patch
        from bridge import llm
        from bridge.ladder import _how_they_decide
        self.signed_for("What documentation must ship for a new billing feature?",
                        "A changelog fragment and the rate card docstring.", {}, "d1")
        env = patch.dict(os.environ, {"BRIDGE_SEMANTIC": "1", "BRIDGE_CLAUDE_BIN": "/nonexistent/claude"})
        env.start()
        self.addCleanup(env.stop)
        question = "What default value for the billing grace period is approved?"
        replies = {"no": {"proposal": "Ship a changelog fragment.", "answers": False, "relation": "analogy"},
                   "same": {"proposal": "Ship a changelog fragment and the rate card docstring.", "answers": True,
                            "relation": "same_policy"}}
        for key, want in (("no", ("", "", "")),):
            with patch.object(llm.Client, "complete_json", lambda self, *a, **k: replies[key]):
                self.assertEqual(_how_they_decide(self.graph, Config(), REPO, question, "", llm.embed(question),
                                                  "Priya Natarajan", path="billing/rates.py"), want)
        docs = "What documentation must ship for a new billing feature?"
        with patch.object(llm.Client, "complete_json", lambda self, *a, **k: replies["same"]):
            proposal, _used, why = _how_they_decide(self.graph, Config(), REPO, docs, "", llm.embed(docs),
                                                    "Priya Natarajan", path="billing/rates.py")
        self.assertEqual(proposal, "Ship a changelog fragment and the rate card docstring.")
        self.assertTrue(why.startswith("how they decide (the same policy as their signed answer)"), why)


class WhatAHandOnTeaches(PortalCase):
    """"Not me, ask them" teaches Bridge a route, and the route must be
    what the person meant. Measured live: the owner of one file handed on
    a docs question, the question named no topic Bridge knew, and Bridge
    learned that the other person decides the file's whole directory,
    the owner's own file included."""

    def handed(self, question, ref, by):
        node = self.node(self.task(ref), question, ref)
        return self.store.refer(node["node_id"], {"person": self.dana},
                                actor=Actor.person(self.graph.get_person(by)))

    def routes(self, person_id):
        return [(r["scope_kind"], r["scope"]) for r in self.graph.authority_rows(REPO) if r["person_id"] == person_id]

    def test_a_docs_question_teaches_the_topic(self):
        row = self.handed("What should the changelog tell users about the renamed helper?", "h1", self.priya)
        self.assertEqual(row["owner_name"], "Dana Ortiz")
        self.assertEqual(row["learned"], [f"docs decisions in {REPO}"])
        self.assertEqual(self.routes(self.dana), [("category", "docs")])
        self.assertIn(f"Bridge will route docs decisions in {REPO} to them first", row["notice"])

    def test_the_file_owner_saying_not_me_does_not_give_the_file_away(self):
        row = self.handed("Should the helper keep its current name?", "h2", self.priya)
        self.assertEqual(row["owner_name"], "Dana Ortiz")
        self.assertEqual(row["learned"], [])
        self.assertEqual(self.routes(self.dana), [])
        self.assertIn("learned no route", row["notice"])
        # Priya still decides her files.
        n = self.node(self.task("h3"), "Should billing/rates.py drop the legacy tier table?", "h3")
        self.assertEqual(n["owner"], "Priya Natarajan")

    def test_someone_else_handing_it_on_still_teaches_the_place(self):
        self.store.update_settings({"coordinator": self.wes})
        row = self.handed("Should the helper keep its current name?", "h4", self.wes)
        self.assertEqual(row["learned"], [f"decisions on billing/* in {REPO}"])
        self.assertEqual(self.routes(self.dana), [("path", "billing/*")])


class DependencyAndApproverTests(PortalCase):
    def test_a_node_names_what_it_depends_on(self):
        task = self.task("d")
        rate = self.node(task, "Which rate applies above the included quota?", "d1")
        rounding = self.node(task, "Round the overage charge to whole cents?", "d2", depends_on=rate["node_id"])
        tree = canvas.get_tree(self.store, task)
        by_id = {n["node_id"]: n for n in canvas._flatten(tree["nodes"])}
        self.assertEqual(by_id[rounding["node_id"]]["depends_on"], [rate["node_id"]])
        self.assertEqual(by_id[rate["node_id"]]["dependents"], [rounding["node_id"]])
        self.assertIn("depend on a decision that still waits", tree["next"])
        with self.assertRaises(Invalid):
            self.node(task, "Something else?", "d3", depends_on="nope")

    def test_every_required_approver_must_sign(self):
        with self.graph.transaction():
            self.graph.add_authority("category", "pricing", "approves", person_id=self.wes, repo=REPO,
                                     source="config", asserted_by="test", accepted=True)
            self.graph.add_authority("category", "pricing", "approves", person_id=self.dana, repo=REPO,
                                     source="config", asserted_by="test", accepted=True)
        task = self.task("e")
        node = self.node(task, "Change the overage pricing tiers for enterprise plans?", "e1")
        self.assertEqual(node["status"], "pending")
        self.assertEqual(sorted(node["required_signers"]), ["Dana Ortiz", "Wes Chen"])
        # The owner answers: one signature of three, still evidence.
        self.answer(node["node_id"], "Keep two tiers, add a third at 100k units")
        view = canvas.node_view(self.store, node["node_id"])
        self.assertEqual(view["status"], "resolved")
        self.assertEqual(view["signoff"], "required")
        self.assertFalse(view["authorized"])
        self.assertTrue(view["blocking"])
        self.assertEqual(view["signatures"], ["Priya Natarajan"])
        self.assertIn("still waiting on", view["next"])
        self.assertIn("Dana Ortiz", view["next"])
        self.assertIn("Wes Chen", view["next"])
        # A person's answer, not evidence, and none of the ladder's search
        # notes beside it. Measured live on eb9d22d: signed answers showed
        # as "Resolved from evidence" next to "memory: best match scored".
        self.assertEqual(view["kind"], "answer")
        self.assertEqual(view["evidence"], "")
        self.assertEqual(view["answered_by"], "Priya Natarajan")
        self.delivery.deliver_now()
        self.assertTrue(any(m["channel"] == "DUWES" and "Sign-off wanted" in m["text"] for m in self.slack.messages))
        self.assertTrue(any(m["channel"] == "DUDAN" and "Sign-off wanted" in m["text"] for m in self.slack.messages))
        with self.assertRaises(Invalid):
            canvas.finish_task(self.store, {"task_id": task})
        # Wes signs: not yet. Dana signs: authorized.
        row = self.store.get_decision(node["node_id"])
        partial = canvas.sign_off(self.store, node["node_id"], {"by": "Wes Chen", "expected_updated_at": row["updated_at"]})
        self.assertEqual(partial["signoff"], "required")
        self.assertIn("still waiting on Dana Ortiz", partial["notice"])
        self.assertEqual(partial["signatures"], ["Priya Natarajan", "Wes Chen"])
        row = self.store.get_decision(node["node_id"])
        done = canvas.sign_off(self.store, node["node_id"], {"by": "Dana Ortiz", "expected_updated_at": row["updated_at"]})
        self.assertEqual(done["signoff"], "signed")
        self.assertTrue(done["authorized"])
        self.assertEqual(done["signatures"], ["Priya Natarajan", "Wes Chen", "Dana Ortiz"])
        # Signed by everyone it needed: a recorded answer, read as one.
        self.assertEqual(done["status"], "answered")
        self.assertEqual(self.store.get_decision(node["node_id"])["status"], "approved")
        self.assertEqual(canvas.finish_task(self.store, {"task_id": task})["status"], "completed")
        kinds = [r["kind"] for r in self.graph.db.execute("SELECT kind FROM events WHERE decision_id=? ORDER BY id", (node["node_id"],))]
        self.assertEqual(kinds.count("signature"), 2)
        self.assertIn("signoff", kinds)


class NotesOverdueTraceTests(PortalCase):
    def test_a_note_reaches_the_agent_on_the_tree_and_through_wait(self):
        task = self.task("f")
        node = self.node(task, "Which rate applies above the included quota?", "f1")
        seen = canvas.get_tree(self.store, task)["observed_at"]
        added = canvas.add_note(self.store, task, {"text": "The Globex contract renews in March; do not change their rate before then.",
                                                   "by": "Wes Chen"})
        self.assertEqual(added["notes"][0]["by"], "Wes Chen")
        tree = canvas.get_tree(self.store, task)
        self.assertEqual(len(tree["notes"]), 1)
        self.assertIn("1 note from people", tree["next"])
        waited = canvas.wait(self.store, {"task_id": task, "timeout": "5", "since": seen})
        self.assertEqual(len(waited["notes"]), 1)
        self.assertIn("renews in March", waited["notes"][0]["text"])
        self.assertFalse(waited["timed_out"])
        # A note during a wait ends the wait.
        threading.Timer(0.3, lambda: canvas.add_note(self.store, task, {"text": "Second note", "by": "Wes Chen"})).start()
        waited = canvas.wait(self.store, {"task_id": task, "timeout": "5"}, interval=0.1)
        self.assertEqual([n["text"] for n in waited["notes"]], ["Second note"])
        self.assertLess(waited["waited_seconds"], 3)
        self.assertEqual(node["status"], "pending")

    def test_overdue_questions_are_listed_and_reminded(self):
        task = self.task("g")
        node = self.node(task, "Which rate applies above the included quota?", "g1")
        old = (datetime.now(timezone.utc) - timedelta(hours=80)).isoformat()
        with self.graph.transaction():
            self.graph.db.execute("UPDATE decisions SET created_at=? WHERE id=?", (old, node["node_id"]))
        self.assertEqual(self.store.inbox(overdue=True)["total"], 1)
        self.assertEqual(self.store.inbox()["counts"]["overdue"], 1)
        self.store.update_settings({"overdue_hours": 96})
        self.assertEqual(self.store.inbox(overdue=True)["total"], 0)
        self.assertEqual(self.store.settings()["overdue_hours"], 96)
        with self.assertRaises(Invalid):
            self.store.update_settings({"overdue_hours": 0})
        self.store.update_settings({"overdue_hours": 24, "coordinator": self.wes})
        self.delivery.deliver_now()
        self.slack.messages.clear()
        self.assertEqual(self.delivery.remind_overdue(), 2)
        self.assertEqual(self.delivery.remind_overdue(), 0)
        self.assertEqual(self.delivery.deliver_now(), 2)
        owner = next(m for m in self.slack.messages if m["channel"] == "DUPRI")
        self.assertIn("Still waiting on you, Priya Natarajan", owner["text"])
        self.assertIn("open for 3 days", owner["text"])
        coordinator = next(m for m in self.slack.messages if m["channel"] == "DUWES")
        self.assertIn("you are the coordinator", coordinator["text"])

    def test_the_trace_of_a_task(self):
        task = self.task("h")
        node = self.node(task, "Which rate applies above the included quota?", "h1")
        self.delivery.deliver_now()
        self.answer(node["node_id"], "The enterprise rate card")
        canvas.add_note(self.store, task, {"text": "context", "by": "Wes Chen"})
        trace = canvas.trace(self.store, task)
        kinds = [e["kind"] for e in trace["events"]]
        for kind in ("task_started", "node_added", "ask_drafted", "notification_queued", "notification_sent",
                     "owner_approved", "task_note"):
            self.assertIn(kind, kinds, kinds)
        self.assertEqual(trace["notifications"][0]["state"], "sent")
        self.assertEqual(trace["nodes"][0]["authorized"], True)
        self.assertEqual(trace["notes"][0]["text"], "context")


class RecordsAndTeamsTests(PortalCase):
    def test_records_from_outside_git_by_api_and_from_slack(self):
        record = self.store.add_record({"repo": REPO, "kind": "ticket", "ref": "LEDGER-42", "title": "Overage billing",
                                        "body": "Decided: overage above the included quota is billed at the enterprise rate card.",
                                        "author": "Wes Chen", "url": "https://tickets.acme.example/LEDGER-42",
                                        "paths": "billing/rates.py"})
        self.assertEqual(record["kind"], "ticket")
        row = self.graph.db.execute("SELECT * FROM intents WHERE repo=? AND kind='ticket' AND ref='LEDGER-42'", (REPO,)).fetchone()
        self.assertIn("enterprise rate card", row["body"])
        self.assertEqual(self.graph.get_source(REPO, "url:ticket:LEDGER-42"), "https://tickets.acme.example/LEDGER-42")
        self.assertEqual(self.graph.paths_of_intents(REPO, [("ticket", "LEDGER-42")])[("ticket", "LEDGER-42")], ["billing/rates.py"])
        with self.assertRaises(Invalid):
            self.store.add_record({"repo": REPO, "kind": "pr", "ref": "1", "title": "x"})
        with self.assertRaises(Invalid):
            self.store.add_record({"repo": REPO, "kind": "doc", "ref": "d1"})
        # A decision written in a Slack channel.
        self.store.update_settings({"slack_capture_repo": REPO})
        event = {"type": "event_callback", "event_id": "Ev7", "event": {
            "type": "message", "channel": "C0BILL", "ts": "1700000000.000100", "user": "UWES",
            "text": "record: partner accounts are billed at the list rate, agreed with <@UPRI> and finance today"}}
        self.assertEqual(handle_slack_event(self.delivery, event), {"ok": True, "captured": True})
        ack = self.slack.messages[-1]
        self.assertEqual(ack["thread_ts"], "1700000000.000100")
        self.assertIn("Recorded as decision record slack C0BILL:1700000000.000100 by Wes Chen", ack["text"])
        row = self.graph.db.execute("SELECT * FROM intents WHERE kind='slack'").fetchone()
        self.assertEqual(row["repo"], REPO)
        self.assertEqual(row["author"], "Wes Chen")
        self.assertIn("list rate", row["body"])
        self.assertIn("agreed with @Priya Natarajan and finance", row["body"])
        self.assertEqual(self.graph.get_source(REPO, "url:slack:C0BILL:1700000000.000100"),
                         "https://slack.com/archives/C0BILL/p1700000000000100")
        self.assertEqual(handle_slack_event(self.delivery, event), {"ok": True, "captured": False})
        mention = {"type": "event_callback", "event_id": "Ev8", "event": {
            "type": "app_mention", "channel": "C0BILL", "ts": "1700000000.000200", "user": "UWES", "text": "<@UBRIDGE> hello"}}
        handle_slack_event(self.delivery, mention)
        self.assertIn("record: <what was decided>", self.slack.messages[-1]["text"])
        # Over HTTP too.
        server = make_server(self.store, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))
        url = f"http://127.0.0.1:{server.server_port}"
        with urlopen(url + "/api/state") as resp:
            csrf = json.load(resp)["csrf_token"]
        req = Request(url + "/api/records", data=json.dumps({"repo": REPO, "kind": "doc", "ref": "pricing-policy",
                                                              "title": "Pricing policy", "body": "Tiers are annual."}).encode(),
                      headers={"Content-Type": "application/json", "X-Bridge-CSRF": csrf})
        with urlopen(req) as resp:
            self.assertEqual(json.load(resp)["ref"], "pricing-policy")
        with urlopen(url + "/api/inbox?overdue=1") as resp:
            self.assertEqual(json.load(resp)["total"], 0)

    def test_teams_is_a_channel_only_transport(self):
        posted = []

        class Opener:
            def __init__(self, req, timeout=0):
                posted.append((req.full_url, json.loads(req.data.decode())))

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"1"
        store = Store(Path(self.temp.name) / "teams.db")
        with store.graph.transaction():
            store.graph.add_person("Priya Natarajan", email="priya@acme.example")
            pid = store.graph.add_person("Wes Chen", email="wes@acme.example")
            store.graph.add_authority("path", "billing/*", "decides", person_id=pid, repo=REPO, source="config",
                                      asserted_by="test", accepted=True)
        delivery = store.connect_delivery(TeamsTransport("https://acme.webhook.office.com/hook", opener=Opener),
                                          base_url="https://bridge.acme.test")
        task = canvas.start_task(store, CFG, {"title": "Add usage-based pricing", "repo": REPO, "paths": "billing/rates.py",
                                              "client_key": "t1"})["task_id"]
        node = canvas.add_node(store, CFG, {"task_id": task, "question": "Which rate applies above the quota?",
                                            "paths": "billing/rates.py", "client_ref": "t1"})
        self.assertEqual(node["owner"], "Wes Chen")
        self.assertEqual(delivery.deliver_now(), 1)
        self.assertEqual(posted[0][0], "https://acme.webhook.office.com/hook")
        self.assertIn("**A decision needs you, Wes Chen.**", posted[0][1]["text"])
        self.assertIn("https://bridge.acme.test/#inbox", posted[0][1]["text"])
        sent = delivery.list("sent")
        self.assertEqual(sent[0]["destination"], "channel")
        self.assertEqual(sent[0]["channel"], "teams")


if __name__ == "__main__":
    unittest.main()
