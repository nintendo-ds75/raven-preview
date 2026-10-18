"""The inbox ties decisions to the signed-in person by id, as the server
does when they act, never by display name."""
import json
from urllib.error import HTTPError

from bridge.accounts import Accounts
from test_auth import BOOTSTRAP, SharedServer


class NamesakeInbox(SharedServer):
    """A seeded owner (no email, so no invitation links to them) and an
    invited member who happens to share their name."""

    def setUp(self):
        super().setUp()
        self.alex = self.post("/api/people", {"name": "Alex Morgan"}, token=BOOTSTRAP)
        self.post("/api/authority", {"person": self.alex["id"], "scope_kind": "path", "scope": "billing/*",
                                     "role": "decides"}, token=BOOTSTRAP)
        requester = self.post("/api/people", {"name": "Req Uester", "email": "req@example.test"}, token=BOOTSTRAP)
        agent = self.post("/api/tokens", {"person_id": requester["id"], "label": "codex"}, token=BOOTSTRAP)["token"]
        task = self.mcp("bridge_start_task", {"title": "Usage pricing", "repo": "acme/platform",
                                              "paths": "billing/usage.py"}, agent)["result"]
        self.node = self.mcp("bridge_add_node", {"task_id": task["task_id"], "question": "Bill the spike?",
                                                 "paths": "billing/usage.py"}, agent)["result"]
        self.assertEqual(self.node["owner"], "Alex Morgan")
        invite = self.post("/api/invitations", {"email": "alex.other@example.test", "role": "member"}, token=BOOTSTRAP)
        self.namesake = Accounts(self.auth).accept({"name": "Alex Morgan", "password": "a-long-enough-password",
                                                    "invite": invite["url"].split("#invite=")[1]})
        self.assertNotEqual(self.namesake, self.alex["id"])
        self.cookie = self.auth.session_cookie(self.namesake).split(";", 1)[0]

    def state_decision(self, **kw):
        state = self.get("/api/state", **kw)
        return state, next(d for d in state["decisions"] if d["id"] == self.node["node_id"])

    def test_the_namesake_is_assigned_nothing_and_the_owner_is(self):
        state, decision = self.state_decision(cookie=self.cookie)
        self.assertEqual(state["me"]["name"], "Alex Morgan")
        self.assertEqual(decision["owner_name"], "Alex Morgan")
        self.assertIs(decision["can_act"], False)
        self.assertEqual([d["id"] for d in state["decisions"] if d.get("can_act")], [])
        # The server refuses what the badge no longer promises.
        with self.assertRaises(HTTPError) as refused:
            self.call("POST", f"/api/decisions/{self.node['node_id']}/answer",
                      {"answer": "Bill it", "expected_updated_at": self.node["updated_at"]},
                      cookie=self.cookie, csrf=state["csrf_token"])
        self.assertEqual(refused.exception.code, 403)
        self.assertIn("Not permitted", json.loads(refused.exception.read())["error"])
        # The seeded owner, by id, is who it waits on.
        owner_token = self.post("/api/tokens", {"person_id": self.alex["id"], "kind": "human"}, token=BOOTSTRAP)["token"]
        _, theirs = self.state_decision(token=owner_token)
        self.assertIs(theirs["can_act"], True)

    def test_standing_follows_the_check_not_the_role(self):
        # An admin who is not the owner has no standing of their own: the
        # inbox does not count the decision as theirs, the override is a
        # separate, recorded act.
        admin = self.post("/api/people", {"name": "Ada Admin", "email": "ada@example.test", "role": "admin"}, token=BOOTSTRAP)
        _, decision = self.state_decision(cookie=self.auth.session_cookie(admin["id"]).split(";", 1)[0])
        self.assertIs(decision["can_act"], False)
        # A settled decision waits on nobody, whoever asks.
        owner_token = self.post("/api/tokens", {"person_id": self.alex["id"], "kind": "human"}, token=BOOTSTRAP)["token"]
        self.post(f"/api/decisions/{self.node['node_id']}/answer",
                  {"answer": "Bill it", "rationale": "policy", "expected_updated_at": self.node["updated_at"]},
                  token=owner_token)
        _, settled = self.state_decision(token=owner_token)
        self.assertIs(settled["can_act"], False)
