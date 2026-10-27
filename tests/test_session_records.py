"""A finished task can leave one compact, attributed session record: off by
default, on by an admin setting, skipped for a person who opted out, built
from the task ledger and never from a prompt, a transcript, a conversation
or the diff body."""
import hashlib
import json
from unittest.mock import patch

from test_contract import ContractCase
from bridge import canvas
from bridge.mcp import call_tool

DIFF = ("diff --git a/billing/usage.py b/billing/usage.py\n"
        "--- a/billing/usage.py\n+++ b/billing/usage.py\n"
        "@@ -1 +1 @@\n-rate = 1\n+rate = SECRET_DIFF_TOKEN_2\n")
SECRETS = ("SECRET_DIFF_TOKEN_2", "SECRET_PROMPT_TOKEN", "SECRET_TRANSCRIPT_TOKEN", "SECRET_SLACK_TOKEN", "SECRET_PAYLOAD_TOKEN")


class SessionRecordTests(ContractCase):
    def signed_task(self, **extra):
        task = self.start(agent="Claude Code", **extra)
        node = self.node(task)
        self.answer(node["node_id"], "Bill at two cents per call.")
        self.sign(node["node_id"])
        call_tool(self.store, "bridge_get_tree", {"task_id": task})
        self.seed_secrets(task, node["node_id"])
        return task, node["node_id"]

    def seed_secrets(self, task, decision_id):
        person = self.store.add_person({"name": "Interviewee " + task})
        with self.store.graph.transaction() as db:
            db.execute("INSERT INTO host_sessions(id,principal,host,session_id,project,repo,task_id,initial_prompt,created_at,updated_at) "
                       "VALUES(?,'p','claude','s1','proj','acme/platform',?,'SECRET_PROMPT_TOKEN','2026-01-01','2026-01-01')", ("hs-" + task, task))
            db.execute("INSERT INTO interviews(id,task_id,decision_id,person_id,client_key,decision_revision,scope,transcript,created_at,updated_at) "
                       "VALUES(?,?,?,?,'k','r','{}','SECRET_TRANSCRIPT_TOKEN','2026-01-01','2026-01-01')", ("iv-" + task, task, decision_id, person["id"]))
            db.execute("INSERT INTO slack_conversation(id,decision_id,channel,thread_ts,person_id,role,text,created_at) "
                       "VALUES(?,?,'C1','1.1',?,'user','SECRET_SLACK_TOKEN','2026-01-01')", ("sc-" + task, decision_id, person["id"]))
            db.execute("INSERT INTO notifications(id,decision_id,run_id,kind,channel,person_name,dedupe_key,payload,state,attempts,created_at) "
                       "VALUES(?,?,?,'ask','slack','Wes',?,'SECRET_PAYLOAD_TOKEN','sent',1,'2026-01-01')", ("n-" + task, decision_id, task, "dedupe-" + task))

    def finish(self, task, **args):
        with patch("bridge.llm.check_conformance", return_value=None):
            return call_tool(self.store, "bridge_finish_task", {"task_id": task, "diff": DIFF, **args})

    def session_rows(self):
        return [dict(r) for r in self.store.graph.db.execute(
            "SELECT i.*, s.id AS record_id, s.head_id, s.namespace FROM intents i JOIN source_records s ON s.intent_id=i.id "
            "WHERE i.kind='session' ORDER BY i.created_at")]

    def test_off_by_default_writes_nothing(self):
        task, _node = self.signed_task()
        self.assertEqual(self.finish(task)["status"], "completed")
        self.assertEqual(self.session_rows(), [])
        self.assertFalse(self.store.settings()["session_records"])

    def test_on_writes_one_attributed_record_from_the_ledger_only(self):
        self.store.update_settings({"session_records": True})
        self.assertTrue(self.store.settings()["session_records"])
        task, node_id = self.signed_task()
        done = self.finish(task, checks="pytest: passed")
        rows = self.session_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["repo"], row["ref"], row["author"], row["namespace"]),
                         ("acme/platform", "source:" + row["ref"][7:], "Claude Code", "raven:session"))
        self.assertEqual(row["title"], "Review billing behavior")
        body = row["body"]
        self.assertIn(f"- {node_id}: Should we waive the usage charge?", body)
        self.assertIn("owner: Wes", body)
        self.assertIn("signed unchanged by Wes", body)
        self.assertIn("standing: authorized", body)
        self.assertIn("Kickoff verdict:", body)
        self.assertIn("- billing/usage.py", body)
        self.assertIn(f"Proof {done['proof']['id']}, diff sha256 {hashlib.sha256(DIFF.encode()).hexdigest()}", body)
        self.assertIn("Final status: completed", body)
        for secret in SECRETS:
            self.assertNotIn(secret, body)
        self.assertNotIn("Bill at two cents", body)
        snapshot = json.loads(self.store.graph.db.execute("SELECT snapshot FROM source_versions WHERE id=?", (row["head_id"],)).fetchone()["snapshot"])
        self.assertEqual(json.loads(snapshot["access_scope"])["basis"], "session")
        self.assertEqual(self.store.list_records("raven:session")["records"][0]["record_id"], row["record_id"])
        # A corrected signature and a rule read differently from a plain one.
        self.assertIn("corrected", canvas._session_outcome({"node_id": "x", "signoff": "signed", "signed_by": "Wes"}, {"x"}))
        self.assertEqual(canvas._session_outcome({"node_id": "y", "signoff": "rule"}, set()), "by rule")
        self.assertEqual(canvas._session_outcome({"node_id": "z", "status": "duplicate", "duplicate_of": "d0"}, set()), "reused from d0")

    def test_opted_out_person_on_the_task_means_no_record(self):
        self.store.update_settings({"session_records": True})
        wes = self.store.graph.find_person("Wes")
        self.store.set_recording_optout(wes["id"], {"optout": True})
        self.assertTrue(self.store.person(wes["id"])["recording_optout"])
        task, _node = self.signed_task(requester="Wes")
        self.assertEqual(self.finish(task)["status"], "completed")
        self.assertEqual(self.session_rows(), [])
        self.assertEqual(self.store.graph.count_events("session_record_skipped", task), 1)
        # Opting back in, the next finished task is recorded.
        self.store.set_recording_optout(wes["id"], {"optout": False})
        other, _node = self.signed_task(requester="Wes")
        self.finish(other)
        self.assertEqual([r["ref"] for r in self.session_rows()], [self.session_rows()[0]["ref"]])
        self.assertEqual(len(self.session_rows()), 1)

    def test_repeat_finish_is_idempotent(self):
        self.store.update_settings({"session_records": True})
        task, _node = self.signed_task()
        self.finish(task)
        self.finish(task)
        rows = self.session_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.store.graph.db.execute("SELECT count(*) c FROM source_versions WHERE record_id=?",
                                                     (rows[0]["record_id"],)).fetchone()["c"], 1)

    def test_a_failed_write_never_fails_the_finish(self):
        self.store.update_settings({"session_records": True})
        task, _node = self.signed_task()
        with patch.object(self.store, "add_record", side_effect=RuntimeError("disk full")):
            self.assertEqual(self.finish(task)["status"], "completed")
        self.assertEqual(self.session_rows(), [])
        self.assertEqual(self.store.graph.count_events("session_record_failed", task), 1)

    def test_long_summaries_are_clipped_and_say_so(self):
        task, _node = self.signed_task()
        with patch("bridge.canvas.SESSION_RECORD_LIMIT", 160):
            body = canvas.session_summary(self.store, task, DIFF)
        self.assertLessEqual(len(body), 160)
        self.assertTrue(body.endswith("\n[clipped]"))
