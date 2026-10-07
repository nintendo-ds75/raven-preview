"""The task page: the link a Slack message carries to one task, what the
page shows the person it was sent to, answering from it on their own
standing, the note box, and the account a person makes from it.

A fake Slack records what would be posted. Nothing here reaches the
network or a model."""

import http.client
import json
import os
import re
import threading
import time
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase, ready_server

from bridge import briefing, canvas
from bridge.auth import Auth
from bridge.authz import Actor, Refused
from bridge.config import Config
from bridge.store import Invalid, Store

CFG = Config(model_api="none")
BASE = "https://raven.acme.test"
BOOTSTRAP = "bootstrap-secret-for-brief-tests"
LINK = re.compile(r"/brief#(rvn_[A-Za-z0-9_-]+)")


class FakeSlack:
    name = "slack"

    def __init__(self):
        self.messages = []

    def open_dm(self, user_id):
        return "D" + user_id

    def post_message(self, channel, text, blocks=None, thread_ts=""):
        self.messages.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return f"1700000000.{len(self.messages):06d}"


class ChannelOnly(FakeSlack):
    """A Teams incoming webhook: one channel, no direct messages."""
    name = "teams"
    supports_dm = False


class BriefCase(OfflineCase):
    """A billing task with three people on it: Priya asked for it, Wes
    decides billing paths, Marisol decides pricing. Val is a viewer and
    Ada an administrator; both have Slack ids."""

    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "brief.db")
        self.graph = self.store.graph
        self.slack = FakeSlack()
        self.delivery = self.store.connect_delivery(self.slack, base_url=BASE)
        with self.graph.transaction():
            self.wes = self.graph.add_person("Wes Chen", email="wes@acme.example", slack_id="UWES")
            self.marisol = self.graph.add_person("Marisol Vega", email="marisol@acme.example", slack_id="UMAR")
            self.priya = self.graph.add_person("Priya Natarajan", email="priya@acme.example", slack_id="UPRI")
            self.val = self.graph.add_person("Val Viewer", email="val@acme.example", slack_id="UVAL", role="viewer")
            self.ada = self.graph.add_person("Ada Admin", email="ada@acme.example", slack_id="UADA", role="admin")
            self.graph.add_authority("path", "billing/*", "decides", person_id=self.wes)
            self.graph.add_authority("category", "pricing", "decides", person_id=self.marisol)

    def task(self, title="Add usage-based pricing", repo="acme/platform"):
        return canvas.start_task(self.store, CFG, {
            "title": title, "repo": repo, "requester": "Priya Natarajan", "paths": "billing/usage.py",
            "goal": "Bill enterprise customers for API calls above their plan, starting next quarter."})["task_id"]

    def node(self, task, question="Should we bill the usage spike, or exclude it as a load test?", **extra):
        return canvas.add_node(self.store, CFG, {
            "task_id": task, "question": question, "paths": "billing/usage.py",
            "context": "Usage jumped 11x on enterprise-two during the rollout.",
            "options": "Bill it | Exclude the load test", **extra})

    def links(self, slack_id):
        """The task links in this person's DMs, by the decision each
        names."""
        out = {}
        for m in self.slack.messages:
            if m["channel"] == "D" + slack_id:
                for token in LINK.findall(m["text"]):
                    out[briefing.resolve(self.graph, token).decision_id] = token
        return out

    def mint(self, person_id, task, decision_id=""):
        with self.graph.transaction():
            return briefing.mint(self.graph, person_id, task, decision_id)

    def link(self, token):
        found = briefing.resolve(self.graph, token)
        self.assertIsNotNone(found, "the link did not resolve")
        return found

    def decision(self, decision_id):
        return self.store.get_decision(decision_id)


class Http:
    """Requests to a server started by the test, with no redirects
    followed and the status always returned."""

    def serve(self, server):
        self.server = server
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_port

    def request(self, method, path, headers=None, data=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        sent = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            sent.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=body, headers=sent)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw
        return response.status, payload, response.headers

    def brief(self, token, path="/api/brief", data=None, headers=None):
        sent = {**({"X-Raven-Link": token} if token else {}), **(headers or {})}
        return self.request("POST" if data is not None else "GET", path, sent, data)


# ---------------- the link in the message ----------------

class LinkInMessageTests(BriefCase):
    """A link signs in as the person, so it goes only where only they
    read it: their own DM."""

    def test_a_dm_carries_the_persons_own_link(self):
        t = self.task()
        n = self.node(t)
        self.assertEqual(self.delivery.deliver_now(), 1)
        message = self.slack.messages[0]
        self.assertEqual(message["channel"], "DUWES")
        tokens = LINK.findall(message["text"])
        self.assertEqual(len(tokens), 1)
        self.assertIn(f"{BASE}/brief#{tokens[0]}", message["text"])
        link = self.link(tokens[0])
        note = self.delivery.list()[0]
        self.assertEqual((link.person["id"], link.run_id, link.decision_id, link.notification_id),
                         (self.wes, t, n["node_id"], note["id"]))
        self.assertEqual(link.asked, n["node_id"])
        # Only the hash is kept: the token is in the message and nowhere else.
        stored = self.graph.db.execute("SELECT token_hash FROM brief_links").fetchone()["token_hash"]
        self.assertNotEqual(stored, tokens[0])
        self.assertNotIn(tokens[0], json.dumps([dict(r) for r in self.graph.db.execute("SELECT * FROM brief_links")]))

    def test_a_fallback_channel_post_carries_no_link(self):
        with self.graph.transaction():
            nobody = self.graph.add_person("No Slack", email="ns@acme.example")
            self.graph.add_authority("path", "infra/*", "decides", person_id=nobody)
        self.store.update_settings({"slack_fallback_channel": "C-RAVEN"})
        n = self.node(self.task(), question="Should deploys pause while the meter migration runs?",
                      paths="infra/deploy.py")
        self.assertEqual(n["owner"], "No Slack")
        self.assertEqual(self.delivery.deliver_now(), 1)
        posted = self.slack.messages[-1]
        self.assertEqual(posted["channel"], "C-RAVEN")
        self.assertIn("posted here instead", posted["text"])
        self.assertNotIn("/brief#", posted["text"])
        self.assertIn(f"{BASE}/#inbox", posted["text"])
        self.assertEqual(self.graph.db.execute("SELECT count(*) c FROM brief_links").fetchone()["c"], 0)

    def test_a_channel_only_transport_carries_no_link(self):
        teams = ChannelOnly()
        delivery = self.store.connect_delivery(teams, base_url=BASE)
        self.node(self.task())
        self.assertEqual(delivery.deliver_now(), 1)
        self.assertEqual(teams.messages[0]["channel"], "channel")
        self.assertIn("Wes Chen", teams.messages[0]["text"])
        self.assertNotIn("/brief#", teams.messages[0]["text"])
        self.assertEqual(self.graph.db.execute("SELECT count(*) c FROM brief_links").fetchone()["c"], 0)

    def test_off_mode_carries_no_link_and_static_does(self):
        self.store.update_settings({"brief_mode": "off"})
        self.node(self.task())
        self.delivery.deliver_now()
        self.assertNotIn("/brief#", self.slack.messages[-1]["text"])
        self.assertIn(f"{BASE}/#inbox", self.slack.messages[-1]["text"])
        # A workspace that saved the agent mode before it was taken out
        # reads as static, and its messages carry the link.
        for stored in ("static", "agent"):
            self.graph.set_setting("brief_mode", stored)
            self.node(self.task(title=f"Pricing with {stored} stored"),
                      question=f"Should the {stored} rollout bill the usage spike?")
            self.delivery.deliver_now()
            self.assertIn("/brief#rvn_", self.slack.messages[-1]["text"], stored)

    def test_no_public_address_means_no_link(self):
        slack = FakeSlack()
        delivery = self.store.connect_delivery(slack, base_url="")
        self.node(self.task())
        delivery.deliver_now()
        self.assertNotIn("/brief#", slack.messages[0]["text"])

    def test_the_token_rides_in_the_fragment(self):
        self.assertEqual(briefing.url_for("https://raven.acme.test/", "rvn_abc"), "https://raven.acme.test/brief#rvn_abc")


# ---------------- opening the page ----------------

class PageTests(BriefCase, Http):
    """GET /api/brief: one task, read for the person the link names, and
    nothing for a link that is not one."""

    def setUp(self):
        super().setUp()
        self.serve(ready_server(self.store, port=0))
        self.t = self.task()
        self.a = self.node(self.t)
        self.b = self.node(self.t, question="Do we apply per-seat minimums under usage-based pricing?",
                           category="pricing")
        self.c = self.node(self.t, question="Should invoices round usage to the nearest thousand calls?")
        self.delivery.deliver_now()
        marisol = Actor.person(self.graph.get_person(self.marisol))
        self.store.answer(self.b["node_id"], {"answer": "No minimums in the first quarter.",
                                              "rationale": "Sales promised it to the pilot customers.",
                                              "expected_updated_at": self.decision(self.b["node_id"])["updated_at"]},
                          actor=marisol)
        self.store.refer(self.c["node_id"], {"person": self.marisol, "by": "Wes Chen",
                                             "expected_updated_at": self.decision(self.c["node_id"])["updated_at"]},
                         actor=Actor.person(self.graph.get_person(self.wes)))
        canvas.add_note(self.store, self.t, {"text": "Enterprise-two is on a custom contract.", "by": "Priya Natarajan"})
        self.delivery.deliver_now()
        self.token = self.links("UWES")[self.a["node_id"]]

    def test_the_page_shows_the_task_to_its_person(self):
        status, page, headers = self.brief(self.token)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(page["viewer"]["name"], "Wes Chen")
        self.assertFalse(page["viewer"]["has_account"])
        self.assertTrue(page["viewer"]["has_email"])
        self.assertEqual(page["requester"]["name"], "Priya Natarajan")
        self.assertTrue(page["requester"]["known"])
        self.assertEqual(page["task"]["id"], self.t)
        self.assertEqual(page["task"]["title"], "Add usage-based pricing")
        self.assertIn("Bill enterprise customers for API calls", page["task"]["goal"])
        focus = page["focus"]
        self.assertEqual(focus["node_id"], self.a["node_id"])
        self.assertEqual(focus["question"], "Should we bill the usage spike, or exclude it as a load test?")
        self.assertEqual(focus["options"], ["Bill it", "Exclude the load test"])
        # The canvas appends paths and options to a node's context; the
        # page shows those on their own, so the context is the agent's.
        self.assertEqual(focus["context"], "Usage jumped 11x on enterprise-two during the rollout.")
        self.assertEqual(focus["paths"], ["billing/usage.py"])
        self.assertEqual((focus["owner"], focus["status"], focus["action"]), ("Wes Chen", "pending", "answer"))
        self.assertTrue(focus["can_act"])
        self.assertEqual(focus["updated_at"], self.decision(self.a["node_id"])["updated_at"])
        self.assertEqual({n["node_id"] for n in page["nodes"]},
                         {self.a["node_id"], self.b["node_id"], self.c["node_id"]})
        contacts = {c["name"]: c for c in page["contacts"]}
        self.assertIn(focus["question"], contacts["Wes Chen"]["asked"])
        self.assertEqual(contacts["Wes Chen"]["messages"], 2)
        self.assertEqual(contacts["Wes Chen"]["handed_on"][0]["to"], "Marisol Vega")
        self.assertEqual(contacts["Wes Chen"]["handed_on"][0]["question"],
                         "Should invoices round usage to the nearest thousand calls?")
        decided = contacts["Marisol Vega"]["decided"]
        self.assertEqual([(d["node_id"], d["answer"]) for d in decided],
                         [(self.b["node_id"], "No minimums in the first quarter.")])
        self.assertIn("Should invoices round usage to the nearest thousand calls?", contacts["Marisol Vega"]["waiting_on"])
        self.assertEqual(page["notes"][0]["text"], "Enterprise-two is on a custom contract.")
        history = {(h["kind"], h["by"]) for h in page["history"]}
        self.assertIn(("task_started", "Priya Natarajan"), history)
        # Who answered is in the history, not just that someone did.
        self.assertIn(("owner_approved", "Marisol Vega"), history)
        self.assertIn(("task_note", "Priya Natarajan"), history)
        handed = next(h for h in page["history"] if h["kind"] == "owner_changed")
        self.assertEqual(handed["text"], "to Marisol Vega (by Wes Chen)")
        # A stranger's page names people, never their addresses; the
        # person's own shows only enough to know it as theirs.
        for email in ("wes@acme.example", "marisol@acme.example", "priya@acme.example"):
            self.assertNotIn(email, json.dumps(page))
        self.assertEqual(page["viewer"]["email_hint"], "w\u2026@acme.example")
        self.assertEqual(json.dumps(page).count("@acme.example"), 1)

    def test_a_link_shows_the_standing_of_its_person(self):
        """Marisol was messaged about the decision handed to them: they may
        answer it, and Wes's is not hers to answer."""
        token = self.links("UMAR")[self.c["node_id"]]
        _, page, _ = self.brief(token)
        self.assertEqual(page["focus"]["node_id"], self.c["node_id"])
        self.assertTrue(page["focus"]["can_act"])
        a = next(n for n in page["nodes"] if n["node_id"] == self.a["node_id"])
        self.assertFalse(a["can_act"])
        self.assertIn("Wes Chen", a["why_not"])

    def test_bad_links_are_refused(self):
        self.assertEqual(self.brief("")[0], 401)
        self.assertEqual(self.brief("not-a-link")[0], 401)
        self.assertEqual(self.brief("rvn_" + "x" * 32)[0], 401)
        self.assertEqual(self.brief("rvn_" + "x" * 200)[0], 401)
        status, body, _ = self.brief(self.token[:-1])
        self.assertEqual(status, 401)
        # The same words whatever the cause: a cut-off paste is not told
        # its link was revoked, and nobody learns which it was.
        self.assertIn("isn't valid: it may be incomplete, expired or revoked", body["error"])
        self.assertEqual(self.brief("rvn_" + "x" * 32)[1]["error"], body["error"])

    def test_an_expired_link_is_refused(self):
        self.assertEqual(self.brief(self.token)[0], 200)
        self.graph.db.execute("UPDATE brief_links SET expires_at=? WHERE id=?",
                              (int(time.time()) - 1, self.link(self.token).id))
        self.assertEqual(self.brief(self.token)[0], 401)

    def test_a_revoked_link_is_refused(self):
        link = self.link(self.token)
        with self.graph.transaction():
            briefing.revoke(self.graph, link.id)
        self.assertEqual(self.brief(self.token)[0], 401)
        self.assertEqual(self.brief(self.token, "/api/brief/note", {"text": "late note"})[0], 401)

    def test_a_link_stops_working_when_its_person_is_made_inactive(self):
        self.graph.db.execute("UPDATE people SET active=0 WHERE id=?", (self.wes,))
        self.assertEqual(self.brief(self.token)[0], 401)

    def test_the_token_is_taken_only_from_its_header(self):
        """Nothing ambient: a token in the address or a cookie is not a
        link, which is why link requests need no CSRF token."""
        for path in (f"/api/brief?token={self.token}", f"/api/brief?link={self.token}",
                     f"/api/brief?X-Raven-Link={self.token}"):
            self.assertEqual(self.request("GET", path)[0], 401, path)
        cookie = f"X-Raven-Link={self.token}; raven_link={self.token}; brief={self.token}"
        self.assertEqual(self.request("GET", "/api/brief", {"Cookie": cookie})[0], 401)
        self.assertEqual(self.request("POST", "/api/brief/note", {"Cookie": cookie}, {"text": "x"})[0], 401)

    def test_the_page_itself_opens_without_an_account(self):
        status, body, headers = self.request("GET", "/brief")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn(b"/brief.js", body)
        status, _, headers = self.request("GET", "/brief.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def test_a_link_opens_nothing_but_its_own_task(self):
        """A link made for a decision on another task shows no focus,
        and that decision cannot be answered from it."""
        other = self.task(title="Invoice emails")
        elsewhere = self.node(other, question="Should invoice emails list every API key's usage?")
        token = self.mint(self.wes, self.t, elsewhere["node_id"])
        status, page, _ = self.brief(token)
        self.assertEqual(status, 200)
        self.assertIsNone(page["focus"])
        self.assertNotIn(elsewhere["node_id"], {n["node_id"] for n in page["nodes"]})
        status, body, _ = self.brief(token, "/api/brief/answer", {
            "decision_id": elsewhere["node_id"], "answer": "Yes",
            "expected_updated_at": self.decision(elsewhere["node_id"])["updated_at"]})
        self.assertEqual(status, 400)
        self.assertIn("not found on this task", body["error"])
        self.assertEqual(self.decision(elsewhere["node_id"])["status"], "pending")


# ---------------- answering from the page ----------------

class AnswerTests(BriefCase, Http):
    """An answer from the page is an answer on the person's own
    standing: the revision they read is named, and the permission check
    decides, as a Slack reply."""

    def setUp(self):
        super().setUp()
        self.t = self.task()
        self.a = self.node(self.t)
        self.delivery.deliver_now()
        self.token = self.links("UWES")[self.a["node_id"]]

    def answer(self, token, decision_id, answer="Exclude the load test.", expected=None, **extra):
        expected = self.decision(decision_id)["updated_at"] if expected is None else expected
        return briefing.act(self.store, self.link(token), {"decision_id": decision_id, "answer": answer,
                                                           "expected_updated_at": expected, **extra})

    def test_the_person_answers_and_it_is_attributed_to_them(self):
        result = self.answer(self.token, self.a["node_id"], rationale="It was our own load test.")
        self.assertEqual(result["notice"], "Recorded as Wes Chen's answer. The coding agent sees it the next time it "
                                           "checks this task.")
        row = self.decision(self.a["node_id"])
        self.assertEqual((row["status"], row["answer"], row["rationale"]),
                         ("approved", "Exclude the load test.", "It was our own load test."))
        self.assertEqual((row["answered_by"], row["signed_by"], row["actor_name"], row["actor_id"]),
                         ("Wes Chen", "Wes Chen", "Wes Chen", self.wes))
        self.assertEqual(row["actor_basis"], "owner")
        event = self.graph.db.execute("SELECT detail FROM events WHERE kind='owner_approved' AND decision_id=?",
                                      (self.a["node_id"],)).fetchone()
        self.assertEqual(json.loads(event["detail"])["source"], "link: Wes Chen")
        node = next(n for n in canvas._flatten(canvas.get_tree(self.store, self.t)["nodes"])
                    if n["node_id"] == self.a["node_id"])
        self.assertTrue(node["authorized"])

    def test_personal_link_can_review_complete_changed_sources_without_an_account(self):
        source_data = {'repo': 'acme/platform', 'kind': 'doc', 'ref': 'POL-1',
                       'body': 'Exclude internal load tests.', 'paths': ['billing/usage.py']}
        source = self.store.add_record(source_data)
        did = self.a['node_id']
        row = self.graph.intents_by_ref(['POL-1'], 'acme/platform')[0]
        self.graph.publish_evidence(did, [row], status='resolved', source='record',
                                    answer='Exclude internal load tests.', kind='evidence', signoff='required')
        self.answer(self.token, did, answer='')
        self.store.add_record({**source_data, 'body': 'Exclude synthetic traffic including partner sandboxes.'})
        page = briefing.overview(self.store, self.link(self.token))
        review = page['focus']['source_revalidation']
        self.assertIn('partner sandboxes', review['sources'][0]['snapshot']['body'])
        with self.assertRaises(Invalid): self.answer(self.token, did, answer='')
        self.answer(self.token, did, answer='Exclude synthetic traffic including partner sandboxes.',
                    source_evidence=review['pins'], source_decision_pins=review['decision_pins'])
        self.assertTrue(self.decision(did)['authorized'])
        self.assertFalse(self.decision(did)['needs_review'])
        self.assertEqual(self.graph.db.execute('SELECT count(*) n FROM account_passwords').fetchone()['n'], 0)

    def test_personal_link_rejects_source_change_after_page_was_read(self):
        self.test_personal_link_can_review_complete_changed_sources_without_an_account()
        did = self.a['node_id']
        review = briefing.overview(self.store, self.link(self.token))['focus']['source_revalidation']
        self.store.add_record({'repo':'acme/platform','kind':'doc','ref':'POL-1','body':'New incompatible policy.'})
        with self.assertRaises(Invalid):
            self.answer(self.token, did, answer='', source_evidence=review['pins'], source_decision_pins=review['decision_pins'])
        self.assertFalse(self.decision(did)['authorized'])

    def test_a_stale_revision_is_refused(self):
        with self.assertRaisesRegex(Invalid, "changed while you were reviewing"):
            self.answer(self.token, self.a["node_id"], expected="2020-01-01T00:00:00+00:00")
        self.assertEqual(self.decision(self.a["node_id"])["status"], "pending")
        with self.assertRaisesRegex(Invalid, "expected_updated_at"):
            briefing.act(self.store, self.link(self.token), {"decision_id": self.a["node_id"], "answer": "x"})
        with self.assertRaisesRegex(Invalid, "Write the decision"):
            self.answer(self.token, self.a["node_id"], answer="")

    def test_a_decision_on_another_task_is_refused(self):
        other = self.node(self.task(title="Invoice emails"), question="Should invoice emails list usage per key?")
        with self.assertRaisesRegex(Invalid, "not found on this task"):
            self.answer(self.token, other["node_id"])
        self.assertEqual(self.decision(other["node_id"])["status"], "pending")

    def test_someone_without_standing_is_refused(self):
        token = self.mint(self.marisol, self.t, self.a["node_id"])
        page = briefing.overview(self.store, self.link(token))
        self.assertFalse(page["focus"]["can_act"])
        with self.assertRaises(Refused) as caught:
            self.answer(token, self.a["node_id"])
        self.assertIn("Wes Chen", str(caught.exception))
        self.assertEqual(self.decision(self.a["node_id"])["status"], "pending")
        self.serve(ready_server(self.store, port=0))
        status, body, _ = self.brief(token, "/api/brief/answer", {
            "decision_id": self.a["node_id"], "answer": "Bill it.",
            "expected_updated_at": self.decision(self.a["node_id"])["updated_at"]})
        self.assertEqual(status, 403)
        self.assertIn("Not permitted", body["error"])

    def test_an_admins_link_cannot_override(self):
        token = self.mint(self.ada, self.t, self.a["node_id"])
        link = self.link(token)
        self.assertEqual((link.actor.role, link.actor.override, link.actor.kind), ("member", False, "link"))
        self.assertEqual(briefing.overview(self.store, link)["viewer"]["role"], "member")
        with self.assertRaises(Refused):
            self.answer(token, self.a["node_id"], override=True)
        self.assertEqual(self.decision(self.a["node_id"])["status"], "pending")

    def test_a_viewers_link_reads_and_cannot_answer_or_add_notes(self):
        token = self.mint(self.val, self.t, self.a["node_id"])
        page = briefing.overview(self.store, self.link(token))
        self.assertEqual(page["viewer"]["role"], "viewer")
        self.assertFalse(page["focus"]["can_act"])
        self.assertIn("viewer", page["focus"]["why_not"])
        with self.assertRaises(Refused):
            self.answer(token, self.a["node_id"])
        with self.assertRaises(Refused):
            briefing.add_note(self.store, self.link(token), {"text": "I have thoughts"})
        self.serve(ready_server(self.store, port=0))
        self.assertEqual(self.brief(token, "/api/brief/note", {"text": "I have thoughts"})[0], 403)
        self.assertEqual(self.brief(token, "/api/brief/answer", {
            "decision_id": self.a["node_id"], "answer": "Bill it.",
            "expected_updated_at": self.decision(self.a["node_id"])["updated_at"]})[0], 403)
        self.assertEqual(canvas.task_notes(self.store, self.t), [])

    def test_signing_off_what_the_agent_settled(self):
        canvas.settle_node(self.store, {"task_id": self.t, "node_id": self.a["node_id"], "answer": "Bill it.",
                                        "rationale": "the contract bills all calls"})
        self.delivery.deliver_now()
        self.assertIn("Sign-off wanted from Wes Chen", self.slack.messages[-1]["text"])
        token = LINK.findall(self.slack.messages[-1]["text"])[0]
        page = briefing.overview(self.store, self.link(token))
        self.assertEqual((page["focus"]["action"], page["focus"]["can_act"]), ("sign", True))
        result = briefing.act(self.store, self.link(token), {
            "decision_id": self.a["node_id"], "expected_updated_at": page["focus"]["updated_at"]})
        self.assertEqual(result["notice"], "Signed off by Wes Chen. The coding agent sees it the next time it checks "
                                           "this task.")
        row = self.decision(self.a["node_id"])
        self.assertEqual((row["signoff"], row["signed_by"], row["answer"]), ("signed", "Wes Chen", "Bill it."))

    def test_correcting_what_the_agent_settled(self):
        canvas.settle_node(self.store, {"task_id": self.t, "node_id": self.a["node_id"], "answer": "Bill it.",
                                        "rationale": "the contract bills all calls"})
        stale = self.decision(self.a["node_id"])["updated_at"]
        result = self.answer(self.token, self.a["node_id"], answer="Exclude it; it was our load test.",
                             rationale="We ran it ourselves.")
        self.assertEqual(result["notice"], "Corrected and signed by Wes Chen. The coding agent sees it the next time it "
                                           "checks this task.")
        row = self.decision(self.a["node_id"])
        self.assertEqual((row["answer"], row["signed_by"], row["status"]),
                         ("Exclude it; it was our load test.", "Wes Chen", "approved"))
        with self.assertRaisesRegex(Invalid, "changed while you were reviewing"):
            self.answer(self.token, self.a["node_id"], answer="Bill it after all.", expected=stale)

    def test_the_person_asked_takes_a_decision_nobody_owns(self):
        """Like a reply in the Slack thread: the person Raven asked takes
        a decision that has no owner, on the revision they read. This was
        refused as stale every time, and left them owning it."""
        with self.graph.transaction():
            self.graph.update_decision(self.a["node_id"], owner="")
        page = briefing.overview(self.store, self.link(self.token))
        self.assertTrue(page["focus"]["can_act"])
        with self.assertRaisesRegex(Invalid, "changed while you were reviewing"):
            self.answer(self.token, self.a["node_id"], expected="2020-01-01T00:00:00+00:00")
        self.assertIsNone(self.decision(self.a["node_id"])["owner_id"], "a refused answer took the decision")
        self.answer(self.token, self.a["node_id"], expected=page["focus"]["updated_at"])
        row = self.decision(self.a["node_id"])
        self.assertEqual((row["status"], row["owner_name"], row["signed_by"]), ("approved", "Wes Chen", "Wes Chen"))

    def test_a_link_someone_made_for_themselves_takes_nothing(self):
        """A signed-in person can open the page on any decision of a task;
        that is a link to read, not a message asking them to decide."""
        with self.graph.transaction():
            self.graph.update_decision(self.a["node_id"], owner="")
        token = self.mint(self.marisol, self.t, self.a["node_id"])
        page = briefing.overview(self.store, self.link(token))
        self.assertFalse(page["focus"]["can_act"])
        self.assertEqual(page["focus"]["why_not"], "nobody owns this decision yet")
        with self.assertRaisesRegex(Invalid, "Assign an owner"):
            self.answer(token, self.a["node_id"])
        self.assertIsNone(self.decision(self.a["node_id"])["owner_id"])

    def test_a_slack_reply_does_not_take_a_decision_nobody_owns(self):
        """In Slack a decision nobody owns is routed in the triage channel
        first: a reply in a DM records nothing and says where to go. The
        page's own take is only for the person the message was sent to."""
        with self.graph.transaction():
            self.graph.update_decision(self.a["node_id"], owner="")
        asked = self.slack.messages[0]
        ts = f"1700000000.{1:06d}"
        reply = self.delivery.receive(asked["channel"], ts, "UWES", "answer: exclude it because it was our load test")
        self.assertIn("triage channel", reply)
        row = self.decision(self.a["node_id"])
        self.assertEqual((row["status"], row["owner_id"]), ("pending", None))


# ---------------- static mode: the note box ----------------

class StaticModeTests(BriefCase, Http):
    def setUp(self):
        super().setUp()
        self.serve(ready_server(self.store, port=0))
        self.t = self.task()
        self.a = self.node(self.t)
        self.delivery.deliver_now()
        self.token = self.links("UWES")[self.a["node_id"]]

    def test_a_note_lands_on_the_tree(self):
        self.assertEqual(briefing.mode(self.graph), "static")
        status, body, _ = self.brief(self.token, "/api/brief/note",
                                     {"text": "Enterprise-two's contract caps overage at 2x."})
        self.assertEqual(status, 200)
        notes = canvas.get_tree(self.store, self.t)["notes"]
        self.assertEqual([(n["by"], n["text"]) for n in notes],
                         [("Wes Chen", "Enterprise-two's contract caps overage at 2x.")])
        _, page, _ = self.brief(self.token)
        self.assertEqual(page["notes"][0]["by"], "Wes Chen")
        self.assertIn(("task_note", "Wes Chen"), {(h["kind"], h["by"]) for h in page["history"]})

    def test_a_note_names_the_link_person_whatever_the_payload_says(self):
        self.assertEqual(self.brief(self.token, "/api/brief/note", {"text": "Ship it", "by": "Priya Natarajan"})[0], 200)
        self.assertEqual(canvas.task_notes(self.store, self.t)[0]["by"], "Wes Chen")
        self.assertEqual(self.brief(self.token, "/api/brief/note", {"text": "  "})[0], 400)

    def test_link_requests_need_no_csrf_token_but_json(self):
        status, _, _ = self.request("POST", "/api/brief/note", {"X-Raven-Link": self.token,
                                                                "Content-Type": "text/plain"}, {"text": "x"})
        self.assertEqual(status, 415)
        status, _, _ = self.request("POST", "/api/brief/note", {"X-Raven-Link": self.token,
                                                                "Origin": "https://evil.example"}, {"text": "x"})
        self.assertEqual(status, 403)


# ---------------- an account from the link ----------------

class AuthOnCase(BriefCase, Http):
    """A shared Raven: auth on, a bootstrap admin, a finished workspace."""

    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"BRIDGE_ADMIN_TOKEN": BOOTSTRAP, "BRIDGE_SECRET": "brief-test-secret"})
        env.start()
        self.addCleanup(env.stop)
        self.auth = Auth(self.store, enabled=True)
        self.graph.set_setting("workspace_name", "Acme")
        from bridge.server import make_server
        self.serve(make_server(self.store, port=0, auth=self.auth))
        self.t = self.task()
        self.a = self.node(self.t)
        self.delivery.deliver_now()
        self.token = self.links("UWES")[self.a["node_id"]]

    def bearer(self, person_id, kind="human"):
        return {"Authorization": "Bearer " + self.auth.create_token(person_id, "tests", kind=kind)["token"]}


class AccountTests(AuthOnCase):
    password = "correct horse battery staple"

    def claim(self, token, **data):
        return self.brief(token, "/api/brief/account", {"password": self.password, **data})

    def test_a_person_makes_their_login_from_the_link(self):
        status, body, headers = self.claim(self.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["redirect"], f"/#runs/{self.t}")
        cookie = SimpleCookie(headers["Set-Cookie"])["bridge_session"].value
        status, me, _ = self.request("GET", "/api/me", {"Cookie": f"bridge_session={cookie}"})
        self.assertEqual(status, 200)
        self.assertEqual((me["me"]["id"], me["me"]["name"], me["me"]["role"]), (self.wes, "Wes Chen", "member"))
        # The login is theirs: email and password sign in from now on.
        from bridge.accounts import Accounts
        self.assertEqual(Accounts(self.auth).login("wes@acme.example", self.password), self.wes)
        _, page, _ = self.brief(self.token)
        self.assertTrue(page["viewer"]["has_account"])
        event = self.graph.db.execute("SELECT detail FROM events WHERE kind='account_claimed'").fetchone()
        self.assertEqual(json.loads(event["detail"]), {"person_id": self.wes, "via": "task link"})

    def test_a_second_claim_is_refused(self):
        self.assertEqual(self.claim(self.token)[0], 200)
        status, body, headers = self.claim(self.token)
        self.assertEqual(status, 400)
        self.assertIn("already have an account", body["error"])
        self.assertNotIn("Set-Cookie", headers)

    def test_a_github_login_is_already_an_account(self):
        self.graph.db.execute("UPDATE people SET github_id='12345' WHERE id=?", (self.wes,))
        token = self.mint(self.wes, self.t, self.a["node_id"])
        self.assertTrue(self.brief(token)[1]["viewer"]["has_account"])
        self.assertEqual(self.claim(token)[0], 400)

    def test_an_administrator_can_turn_it_off(self):
        self.graph.set_setting("brief_signup", "0")
        status, body, _ = self.claim(self.token)
        self.assertEqual(status, 400)
        self.assertIn("invitation", body["error"])
        self.assertIsNone(self.graph.db.execute("SELECT 1 FROM account_passwords WHERE person_id=?",
                                                (self.wes,)).fetchone())

    def test_an_email_another_person_has_is_refused(self):
        with self.graph.transaction():
            nomail = self.graph.add_person("Nomail Person", slack_id="UNOM")
        token = self.mint(nomail, self.t)
        self.assertFalse(self.brief(token)[1]["viewer"]["has_email"])
        status, body, _ = self.claim(token)
        self.assertEqual(status, 400)
        self.assertIn("valid email", body["error"])
        status, body, _ = self.claim(token, email="WES@acme.example")
        self.assertEqual(status, 400)
        self.assertIn("belongs to another person", body["error"])
        status, _, _ = self.claim(token, email="nomail@acme.example")
        self.assertEqual(status, 200)
        self.assertEqual(self.graph.get_person(nomail)["email"], "nomail@acme.example")

    def test_a_short_password_is_refused(self):
        status, body, _ = self.brief(self.token, "/api/brief/account", {"password": "short"})
        self.assertEqual(status, 400)
        self.assertIn("12", body["error"])

    def test_an_administrators_link_makes_no_login(self):
        """The link acts as a member; a login made from it would hand the
        admin role to whoever holds the link."""
        token = self.mint(self.ada, self.t)
        status, body, headers = self.claim(token)
        self.assertEqual(status, 400)
        self.assertIn("administrator", body["error"])
        self.assertNotIn("Set-Cookie", headers)

    def test_a_viewer_gets_a_viewer_login(self):
        token = self.mint(self.val, self.t)
        status, _, headers = self.claim(token)
        self.assertEqual(status, 200)
        cookie = SimpleCookie(headers["Set-Cookie"])["bridge_session"].value
        me = self.request("GET", "/api/me", {"Cookie": f"bridge_session={cookie}"})[1]["me"]
        self.assertEqual(me["role"], "viewer")


class AccountWithoutAuthTests(BriefCase, Http):
    def test_there_is_no_account_to_make(self):
        self.serve(ready_server(self.store, port=0))
        t = self.task()
        a = self.node(t)
        self.delivery.deliver_now()
        token = self.links("UWES")[a["node_id"]]
        status, body, headers = self.brief(token, "/api/brief/account", {"password": "correct horse battery staple"})
        self.assertEqual(status, 400)
        self.assertIn("without sign-in", body["error"])
        self.assertNotIn("Set-Cookie", headers)


# ---------------- opening the page for yourself ----------------

class TaskLinkRouteTests(AuthOnCase):
    """POST /api/tasks/<id>/link: a signed-in person opens the task page
    as themselves."""

    def test_a_member_gets_a_working_link_for_themselves(self):
        status, body, _ = self.request("POST", f"/api/tasks/{self.t}/link", self.bearer(self.marisol),
                                       {"decision_id": self.a["node_id"]})
        self.assertEqual(status, 200)
        self.assertTrue(body["url"].startswith(f"http://127.0.0.1:{self.port}/brief#rvn_"))
        token = body["url"].split("#", 1)[1]
        link = self.link(token)
        self.assertEqual((link.person["id"], link.run_id, link.decision_id, link.asked),
                         (self.marisol, self.t, self.a["node_id"], ""))
        status, page, _ = self.brief(token)
        self.assertEqual(status, 200)
        self.assertEqual(page["viewer"]["name"], "Marisol Vega")
        self.assertEqual(page["focus"]["node_id"], self.a["node_id"])

    def test_a_link_without_a_decision_opens_the_task(self):
        status, body, _ = self.request("POST", f"/api/tasks/{self.t}/link", self.bearer(self.wes), {})
        self.assertEqual(status, 200)
        page = self.brief(body["url"].split("#", 1)[1])[1]
        self.assertIsNone(page["focus"])
        self.assertEqual(page["task"]["id"], self.t)

    def test_what_is_refused(self):
        other = self.node(self.task(title="Invoice emails"), question="Should invoice emails list usage per key?")
        status, body, _ = self.request("POST", f"/api/tasks/{self.t}/link", self.bearer(self.marisol),
                                       {"decision_id": other["node_id"]})
        self.assertEqual(status, 400)
        self.assertIn("not on this task", body["error"])
        self.assertEqual(self.request("POST", f"/api/tasks/{self.t}/link", self.bearer(self.marisol),
                                      {"decision_id": "no-such-decision"})[0], 400)
        self.assertEqual(self.request("POST", "/api/tasks/no-such-task/link", self.bearer(self.marisol), {})[0], 400)
        self.assertEqual(self.request("POST", f"/api/tasks/{self.t}/link", self.bearer(self.val), {})[0], 403)
        self.assertEqual(self.request("POST", f"/api/tasks/{self.t}/link", self.bearer(self.wes, "agent"), {})[0], 403)
        # No identity: a browser write without its session token is refused first.
        self.assertEqual(self.request("POST", f"/api/tasks/{self.t}/link", {}, {})[0], 403)
        self.assertEqual(self.request("POST", f"/api/tasks/{self.t}/link",
                                      {"Authorization": f"Bearer {BOOTSTRAP}"}, {})[0], 400)

    def test_settings_choose_what_messages_link_to(self):
        admin = {"Authorization": f"Bearer {BOOTSTRAP}"}
        status, body, _ = self.request("POST", "/api/settings", admin, {"brief_mode": "off"})
        self.assertEqual((status, body["brief_mode"]), (200, "off"))
        self.assertEqual(briefing.mode(self.graph), "off")
        for refused in ("agent", "chatty"):
            status, body, _ = self.request("POST", "/api/settings", admin, {"brief_mode": refused})
            self.assertEqual(status, 400)
            self.assertIn("brief_mode must be one of off, static", body["error"])
        self.assertEqual(self.request("POST", "/api/settings", self.bearer(self.wes), {"brief_mode": "static"})[0], 403)
        self.assertEqual(briefing.mode(self.graph), "off")
        status, body, _ = self.request("GET", "/api/people", admin)
        self.assertEqual(body["settings"]["brief_mode"], "off")
        # A stored value nobody can choose reads as the default, the agent
        # mode saved before it was taken out among them.
        for stored in ("loud", "agent"):
            self.graph.set_setting("brief_mode", stored)
            self.assertEqual(briefing.mode(self.graph), "static")


class OperatorPreviewTests(BriefCase, Http):
    """With auth off, the local operator previews the page as a person on
    the map."""

    def test_the_operator_previews_the_page_as_a_person(self):
        self.serve(ready_server(self.store, port=0))
        t = self.task()
        a = self.node(t)
        csrf = self.request("GET", "/api/state")[1]["csrf_token"]
        headers = {"X-Bridge-CSRF": csrf}
        status, body, _ = self.request("POST", f"/api/tasks/{t}/link", headers,
                                       {"person": "Wes Chen", "decision_id": a["node_id"]})
        self.assertEqual(status, 200)
        page = self.brief(body["url"].split("#", 1)[1])[1]
        self.assertEqual((page["viewer"]["name"], page["focus"]["node_id"]), ("Wes Chen", a["node_id"]))
        status, body, _ = self.request("POST", f"/api/tasks/{t}/link", headers, {})
        self.assertEqual(status, 400)
        self.assertIn("Name the person", body["error"])
        self.assertEqual(self.request("POST", f"/api/tasks/{t}/link", {}, {"person": "Wes Chen"})[0], 403)


# ---------------- what the page says, in plain words ----------------

class PlainWordsTests(BriefCase):
    """The page reads the store, not the strings written for Slack and the
    audit trail. Measured on prometheus/prometheus: "What Raven found" was
    the Slack cut of the evidence, with ids, a similarity score and a
    pointer to an inbox; "Why this came to you" was the first four
    fragments of the routing evidence."""

    def page(self, person_id, task, decision_id):
        with self.graph.transaction():
            token = briefing.mint(self.graph, person_id, task, decision_id)
        return briefing.overview(self.store, self.link(token))

    def test_what_raven_found_names_what_it_cites_and_nothing_of_the_search(self):
        earlier = self.node(self.task(title="Pricing pilot"), question="Should load-test traffic be billed?")
        self.store.answer(earlier["node_id"], {"answer": "Never bill our own load tests.", "rationale": "policy",
                                               "expected_updated_at": self.decision(earlier["node_id"])["updated_at"]},
                          actor=Actor.person(self.graph.get_person(self.wes)))
        self.store.add_record({"repo": "acme/platform", "kind": "ticket", "ref": "BILL-42", "author": "Dana Ortiz",
                               "title": "Exclude load tests from usage billing", "status": "done"})
        t = self.task()
        n = self.node(t)
        evidence = (f"memory: best match scored 0.38, under the 0.6 floor; records: selected ticket BILL-42 [done]: Exclude load "
                    f"tests from usage billing; assumption: 'policy' is not a low-stakes category; how they decide: "
                    f"Wes Chen answered \"Should load-test traffic be billed?\" (decision {earlier['node_id']}, "
                    f"similarity 0.38), because: policy; a prediction for Wes Chen to confirm, not their answer here")
        with self.graph.transaction():
            self.graph.db.execute("UPDATE decisions SET evidence=? WHERE id=?", (evidence, n["node_id"]))
        found = self.page(self.wes, t, n["node_id"])["focus"]["found"]
        # The ladder cited it as how Wes decides; the lead says only that it
        # may bear on this, as the precedent does.
        self.assertEqual([(f["kind"], f["lead"]) for f in found], [
            ("decision", "Earlier, you answered a question on another task that may bear on this:"),
            ("record", "Ticket BILL-42 by Dana Ortiz:")])
        self.assertEqual((found[0]["question"], found[0]["quote"]),
                         ("Should load-test traffic be billed?", "Never bill our own load tests."))
        self.assertEqual(found[0]["note"], "Signed for that question. Context here, not approval.")
        self.assertEqual((found[1]["quote"], found[1]["note"]), ("Exclude load tests from usage billing", "Done."))
        said = json.dumps(found)
        for internal in (earlier["node_id"], "0.38", "assumption", "cut here", "inbox", "_["):
            self.assertNotIn(internal, said)
        # Slack keeps its own cut, without the assumption glued to it.
        from bridge.delivery import _found_for_owner
        self.assertNotIn("assumption", _found_for_owner(evidence))
        self.assertIn(earlier["node_id"], _found_for_owner(evidence))

    def test_how_an_owner_decides_is_dropped_once_it_is_someone_elses_or_unrelated(self):
        """Measured on prometheus/prometheus: the handed-on WAL watcher
        question showed "How Tomas Novak decided a similar question"
        about out-of-order samples, a match the ladder scored under its
        floor, on the page of the person it was handed to."""
        earlier = self.node(self.task(title="Pricing pilot"), question="Should load-test traffic be billed?")
        self.store.answer(earlier["node_id"], {"answer": "Never bill our own load tests.",
                                               "expected_updated_at": self.decision(earlier["node_id"])["updated_at"]},
                          actor=Actor.person(self.graph.get_person(self.wes)))
        t = self.task()
        related = self.node(t)
        unrelated = self.node(t, question="Should invoices round to the nearest cent?")
        for n in (related, unrelated):
            with self.graph.transaction():
                self.graph.db.execute("UPDATE decisions SET evidence=? WHERE id=?", (
                    f'how they decide: Wes Chen answered "Should load-test traffic be billed?" (decision '
                    f'{earlier["node_id"]}, similarity 0.38)', n["node_id"]))
        leads = lambda n, who: [f["lead"] for f in self.page(who, t, n["node_id"])["focus"]["found"]]
        self.assertEqual(leads(related, self.wes),
                         ["Earlier, you answered a question on another task that may bear on this:"])
        self.assertEqual(leads(unrelated, self.wes), [], "one shared word is not a similar question")
        self.store.refer(related["node_id"], {"person": self.marisol, "scope_kind": "none",
                                              "expected_updated_at": self.decision(related["node_id"])["updated_at"]},
                         actor=Actor.person(self.graph.get_person(self.wes)))
        self.assertEqual(leads(related, self.marisol), [], "after a hand-on it describes someone else's judgment")

    def test_why_it_came_to_you_reads_the_map_and_the_listing(self):
        with self.graph.transaction():
            self.graph.db.execute("UPDATE people SET github_login='wesc' WHERE id=?", (self.wes,))
            self.graph.add_listing("acme/platform", "codeowners", "/billing/", "@wesc", ord=1)
        t = self.task()
        n = self.node(t)
        why = self.page(self.wes, t, n["node_id"])["focus"]["why"]
        self.assertEqual(why["heading"], "Why this came to you")
        self.assertEqual(why["summary"], "Raven's ownership map lists you as deciding billing/*; you are also in "
                                         "CODEOWNERS for /billing/.")
        # A note holding "; " of its own is one line, whole; what is for
        # an administrator, or already on the page, is left out.
        long = ("verified: Wes Chen decides for billing/* (matches billing/usage.py) (config); note: CODEOWNERS lists "
                "dana here; the organization's authority map names Wes Chen, so Bridge routes to Wes Chen; dana "
                "still holds the CODEOWNERS listing; area: the agent is working in billing/usage.py; GitHub not "
                "synced for acme/platform: Bridge knows past reviews only from git trailers")
        with self.graph.transaction():
            self.graph.db.execute("UPDATE decisions SET owner_evidence=? WHERE id=?", (long, n["node_id"]))
        details = self.page(self.wes, t, n["node_id"])["focus"]["why"]["details"]
        self.assertEqual(details, [
            "Wes Chen decides for billing/* (matches billing/usage.py)",
            "CODEOWNERS lists dana here; the organization's authority map names Wes Chen, so Raven routes to Wes "
            "Chen; dana still holds the CODEOWNERS listing"])
        # Someone else reading it is told why Raven asked the owner.
        other = self.page(self.marisol, t, n["node_id"])["focus"]["why"]
        self.assertEqual(other["heading"], "Why Raven asked Wes Chen")
        self.assertTrue(other["summary"].startswith("Raven's ownership map lists Wes Chen as deciding billing/*"))

    def test_a_hand_on_says_who_and_what_answering_teaches(self):
        t = self.task()
        n = self.node(t, question="Should invoices round usage to the nearest thousand calls?")
        self.store.refer(n["node_id"], {"person": self.marisol, "note": "Marisol owns invoice formatting",
                                        "scope_kind": "path", "scope": "billing/*",
                                        "expected_updated_at": self.decision(n["node_id"])["updated_at"]},
                         actor=Actor.person(self.graph.get_person(self.wes)))
        why = self.page(self.marisol, t, n["node_id"])["focus"]["why"]
        self.assertEqual(why["summary"], "Wes Chen handed this on to you: “Marisol owns invoice formatting”. "
                                         "If you answer, Raven asks you first about later questions under billing/.")
        self.assertEqual(why["details"], [])

    def test_notes_say_where_they_came_from(self):
        t = self.task()
        canvas.add_note(self.store, t, {"text": "Enterprise-two is on a custom contract.", "by": "Priya Natarajan"})
        briefing.add_note(self.store, self.link(self.mint(self.wes, t)), {"text": "Never bill our own load tests."})
        notes = self.page(self.wes, t, "")["notes"]
        self.assertEqual([(n["by"], n["source"]) for n in notes],
                         [("Priya Natarajan", ""), ("Wes Chen", "page")])


# ---------------- a link that no longer works ----------------

class ResendTests(BriefCase, Http):
    """An expired link asks for a new one, which goes to the person it
    named, in their own DM; whoever asks is told the same thing."""

    def setUp(self):
        super().setUp()
        self.serve(ready_server(self.store, port=0))
        self.t = self.task()
        self.a = self.node(self.t)
        self.delivery.deliver_now()
        self.token = self.links("UWES")[self.a["node_id"]]

    def expire(self, token):
        self.graph.db.execute("UPDATE brief_links SET expires_at=? WHERE token_hash=?",
                              (int(time.time()) - 60, briefing._hash(token)))

    def resend(self, token):
        status, body, _ = self.brief(token, "/api/brief/resend", {})
        self.assertEqual(status, 200)
        return body

    def new_links(self):
        return [m for m in self.slack.messages if "A new link to your task" in m["text"]]

    def test_an_expired_link_brings_a_new_one_to_its_person(self):
        old = self.link(self.token)
        self.expire(self.token)
        self.assertEqual(self.brief(self.token)[0], 401)
        body = self.resend(self.token)
        self.assertIn("If this link was yours", body["notice"])
        sent = self.new_links()
        self.assertEqual([m["channel"] for m in sent], ["DUWES"])
        fresh = self.link(LINK.findall(sent[0]["text"])[0])
        self.assertEqual((fresh.person["id"], fresh.run_id, fresh.decision_id, fresh.asked),
                         (self.wes, self.t, self.a["node_id"], self.a["node_id"]))
        self.assertNotEqual(fresh.id, old.id)
        self.assertEqual(self.brief(LINK.findall(sent[0]["text"])[0])[0], 200)
        # Once an hour per link.
        self.assertEqual(self.resend(self.token), body)
        self.assertEqual(len(self.new_links()), 1)

    def test_nothing_is_sent_for_a_link_that_is_not_an_expired_one(self):
        expected = self.resend("rvn_" + "x" * 32)
        self.assertEqual(self.resend(self.token), expected)  # still works: nothing to replace
        with self.graph.transaction():
            briefing.revoke(self.graph, self.link(self.token).id)
        self.expire(self.token)
        self.assertEqual(self.resend(self.token), expected)  # revoked stays revoked
        self.assertEqual(self.resend(""), expected)
        self.assertEqual(self.new_links(), [])

    def test_off_mode_sends_nothing(self):
        self.expire(self.token)
        self.store.update_settings({"brief_mode": "off"})
        self.resend(self.token)
        self.assertEqual(self.new_links(), [])


class OwnLinkTests(AuthOnCase):
    def test_opening_the_page_again_keeps_one_link_of_your_own(self):
        """Every click minted another link that worked for two weeks."""
        first = self.request("POST", f"/api/tasks/{self.t}/link", self.bearer(self.wes),
                             {"decision_id": self.a["node_id"]})[1]["url"].split("#", 1)[1]
        second = self.request("POST", f"/api/tasks/{self.t}/link", self.bearer(self.wes),
                              {"decision_id": self.a["node_id"]})[1]["url"].split("#", 1)[1]
        self.assertIsNone(briefing.resolve(self.graph, first))
        self.assertIsNotNone(briefing.resolve(self.graph, second))
        # The link that came with the message is not one of those.
        self.assertIsNotNone(briefing.resolve(self.graph, self.token))

    def test_the_app_knows_what_the_page_offers(self):
        settings = self.request("GET", "/api/state", self.bearer(self.wes))[1]["settings"]
        self.assertEqual(settings["brief_mode"], "static")


class AccountOfferTests(AuthOnCase):
    """The page offers an account only where one can be made from it."""

    def viewer(self, token):
        return self.brief(token)[1]["viewer"]

    def test_who_is_offered_an_account(self):
        v = self.viewer(self.token)
        self.assertEqual((v["can_create_account"], v["github_sign_in"], v["sign_in"]), (True, False, True))
        self.assertEqual(self.viewer(self.mint(self.ada, self.t))["can_create_account"], False)
        self.auth.github_client_id, self.auth.github_client_secret = "id", "secret"
        self.assertTrue(self.viewer(self.token)["github_sign_in"])
        self.graph.set_setting("brief_signup", "0")
        v = self.viewer(self.token)
        self.assertEqual((v["can_create_account"], v["github_sign_in"]), (False, False))

    def test_without_sign_in_there_is_nothing_to_offer(self):
        with self.graph.transaction():
            token = briefing.mint(self.graph, self.wes, self.t, self.a["node_id"])
        v = briefing.overview(self.store, self.link(token))["viewer"]
        self.assertEqual((v["can_create_account"], v["sign_in"]), (False, False))



# ---------------- what the page found, a decision opened from the list ----------------

class PrecedentTests(BriefCase):
    """What Raven found on an open decision includes the signed answer on
    another task that a search of the workspace's memory for its question
    finds. Measured on prometheus/prometheus: the
    ask-time search, held to its prediction floor, cited nothing, and the
    signed "Never drop samples silently" was one search away."""

    def page(self, person_id, task, decision_id, **kw):
        with self.graph.transaction():
            token = briefing.mint(self.graph, person_id, task, decision_id)
        return briefing.overview(self.store, self.link(token), **kw)

    def earlier(self, question, answer, title="Usage exports"):
        e = self.node(self.task(title=title), question=question)
        self.store.answer(e["node_id"], {"answer": answer, "rationale": "policy",
                                         "expected_updated_at": self.decision(e["node_id"])["updated_at"]},
                          actor=Actor.person(self.graph.get_person(self.wes)))
        return e

    def test_a_signed_answer_on_another_task_is_shown_as_context(self):
        self.earlier("Should the usage export include internal test accounts?",
                     "No: internal accounts stay out of every export.")
        t = self.task()
        n = self.node(t)
        self.assertNotIn("decision ", self.decision(n["node_id"])["evidence"])
        found = self.page(self.marisol, t, n["node_id"])["focus"]["found"]
        self.assertEqual([(f["lead"], f["question"], f["quote"], f["note"]) for f in found], [
            ("Earlier, Wes Chen answered a question on another task that may bear on this:",
             "Should the usage export include internal test accounts?",
             "No: internal accounts stay out of every export.",
             "Signed for that question. Context here, not approval.")])
        self.assertEqual(self.page(self.wes, t, n["node_id"])["focus"]["found"][0]["lead"],
                         "Earlier, you answered a question on another task that may bear on this:")

    def test_one_shared_word_a_same_task_answer_or_an_answered_decision_shows_nothing(self):
        self.earlier("Should the onboarding email mention usage?", "Yes, in one line.")
        t = self.task()
        n = self.node(t)
        self.assertEqual(self.page(self.wes, t, n["node_id"])["focus"]["found"], [])
        # A decision on the same task is under Decisions already.
        same = self.node(t, question="Should the usage export include internal test accounts?")
        self.store.answer(same["node_id"], {"answer": "No.", "expected_updated_at":
                                            self.decision(same["node_id"])["updated_at"]},
                          actor=Actor.person(self.graph.get_person(self.wes)))
        self.assertEqual(self.page(self.wes, t, n["node_id"])["focus"]["found"], [])
        # Once answered, what an open question needed is not searched for.
        self.earlier("Should the usage export include internal test accounts?",
                     "No: internal accounts stay out of every export.", title="Usage exports again")
        self.store.answer(n["node_id"], {"answer": "Exclude the load test.", "expected_updated_at":
                                         self.decision(n["node_id"])["updated_at"]},
                          actor=Actor.person(self.graph.get_person(self.wes)))
        self.assertEqual(self.page(self.wes, t, n["node_id"])["focus"]["found"], [])


class FocusTests(BriefCase, Http):
    """A decision opened from the page's list comes with everything the
    one the link names has. It came with its context, brief, what Raven
    found and why it came to them emptied."""

    def setUp(self):
        super().setUp()
        self.serve(ready_server(self.store, port=0))
        self.t = self.task()
        self.a = self.node(self.t)
        self.b = self.node(self.t, question="Should usage above the plan be billed monthly or quarterly?",
                           context="Finance closes the books monthly.")
        self.delivery.deliver_now()
        self.token = self.links("UWES")[self.a["node_id"]]

    def test_another_decision_on_the_task_opens_whole(self):
        status, page, _ = self.brief(self.token, f"/api/brief?focus={self.b['node_id']}")
        self.assertEqual(status, 200)
        f = page["focus"]
        self.assertEqual((f["node_id"], f["context"], f["asked"]), (self.b["node_id"], "Finance closes the books monthly.",
                                                                     False))
        self.assertIsNotNone(f["why"])
        self.assertTrue(page["asked_focus"])
        _, page, _ = self.brief(self.token)
        self.assertEqual((page["focus"]["node_id"], page["focus"]["asked"]), (self.a["node_id"], True))

    def test_a_decision_on_another_task_opens_the_links_own(self):
        other = self.node(self.task(title="Invoice emails"), question="Should invoice emails list usage per key?")
        _, page, _ = self.brief(self.token, f"/api/brief?focus={other['node_id']}")
        self.assertEqual(page["focus"]["node_id"], self.a["node_id"])
        self.assertNotIn("invoice emails list usage", json.dumps(page).lower())


class ReasonTests(BriefCase):
    """An answer given without a reason records none. "answered from the
    task page" showed under Why in Decisions and Memory as if it were the
    person's reasoning."""

    def test_no_reason_given_is_not_written_as_one(self):
        t = self.task()
        n = self.node(t)
        self.delivery.deliver_now()
        link = self.link(self.links("UWES")[n["node_id"]])
        briefing.act(self.store, link, {"decision_id": n["node_id"], "answer": "Exclude the load test.",
                                        "expected_updated_at": self.decision(n["node_id"])["updated_at"]})
        row = self.decision(n["node_id"])
        self.assertNotIn("task page", row["rationale"])
        page = briefing.overview(self.store, link)
        self.assertEqual(page["focus"]["rationale"], "")
        self.assertEqual(next(x for x in page["nodes"] if x["node_id"] == n["node_id"])["rationale"], "")
        # A correction without a reason, likewise.
        briefing.act(self.store, link, {"decision_id": n["node_id"], "answer": "Bill it.",
                                        "expected_updated_at": self.decision(n["node_id"])["updated_at"]})
        self.assertNotIn("task page", self.decision(n["node_id"])["rationale"])
        self.assertEqual(briefing.overview(self.store, link)["focus"]["rationale"], "")


# ---------------- handing on from the page ----------------

class HandOnTests(BriefCase, Http):
    """`not me @person`, from the page: the same store.refer the Slack
    reply uses, on the person's own standing, with the revision they
    read."""

    def setUp(self):
        super().setUp()
        self.serve(ready_server(self.store, port=0))
        self.t = self.task()
        self.a = self.node(self.t)
        self.delivery.deliver_now()
        self.token = self.links("UWES")[self.a["node_id"]]

    def refer(self, token, person, note="Pricing calls are hers.", expected=None, decision_id=None):
        decision_id = decision_id or self.a["node_id"]
        expected = self.decision(decision_id)["updated_at"] if expected is None else expected
        return self.brief(token, "/api/brief/refer", {"decision_id": decision_id, "expected_updated_at": expected,
                                                      "person": person, "note": note})

    def test_the_page_offers_the_people_the_map_names(self):
        f = briefing.overview(self.store, self.link(self.token))["focus"]
        self.assertTrue(f["can_refer"])
        names = [x["name"] for x in f["handon"]]
        self.assertIn("Marisol Vega", names)
        self.assertNotIn("Wes Chen", names)
        self.assertEqual(next(x for x in f["handon"] if x["name"] == "Marisol Vega")["why"], "decides pricing questions")

    def test_handing_on_reassigns_it_and_messages_the_next_person(self):
        with self.graph.transaction():
            self.graph.db.execute("UPDATE decisions SET kind='prediction', prediction='Exclude it.' WHERE id=?",
                                  (self.a["node_id"],))
        before = len(self.slack.messages)
        status, body, _ = self.refer(self.token, self.marisol)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["notice"].startswith("Handed to Marisol Vega, for this question only. It now waits on "
                                                  "them."), body)
        # From a link, a hand-on teaches Raven no route: a forwarded link
        # could otherwise change who decides for the whole workspace.
        self.assertEqual([a for a in self.store.authority() if a["source"] == "referral"], [])
        row = self.decision(self.a["node_id"])
        self.assertEqual(row["owner_name"], "Marisol Vega")
        self.assertIn('handed on by Wes Chen: "Pricing calls are hers."', row["owner_evidence"])
        # The prediction was Wes's, and the inbox no longer calls it one.
        self.assertEqual((row["prediction"] or "", row["kind"]), ("", "new"))
        self.delivery.deliver_now()
        self.assertIn("DUMAR", [m["channel"] for m in self.slack.messages[before:]])
        self.assertIn(self.a["node_id"], self.links("UMAR"))
        # Wes still decides billing/*, so they keep their standing; the page
        # says they handed it on, and its list offers them no "Answer this".
        page = self.brief(self.token)[1]
        self.assertEqual(page["focus"]["handed_to"], "Marisol Vega")
        self.assertEqual(next(n for n in page["nodes"] if n["node_id"] == self.a["node_id"])["handed_to"],
                         "Marisol Vega")
        # By name works too, as in Slack.
        status, body, _ = self.refer(self.links("UMAR")[self.a["node_id"]], "Wes Chen", note="")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.decision(self.a["node_id"])["owner_name"], "Wes Chen")
        self.assertEqual(self.brief(self.token)[1]["focus"]["handed_to"], "")

    def test_what_is_refused(self):
        status, body, _ = self.refer(self.token, self.marisol, expected="2020-01-01T00:00:00+00:00")
        self.assertEqual(status, 400)
        self.assertIn("changed while you were reviewing", body["error"])
        self.assertEqual(self.refer(self.token, "Nobody Known")[0], 400)
        self.assertIn("Hand it to someone else", self.refer(self.token, self.wes)[1]["error"])
        self.assertEqual(self.refer(self.token, "")[0], 400)
        other = self.node(self.task(title="Invoice emails"), question="Should invoice emails list usage per key?")
        self.assertIn("not found on this task", self.refer(self.token, self.marisol, decision_id=other["node_id"])[1]["error"])
        # Someone the decision is not for may not hand it on.
        stranger = self.mint(self.priya, self.t, self.a["node_id"])
        self.assertFalse(briefing.overview(self.store, self.link(stranger))["focus"]["can_refer"])
        status, body, _ = self.refer(stranger, self.marisol)
        self.assertEqual(status, 403)
        self.assertEqual(self.decision(self.a["node_id"])["owner_name"], "Wes Chen")


class HandOnFromALinkTests(BriefCase, Http):
    """A hand-on from the page, round 3. A link can be forwarded, and a
    hand-on from one taught Raven, for the whole workspace, that the
    person named decides a directory: the next tsdb/wlog question went to
    them over the administrator's map, and the evidence called it
    verified. From a link it is for that question only, and the form says
    so before it is sent. The picker names the people CODEOWNERS lists
    whom the workspace has no person for, and a refusal says why in terms
    of what was typed."""

    def setUp(self):
        super().setUp()
        self.serve(ready_server(self.store, port=0))
        with self.graph.transaction():
            self.graph.add_authority("path", "tsdb/*", "decides", person_id=self.marisol)
            self.graph.db.execute("UPDATE people SET github_login='wesc' WHERE id=?", (self.wes,))
            self.graph.add_listing("acme/platform", "codeowners", "/tsdb", "@wesc", ord=1)
            self.graph.add_listing("acme/platform", "codeowners", "/tsdb", "@kmarsh", ord=1)
        self.t = self.task()
        self.n = self.node(self.t, question="Should the WAL watcher track sample counts per remote-write queue?",
                           paths="tsdb/wlog/watcher.go")
        # Marisol, who decides tsdb/*, hands it to Wes in Slack's way.
        self.store.refer(self.n["node_id"], {"person": self.wes, "note": "Wes wrote the watcher.",
                                             "expected_updated_at": self.decision(self.n["node_id"])["updated_at"]},
                         actor=Actor.person(self.graph.get_person(self.marisol)))
        self.delivery.deliver_now()
        self.token = self.links("UWES")[self.n["node_id"]]

    def refer(self, person, note="Priya owns the watcher's memory budget."):
        return self.brief(self.token, "/api/brief/refer", {
            "decision_id": self.n["node_id"], "person": person, "note": note,
            "expected_updated_at": self.decision(self.n["node_id"])["updated_at"]})

    def test_the_form_says_what_it_leaves_alone_and_teaches_no_route(self):
        f = self.brief(self.token)[1]["focus"]
        # What a hand-on in the app would teach, said before it is sent.
        self.assertEqual(f["handon_keeps"], "later questions under tsdb/wlog/")
        before = [a for a in self.store.authority() if a["source"] == "referral"]
        status, body, _ = self.refer(self.priya)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["notice"], "Handed to Priya Natarajan, for this question only. It now waits on them.")
        self.assertEqual([a for a in self.store.authority() if a["source"] == "referral"], before)
        row = self.decision(self.n["node_id"])
        self.assertEqual(row["owner_name"], "Priya Natarajan")
        self.assertNotIn("once you answer", row["owner_evidence"])
        # Priya answers; the next question there still goes by the map.
        self.store.answer(self.n["node_id"], {"answer": "Only in the queue manager.",
                                              "expected_updated_at": row["updated_at"]},
                          actor=Actor.person(self.graph.get_person(self.priya)))
        later = self.node(self.t, question="Should the WAL reader skip corrupt segments?", paths="tsdb/wlog/reader.go")
        self.assertEqual(later["owner"], "Marisol Vega", later["owner_evidence"])

    def test_the_picker_marks_who_handed_it_and_who_raven_cannot_message(self):
        f = self.brief(self.token)[1]["focus"]
        marisol = next(x for x in f["handon"] if x["name"] == "Marisol Vega")
        self.assertTrue(marisol["handed_you"])
        self.assertEqual(f["handon"][-1]["name"], "Marisol Vega", "whoever handed it to you is listed last")
        self.assertEqual(f["handon_unknown"], [{"name": "@kmarsh", "why": "CODEOWNERS for /tsdb"}])
        self.assertNotIn("Wes Chen", [x["name"] for x in f["handon_unknown"]])

    def test_a_refusal_says_why_in_terms_of_what_was_typed(self):
        status, body, _ = self.refer("kmarsh")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "kmarsh is in CODEOWNERS for /tsdb but has no Raven person yet. An admin "
                                        "can add them, or pick someone in the list.")
        status, body, _ = self.refer("Nobody Known")
        self.assertEqual(body["error"], "Raven has no person called Nobody Known. Pick someone in the list, or ask a "
                                        "Raven admin to add them.")
        self.assertEqual(self.decision(self.n["node_id"])["owner_name"], "Wes Chen")

    def test_your_own_hand_on_reads_in_the_first_person_with_one_period(self):
        status, body, _ = self.refer(self.priya, note="She knows what per-tenant counts would cost there.")
        self.assertEqual(status, 200, body)
        why = self.brief(self.token)[1]["focus"]["why"]
        self.assertEqual(why["summary"], "You handed this on to Priya Natarajan: “She knows what per-tenant counts "
                                         "would cost there.”")
        # A reason with no closing stop gets the sentence's own.
        self.assertEqual(briefing._quoted("Wes wrote the watcher"), "“Wes wrote the watcher”.")


class ReferralEvidenceTests(BriefCase):
    """A route a hand-on taught is one person's word, accepted when the
    other answered. It routes as the map does, and neither the evidence
    nor the page calls it verified or the ownership map."""

    def test_a_learned_route_is_said_to_be_a_hand_on(self):
        t = self.task()
        n = self.node(t, question="Should the CSV writer quote every field?", paths="billing/exports/csv.py")
        self.store.refer(n["node_id"], {"person": self.marisol, "scope_kind": "path", "scope": "billing/exports/*",
                                        "expected_updated_at": self.decision(n["node_id"])["updated_at"]},
                         actor=Actor.person(self.graph.get_person(self.wes)))
        self.store.answer(n["node_id"], {"answer": "Yes.", "expected_updated_at": self.decision(n["node_id"])["updated_at"]},
                          actor=Actor.person(self.graph.get_person(self.marisol)))
        m = self.node(t, question="Should the CSV writer use semicolons as separators?", paths="billing/exports/csv.py")
        self.assertEqual(m["owner"], "Marisol Vega", m["owner_evidence"])
        self.assertIn("(referral, asserted by Wes Chen)", m["owner_evidence"])
        with self.graph.transaction():
            token = briefing.mint(self.graph, self.marisol, t, m["node_id"])
        why = briefing.overview(self.store, self.link(token))["focus"]["why"]
        self.assertEqual(why["summary"], "Wes Chen handed you an earlier question here, so Raven asks you first about "
                                         "questions under billing/exports/. That is a hand-on, not Raven's ownership "
                                         "map.")
        self.assertTrue(re.match(r"Marisol Vega decides for billing/exports/\* \(matches billing/exports/\S*\), from a "
                                 r"hand-on by Wes Chen", why["details"][0]),
                        why["details"])
        self.assertNotIn("verified", json.dumps(why))


class PeopleWordsTests(BriefCase):
    """Who decided a node, one rule for the page and the app: the person
    whose answer it is. An answered node keeps the kind it was asked
    with, and the app called Mei's own answer "Signed"."""

    def test_an_owner_who_answered_decided_it(self):
        t = self.task()
        n = self.node(t)
        self.store.answer(n["node_id"], {"answer": "Exclude it.", "expected_updated_at": self.decision(n["node_id"])["updated_at"]},
                          actor=Actor.person(self.graph.get_person(self.wes)))
        with self.graph.transaction():
            token = briefing.mint(self.graph, self.wes, t, n["node_id"])
        view = briefing.overview(self.store, self.link(token))
        node = next(x for x in view["nodes"] if x["node_id"] == n["node_id"])
        self.assertEqual((node["kind"], briefing.decided_by(node)), ("new", "Wes Chen"))
        wes = next(c for c in view["contacts"] if c["name"] == "Wes Chen")
        self.assertEqual([(d["question"][:20], d.get("signed", False)) for d in wes["decided"]],
                         [("Should we bill the u", False)])
        # A signature on an answer somebody else supplied is a signature.
        self.assertEqual(briefing.decided_by({"answered_by": "Memory", "kind": "resolved", "signatures": ["Wes Chen"]}),
                         "")


class NoteWithdrawTests(BriefCase, Http):
    """A note added from the page went straight to the coding agent and
    could not be taken back. Its author can withdraw it; the note stays,
    marked, and a second note tells the agent."""

    def setUp(self):
        super().setUp()
        self.serve(ready_server(self.store, port=0))
        self.t = self.task()
        self.a = self.node(self.t)
        self.delivery.deliver_now()
        self.token = self.links("UWES")[self.a["node_id"]]

    def test_the_author_withdraws_their_own_note_once(self):
        self.assertEqual(self.brief(self.token, "/api/brief/note", {"text": "Enterprise-two caps overage at 2x."})[0],
                         200)
        note = self.brief(self.token)[1]["notes"][0]
        self.assertEqual((note["mine"], note["withdrawn"]), (True, False))
        other = self.mint(self.marisol, self.t, self.a["node_id"])
        self.assertFalse(self.brief(other)[1]["notes"][0]["mine"])
        status, body, _ = self.brief(other, "/api/brief/withdraw", {"note_id": note["id"]})
        self.assertEqual(status, 400)
        self.assertIn("Only the person who added a note", body["error"])
        self.assertEqual(self.brief(self.token, "/api/brief/withdraw", {})[0], 400)
        for _ in range(2):
            status, body, _ = self.brief(self.token, "/api/brief/withdraw", {"note_id": note["id"]})
            self.assertEqual((status, body["withdrawn"]), (200, True))
        notes = self.brief(self.token)[1]["notes"]
        self.assertEqual([(n["text"], n["withdrawn"]) for n in notes], [("Enterprise-two caps overage at 2x.", True)])
        tree = [n["text"] for n in canvas.get_tree(self.store, self.t)["notes"]]
        self.assertEqual(tree, ["Enterprise-two caps overage at 2x.",
                                "Withdrawn by its author: Enterprise-two caps overage at 2x."])
        # A note from the app is not the page's to withdraw.
        canvas.add_note(self.store, self.t, {"text": "From the app.", "by": "Wes Chen"})
        app_note = self.brief(self.token)[1]["notes"][-1]
        self.assertFalse(app_note["mine"])
        self.assertEqual(self.brief(self.token, "/api/brief/withdraw", {"note_id": app_note["id"]})[0], 400)


class LinkShapeTests(BriefCase):
    """The page tells a cut-off link from one that expired by its length:
    every token Raven mints is "rvn_" and 32 more characters."""

    def test_a_token_has_one_shape(self):
        t = self.task()
        for _ in range(20):
            self.assertRegex(self.mint(self.wes, t), r"^rvn_[A-Za-z0-9_-]{32}$")
        page = Path(__file__).resolve().parents[1].joinpath("web", "brief.js").read_text()
        self.assertIn("const WELL_FORMED = /^rvn_[A-Za-z0-9_-]{32}$/;", page)

    def test_the_dm_says_who_asked_and_through_which_agent(self):
        t = canvas.start_task(self.store, CFG, {"title": "Add usage-based pricing", "repo": "acme/platform",
                                                "requester": "Priya Natarajan", "agent": "Claude Code",
                                                "paths": "billing/usage.py"})["task_id"]
        self.node(t)
        self.delivery.deliver_now()
        text = self.slack.messages[-1]["text"]
        self.assertIn("Task: Add usage-based pricing (acme/platform), requested by Priya Natarajan via Claude Code",
                      text)


# ---------------- the message ----------------

class MessageTextTests(BriefCase):
    """The first DM a stranger gets is short: the link line says what it
    is in a few words, and the rule syntax waits until they have answered
    in Slack."""

    def test_the_link_line_is_short_and_the_rule_hint_waits(self):
        n = self.node(self.task())
        self.delivery.deliver_now()
        first = self.slack.messages[-1]["text"]
        line = next(x for x in first.split("\n") if "/brief#" in x)
        self.assertTrue(line.endswith("|Open the task> (your own link, no account needed)"), line)
        self.assertIn("`not me @person`", first)
        self.assertNotIn("rule if", first)
        self.store.answer(n["node_id"], {"answer": "Exclude it.", "rationale": "load test", "source": "slack: Wes Chen",
                                         "expected_updated_at": self.decision(n["node_id"])["updated_at"]},
                          actor=Actor.person(self.graph.get_person(self.wes)))
        self.node(self.task(title="Invoice emails"), question="Should invoice emails list usage per key?")
        self.delivery.deliver_now()
        self.assertIn("rule if <words> until <date>", self.slack.messages[-1]["text"])

if __name__ == "__main__":
    unittest.main()
