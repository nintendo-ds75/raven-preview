"""The trust contract, from the first-user readiness review: every probe
in `reviews/first-user-readiness-6960076/probes.py` asserted here as the
safe outcome, and the permitted person still able to do their work.

Each test names the boundary it holds: who may act, what a signature
covers, what a message a person replied to was showing, what a rule
still authorizes, what a webhook proves, and what a failed inbound
event must not lose.
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.error import HTTPError

from fixtures import OfflineCase

from bridge import canvas
from bridge.auth import Auth
from bridge.authz import Actor, Refused
from bridge.github import sync_pull, sync_repo, sync_state
from bridge.store import Invalid
from test_auth import BOOTSTRAP, SharedServer
from test_github import REPO as GH_REPO, GitHubCase
from test_portal import CFG, REPO, PortalCase
from test_rules import RuleCase


def finish(store, task):
    try:
        return canvas.finish_task(store, {"task_id": task})["status"]
    except Invalid as error:
        return f"refused: {error}"


class WhoMayAct(SharedServer):
    """Blocker 1: an agent credential and an unrelated person cannot
    authorize somebody else's decision, on any transport."""

    def setUp(self):
        super().setUp()
        self.owner = self.post("/api/people", {"name": "Policy Owner", "email": "owner@example.test"}, token=BOOTSTRAP)
        self.other = self.post("/api/people", {"name": "Other Member", "email": "other@example.test"}, token=BOOTSTRAP)
        self.post("/api/authority", {"person": self.owner["id"], "scope_kind": "path", "scope": "billing/*",
                                     "role": "decides"}, token=BOOTSTRAP)

    def agent_node(self):
        person = self.post("/api/people", {"name": "Agent Requester", "email": "requester@example.test"}, token=BOOTSTRAP)
        token = self.post("/api/tokens", {"person_id": person["id"], "label": "coding-agent"}, token=BOOTSTRAP)["token"]
        task = self.mcp("bridge_start_task", {
            "title": "Change invoice policy", "repo": "acme/platform", "paths": "billing/rates.py"}, token)["result"]
        node = self.mcp("bridge_add_node", {
            "task_id": task["task_id"], "question": "Should usage spikes be billed?",
            "paths": "billing/rates.py"}, token)["result"]
        return token, task, node

    def test_an_agent_credential_writes_nodes_and_authorizes_nothing(self):
        token, task, node = self.agent_node()
        self.assertEqual(node["owner"], "Policy Owner")
        for path, body in ((f"/api/decisions/{node['node_id']}/answer",
                            {"answer": "Bill all spikes", "expected_updated_at": node["updated_at"]}),
                           (f"/api/decisions/{node['node_id']}/signoff",
                            {"by": "Agent Requester", "expected_updated_at": node["updated_at"]}),
                           (f"/api/decisions/{node['node_id']}/refer", {"person": self.other["id"]}),
                           (f"/api/decisions/{node['node_id']}/rule", {"by": "Agent Requester"}),
                           ("/api/tokens", {"label": "stronger"}),
                           ("/api/people", {"name": "Someone"})):
            self.assertEqual(self.status_of("POST", path, body, token=token), 403, path)
        # It cannot become a browser session either.
        status, headers, _ = self.raw("POST", "/auth/token", {"Content-Type": "application/x-www-form-urlencoded"},
                                      f"token={token}".encode())
        self.assertEqual(status, 302)
        self.assertIn("agent%20credential", headers.get("Location", ""))
        # The decision is untouched and the task cannot finish.
        row = self.store.get_decision(node["node_id"])
        self.assertEqual((row["status"], row["answer"]), ("pending", None))
        done = self.mcp("bridge_finish_task", {"task_id": task["task_id"]}, token)
        self.assertTrue(done["isError"])
        self.assertIn("wait", done["result"])

    def test_an_unrelated_member_cannot_answer_and_the_owner_can(self):
        _token, task, node = self.agent_node()
        outsider = self.auth.session_cookie(self.other["id"]).split(";", 1)[0]
        state = self.get("/api/state", cookie=outsider)
        self.assertEqual(self.status_of("POST", f"/api/decisions/{node['node_id']}/answer",
                                        {"answer": "Bill it", "expected_updated_at": node["updated_at"]},
                                        cookie=outsider, csrf=state["csrf_token"]), 403)
        self.assertEqual(self.store.get_decision(node["node_id"])["status"], "pending")
        # The person it is routed to answers, and the task finishes.
        owner = self.auth.session_cookie(self.owner["id"]).split(";", 1)[0]
        state = self.get("/api/state", cookie=owner)
        row = self.post(f"/api/decisions/{node['node_id']}/answer",
                        {"answer": "Bill all spikes", "rationale": "policy",
                         "expected_updated_at": node["updated_at"]}, cookie=owner, csrf=state["csrf_token"])
        self.assertEqual((row["signed_by"], row["answered_by"]), ("Policy Owner", "Policy Owner"))
        self.assertEqual(row["actor_basis"], "owner")
        agent = self.post("/api/tokens", {"person_id": self.owner["id"]}, token=BOOTSTRAP)["token"]
        self.mcp("bridge_get_tree", {"task_id": task["task_id"]}, agent)
        done = self.mcp("bridge_finish_task", {"task_id": task["task_id"]}, agent)
        self.assertEqual(done["result"]["status"], "completed")

    def test_an_admin_decides_for_someone_only_by_saying_so(self):
        _token, _task, node = self.agent_node()
        admin = self.post("/api/people", {"name": "Ada Admin", "email": "ada@example.test", "role": "admin"},
                          token=BOOTSTRAP)
        cookie = self.auth.session_cookie(admin["id"]).split(";", 1)[0]
        state = self.get("/api/state", cookie=cookie)
        try:
            self.call("POST", f"/api/decisions/{node['node_id']}/answer",
                      {"answer": "Bill it", "expected_updated_at": node["updated_at"]}, cookie=cookie,
                      csrf=state["csrf_token"])
            self.fail("an admin is not this decision's owner")
        except HTTPError as error:
            self.assertEqual(error.code, 403)
            self.assertIn("override=true", json.loads(error.read())["error"])
        row = self.post(f"/api/decisions/{node['node_id']}/answer",
                        {"answer": "Bill it", "override": True, "expected_updated_at": node["updated_at"]},
                        cookie=cookie, csrf=state["csrf_token"])
        self.assertEqual(row["actor_basis"], "admin-override")
        self.assertEqual(row["actor_name"], "Ada Admin")
        self.assertEqual(row["answered_by"], "Policy Owner")


class WhoTheSignInIs(PortalCase):
    """Blocker 2: GitHub sign-in binds to an exact identity."""

    def sign_in(self, auth, login, name, emails, gid=""):
        return auth.sign_in_github_user({"id": gid or str(abs(hash(login)) % 10 ** 9), "login": login, "name": name,
                                         "emails": emails, "primary_email": emails[0] if emails else "",
                                         "orgs": ["acme"]})

    def test_a_lookalike_login_is_not_the_admin(self):
        with self.graph.transaction():
            pid = self.graph.add_person("Alice Admin", email="alice@company.test", github_login="alice-work", role="admin")
        auth = Auth(self.store, enabled=True)
        auth.allow_signup = False
        auth.github_org = "acme"
        for login, name, emails in (("aliceadmin", "Another User", ["different@example.test"]),
                                    ("alice", "Alice Admin", ["else@example.test"]),
                                    ("aliceAdmin", "Alice Admin", [])):
            with self.assertRaises(Invalid, msg=login):
                self.sign_in(auth, login, name, emails)
        # The login the organization recorded signs in, and binds to the id.
        person = self.sign_in(auth, "alice-work", "Alice Admin", ["alice@company.test"], gid="4242")
        self.assertEqual(person["id"], pid)
        self.assertEqual(self.graph.get_person(pid)["github_id"], "4242")
        # A rename follows the id; the old login on a new account does not.
        renamed = self.sign_in(auth, "alice-new", "Alice Admin", ["alice@company.test"], gid="4242")
        self.assertEqual(renamed["id"], pid)
        self.assertEqual(self.graph.get_person(pid)["github_login"], "alice-new")
        with self.assertRaises(Invalid):
            self.sign_in(auth, "alice-work", "Impostor", ["impostor@example.test"], gid="9999")

    def test_an_email_links_once_and_membership_is_not_identity(self):
        with self.graph.transaction():
            self.graph.add_person("Existing Admin", email="admin@company.test", github_login="admin-login", role="admin")
            self.graph.add_person("New Person", email="new@company.test")
        auth = Auth(self.store, enabled=True)
        auth.allow_signup = False
        auth.github_org = "acme"
        # A person with no login on record links by verified email once.
        linked = self.sign_in(auth, "newperson", "New Person", ["new@company.test"], gid="11")
        self.assertEqual(linked["name"], "New Person")
        with self.assertRaises(Invalid):
            self.sign_in(auth, "someone-else", "Someone Else", ["new@company.test"], gid="12")
        # An admin's email never re-links: their login is already bound.
        with self.assertRaises(Invalid):
            self.sign_in(auth, "admin-lookalike", "Existing Admin", ["admin@company.test"], gid="13")
        # Org membership admits nobody on its own.
        auth.allow_signup = True
        fresh = self.sign_in(auth, "brand-new", "Brand New", ["brand@company.test"], gid="14")
        self.assertNotEqual(fresh["id"], linked["id"])
        self.assertEqual(fresh["role"], "member")


class WhatASignatureCovers(PortalCase):
    """Blocker 3: a signature covers the text it was given for."""

    def setUp(self):
        super().setUp()
        with self.graph.transaction():
            for pid in (self.wes, self.dana):
                self.graph.add_authority("category", "pricing", "approves", person_id=pid, repo=REPO,
                                         source="config", asserted_by="review", accepted=True)

    def partly_approved(self):
        task = self.task("multi")
        node = self.node(task, "Change the overage pricing tiers for enterprise plans?", "n")
        self.answer(node["node_id"], "Charge 2 cents")
        row = self.store.get_decision(node["node_id"])
        canvas.sign_off(self.store, node["node_id"], {"by": "Wes Chen", "expected_updated_at": row["updated_at"]})
        view = canvas.node_view(self.store, node["node_id"])
        self.assertEqual(sorted(view["signatures"]), ["Priya Natarajan", "Wes Chen"])
        return task, node

    def test_a_correction_drops_the_signatures_it_did_not_cover(self):
        task, node = self.partly_approved()
        row = self.store.get_decision(node["node_id"])
        after = canvas.sign_off(self.store, node["node_id"], {"by": "Dana Ortiz", "answer": "Charge 20 cents",
                                                              "expected_updated_at": row["updated_at"]})
        self.assertEqual(after["answer"], "Charge 20 cents")
        self.assertEqual(after["signatures"], ["Dana Ortiz"])
        self.assertFalse(after["authorized"])
        self.assertIn("still waiting on Wes Chen", after.get("notice", ""))
        self.assertIn("refused", finish(self.store, task))
        # Wes reviews the new text and signs it: now it is authorized.
        row = self.store.get_decision(node["node_id"])
        signed = canvas.sign_off(self.store, node["node_id"], {"by": "Wes Chen", "expected_updated_at": row["updated_at"]})
        self.assertTrue(signed["authorized"])
        self.assertEqual(sorted(signed["signatures"]), ["Dana Ortiz", "Wes Chen"])
        self.assertEqual(finish(self.store, task), "completed")

    def test_an_agent_re_settling_drops_the_signatures_too(self):
        task, node = self.partly_approved()
        canvas.settle_node(self.store, {"task_id": task, "node_id": node["node_id"], "answer": "Charge 20 cents"})
        view = canvas.node_view(self.store, node["node_id"])
        self.assertEqual(view["signatures"], [])
        self.assertFalse(view["authorized"])
        row = self.store.get_decision(node["node_id"])
        partial = canvas.sign_off(self.store, node["node_id"], {"by": "Dana Ortiz", "expected_updated_at": row["updated_at"]})
        self.assertEqual(partial["signatures"], ["Dana Ortiz"])
        self.assertFalse(partial["authorized"])
        self.assertIn("refused", finish(self.store, task))

    def test_a_signature_for_a_revision_that_moved_on_is_refused(self):
        task, node = self.partly_approved()
        stale = self.store.get_decision(node["node_id"])["updated_at"]
        canvas.settle_node(self.store, {"task_id": task, "node_id": node["node_id"], "answer": "Charge 30 cents"})
        with self.assertRaises(Invalid):
            canvas.sign_off(self.store, node["node_id"], {"by": "Dana Ortiz", "expected_updated_at": stale})


class WhatTheMessageShowed(PortalCase):
    """Blocker 4: a Slack reply is bound to what the person was shown."""

    def thread(self, node_id, kind="signoff"):
        self.delivery.deliver_now()
        row = self.graph.db.execute("SELECT * FROM notifications WHERE decision_id=? AND kind=? AND state='sent' "
                                    "ORDER BY created_at DESC LIMIT 1", (node_id, kind)).fetchone()
        self.assertIsNotNone(row, f"no {kind} message went out")
        return dict(row)["external_ref"].split(":", 1)

    def test_an_approval_of_an_answer_that_changed_is_refused(self):
        task = self.task("slack")
        node = self.node(task, "Should we bill usage spikes?", "n")
        canvas.settle_node(self.store, {"task_id": task, "node_id": node["node_id"], "answer": "Waive spikes"})
        channel, ts = self.thread(node["node_id"])
        canvas.settle_node(self.store, {"task_id": task, "node_id": node["node_id"], "answer": "Charge every spike"})
        reply = self.delivery.receive(channel, ts, "UPRI", "approve", event_id="stale-1")
        self.assertIn("Not recorded", reply)
        self.assertIn("the answer changed since this message", reply)
        view = canvas.node_view(self.store, node["node_id"])
        self.assertFalse(view["authorized"])
        self.assertIn("refused", finish(self.store, task))
        # The current state went out; approving that one stands.
        channel, ts = self.thread(node["node_id"])
        self.assertIn("Signed off by Priya Natarajan", self.delivery.receive(channel, ts, "UPRI", "approve", event_id="fresh-1"))
        self.assertTrue(canvas.node_view(self.store, node["node_id"])["authorized"])

    def test_a_viewer_and_a_stranger_cannot_approve_from_slack(self):
        with self.graph.transaction():
            self.graph.add_person("Read Only", email="readonly@example.test", slack_id="UVIEW", role="viewer")
        task = self.task("viewer")
        node = self.node(task, "Should we bill usage spikes?", "n")
        canvas.settle_node(self.store, {"task_id": task, "node_id": node["node_id"], "answer": "Waive spikes"})
        channel, ts = self.thread(node["node_id"])
        self.assertIn("Not permitted", self.delivery.receive(channel, ts, "UVIEW", "approve", event_id="v1"))
        self.assertIn("could not verify you as an active member", self.delivery.receive(channel, ts, "UNKNOWN", "approve", event_id="v2"))
        self.assertFalse(canvas.node_view(self.store, node["node_id"])["authorized"])
        self.assertIn("refused", finish(self.store, task))

    def test_a_conversational_reply_is_not_an_answer(self):
        task = self.task("chat")
        node = self.node(task, "Should we bill usage spikes?", "n")
        channel, ts = self.thread(node["node_id"], kind="ask")
        reply = self.delivery.receive(channel, ts, "UPRI", "I'll look at this tomorrow", event_id="c1")
        self.assertIn("Not recorded", reply)
        self.assertIn("answer:", reply)
        self.assertEqual(self.store.get_decision(node["node_id"])["status"], "pending")
        stated = self.delivery.receive(channel, ts, "UPRI", "answer: bill every spike because the contract says so",
                                       event_id="c2")
        self.assertIn("Recorded as Priya Natarajan's answer", stated)
        row = self.store.get_decision(node["node_id"])
        self.assertEqual(row["answer"], "bill every spike")
        self.assertEqual(row["rationale"], "the contract says so")


class WhatARuleStillAuthorizes(RuleCase):
    """Blockers 5 and 6: a rule that ended, expired or does not fit."""

    def covered_task(self, **rule):
        source = self.signed_answer()
        self.rule(source, **rule)
        task = self.task("covered")
        node = self.node(task, ref="c1")
        self.assertEqual(node["signoff"], "rule")
        return source, task, node

    def test_ending_a_rule_puts_outstanding_work_back_in_front_of_a_person(self):
        source, task, node = self.covered_task()
        ended = self.rule(source, end=True)
        self.assertEqual(ended["invalidated"], [node["node_id"]])
        view = canvas.node_view(self.store, node["node_id"])
        self.assertEqual(view["signoff"], "required")
        self.assertFalse(view["authorized"])
        self.assertTrue(view["needs_review"])
        self.assertIn("was ended", view["review_reason"])
        self.assertIn("refused", finish(self.store, task))

    def test_an_expired_rule_stops_authorizing_outstanding_work(self):
        later = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        source, task, node = self.covered_task(expires=later)
        # Three days later: the sweep before any read of authorization.
        future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        self.assertEqual(self.graph.expire_rules(now=future), [source])
        view = canvas.node_view(self.store, node["node_id"])
        self.assertFalse(view["authorized"])
        self.assertEqual(view["signoff"], "required")
        self.assertIn("expired", view["review_reason"])
        self.assertIn("refused", finish(self.store, task))
        # A task that already finished keeps its history.
        self.assertEqual(self.store.get_decision(source)["reusable"], 0)

    def test_a_denied_or_unstated_condition_does_not_authorize(self):
        source = self.signed_answer()
        self.rule(source, conditions="enterprise plan")
        denied = self.node(self.task("negative"), context="Globex is NOT on the enterprise plan; it is on the starter plan",
                           ref="n1")
        self.assertEqual(denied["signoff"], "required")
        self.assertFalse(denied["authorized"])
        self.assertIn("denies", denied["evidence"])
        self.assertIn("refused", finish(self.store, denied["task_id"]))
        # A fact the agent states, not a phrase it happens to carry.
        self.rule(source, conditions="plan=enterprise")
        unstated = self.node(self.task("unstated"), context="Globex is on the enterprise plan", ref="u1")
        self.assertEqual(unstated["signoff"], "required")
        self.assertIn("did not state", unstated["evidence"])
        stated = canvas.add_node(self.store, CFG, {"task_id": self.task("stated"), "question": QUESTION,
                                                   "context": "Globex", "paths": "billing/rates.py",
                                                   "client_ref": "s1", "facts": "plan=enterprise"})
        self.assertEqual(stated["signoff"], "rule")
        self.assertTrue(stated["authorized"])
        contradicted = canvas.add_node(self.store, CFG, {"task_id": self.task("contra"), "question": QUESTION,
                                                         "context": "Initech", "paths": "billing/rates.py",
                                                         "client_ref": "x1", "facts": "plan=starter"})
        self.assertEqual(contradicted["signoff"], "required")
        self.assertIn("plan=starter", contradicted["evidence"])

    def test_a_rule_applies_in_its_own_scope_unless_its_owner_said_anywhere(self):
        # The signed answer is about one account; the rule made of it
        # covers that account until its owner says it covers any.
        first = self.task("globex")
        source_node = canvas.add_node(self.store, CFG, {"task_id": first, "question": QUESTION,
                                                        "context": "The account Globex is on the enterprise plan",
                                                        "paths": "billing/rates.py", "client_ref": "g1"})
        row = self.store.get_decision(source_node["node_id"])
        self.store.answer(source_node["node_id"], {"answer": "Round half up to whole cents", "rationale": "invoice totals",
                                                   "signed_by": "Priya Natarajan", "expected_updated_at": row["updated_at"]})
        canvas.finish_task(self.store, {"task_id": first})
        source = source_node["node_id"]
        elsewhere = {"question": QUESTION, "context": "The account Initech is on the starter plan",
                     "paths": "billing/rates.py"}
        self.rule(source)
        other = canvas.add_node(self.store, CFG, {"task_id": self.task("other"), "client_ref": "o1", **elsewhere})
        self.assertEqual(other["signoff"], "required")
        self.assertIn("own scope only", other["evidence"])
        self.rule(source, scope="any")
        anywhere = canvas.add_node(self.store, CFG, {"task_id": self.task("any"), "client_ref": "a1", **elsewhere})
        self.assertEqual(anywhere["signoff"], "rule")
        self.assertTrue(anywhere["authorized"])


class WhatAWebhookProves(GitHubCase):
    """Blocker 8: one object is not a complete poll."""

    def test_a_webhook_never_advances_the_polling_watermark(self):
        import copy
        routes = self.api.routes
        newer = copy.deepcopy(routes[f"/repos/{GH_REPO}/pulls/42"])
        newer.update(number=43, updated_at="2026-06-11T12:00:00Z", merge_commit_sha="f" * 40)
        routes[f"/repos/{GH_REPO}/pulls/43"] = newer
        for suffix in ("files", "reviews"):
            routes[f"/repos/{GH_REPO}/pulls/43/{suffix}"] = copy.deepcopy(routes[f"/repos/{GH_REPO}/pulls/42/{suffix}"])
        routes[f"/repos/{GH_REPO}/pulls"].insert(0, newer)
        sync_pull(self.graph, self.api, GH_REPO, 43)
        state = sync_state(self.graph, GH_REPO)
        self.assertEqual(state["cursor"], "")
        self.assertEqual(state["stats"]["webhook_number"], 43)
        self.assertEqual(state["last_success_at"], "")
        polled = sync_repo(self.graph, self.api, GH_REPO)
        numbers = [r[0] for r in self.graph.db.execute("SELECT number FROM gh_pulls ORDER BY number")]
        self.assertEqual(numbers, [42, 43])
        self.assertEqual(polled["pulls"], 2)
        self.assertEqual(sync_state(self.graph, GH_REPO)["cursor"], "2026-06-11T12:00:00Z")


class WhatAFailedReplyMustNotLose(PortalCase):
    """Blocker 7: an inbound event that could not be applied is kept."""

    def test_a_failed_reply_is_recorded_and_applied_on_retry(self):
        task = self.task("retry")
        node = self.node(task, "Should we bill usage spikes?", "n")
        self.delivery.deliver_now()
        note = dict(self.graph.db.execute("SELECT * FROM notifications WHERE decision_id=? AND kind='ask' AND state='sent'",
                                          (node["node_id"],)).fetchone())
        channel, ts = note["external_ref"].split(":", 1)
        text = "answer: waive the spikes because they are load tests"
        with patch.object(self.store, "answer", side_effect=RuntimeError("simulated transient failure")):
            with self.assertRaises(RuntimeError):
                self.delivery.receive(channel, ts, "UPRI", text, event_id="retry-1")
        failed = self.delivery.inbound_failed()
        self.assertEqual([f["id"] for f in failed], ["retry-1"])
        self.assertIn("simulated transient failure", failed[0]["error"])
        self.assertEqual(self.store.get_decision(node["node_id"])["status"], "pending")
        # Slack retries the same delivery: it is applied, once.
        reply = self.delivery.receive(channel, ts, "UPRI", text, event_id="retry-1")
        self.assertIn("Recorded as Priya Natarajan's answer", reply)
        row = self.store.get_decision(node["node_id"])
        self.assertEqual(row["answer"], "waive the spikes")
        self.assertEqual(self.delivery.inbound_failed(), [])
        # A duplicate of the applied event changes nothing.
        self.assertEqual(self.delivery.receive(channel, ts, "UPRI", text, event_id="retry-1"), "")
        self.assertEqual(len(self.store.get_decision(node["node_id"])["events"]),
                         len(row["events"]))
        self.assertEqual(finish(self.store, task), "completed")

    def test_an_operator_can_retry_a_failed_event_from_deliveries(self):
        task = self.task("operator")
        node = self.node(task, "Should we bill usage spikes?", "n")
        self.delivery.deliver_now()
        note = dict(self.graph.db.execute("SELECT * FROM notifications WHERE decision_id=? AND kind='ask' AND state='sent'",
                                          (node["node_id"],)).fetchone())
        channel, ts = note["external_ref"].split(":", 1)
        with patch.object(self.store, "answer", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.delivery.receive(channel, ts, "UPRI", "answer: bill them because policy", event_id="op-1")
        applied = self.delivery.retry_inbound("op-1")
        self.assertEqual(applied["state"], "applied")
        self.assertEqual(self.store.get_decision(node["node_id"])["answer"], "bill them")
        with self.assertRaises(Invalid):
            self.delivery.retry_inbound("op-1")


class ThePermittedPathStillWorks(PortalCase):
    """The boundaries above cost the people who own decisions nothing."""

    def test_the_owner_answers_in_slack_and_the_agent_resumes(self):
        task = self.task("loop")
        node = self.node(task, "Which rate applies above the included quota?", "n")
        seen = canvas.get_tree(self.store, task)["observed_at"]
        self.delivery.deliver_now()
        note = dict(self.graph.db.execute("SELECT * FROM notifications WHERE decision_id=? AND kind='ask' AND state='sent'",
                                          (node["node_id"],)).fetchone())
        channel, ts = note["external_ref"].split(":", 1)
        reply = self.delivery.receive(channel, ts, "UPRI",
                                      "answer: the enterprise rate card because the contract references it",
                                      event_id="ok-1")
        self.assertIn("Recorded as Priya Natarajan's answer", reply)
        waited = canvas.wait(self.store, {"task_id": task, "timeout": "5", "since": seen})
        self.assertFalse(waited["timed_out"])
        self.assertEqual(waited["changed"][0]["to"], "answered")
        self.assertTrue(waited["changed"][0]["authorized"])
        self.assertEqual(finish(self.store, task), "completed")
        row = self.store.get_decision(node["node_id"])
        self.assertEqual((row["actor_name"], row["actor_basis"]), ("Priya Natarajan", "owner"))

    def test_the_coordinator_routes_and_the_owner_decides(self):
        self.store.update_settings({"coordinator": self.wes})
        task = self.task("routing")
        node = self.node(task, "Which rate applies above the included quota?", "n")
        wes = Actor.person(self.graph.get_person(self.wes))
        # The coordinator may hand it on, and may not decide it.
        handed = self.store.refer(node["node_id"], {"person": self.dana}, actor=wes)
        self.assertEqual(handed["owner_name"], "Dana Ortiz")
        with self.assertRaises(Refused):
            self.store.answer(node["node_id"], {"answer": "mine now", "expected_updated_at": handed["updated_at"]},
                              actor=wes)
        dana = Actor.person(self.graph.get_person(self.dana))
        row = self.store.answer(node["node_id"], {"answer": "the enterprise rate card", "rationale": "contract",
                                                  "expected_updated_at": handed["updated_at"]}, actor=dana)
        self.assertEqual((row["signed_by"], row["actor_basis"]), ("Dana Ortiz", "owner"))


class WhatAHandOnGrants(PortalCase):
    """A hand-on teaches a route, and the route becomes standing to
    approve once the person answers, so it must be the decision's own
    topic and nothing its words happen to mention. Measured live on main
    6d90c3d: a docs question asking whether the release notes should say
    "security fix" was handed on, and the person it went to could then
    approve security decisions."""

    def as_person(self, person_id):
        return Actor.person(self.graph.get_person(person_id))

    def handed(self, question, ref, category="", **extra):
        node = self.node(self.task(ref), question, ref, **({"category": category} if category else {}))
        return node, self.store.refer(node["node_id"], {"person": self.dana, **extra}, actor=self.as_person(self.priya))

    def may_answer(self, person_id, node_id):
        from bridge.authz import basis_for
        return basis_for(self.graph, self.as_person(person_id), self.store.get_decision(node_id), "answer")[0]

    def answer_as(self, person_id, node_id, text):
        row = self.store.get_decision(node_id)
        return self.store.answer(node_id, {"answer": text, "rationale": "r", "expected_updated_at": row["updated_at"]},
                                 actor=self.as_person(person_id))

    def test_a_docs_hand_on_grants_docs_and_never_security(self):
        security = self.node(self.task("s"), "May certificate verification be skipped after a validation error?", "s",
                             category="security")
        self.assertEqual(self.may_answer(self.dana, security["node_id"]), "")
        docs, handed = self.handed("Should the release notes call the redirect change a security fix, or "
                                   "compatibility hardening?", "d", category="docs")
        self.assertEqual(handed["learned"], [f"docs decisions in {REPO}"])
        self.answer_as(self.dana, docs["node_id"], "Credential-leak prevention, with a compatibility note")
        self.assertEqual(self.may_answer(self.dana, security["node_id"]), "")
        later = self.node(self.task("d2"), "What should the changelog say about the new header default?", "d2",
                          category="docs")
        self.assertIn(self.may_answer(self.dana, later["node_id"]), ("owner", "authority"))

    def test_a_question_on_two_topics_teaches_nothing_unless_the_person_picks(self):
        _, handed = self.handed("Should the security fix also change the pricing tiers?", "a")
        self.assertEqual(handed["learned"], [])
        self.assertIn("security", handed["notice"])
        self.assertIn("pricing", handed["notice"])
        self.assertIn("name the scope when you hand on", handed["notice"])
        _, picked = self.handed("Should the vulnerability patch reprice the enterprise tiers?", "b",
                                scope_kind="category", scope="pricing")
        self.assertEqual(picked["learned"], [f"pricing decisions in {REPO}"])
        _, only = self.handed("Which rate applies above the included quota?", "c", scope_kind="none")
        self.assertEqual(only["learned"], [])
        self.assertIn("for this question only", only["notice"])
        with self.assertRaises(Invalid):
            self.handed("Which rounding applies to the invoice?", "e", scope_kind="category", scope="everything")
        # Nothing to go on at all no longer teaches "every decision in the repository".
        node = canvas.add_node(self.store, CFG, {"task_id": self.task("f"), "question": "Which name should it keep?",
                                                 "client_ref": "f"})
        row = self.store.get_decision(node["node_id"])
        if row["status"] == "pending":
            self.assertEqual(self.store.refer(node["node_id"], {"person": self.dana}, actor=None)["learned"], [])

    def test_the_route_picked_is_accepted_by_answering_the_question_it_came_with(self):
        node, handed = self.handed("Which rate applies above the included quota?", "p", scope_kind="category",
                                   scope="docs")
        self.assertEqual(handed["learned"], [f"docs decisions in {REPO}"])
        learned = [a for a in self.graph.authority_rows(REPO) if a["person_id"] == self.dana]
        self.assertEqual([(a["scope"], a["accepted"]) for a in learned], [("docs", 0)])
        self.answer_as(self.dana, node["node_id"], "The enterprise rate card")
        learned = [a for a in self.graph.authority_rows(REPO) if a["person_id"] == self.dana]
        self.assertEqual([(a["scope"], a["accepted"]) for a in learned], [("docs", 1)])

    def test_a_word_in_the_question_or_context_grants_no_standing(self):
        with self.graph.transaction():
            self.graph.add_authority("category", "docs", "decides", person_id=self.dana, repo=REPO,
                                     source="config", asserted_by="test", accepted=True)
        node = self.node(self.task("x"), "May certificate verification be skipped when the docs example fails?", "x",
                         context="The docs say it never is.", category="security")
        self.assertEqual(self.may_answer(self.dana, node["node_id"]), "")
        docs = self.node(self.task("y"), "Should the docs recommend pinning certificates?", "y")
        self.assertIn(self.may_answer(self.dana, docs["node_id"]), ("owner", "authority"))

    def test_in_slack_the_person_names_the_scope_or_this_one_only(self):
        first = self.node(self.task("sl1"), "Should the security fix also change the pricing tiers?", "sl1")
        second = self.node(self.task("sl2"), "Should the refund window change for annual plans?", "sl2")
        self.delivery.deliver_now()

        def thread(node_id):
            note = dict(self.graph.db.execute("SELECT * FROM notifications WHERE decision_id=? AND kind='ask' "
                                              "AND state='sent'", (node_id,)).fetchone())
            return note["external_ref"].split(":", 1)
        channel, ts = thread(first["node_id"])
        reply = self.delivery.receive(channel, ts, "UPRI", "not me <@UDAN> for pricing", event_id="h-1")
        self.assertIn(f"Raven will route pricing decisions in {REPO} to them first", reply)
        channel, ts = thread(second["node_id"])
        reply = self.delivery.receive(channel, ts, "UPRI", "not me Dana Ortiz, just this one", event_id="h-2")
        self.assertIn("Handed to Dana Ortiz for this question only", reply)
        self.assertEqual(sorted(a["scope"] for a in self.graph.authority_rows(REPO) if a["person_id"] == self.dana),
                         ["pricing"])
        # A name typed out with the scope after it and no comma. Measured
        # live on eb9d22d: "not me @Theo Release just this one" read the
        # whole tail as the person's name.
        third = self.node(self.task("sl3"), "Should the invoice footer name the tax office?", "sl3")
        self.delivery.deliver_now()
        channel, ts = thread(third["node_id"])
        reply = self.delivery.receive(channel, ts, "UPRI", "not me @Dana Ortiz just this one", event_id="h-3")
        self.assertIn("Handed to Dana Ortiz for this question only", reply)
        self.assertEqual(self.store.get_decision(third["node_id"])["owner_name"], "Dana Ortiz")

    def test_the_card_offers_the_scopes_and_says_why_it_learns_none(self):
        node = self.node(self.task("w"), "Should the security fix also change the pricing tiers?", "w")
        plan = self.store.handon_scopes(self.store.get_decision(node["node_id"]), self.as_person(self.priya))
        self.assertEqual(plan["default"]["scope_kind"], "none")
        self.assertEqual({o["label"] for o in plan["options"]},
                         {"pricing decisions", "security decisions", "decisions on billing/*", "this question only"})
        self.assertIn("one hand-on does not say which", plan["why_none"])

class WhatASignedPredictionSays(PortalCase):
    """Where an answer came from is provenance; a signature is what
    authorizes it, and every surface says so. Measured live on main
    6d90c3d: the owner signed a prediction, the node was authorized, and
    the agent was still told it was "a prediction, not a decision"."""

    def predicted(self, ref, status="proposed", signers=()):
        node = self.node(self.task(ref), "Which extra headers join the default redirect removal list?", ref)
        with self.graph.transaction():
            self.graph.update_decision(node["node_id"], status=status, kind="prediction", source="memory",
                                       answer="Only X-Api-Key", signoff="required")
            if signers:
                self.graph.db.execute("UPDATE decisions SET required_signers=? WHERE id=?",
                                      (json.dumps(list(signers)), node["node_id"]))
        return node["node_id"]

    def seen(self, node_id):
        run = self.store.get_decision(node_id)["run_id"]
        return next(n for n in canvas._flatten(canvas.get_tree(self.store, run)["nodes"]) if n["node_id"] == node_id)

    def sign(self, node_id, person_id, **data):
        row = self.store.get_decision(node_id)
        return canvas.sign_off(self.store, node_id, {"expected_updated_at": row["updated_at"], **data},
                               actor=Actor.person(self.graph.get_person(person_id)))

    def test_a_signed_prediction_tells_the_agent_to_act_on_it(self):
        nid = self.predicted("p")
        before = self.seen(nid)
        self.assertEqual((before["status"], before["authorized"]), ("predicted", False))
        self.assertIn("a prediction, not a decision", before["next"])
        self.sign(nid, self.priya)
        after = self.seen(nid)
        self.assertEqual((after["status"], after["authorized"], after["blocking"]), ("resolved", True, False))
        self.assertEqual(self.store.get_decision(nid)["kind"], "prediction")
        self.assertNotIn("not a decision", after["next"])
        self.assertIn("signed by Priya Natarajan: act on this answer (it began as a prediction", after["next"])
        self.assertFalse(self.store.get_decision(nid)["approval_pending"])
        self.assertEqual(finish(self.store, self.store.get_decision(nid)["run_id"]), "completed")

    def test_a_signed_default_says_where_it_began(self):
        nid = self.predicted("a", status="assumed")
        self.sign(nid, self.priya)
        self.assertIn("it began as a default Raven assumed", self.seen(nid)["next"])

    def test_every_required_signer_still_counts(self):
        nid = self.predicted("c", signers=("Priya Natarajan", "Wes Chen"))
        self.sign(nid, self.priya)
        halfway = self.seen(nid)
        self.assertFalse(halfway["authorized"])
        self.assertIn("still waiting on Wes Chen", halfway["next"])
        self.sign(nid, self.wes)
        done = self.seen(nid)
        self.assertTrue(done["authorized"])
        self.assertIn("act on this answer", done["next"])

    def test_a_correction_after_the_signature_is_the_answer(self):
        nid = self.predicted("k")
        self.sign(nid, self.priya)
        self.sign(nid, self.priya, answer="X-Api-Key and X-Auth-Token", rationale="both carry credentials")
        after = self.seen(nid)
        self.assertEqual(after["status"], "answered")
        self.assertIn("act on the answer from Priya Natarajan", after["next"])

class WhoMustApprove(OfflineCase):
    """A reviewer the authority map says must approve a path signs every
    decision about that path, however the agent names it. Measured live
    on main 35a5f84 and again on 0fd61b5: a node that inherited the file
    from its task or parent, or listed it second, dropped the reviewer,
    and the task finished on the owner's answer alone."""

    REPO = "evaluation/approval"
    QUESTION = "Should the new optional setting preserve current behavior by default?"

    def setUp(self):
        super().setUp()
        from pathlib import Path
        from bridge.store import Store
        self.store = Store(Path(self.temp.name) / "approve.db")
        self.graph = self.store.graph
        with self.graph.transaction():
            self.owner = self.graph.add_person("A Runtime Owner", email="runtime@scope.invalid")
            self.reviewer = self.graph.add_person("Z Required Reviewer", email="reviewer@scope.invalid")
            self.graph.add_authority("repo", "", "decides", person_id=self.owner, repo=self.REPO, source="config",
                                     asserted_by="operator", accepted=True)
            self.graph.add_authority("path", "protected/*", "approves", person_id=self.reviewer, repo=self.REPO,
                                     source="config", asserted_by="operator", accepted=True)

    def act(self, person_id):
        return Actor.person(self.graph.get_person(person_id))

    def node(self, shape):
        task = canvas.start_task(self.store, CFG, {"title": "Set compatibility policy", "repo": self.REPO,
                                                   "paths": "protected/retry.py", "client_key": shape})["task_id"]
        args = {"task_id": task, "question": self.QUESTION, "client_ref": "child", "category": "compat"}
        if shape == "explicit":
            args["paths"] = "protected/retry.py"
        if shape == "second":
            args["paths"] = "public/docs.rst, protected/retry.py"
        if shape == "parent":
            parent = canvas.add_node(self.store, CFG, {"task_id": task, "paths": "protected/retry.py", "client_ref": "root",
                                                       "question": "Which API shape should be chosen for this option?",
                                                       "category": "compat"})
            self.store.answer(parent["node_id"], {"answer": "An optional numeric parameter.", "rationale": "defaults"},
                              actor=self.act(self.owner))
            row = self.store.get_decision(parent["node_id"])
            canvas.sign_off(self.store, parent["node_id"], {"expected_updated_at": row["updated_at"]},
                            actor=self.act(self.reviewer))
            args["parent_id"] = parent["node_id"]
        return task, canvas.add_node(self.store, CFG, args)

    def test_every_shape_needs_the_reviewer(self):
        for shape in ("explicit", "task", "parent", "second"):
            with self.subTest(shape=shape):
                task, node = self.node(shape)
                self.assertEqual(node["required_signers"], ["Z Required Reviewer"])
                self.store.answer(node["node_id"], {"answer": "Preserve all existing defaults.",
                                                    "rationale": "New behavior is opt-in."}, actor=self.act(self.owner))
                refused = finish(self.store, task)
                self.assertTrue(refused.startswith("refused"), refused)
                self.assertIn("sign-off wanted from Z Required Reviewer", refused)
                self.assertNotIn("sign-off wanted from A Runtime Owner", refused)
                seen = next(n for n in canvas._flatten(canvas.get_tree(self.store, task)["nodes"])
                            if n["node_id"] == node["node_id"])
                self.assertIn("still waiting on Z Required Reviewer", seen["next"])
                summary = canvas.get_tree(self.store, task)["next"]
                self.assertIn("still waiting on Z Required Reviewer", summary)
                self.assertNotIn("A Runtime Owner", summary)
                row = self.store.get_decision(node["node_id"])
                canvas.sign_off(self.store, node["node_id"], {"expected_updated_at": row["updated_at"]},
                                actor=self.act(self.reviewer))
                self.assertEqual(finish(self.store, task), "completed")

    def test_an_inherited_path_reaches_who_decides_it_without_a_tree(self):
        with self.graph.transaction():
            lee = self.graph.add_person("Lee Ledger", email="lee@scope.invalid")
            self.graph.add_authority("path", "ledger/*", "decides", person_id=lee, repo="demo/books", source="config",
                                     asserted_by="operator", accepted=True)
        task = canvas.start_task(self.store, CFG, {"title": "Close the books early", "repo": "demo/books",
                                                   "paths": "ledger/close.py"})["task_id"]
        node = canvas.add_node(self.store, CFG, {"task_id": task,
                                                 "question": "May the month-end close run before every import lands?"})
        self.assertEqual(node["owner"], "Lee Ledger")
        self.assertIn("inherited", node["owner_evidence"])

    def test_standing_reads_every_path_the_decision_is_about(self):
        from bridge.authz import _scope_holders
        decision = {"repo": self.REPO, "path": "public/docs.rst", "question": self.QUESTION, "category": "compat",
                    "scope_paths": json.dumps(["public/docs.rst", "protected/retry.py"])}
        self.assertIn(self.reviewer, [p["id"] for p in _scope_holders(self.graph, decision)])
        decision["scope_paths"] = ""
        self.assertNotIn(self.reviewer, [p["id"] for p in _scope_holders(self.graph, decision)])

    def test_not_knowing_who_approves_is_not_nobody(self):
        from unittest import mock
        with mock.patch.object(canvas, "_approvers", side_effect=RuntimeError("authority map unreadable")):
            task, node = self.node("explicit")
        row = self.store.get_decision(node["node_id"])
        self.assertTrue(row["needs_review"])
        self.assertIn("could not work out who must approve", row["review_reason"])
        self.assertIn("could not work out who must approve", finish(self.store, task))

class WhatAFollowUpHolds(OfflineCase):
    """A follow-up a person marks required holds the task until the agent
    adopts it and it is answered; an optional one never holds it.
    Measured live on 35a5f84: an owner's "before release, which option
    handles redirects?" was left unadopted and the task finished."""

    REPO = "evaluation/followup"
    QUESTION = "Before release, which option will handle redirects: apply jitter there too, or exclude redirects?"

    def setUp(self):
        super().setUp()
        from pathlib import Path
        from bridge.store import Store
        self.store = Store(Path(self.temp.name) / "followup.db")
        graph = self.store.graph
        with graph.transaction():
            person = graph.add_person("Runtime Owner", email="runtime@followup.invalid")
            graph.add_authority("repo", "", "decides", person_id=person, repo=self.REPO, source="config",
                                asserted_by="operator", accepted=True)
        self.actor = Actor.person(graph.get_person(person))
        self.task = canvas.start_task(self.store, CFG, {"title": "Choose default retry policy", "repo": self.REPO,
                                                        "paths": "src/retry.py"})["task_id"]
        node = canvas.add_node(self.store, CFG, {"task_id": self.task, "paths": "src/retry.py",
                                                 "question": "Must the optional policy preserve defaults?"})
        self.store.answer(node["node_id"], {"answer": "Yes, preserve defaults.", "rationale": "Compatibility."},
                          actor=self.actor)
        self.parent = node["node_id"]

    def test_an_optional_follow_up_never_holds_the_task(self):
        added = canvas.add_followups(self.store, CFG, self.parent, {"questions": [self.QUESTION]},
                                     actor=self.actor)["nodes"][0]
        self.assertFalse(added["followup_required"])
        self.assertFalse(added["blocking"])
        self.assertEqual(finish(self.store, self.task), "completed")

    def test_a_required_follow_up_holds_the_task_until_it_is_answered(self):
        added = canvas.add_followups(self.store, CFG, self.parent, {"questions": [self.QUESTION], "required": True},
                                     actor=self.actor)["nodes"][0]
        self.assertTrue(added["followup_required"])
        self.assertTrue(added["blocking"])
        self.assertIn("marked it required", added["next"])
        self.assertIn("required: the task cannot finish", canvas.get_tree(self.store, self.task)["next"])
        refused = finish(self.store, self.task)
        self.assertIn(f"bridge_add_node(adopt={added['node_id']})", refused)
        adopted = canvas.add_node(self.store, CFG, {"task_id": self.task, "question": self.QUESTION,
                                                    "adopt": added["node_id"]})
        self.assertTrue(finish(self.store, self.task).startswith("refused"))
        self.store.answer(adopted["node_id"], {"answer": "Exclude redirects.", "rationale": "A separate change."},
                          actor=self.actor)
        self.assertEqual(finish(self.store, self.task), "completed")

    def test_too_many_or_too_long_follow_ups_are_refused_not_dropped(self):
        with self.assertRaises(Invalid) as many:
            canvas.add_followups(self.store, CFG, self.parent, {"questions": [f"Question {i}?" for i in range(11)]},
                                 actor=self.actor)
        self.assertIn("At most 10", str(many.exception))
        with self.assertRaises(Invalid) as long:
            canvas.add_followups(self.store, CFG, self.parent, {"questions": ["Why " * 600 + "?"]}, actor=self.actor)
        self.assertIn("at most 2000 characters", str(long.exception))

    def test_marking_an_optional_follow_up_required_holds_it(self):
        canvas.add_followups(self.store, CFG, self.parent, {"questions": [self.QUESTION]}, actor=self.actor)
        again = canvas.add_followups(self.store, CFG, self.parent, {"questions": [self.QUESTION], "required": "true"},
                                     actor=self.actor)["nodes"][0]
        self.assertTrue(again["followup_required"])
        self.assertTrue(finish(self.store, self.task).startswith("refused"))

QUESTION = "Round the overage charge to whole cents?"


if __name__ == "__main__":
    unittest.main()
