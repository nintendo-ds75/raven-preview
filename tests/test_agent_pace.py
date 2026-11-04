"""An open call blocks only the code that needs its answer. raven_wait says
what each waiting node blocks (its path and the nodes that depend on it),
counts the waits that come back with nothing new, and at the cap tells the
agent to stop and report instead of waiting on. Measured on Harborline dry
run 10: every open call came back blocking, the Stop hook sent the agent
back to wait, and it waited 29 times without writing code."""
import json
import os
import unittest
from unittest.mock import patch

from fixtures import OfflineCase
from raven import host_sessions as hs
from raven.mcp import dispatch


class AgentPaceTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.warm_store("qemulike", "pace.db")
        self.addCleanup(self.store.graph.close)
        self.task = self.call("raven_start_task", {"title": "Drop the legacy header from hw/net/virtio-net.c, breaking compat",
                                                    "repo": "qemulike", "paths": "hw/net/virtio-net.c"})["task_id"]

    def call(self, name, args):
        r = dispatch(self.store, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": name, "arguments": args}})
        self.assertFalse(r["result"]["isError"], r["result"]["content"][0]["text"])
        return json.loads(r["result"]["content"][0]["text"])

    def test_waiting_on_names_what_each_open_call_blocks(self):
        first = self.call("raven_add_node", {"task_id": self.task, "paths": "hw/net/virtio-net.c", "client_ref": "a",
                                              "question": "Should hw/net/virtio-net.c drop the legacy header?"})
        self.assertEqual(first["status"], "pending")
        self.assertIn("blocks only the code that needs the answer, so build the rest first", first["next"])
        second = self.call("raven_add_node", {"task_id": self.task, "paths": "hw/net/virtio-net.c", "client_ref": "b",
                                               "question": "Which release note announces the header removal?",
                                               "depends_on": first["node_id"]})
        result = self.call("raven_wait", {"task_id": self.task, "timeout": "0"})
        waiting = {w["node_id"]: w for w in result["waiting_on"]}
        self.assertEqual(waiting[first["node_id"]]["blocks"], [second["node_id"]])
        self.assertEqual(waiting[first["node_id"]]["path"], "hw/net/virtio-net.c")
        self.assertNotIn("blocks", waiting[second["node_id"]])  # nothing depends on it: no empty list
        self.assertIn("each blocks only the code that needs its answer (its path and the nodes in blocks), not the task",
                      result["next"])
        self.assertIn("wait again only when nothing independent is left", result["next"])

    def test_idle_waits_are_counted_and_the_cap_says_stop(self):
        self.call("raven_add_node", {"task_id": self.task, "paths": "hw/net/virtio-net.c", "client_ref": "a",
                                      "question": "Should hw/net/virtio-net.c drop the legacy header?"})
        with patch.dict(os.environ, {"RAVEN_STOP_HOLD_WAITS": "2"}):
            first = self.call("raven_wait", {"task_id": self.task, "timeout": "0"})
            self.assertEqual(first["pace"], {"idle_waits": 1, "idle_wait_cap": 2,
                                             "waiting_since": first["pace"]["waiting_since"]})
            self.assertTrue(first["pace"]["waiting_since"])
            self.assertNotIn("Stop now", first["next"])
            self.assertIn("counted in pace", first["next"])
            self.assertNotIn("1 idle", first["next"])
            second = self.call("raven_wait", {"task_id": self.task, "timeout": "0"})
            self.assertEqual(second["pace"]["idle_waits"], 2)
            self.assertTrue(second["next"].startswith("2 waits in a row returned nothing new (the cap). Stop now and report"))
            self.assertIn("do not wait again", second["next"])
            self.assertEqual(hs.agent_state(self.store.graph.db, self.task), "waiting")
            # Work on the task starts the count over.
            self.call("raven_add_node", {"task_id": self.task, "paths": "hw/net/virtio-net.c", "client_ref": "b",
                                          "question": "Which release note announces the header removal?"})
            self.assertEqual(hs.pace(self.store, self.task)["idle_waits"], 0)
            self.assertEqual(hs.agent_state(self.store.graph.db, self.task), "building")

    def test_a_task_with_nothing_waiting_is_not_an_idle_wait(self):
        result = self.call("raven_wait", {"task_id": self.task, "timeout": "0"})
        self.assertEqual(result["pace"]["idle_waits"], 0)
        self.assertEqual(result["waiting_on"], [])

    def test_the_cap_is_configured_and_off_by_default(self):
        with patch.dict(os.environ, {"RAVEN_STOP_HOLD_WAITS": "2"}):
            self.assertEqual(hs._wait_cap(), 2)
        with patch.dict(os.environ, {"RAVEN_STOP_HOLD_WAITS": "-3"}):
            self.assertEqual(hs._wait_cap(), 0)
        with patch.dict(os.environ, {"RAVEN_STOP_HOLD_WAITS": "nonsense"}):
            self.assertEqual(hs._wait_cap(), 0)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RAVEN_STOP_HOLD_WAITS", None)
            self.assertEqual(hs._wait_cap(), 0)

    def test_without_a_cap_the_agent_is_held_and_told_to_wait_again(self):
        """Measured on the Parcelwise take 4 (Nov 2): with a cap of 6 the
        agent stopped after five minutes, nothing resumed a plain
        interactive session, and the requester had to type "everyone has
        answered". By default the hold lasts until the people answer."""
        self.call("raven_add_node", {"task_id": self.task, "paths": "hw/net/virtio-net.c", "client_ref": "a",
                                      "question": "Should hw/net/virtio-net.c drop the legacy header?"})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RAVEN_STOP_HOLD_WAITS", None)
            os.environ.pop("RAVEN_STOP_HOLD_MINUTES", None)
            for n in range(1, 9):
                result = self.call("raven_wait", {"task_id": self.task, "timeout": "0"})
                self.assertEqual((result["pace"]["idle_waits"], result["pace"]["idle_wait_cap"]), (n, 0))
                self.assertNotIn("Stop now", result["next"])
                self.assertIn("call raven_wait again with since, do not stop to report", result["next"])
            self.store.graph.db.execute(
                "INSERT INTO host_sessions(id,principal,host,session_id,project,repo,task_id,created_at,updated_at) "
                "VALUES('b1','agent','claude','s1','/p','qemulike',?,'2026-11-02T20:00:00+00:00','2026-11-02T20:00:00+00:00')",
                (self.task,))
            held = hs.stop(self.store, {"id": "b1", "task_id": self.task})
            self.assertTrue(held["hold"], held)
            self.assertEqual(held["idle_wait_cap"], 0)
            self.assertIn("8 wait(s) in a row returned nothing new", held["reason"])
            self.assertIn("held until they answer", held["reason"])
            self.assertNotIn("more allowed", held["reason"])
            self.assertNotIn("you may stop", held["reason"])
            self.assertEqual(self.store.graph.db.execute(
                "SELECT count(*) FROM events WHERE run_id=? AND kind='host_stopped_waiting'", (self.task,)).fetchone()[0], 0)
        with patch.dict(os.environ, {"RAVEN_STOP_HOLD_MINUTES": "0"}):
            self.assertFalse(hs.stop(self.store, {"id": "b1", "task_id": self.task})["hold"])
            why = [json.loads(r["detail"])["why"] for r in self.store.graph.db.execute(
                "SELECT detail FROM events WHERE run_id=? AND kind='host_stopped_waiting'", (self.task,))]
            self.assertEqual(why, ["hold budget"])


if __name__ == "__main__":
    unittest.main()
