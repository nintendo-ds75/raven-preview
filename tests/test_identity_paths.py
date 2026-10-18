"""Each way a decision can be tied to the wrong person, probed: a Slack
reply from somebody else in a thread meant for the owner, a task link
forwarded, a correction by a signer who shares a name, and what an
agent credential may claim about who asked."""
import json
import unittest
from pathlib import Path
from urllib.error import HTTPError

from fixtures import OfflineCase
from bridge import briefing, canvas
from bridge.authz import Actor
from bridge.store import Store
from test_delivery import DeliveryCase
from test_portal import CFG
from test_security_probes import LinkProbes, Workspace


class CorrectionKeepsThePerson(OfflineCase):
    def workspace(self):
        store = Store(Path(self.temp.name) / "namesake.db")
        graph = store.graph
        with graph.transaction():
            wes = graph.add_person("Wes Chen", email="wes@acme.example")
            graph.add_authority("path", "billing/*", "decides", person_id=wes)
        task = canvas.start_task(store, CFG, {"title": "Pricing", "repo": "acme/platform", "paths": "billing/usage.py",
                                              "requester": "Priya Natarajan"})["task_id"]
        return store, graph, wes, task

    def owner_row(self, store, node_id):
        return store.graph.db.execute("SELECT person_id, name FROM owners WHERE id=?",
                                      (store.get_decision(node_id)["owner_id"],)).fetchone()

    def test_a_signers_correction_stays_bound_to_their_id_beside_a_namesake(self):
        store, graph, wes, task = self.workspace()
        node = canvas.add_node(store, CFG, {"task_id": task, "question": "Bill the spike?", "paths": "billing/usage.py",
                                            "options": "Bill it | Exclude it"})
        self.assertEqual(self.owner_row(store, node["node_id"])["person_id"], wes)
        # A namesake joins after the question was routed, as an invited member would.
        with graph.transaction():
            graph.add_person("Wes Chen", email="wes.other@acme.example", merge=False)
        actor = Actor(id=wes, name="Wes Chen", role="member", kind="session")
        canvas.sign_off(store, node["node_id"], {"answer": "Exclude it, it was our load test",
                                                 "expected_updated_at": node["updated_at"]}, actor=actor)
        after = store.get_decision(node["node_id"])
        row = self.owner_row(store, node["node_id"])
        self.assertEqual((row["person_id"], row["name"]), (wes, "Wes Chen"))
        self.assertEqual((after["actor_id"], after["answered_by"], after["signed_by"]), (wes, "Wes Chen", "Wes Chen"))
        # And Wes still holds the decision as its owner, by id.
        from bridge.authz import basis_for
        self.assertEqual(basis_for(graph, actor, after, "correct")[0], "owner")

    @unittest.expectedFailure
    def test_routing_binds_the_authority_holder_by_id_when_a_namesake_exists(self):
        # Known gap: the authority map names Wes by id, but routing reduces
        # the candidate to a display name and resolves it again, so with a
        # namesake on the map the owner row comes out unbound and nobody
        # holds the decision as its owner. Carrying the person id through
        # the ranked candidates (signals, routing, contact_reasoning) is
        # the fix; until then this documents the gap.
        store, graph, wes, task = self.workspace()
        with graph.transaction():
            graph.add_person("Wes Chen", email="wes.other@acme.example", merge=False)
        node = canvas.add_node(store, CFG, {"task_id": task, "question": "Bill the spike?", "paths": "billing/usage.py",
                                            "options": "Bill it | Exclude it"})
        self.assertEqual(self.owner_row(store, node["node_id"])["person_id"], wes)


class SlackReplyFromSomebodyElse(DeliveryCase):
    def test_a_reply_in_the_owners_thread_from_another_member_records_nothing(self):
        node = self.node(self.task())
        self.delivery.deliver_now()
        message = self.slack.messages[0]
        self.assertEqual(message["channel"], "DUWES")
        # Priya (UPRI) is on the map with no authority over billing; her
        # words in Wes's thread decide nothing, however they are phrased.
        for text in ("answer: Bill it, it was real traffic", "sign off", "rule if load test"):
            response = self.reply(message, "UPRI", text, literal=True)
            self.assertIn("Not permitted", response, text)
        decision = self.store.get_decision(node["node_id"])
        self.assertEqual((decision["status"], decision["answer"], decision["owner_name"]), ("pending", None, "Wes Chen"))
        # A Slack id Raven cannot verify is nobody.
        self.slack.directory = {}
        response = self.delivery.receive(message["channel"], message["ts"], "UNOBODY", "answer: Bill it",
                                         event_id="stranger", occurrence={"platform": "slack", "id": "1.1",
                                                                          "timestamp": "1.1", "reply_to": message["ts"]})
        self.assertIn("could not verify you", response)
        self.assertEqual(self.store.get_decision(node["node_id"])["status"], "pending")


class ForwardedLink(LinkProbes):
    def link_for(self, person):
        with self.store.graph.transaction():
            return briefing.mint(self.store.graph, person["id"], self.task, self.node["node_id"], "n-fwd")

    def test_a_link_opened_while_signed_in_as_somebody_else_acts_as_nobody(self):
        link = self.link_for(self.owner)
        body = {"decision_id": self.node["node_id"], "answer": "Bill them",
                "expected_updated_at": self.node["updated_at"]}
        other = self.auth.session_cookie(self.other["id"]).split(";", 1)[0]
        status, answer = self.brief("POST", "/api/brief/answer", body, link=link, headers={"Cookie": other})
        self.assertEqual(status, 403)
        self.assertIn("sent to Policy Owner", answer["error"])
        self.assertIn("signed in as Other Member", answer["error"])
        status, _ = self.brief("POST", "/api/brief/note", {"text": "mine?"}, link=link, headers={"Cookie": other})
        self.assertEqual(status, 403)
        status, _ = self.brief("POST", "/api/brief/account", {"password": "a-long-enough-password"}, link=link,
                               headers={"Cookie": other})
        self.assertEqual(status, 403)
        self.assertEqual(self.decision()["status"], "pending")
        # The person it was sent to, signed in or not, is who it acts as.
        mine = self.auth.session_cookie(self.owner["id"]).split(";", 1)[0]
        status, page = self.brief("GET", "/api/brief", link=link, headers={"Cookie": mine})
        self.assertEqual((status, page["viewer"]["signed_in"]), (200, True))
        status, _ = self.brief("POST", "/api/brief/answer", body, link=link, headers={"Cookie": mine})
        self.assertEqual(status, 200)
        row = self.decision()
        self.assertEqual((row["answered_by"], row["actor_id"]), ("Policy Owner", self.owner["id"]))

    def test_a_forwarded_link_with_no_session_still_acts_as_its_person(self):
        # Semantics on record: possession of the link is the proof, as the
        # Slack DM it arrived in is. The record names the link's person.
        link = self.link_for(self.owner)
        status, _ = self.brief("POST", "/api/brief/answer", {"decision_id": self.node["node_id"], "answer": "Bill them",
                                                              "expected_updated_at": self.node["updated_at"]}, link=link)
        self.assertEqual(status, 200)
        row = self.decision()
        self.assertEqual((row["answered_by"], row["actor_id"], row["actor_basis"]), ("Policy Owner", self.owner["id"], "owner"))


class AgentCredentialIdentity(Workspace):
    def test_a_task_and_its_nodes_name_the_credentials_person_as_requester(self):
        other_agent = self.token(self.other, "agent", "codex")
        task = self.mcp("bridge_start_task", {"title": "Rates", "repo": "acme/platform", "paths": "billing/rates.py"},
                        other_agent)["result"]
        self.assertEqual(self.store.graph.get_task(task["task_id"])["requester"], "Other Member")
        node = self.mcp("bridge_add_node", {"task_id": task["task_id"], "question": "Round up?",
                                            "paths": "billing/rates.py"}, other_agent)["result"]
        self.assertEqual(node["owner"], "Policy Owner")
        # Naming the owner as requester does not route the question away from them.
        claimed = self.mcp("bridge_add_node", {"task_id": task["task_id"], "question": "Round down?",
                                               "paths": "billing/rates.py", "requester": "Policy Owner"}, other_agent)
        self.assertEqual(claimed["result"]["owner"], "Policy Owner", claimed)

    def test_an_admin_decides_for_the_owner_only_by_saying_so_and_the_record_says_who(self):
        state = self.get("/api/state", token=self.admin_token)
        decision = next(d for d in state["decisions"] if d["id"] == self.node["node_id"])
        self.assertIs(decision["can_act"], False)
        body = {"answer": "Bill them", "expected_updated_at": self.node["updated_at"]}
        with self.assertRaises(HTTPError) as refused:
            self.call("POST", f"/api/decisions/{self.node['node_id']}/answer", body, token=self.admin_token)
        self.assertEqual(refused.exception.code, 403)
        row = self.post(f"/api/decisions/{self.node['node_id']}/answer", {**body, "override": True}, token=self.admin_token)
        self.assertEqual((row["answered_by"], row["actor_name"], row["actor_id"], row["actor_basis"]),
                         ("Policy Owner", "Ada Admin", self.admin["id"], "admin-override"))
        recorded = [e for e in row["events"] if e["kind"] == "owner_approved"][0]
        detail = json.loads(recorded["detail"]) if recorded["detail"].startswith("{") else {"text": recorded["detail"]}
        self.assertIn("admin-override", json.dumps(detail))
