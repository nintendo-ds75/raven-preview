"""A record written over HTTP or MCP carries the server-stamped access scope
of the credential that wrote it, whatever the body claims: who attested it
and who imported it. The author stays the source's own (a ticket's reporter)
and falls back to the importer when the body names none; session records are
Raven's own and cannot be imported."""
import json

from test_auth import BOOTSTRAP, SharedServer
from bridge import mcp
from bridge.store import Invalid

REPO = "acme/platform"


class RecordAttestationTests(SharedServer):
    def setUp(self):
        super().setUp()
        self.pat = self.post("/api/people", {"name": "Pat Member", "email": "pat@acme.example"}, token=BOOTSTRAP)
        self.pat_token = self.post("/api/tokens", {"person_id": self.pat["id"], "kind": "human"}, token=BOOTSTRAP)["token"]
        self.pat_agent = self.post("/api/tokens", {"person_id": self.pat["id"], "label": "claude-code"}, token=BOOTSTRAP)["token"]
        viewer = self.post("/api/people", {"name": "Val Viewer", "role": "viewer"}, token=BOOTSTRAP)
        self.viewer_token = self.post("/api/tokens", {"person_id": viewer["id"], "kind": "human"}, token=BOOTSTRAP)["token"]

    def body(self, **extra):
        return {"repo": REPO, "kind": "email", "ref": "paste:1", "provider": "generic", "namespace": "raven:paste",
                "title": "Customer asks for annual billing", "body": "Could we pay yearly?", **extra}

    def stored(self, result):
        record = self.store.get_record(result["source"]["record_id"], REPO)
        scope = record["versions"][-1]["snapshot"]["access_scope"]
        return record["source"], json.loads(scope) if scope else {}

    def stamp(self, transport="token"):
        return {"basis": "author", "attested_by": self.pat["id"], "imported_by": self.pat["id"],
                "transport": transport, "workspace_shared": True}

    def last_event(self):
        return json.loads(self.store.graph.db.execute(
            "SELECT detail FROM events WHERE kind='record_added' ORDER BY id DESC LIMIT 1").fetchone()["detail"])

    def test_forged_scope_is_overwritten_and_the_claimed_author_is_kept(self):
        forged = json.dumps({"basis": "author", "attested_by": "someone-else", "transport": "session", "workspace_shared": True})
        result = self.post("/api/records", self.body(author="The CEO", access_scope=forged), token=self.pat_token)
        source, scope = self.stored(result)
        self.assertEqual(source["author"], "The CEO")
        self.assertEqual(scope, self.stamp())
        event = self.last_event()
        self.assertEqual((event["author"], event["attested_by"], event["imported_by"]), ("The CEO", self.pat["id"], self.pat["id"]))
        self.assertNotIn("claimed_author", event)
        # A later import of the same record by the same person keeps the stamp
        # and follows the source's author.
        again = self.post("/api/records", self.body(author="Somebody", body="Could we pay yearly? Please."), token=self.pat_token)
        source, scope = self.stored(again)
        self.assertEqual((source["author"], scope), ("Somebody", self.stamp()))

    def test_paste_without_an_author_is_attributed_to_the_person_who_pasted_it(self):
        result = self.post("/api/records", self.body(), token=self.pat_token)
        source, scope = self.stored(result)
        self.assertEqual((source["author"], scope), ("Pat Member", self.stamp()))
        self.assertEqual(self.last_event()["author"], "Pat Member")
        blank = self.post("/api/records", self.body(ref="paste:2", author=""), token=self.pat_token)
        self.assertEqual(self.stored(blank)[0]["author"], "Pat Member")

    def test_mcp_import_keeps_the_ticket_reporter_and_stamps_the_credential_behind_it(self):
        result = self.mcp("bridge_import_record", self.body(kind="ticket", ref="PLAT-7", provider="jira",
                                                             namespace="acme.atlassian.net", author="Rae Reporter"), self.pat_agent)
        self.assertFalse(result["isError"], result)
        source, scope = self.stored(result["result"])
        self.assertEqual(source["author"], "Rae Reporter")
        self.assertEqual(scope, self.stamp("agent"))
        listed = self.get("/api/records?namespace=acme.atlassian.net", token=self.pat_token)["records"][0]
        self.assertEqual((listed["author"], listed["attested_by"]), ("Rae Reporter", self.pat["id"]))

    def test_stdio_import_is_the_local_operator_and_in_process_writers_keep_the_source_author(self):
        over_stdio = mcp.call_tool(self.store, "bridge_import_record", self.body(ref="paste:stdio"))
        self.assertEqual(self.stored(over_stdio)[0]["author"], "Local operator")
        named = mcp.call_tool(self.store, "bridge_import_record", self.body(ref="paste:stdio-named", author="Rae Reporter"))
        source, scope = self.stored(named)
        self.assertEqual((source["author"], scope["basis"], scope["transport"]), ("Rae Reporter", "author", "operator"))
        self.assertEqual(scope["attested_by"], scope["imported_by"])
        in_process = self.store.add_record(self.body(ref="paste:connector", author="Source Writer"))
        source, scope = self.stored(in_process)
        self.assertEqual((source["author"], scope), ("Source Writer", {}))

    def test_session_records_cannot_be_imported(self):
        self.assertEqual(self.status_of("POST", "/api/records", self.body(kind="session"), token=self.pat_token), 400)
        self.assertEqual(self.status_of("POST", "/api/records", self.body(kind="conversation"), token=self.pat_token), 400)
        refused = self.mcp("bridge_import_record", self.body(kind="session"), self.pat_agent)
        self.assertTrue(refused["isError"])
        self.assertIn("written by Raven, not imported", refused["result"])
        with self.assertRaisesRegex(Invalid, "written by Raven"):
            mcp.call_tool(self.store, "bridge_import_record", self.body(kind="session"))
        self.assertEqual(self.store.graph.db.execute("SELECT count(*) c FROM intents WHERE kind IN ('session','conversation')").fetchone()["c"], 0)

    def test_viewer_and_agent_credentials_cannot_paste_evidence(self):
        self.assertEqual(self.status_of("POST", "/api/records", self.body(), token=self.viewer_token), 403)
        self.assertEqual(self.status_of("POST", "/api/records", self.body(), token=self.pat_agent), 403)
        # No credential and no session token: refused before anything is read.
        self.assertEqual(self.status_of("POST", "/api/records", self.body()), 403)
        self.assertEqual(self.store.graph.db.execute("SELECT count(*) c FROM intents").fetchone()["c"], 0)

    def test_pasted_records_are_listed_for_members_without_bodies(self):
        self.post("/api/records", self.body(), token=self.pat_token)
        listing = self.get("/api/records?namespace=raven%3Apaste&limit=10", token=self.pat_token)
        self.assertEqual([r["title"] for r in listing["records"]], ["Customer asks for annual billing"])
        self.assertEqual(listing["records"][0]["attested_by"], self.pat["id"])
        self.assertNotIn("Could we pay yearly", json.dumps(listing))
        self.assertEqual(self.status_of("GET", "/api/records?namespace=raven%3Apaste", token=self.viewer_token), 403)
        # A GET that cannot be served answers 404, as every record read does.
        self.assertEqual(self.status_of("GET", "/api/records?namespace=raven%3Apaste&limit=0", token=self.pat_token), 404)
