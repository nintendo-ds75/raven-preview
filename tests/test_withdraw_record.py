"""A record's attested author, or an admin, withdraws it: the research
catalog drops it, a proposal citing it is withheld, a decision that relied
on it goes to review, the corpus revision moves, the audit history stays."""
import json

from fixtures import OfflineCase
from test_auth import BOOTSTRAP, SharedServer
from bridge import objective_research as research, objectives as obj
from bridge.authz import Actor
from bridge.store import Invalid

REPO = "acme/app"


class WithdrawRecordTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store("withdraw.db")
        self.author = Actor(id="p-author", name="Pat Author", role="member", kind="token")
        self.other = Actor(id="p-other", name="Oli Other", role="member", kind="token")
        self.admin = Actor(id="p-admin", name="Ada Admin", role="admin", kind="session")
        self.store.add_owner({"name": "Owner", "team": "Policy", "patterns": "*"})

    def paste(self, ref="paste:1", actor=None, **extra):
        return self.store.add_record({"repo": REPO, "kind": "email", "ref": ref, "provider": "generic", "namespace": "raven:paste",
                                      "title": "Customer wants yearly billing", "body": "Could we pay yearly?", **extra},
                                     actor=actor or self.author)

    def dependent_decision(self, source):
        g = self.store.graph
        task = self.store.add_run({"title": "Add yearly billing", "repo": REPO})["id"]
        did = g.add_decision(task, "Offer yearly billing?", "billing", "pending", repo=REPO)
        g.publish_evidence(did, [source], status="resolved", source="record",
                           answer="Yes, yearly.", kind="evidence", signoff="required")
        return did

    def proposal(self, source):
        pin = {k: source[k] for k in ("record_id", "source_version_id")}
        payload = {"request_key": "create", "title": "Pilot", "outcome": "Prepare yearly billing",
                   "success_criteria": "Invoice works", "stop_criteria": "Customer declines", "guardrails": "No sends",
                   "repository": REPO, "provider": "claude", "why_now": "Customer asked", "cases": {},
                   "judgment_kind": "interpretation_tradeoff", "citations": [{**pin, "quote": "Could we pay yearly"}]}
        return obj.propose(self.store, payload, [pin])

    def test_only_the_attested_author_or_an_admin_may_withdraw(self):
        source = self.paste()["source"]
        with self.assertRaisesRegex(Invalid, "wrote this record, or an admin"):
            self.store.withdraw_record(source["record_id"], {"reason": "not mine"}, self.other)
        with self.assertRaisesRegex(Invalid, "wrote this record, or an admin"):
            self.store.withdraw_record(source["record_id"], {"reason": "agent"}, Actor(id="p-author", name="Pat", role="member", kind="agent"))
        self.assertEqual(self.store.get_record(source["record_id"], REPO)["source"]["availability"], "available")
        with self.assertRaisesRegex(Invalid, "reason"):
            self.store.withdraw_record(source["record_id"], {}, self.author)
        done = self.store.withdraw_record(source["record_id"], {"reason": "Customer asked us not to share it"}, self.author)
        self.assertEqual((done["availability"], done["withdrawn"]), ("inaccessible", True))
        other = self.paste(ref="paste:2", actor=self.other)["source"]
        self.assertTrue(self.store.withdraw_record(other["record_id"], {"reason": "duplicate"}, self.admin)["withdrawn"])
        # Authorisation follows access_scope.attested_by, not the stored author:
        # a ticket Pat imported in Oli's name is still Pat's to withdraw.
        imported = self.paste(ref="paste:3", author="Oli Other")["source"]
        self.assertEqual(imported["author"], "Oli Other")
        with self.assertRaisesRegex(Invalid, "wrote this record, or an admin"):
            self.store.withdraw_record(imported["record_id"], {"reason": "in my name"}, self.other)
        self.assertTrue(self.store.withdraw_record(imported["record_id"], {"reason": "wrong ticket"}, self.author)["withdrawn"])
        # An in-process record with no attested author: only an admin.
        unattested = self.store.add_record({"repo": REPO, "kind": "note", "ref": "n1", "title": "Note", "body": "x"})["source"]
        with self.assertRaises(Invalid):
            self.store.withdraw_record(unattested["record_id"], {"reason": "mine?"}, self.author)
        self.assertTrue(self.store.withdraw_record(unattested["record_id"], {"reason": "stale"}, self.admin)["withdrawn"])
        with self.assertRaises(Invalid):
            self.store.withdraw_record("nope", {"reason": "x"}, self.admin)

    def test_withdrawal_blocks_reuse_and_keeps_history(self):
        source = self.paste()["source"]
        did = self.dependent_decision(source)
        proposal = self.proposal(source)
        before = research.corpus_revision(self.store, REPO)
        self.assertIn(source["record_id"], research._catalog(self.store, REPO))
        self.assertFalse(obj.overview(self.store)["proposals"][0].get("withheld"))
        result = self.store.withdraw_record(source["record_id"], {"reason": "Customer asked us not to share it"}, self.author)
        self.assertEqual(result["affected_decisions"], [did])
        self.assertNotIn(source["record_id"], research._catalog(self.store, REPO))
        self.assertNotEqual(research.corpus_revision(self.store, REPO), before)
        view = obj.overview(self.store)["proposals"][0]
        self.assertEqual(view["id"], proposal["id"])
        self.assertTrue(view["withheld"])
        self.assertNotIn("Could we pay yearly", json.dumps(view))
        row = self.store.graph.db.execute("SELECT needs_review, review_reason FROM decisions WHERE id=?", (did,)).fetchone()
        self.assertEqual(row["needs_review"], 1)
        self.assertIn("withdrawn", row["review_reason"])
        with self.assertRaisesRegex(Invalid, "unavailable"):
            self.store.get_record(source["record_id"], REPO)
        withheld = self.store.graph.db.execute("SELECT count(*) c FROM withheld_source_versions WHERE source_version_id=?",
                                               (source["source_version_id"],)).fetchone()["c"]
        self.assertEqual(withheld, 1)
        # History stays: the version row and the event are still there.
        self.assertEqual(self.store.graph.db.execute("SELECT count(*) c FROM source_versions WHERE record_id=?",
                                                     (source["record_id"],)).fetchone()["c"], 1)
        event = json.loads(self.store.graph.db.execute(
            "SELECT detail FROM events WHERE kind='record_withdrawn'").fetchone()["detail"])
        self.assertEqual((event["record_id"], event["actor_id"], event["reason"]),
                         (source["record_id"], "p-author", "Customer asked us not to share it"))
        listed = self.store.list_records("raven:paste")["records"][0]
        self.assertEqual((listed["record_id"], listed["availability"]), (source["record_id"], "inaccessible"))
        # Withdrawing twice is a no-op, not an error.
        self.assertTrue(self.store.withdraw_record(source["record_id"], {"reason": "again"}, self.author)["already"])


class WithdrawRouteTests(SharedServer):
    def test_author_withdraws_over_http_and_others_cannot(self):
        pat = self.post("/api/people", {"name": "Pat Author"}, token=BOOTSTRAP)
        oli = self.post("/api/people", {"name": "Oli Other"}, token=BOOTSTRAP)
        val = self.post("/api/people", {"name": "Val Viewer", "role": "viewer"}, token=BOOTSTRAP)
        tokens = {p["id"]: self.post("/api/tokens", {"person_id": p["id"], "kind": "human"}, token=BOOTSTRAP)["token"]
                  for p in (pat, oli, val)}
        record = self.post("/api/records", {"repo": REPO, "kind": "slack", "ref": "paste:http", "provider": "generic",
                                            "namespace": "raven:paste", "title": "Thread", "body": "Decided in Slack."},
                           token=tokens[pat["id"]])
        rid = record["source"]["record_id"]
        self.assertEqual(self.status_of("POST", f"/api/records/{rid}/withdraw", {"reason": "x"}, token=tokens[val["id"]]), 403)
        self.assertEqual(self.status_of("POST", f"/api/records/{rid}/withdraw", {"reason": "x"}, token=tokens[oli["id"]]), 400)
        self.assertEqual(self.get("/api/records?namespace=raven%3Apaste", token=tokens[oli["id"]])["records"][0]["availability"], "available")
        done = self.post(f"/api/records/{rid}/withdraw", {"reason": "Shared by mistake"}, token=tokens[pat["id"]])
        self.assertEqual(done["availability"], "inaccessible")
        self.assertEqual(self.get("/api/records?namespace=raven%3Apaste", token=tokens[oli["id"]])["records"][0]["availability"], "inaccessible")
        # A second withdrawal is a no-op; the sanitizer redacts the withdrawn
        # record's identity on the way out and keeps its availability.
        admin_done = self.post(f"/api/records/{rid}/withdraw", {"reason": "again"}, token=BOOTSTRAP)
        self.assertEqual(admin_done["availability"], "inaccessible")
        self.assertEqual(self.store.graph.db.execute("SELECT count(*) c FROM events WHERE kind='record_withdrawn'").fetchone()["c"], 1)

    def test_recording_optout_is_the_persons_own_or_an_admins(self):
        pat = self.post("/api/people", {"name": "Pat Author"}, token=BOOTSTRAP)
        oli = self.post("/api/people", {"name": "Oli Other"}, token=BOOTSTRAP)
        pat_token = self.post("/api/tokens", {"person_id": pat["id"], "kind": "human"}, token=BOOTSTRAP)["token"]
        oli_token = self.post("/api/tokens", {"person_id": oli["id"], "kind": "human"}, token=BOOTSTRAP)["token"]
        self.assertEqual(self.status_of("POST", f"/api/people/{pat['id']}/recording", {"optout": True}, token=oli_token), 400)
        self.assertEqual(self.status_of("POST", f"/api/people/{pat['id']}/recording", {"optout": "yes"}, token=pat_token), 400)
        self.assertTrue(self.post(f"/api/people/{pat['id']}/recording", {"optout": True}, token=pat_token)["recording_optout"])
        people = self.get("/api/people", token=oli_token)["people"]
        self.assertEqual({p["name"]: p["recording_optout"] for p in people if p["name"].startswith(("Pat", "Oli"))},
                         {"Pat Author": True, "Oli Other": False})
        self.assertFalse(self.post(f"/api/people/{pat['id']}/recording", {"optout": False}, token=BOOTSTRAP)["recording_optout"])
        settings = self.post("/api/settings", {"session_records": True, "objective_scope_fallback": "none"}, token=BOOTSTRAP)
        self.assertEqual((settings["session_records"], settings["objective_scope_fallback"]), (True, "none"))
        self.assertEqual(self.status_of("POST", "/api/settings", {"objective_scope_fallback": "anyone"}, token=BOOTSTRAP), 400)
        self.assertEqual(self.status_of("POST", "/api/settings", {"session_records": True}, token=pat_token), 403)
