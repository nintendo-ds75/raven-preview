"""Adversarial boundaries for the single-workspace Raven deployment.

Repository scopes constrain decision authority, task links constrain their
bearer, and separate databases are separate workspaces. Workspace accounts
still intentionally share read access; these tests do not assert a tenant
ACL that Raven does not implement.
"""

import time
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import briefing, canvas
from bridge.auth import Auth
from bridge.authz import Actor, Refused, basis_for
from bridge.config import Config
from bridge.delivery import handle_slack_event
from bridge.store import Invalid, Store
from test_auth import BOOTSTRAP, SharedServer
from test_slack_discovery import DirectorySlack, member

CFG = Config(model_api="none")


class DecisionCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "isolation.db")
        self.graph = self.store.graph
        self.alice = self.store.add_person({"name": "Alice Billing", "email": "alice@example.test"})
        self.bob = self.store.add_person({"name": "Bob Security", "email": "bob@example.test"})

    def decision(self, person, repo="acme/service", title="Policy"):
        run = self.store.add_run({"title": title, "repo": repo})
        with self.graph.transaction():
            did = self.graph.add_decision(run["id"], title, "", "pending", repo=repo,
                                          owner=person["name"], path="src/policy.py")
        return self.store.get_decision(did)

    def answer(self, decision, person, **extra):
        current = self.store.get_decision(decision["id"])
        return self.store.answer(decision["id"], {"answer": "Keep the safe policy",
            "expected_updated_at": current["updated_at"], **extra}, actor=Actor.person(person, kind="session"))


class SupersessionAuthorizationTests(DecisionCase):
    def test_an_owner_cannot_retire_another_owners_decision(self):
        victim = self.decision(self.bob, title="Require approval for key rotation")
        self.answer(victim, self.bob)
        attack = self.decision(self.alice, title="Adjust billing rounding")
        before = self.store.get_decision(victim["id"])
        with self.assertRaises(Refused):
            self.answer(attack, self.alice, supersedes=victim["id"])
        self.assertEqual(self.store.get_decision(victim["id"]), before)
        self.assertEqual(self.store.get_decision(attack["id"])["status"], "pending")

    def test_supersession_does_not_cross_repositories_even_for_the_same_owner(self):
        victim = self.decision(self.alice, repo="other/service")
        attack = self.decision(self.alice, repo="acme/service")
        with self.assertRaises(Invalid):
            self.answer(attack, self.alice, supersedes=victim["id"])
        self.assertFalse(self.store.get_decision(victim["id"])["superseded_by"])

    def test_supersession_cannot_form_a_self_reference(self):
        decision = self.decision(self.alice)
        with self.assertRaises(Invalid):
            self.answer(decision, self.alice, supersedes=decision["id"])
        self.assertEqual(self.store.get_decision(decision["id"])["status"], "pending")

    def test_authorized_supersession_is_audited_on_the_retired_task(self):
        previous = self.decision(self.alice, title="Previous policy")
        self.answer(previous, self.alice)
        replacement = self.decision(self.alice, title="Replacement policy")
        self.answer(replacement, self.alice, supersedes=previous["id"])
        retired = self.store.get_decision(previous["id"])
        self.assertEqual(retired["superseded_by"], replacement["id"])
        event = self.graph.db.execute("SELECT run_id FROM events WHERE decision_id=? AND kind='superseded'",
                                      (previous["id"],)).fetchone()
        self.assertEqual(event["run_id"], previous["run_id"])


class AuthorityFreshnessTests(DecisionCase):
    def test_authority_expires_without_needing_an_unrelated_database_write(self):
        decision = self.decision(self.bob)
        self.store.add_authority({"person": self.alice["id"], "scope_kind": "repo", "role": "decides",
            "repo": "acme/service", "effective_to": "2030-01-02T00:00:00+00:00"})
        actor = Actor.person(self.alice, kind="session")
        with patch("bridge.graph.now_iso", return_value="2030-01-01T00:00:00+00:00"):
            self.assertEqual(basis_for(self.graph, actor, decision, "answer")[0], "authority")
        with patch("bridge.graph.now_iso", return_value="2030-01-03T00:00:00+00:00"):
            self.assertEqual(basis_for(self.graph, actor, decision, "answer")[0], "")

    def test_team_removal_on_another_connection_revokes_scope_authority(self):
        decision = self.decision(self.bob)
        team = self.store.add_team({"name": "Approvers", "members": [self.alice["id"]]})
        self.store.add_authority({"team": team["id"], "scope_kind": "repo", "role": "decides",
                                  "repo": "acme/service"})
        actor = Actor.person(self.alice, kind="session")
        self.assertEqual(basis_for(self.graph, actor, decision, "answer")[0], "authority")
        other = Store(self.store.path)
        self.addCleanup(other.graph.close)
        with other.graph.transaction():
            other.graph.set_team_members(team["id"], [], replace=True)
        self.assertEqual(basis_for(self.graph, actor, decision, "answer")[0], "")


class SlackWorkspaceIsolationTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "slack-isolation.db")
        self.graph = self.store.graph
        self.slack = DirectorySlack([member("UALICE", "Alice Billing", "alice@example.test")])
        self.delivery = self.store.connect_delivery(self.slack)
        self.delivery.sync_directory()
        self.owner = self.graph.find_person("UALICE")
        self.store.add_authority({"person": self.owner["id"], "scope_kind": "repo", "role": "decides",
                                  "repo": "acme/service"})
        task = canvas.start_task(self.store, CFG, {"title": "Update billing", "repo": "acme/service"})
        self.node = canvas.add_node(self.store, CFG, {"task_id": task["task_id"],
            "question": "Should invoices require approval before dispatch?", "paths": "billing/dispatch.py"})
        self.delivery.deliver_now()
        self.message = self.slack.messages[0]

    def event(self, team="TTEST", event_id="E1"):
        result = {"type": "event_callback", "event_id": event_id, "event": {
            "type": "message", "channel": self.message["channel"], "thread_ts": self.message["ts"],
            "user": "UALICE", "text": "answer: Require approval because dispatch must be reviewed"}}
        if team is not None:
            result["team_id"] = team
        return result

    def test_foreign_and_unscoped_events_never_enter_the_durable_queue(self):
        with patch.object(self.delivery.inbox, "start") as start:
            for team in ("TOTHER", None):
                result = self.delivery.inbox.enqueue(self.event(team, "E" + str(team)))
                self.assertFalse(result.get("queued"))
            start.assert_not_called()
        self.assertEqual(self.graph.db.execute("SELECT count(*) n FROM slack_ingress").fetchone()["n"], 0)
        self.assertFalse(self.store.get_decision(self.node["node_id"])["answer"])

    def test_foreign_or_unscoped_replay_cannot_apply_or_acknowledge(self):
        sent = len(self.slack.messages)
        for team in ("TOTHER", None):
            handle_slack_event(self.delivery, self.event(team, "replay-" + str(team)))
            self.assertFalse(self.store.get_decision(self.node["node_id"])["answer"])
        self.assertEqual(len(self.slack.messages), sent)
        self.assertEqual(self.graph.db.execute("SELECT count(*) n FROM webhook_receipts").fetchone()["n"], 0)

    def test_foreign_capture_is_rejected_before_creating_evidence(self):
        event = self.event("TOTHER", "foreign-record")
        event["event"].pop("thread_ts")
        event["event"].update({"ts": "1700000000.123456", "text": "record: All safeguards are waived"})
        self.assertEqual(handle_slack_event(self.delivery, event).get("ignored"), "workspace_mismatch")
        self.assertEqual(self.graph.db.execute("SELECT count(*) n FROM intents WHERE kind='slack'").fetchone()["n"], 0)
        self.assertEqual(self.graph.db.execute("SELECT count(*) n FROM webhook_receipts").fetchone()["n"], 0)

    def test_an_unknown_workspace_cannot_be_pinned_from_an_event(self):
        fresh = Store(Path(self.temp.name) / "not-connected.db")
        self.addCleanup(fresh.graph.close)
        self.assertEqual(fresh.delivery.inbox.enqueue(self.event()), {"ok": True, "ignored": "workspace_mismatch"})
        self.assertEqual(fresh.graph.get_setting("slack_team_id"), "")
        self.assertEqual(fresh.graph.db.execute("SELECT count(*) n FROM slack_ingress").fetchone()["n"], 0)

    def test_bound_workspace_event_still_records_the_verified_member(self):
        with patch.object(self.delivery.inbox, "start"):
            self.assertTrue(self.delivery.inbox.enqueue(self.event())["queued"])
        self.assertEqual(self.delivery.inbox.process(), 1)
        row = self.store.get_decision(self.node["node_id"])
        self.assertEqual(row["status"], "approved")
        self.assertEqual(row["actor_id"], self.owner["id"])


class CredentialIsolationTests(SharedServer):
    def test_demoting_a_person_to_viewer_makes_their_existing_agent_read_only(self):
        person = self.post("/api/people", {"name": "Agent owner"}, token=BOOTSTRAP)
        token = self.post("/api/tokens", {"person_id": person["id"]}, token=BOOTSTRAP)["token"]
        task = self.mcp("bridge_start_task", {"title": "Existing task", "repo": "acme/service"}, token)["result"]
        diff = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n"
        self.assertFalse(self.mcp("bridge_finish_task", {"task_id": task["task_id"], "diff": diff}, token)["isError"])
        with self.store.graph.transaction():
            self.store.graph.db.execute("UPDATE people SET role='viewer' WHERE id=?", (person["id"],))
        self.assertFalse(self.mcp("bridge_get_tree", {"task_id": task["task_id"]}, token)["isError"])
        self.assertFalse(self.mcp("bridge_export_proof", {"task_id": task["task_id"]}, token)["isError"])
        writes = [("/api/tasks/start", {"title": "New task", "repo": "acme/service"}),
                  ("/api/runs", {"title": "New run"}),
                  (f"/api/tasks/{task['task_id']}/nodes", {"question": "Mutate this task?"}),
                  (f"/api/tasks/{task['task_id']}/finish", {})]
        for path, data in writes:
            self.assertEqual(self.status_of("POST", path, data, token=token), 403, path)
        for name, arguments in [("bridge_start_task", {"title": "New", "repo": "acme/service"}),
                                ("bridge_add_node", {"task_id": task["task_id"], "question": "New?"}),
                                ("bridge_finish_task", {"task_id": task["task_id"]}),
                                ("bridge_import_record", {"repo": "acme/service", "kind": "note", "ref": "bad"})]:
            self.assertEqual(self.status_of("POST", "/mcp", {"jsonrpc": "2.0", "id": 1,
                "method": "tools/call", "params": {"name": name, "arguments": arguments}}, token=token), 403, name)
        self.assertEqual(self.store.graph.db.execute("SELECT count(*) n FROM runs").fetchone()["n"], 1)

    def test_expired_session_is_refused_even_with_a_valid_signature(self):
        person = self.post("/api/people", {"name": "Alice"}, token=BOOTSTRAP)
        with patch("bridge.auth.time.time", return_value=time.time() - 15 * 86400):
            cookie = self.auth.session_cookie(person["id"]).split(";", 1)[0]
        self.assertEqual(self.status_of("GET", "/api/state", cookie=cookie), 401)

    def test_malformed_non_ascii_credentials_fail_closed(self):
        self.assertIsNone(self.auth.identify({"Authorization": "Bearer brg_é"}))
        self.assertIsNone(self.auth.identify({"Cookie": "bridge_session=person.123.é"}))

    def test_task_link_cannot_be_used_as_a_general_bearer_or_mcp_credential(self):
        person = self.post("/api/people", {"name": "Alice", "email": "alice@example.test"}, token=BOOTSTRAP)
        run = self.store.add_run({"title": "Private review", "repo": "acme/service"})
        with self.store.graph.transaction():
            token = briefing.mint(self.store.graph, person["id"], run["id"])
        self.assertEqual(self.status_of("GET", "/api/state", token=token), 401)
        self.assertEqual(self.status_of("POST", "/mcp", {"jsonrpc": "2.0", "id": 1,
            "method": "tools/list"}, token=token), 401)

    def test_agent_cannot_smuggle_attribution_or_signature_through_mcp(self):
        person = self.post("/api/people", {"name": "Agent owner"}, token=BOOTSTRAP)
        token = self.post("/api/tokens", {"person_id": person["id"]}, token=BOOTSTRAP)["token"]
        task = self.mcp("bridge_start_task", {"title": "Task", "repo": "acme/service"}, token)["result"]
        result = self.mcp("bridge_add_node", {"task_id": task["task_id"], "question": "Use the safe default?",
            "signed_by": "Alice Admin", "signoff": "signed"}, token)
        self.assertTrue(result["isError"])
        self.assertIn("Unknown argument", result["result"])
        self.assertEqual(self.store.graph.db.execute("SELECT count(*) n FROM decisions").fetchone()["n"], 0)

    def test_revoked_agent_fails_http_and_mcp_immediately(self):
        person = self.post("/api/people", {"name": "Agent owner"}, token=BOOTSTRAP)
        issued = self.post("/api/tokens", {"person_id": person["id"]}, token=BOOTSTRAP)
        self.post(f"/api/tokens/{issued['id']}/revoke", {}, token=BOOTSTRAP)
        self.assertEqual(self.status_of("GET", "/api/state", token=issued["token"]), 401)
        self.assertEqual(self.status_of("POST", "/mcp", {"jsonrpc": "2.0", "id": 1,
            "method": "tools/list"}, token=issued["token"]), 401)


class SeparateWorkspaceCredentialsTests(OfflineCase):
    def test_expired_oauth_state_is_consumed_without_exchanging_the_code(self):
        store = Store(Path(self.temp.name) / "oauth.db")
        auth = Auth(store, enabled=True)
        auth._states["expired"] = time.time() - 601
        with patch("bridge.auth._http_json") as fetch:
            with self.assertRaises(Invalid):
                auth.github_exchange("code", "expired", "http://localhost/auth/github/callback")
            fetch.assert_not_called()
        self.assertNotIn("expired", auth._states)

    def test_tokens_and_task_links_from_another_database_are_not_credentials(self):
        first = Store(Path(self.temp.name) / "first.db")
        second = Store(Path(self.temp.name) / "second.db")
        person = first.add_person({"name": "Same Display Name", "email": "same@example.test"})
        second.add_person({"name": "Same Display Name", "email": "same@example.test"})
        auth_a, auth_b = Auth(first, enabled=True), Auth(second, enabled=True)
        token = auth_a.create_token(person["id"])["token"]
        self.assertIsNotNone(auth_a.identify({"Authorization": "Bearer " + token}))
        self.assertIsNone(auth_b.identify({"Authorization": "Bearer " + token}))
        cookie = auth_a.session_cookie(person["id"]).split(";", 1)[0]
        self.assertIsNone(auth_b.identify({"Cookie": cookie}))
        run = first.add_run({"title": "Private task", "repo": "acme/service"})
        with first.graph.transaction():
            link = briefing.mint(first.graph, person["id"], run["id"])
        self.assertIsNotNone(briefing.resolve(first.graph, link))
        self.assertIsNone(briefing.resolve(second.graph, link))
