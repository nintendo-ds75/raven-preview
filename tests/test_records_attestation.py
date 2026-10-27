"""A record written over HTTP or MCP carries the server-stamped author and
scope of the credential that wrote it, whatever the body claims; session
records are Raven's own and cannot be imported."""
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

    def test_member_post_is_stamped_with_the_actor_not_the_forged_author(self):
        forged = json.dumps({"basis": "author", "attested_by": "someone-else", "transport": "session", "workspace_shared": True})
        result = self.post("/api/records", self.body(author="The CEO", access_scope=forged), token=self.pat_token)
        source, scope = self.stored(result)
        self.assertEqual(source["author"], "Pat Member")
        self.assertEqual(scope, {"basis": "author", "attested_by": self.pat["id"], "transport": "token", "workspace_shared": True})
        event = json.loads(self.store.graph.db.execute(
            "SELECT detail FROM events WHERE kind='record_added' ORDER BY id DESC LIMIT 1").fetchone()["detail"])
        self.assertEqual((event["author"], event["claimed_author"], event["attested_by"]), ("Pat Member", "The CEO", self.pat["id"]))
        # A later import of the same record by the same person keeps the stamp.
        again = self.post("/api/records", self.body(author="Somebody", body="Could we pay yearly? Please."), token=self.pat_token)
        self.assertEqual(self.stored(again)[0]["author"], "Pat Member")

    def test_mcp_import_is_stamped_with_the_credential_behind_it(self):
        result = self.mcp("bridge_import_record", self.body(author="Forged"), self.pat_agent)
        self.assertFalse(result["isError"], result)
        source, scope = self.stored(result["result"])
        self.assertEqual(source["author"], "Pat Member")
        self.assertEqual((scope["basis"], scope["attested_by"], scope["transport"]), ("author", self.pat["id"], "agent"))

    def test_stdio_import_is_the_local_operator_and_in_process_writers_keep_the_source_author(self):
        over_stdio = mcp.call_tool(self.store, "bridge_import_record", self.body(ref="paste:stdio", author="Forged"))
        self.assertEqual(self.stored(over_stdio)[0]["author"], "Local operator")
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
