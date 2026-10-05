import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bridge import canvas
from bridge.mcp import HANDLERS, dispatch
from fixtures import ready_server as make_server
from bridge.store import Invalid, Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.db")
        self.owner = self.store.add_owner({"name": "Wes", "team": "Data", "patterns": "billing/*"})
        self.run = self.store.add_run({"title": "Add usage pricing"})

    def request(self, **changes):
        return self.store.request({"run_id": self.run["id"], "question": "Should test traffic be billed?", "context": "An 11x spike during a load test", "path": "billing/usage.py", **changes})

    def test_owner_routing_pending_answer_persistence(self):
        question = self.request()
        self.assertEqual(question["owner_id"], self.owner["id"])
        self.assertTrue(question["approval_pending"])
        result = self.store.answer(question["id"], {"answer": "Exclude tagged test traffic", "rationale": "The load test was internal"})
        self.assertFalse(result["approval_pending"])
        self.assertEqual(result["answered_by"], "Wes")
        reopened = Store(self.store.path)
        self.assertEqual(reopened.get_decision(question["id"])["answer"], result["answer"])
        self.assertEqual(reopened.state()["runs"][0]["status"], "working")

    def test_all_pending_decisions_must_resolve(self):
        first, second = self.request(), self.request(question="Who owns the pricing migration?")
        self.store.answer(first["id"], {"answer": "Exclude"})
        self.assertEqual(self.store.state()["runs"][0]["status"], "needs_judgment")
        with self.assertRaises(Invalid):
            self.store.update_run(self.run["id"], {"status": "completed"})
        self.store.answer(second["id"], {"answer": "Pricing team"})
        self.store.update_run(self.run["id"], {"status": "completed"})
        with self.assertRaises(Invalid):
            self.request()

    def test_predictions_never_approve_and_corrections_withdraw_stale_suggestions(self):
        prior = self.request()
        self.store.answer(prior["id"], {"answer": "Exclude tagged traffic"})
        later = self.request()
        self.assertEqual(later["prediction"], "Exclude tagged traffic")
        self.assertIsNone(later["answer"])
        self.assertTrue(later["approval_pending"])
        self.store.answer(prior["id"], {"answer": "Exclude only verified internal tests"})
        self.assertIsNone(self.store.get_decision(later["id"])["prediction"])
        events = self.store.get_decision(prior["id"])["events"]
        self.assertTrue(any(e["kind"] == "previous_answer" and "Exclude tagged traffic" in e["detail"] for e in events))
        self.assertEqual(self.store.search("test traffic billed")["matches"][0]["answer"], "Exclude only verified internal tests")

    def test_unassigned_request_needs_valid_owner(self):
        question = self.request(path="unknown/file.py")
        self.assertIsNone(question["owner_id"])
        with self.assertRaises(Invalid):
            self.store.answer(question["id"], {"answer": "Proceed"})
        with self.assertRaises(Invalid):
            self.store.assign(question["id"], {"owner_id": "missing"})
        self.store.assign(question["id"], {"owner_id": self.owner["id"]})
        self.store.answer(question["id"], {"answer": "Proceed"})

    def test_routing_precedence_and_reassignment(self):
        other = self.store.add_owner({"name": "Marisol", "team": "Pricing", "patterns": "billing/usage.py"})
        question = self.request()
        self.assertEqual(question["owner_id"], other["id"])
        self.assertEqual(self.request(owner_id=self.owner["id"])["owner_id"], self.owner["id"])
        self.store.answer(question["id"], {"answer": "Exclude"})
        candidate = self.request()
        result = self.store.assign(candidate["id"], {"owner_id": self.owner["id"]})
        self.assertIsNone(result["prediction"])

    def test_concurrent_stale_answer_rejected(self):
        question = self.request()
        def answer(value):
            try:
                self.store.answer(question["id"], {"answer": value, "expected_updated_at": question["updated_at"]})
                return True
            except Invalid:
                return False
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(answer, ["Include", "Exclude"]))
        self.assertEqual(sorted(results), [False, True])

    def test_validation_does_not_create_partial_decisions(self):
        for change in [{"question": " "}, {"context": []}, {"run_id": "missing"}, {"owner_id": "missing"}, {"path": 123}]:
            with self.assertRaises(Invalid):
                self.request(**change)
        self.assertEqual(self.store.state()["decisions"], [])

    def test_explicit_demo_after_local_login(self):
        store = Store(Path(self.temp.name) / "later-demo.db")
        store.graph.add_person("Local developer", email="developer@bridge.local", role="admin")
        self.assertEqual(len(store.state()["runs"]), 0)
        store.seed()
        self.assertEqual(len(store.state()["runs"]), 5)

    def test_the_demo_seed_shows_a_verdict_a_tree_and_a_signoff(self):
        """--demo on an empty database: the three owners, the answered
        prior, three open questions, and one canvas task with its verdict,
        a node the agent settled (sign-off wanted) and a follow-up a person
        added under it. Seeding twice adds nothing."""
        store = Store(Path(self.temp.name) / "demo.db")
        store.seed()
        store.seed()
        state = store.state()
        self.assertEqual(len(state["owners"]), 3)
        self.assertEqual(len(state["runs"]), 5)
        task = next(r for r in state["runs"] if r["title"] == "Move the nightly backup job to the new scheduler")
        self.assertEqual((task["verdict"], task["requester"]), ("pass", "Priya Natarajan"))
        by_status = {}
        for d in state["decisions"]:
            by_status.setdefault(d["status"], []).append(d)
        self.assertEqual({k: len(v) for k, v in by_status.items()}, {"pending": 3, "approved": 1, "resolved": 1, "suggested": 1})
        settled = by_status["resolved"][0]
        self.assertEqual((settled["kind"], settled["signoff"], settled["owner_name"]), ("agent", "required", "Alex Morgan"))
        followup = by_status["suggested"][0]
        self.assertEqual((followup["parent_id"], followup["origin"], followup["depth"]), (settled["id"], "human", 1))
        self.assertTrue(all(d["prediction"] is None for d in by_status["pending"] if d["owner_name"] != "Marisol Vega"))

    def test_full_export_retains_more_than_activity_limit(self):
        before = len(self.store.state(full_history=True)["events"])
        with self.store.connect() as db:
            for i in range(110):
                self.store.event(db, "test", str(i))
        self.assertEqual(len(self.store.state()["events"]), 100)
        self.assertEqual(len(self.store.state(full_history=True)["events"]), before + 110)

    def test_the_polled_payload_is_bounded_and_the_export_is_not(self):
        with self.store.connect() as db:
            db.executemany("INSERT INTO runs(id, title, agent, repo, status, updated_at) VALUES(?,?,?,?,?,?)",
                           [(f"r{i:04d}", "t", "a", "local", "working", "2000-01-01T00:00:00+00:00") for i in range(520)])
        state = self.store.state()
        self.assertEqual(len(state["runs"]), 500)
        self.assertEqual(state["runs"][0]["id"], self.run["id"])
        self.assertEqual(len(self.store.state(full_history=True)["runs"]), 521)

    def test_mcp_tools_do_not_offer_agent_approval(self):
        response = dispatch(self.store, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = [t["name"] for t in response["result"]["tools"]]
        self.assertIn("bridge_import_record", names)
        self.assertIn("bridge_connection_status", names)
        self.assertEqual(names, list(HANDLERS))
        self.assertFalse(any("approve" in name for name in names))
        response = dispatch(self.store, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "bridge_approve", "arguments": {}}})
        self.assertTrue(response["result"]["isError"])
        response = dispatch(self.store, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "bridge_get_tree", "arguments": {"task_id": 7}}})
        self.assertTrue(response["result"]["isError"])
        self.assertIn("task_id", response["result"]["content"][0]["text"])

    def test_any_tool_failure_is_an_error_result_under_the_request_id(self):
        def boom(store, args):
            raise RuntimeError("disk on fire")
        stderr = io.StringIO()
        with patch.dict(HANDLERS, {"bridge_get_tree": boom}), redirect_stderr(stderr):
            response = dispatch(self.store, {"jsonrpc": "2.0", "id": 41, "method": "tools/call", "params": {"name": "bridge_get_tree", "arguments": {"task_id": "x"}}})
        self.assertEqual(response["id"], 41)
        self.assertNotIn("error", response)
        self.assertTrue(response["result"]["isError"])
        self.assertIn("disk on fire", response["result"]["content"][0]["text"])
        self.assertIn("disk on fire", stderr.getvalue())

    def test_a_path_item_over_the_cap_is_refused(self):
        long_path = "a" * 600
        with self.assertRaises(Invalid):
            canvas._paths(long_path)
        with self.assertRaises(Invalid):
            canvas._options(["ok", long_path])
        self.assertEqual(canvas._paths("a" * 500 + ", /b"), ["a" * 500, "b"])
        response = dispatch(self.store, {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "bridge_start_task", "arguments": {"title": "t", "repo": "r", "paths": long_path}}})
        self.assertTrue(response["result"]["isError"])
        self.assertIn("paths", response["result"]["content"][0]["text"])
        self.assertEqual(len(self.store.state()["runs"]), 1)

    def test_stdio_round_trip(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "bridge_start_task", "arguments": {"title": "Add usage pricing", "repo": "acme/platform", "agent": "test"}}},
            {"jsonrpc": "2.0", "id": 9, "method": "ping", "params": {"padding": "x" * (1 << 20)}},
        ]
        env = {**os.environ, "BRIDGE_SEMANTIC": "0", "BRIDGE_LIVE": "0"}
        env.pop("ANTHROPIC_API_KEY", None)
        completed = subprocess.run([sys.executable, "-m", "bridge", "mcp", "--db", self.store.path], input="\n".join(json.dumps(m) for m in messages) + "\n", capture_output=True, text=True, timeout=10, check=True, env=env)
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(len(responses), 3)
        task = json.loads(responses[1]["result"]["content"][0]["text"])
        self.assertEqual(responses[2], {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        message = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "bridge_add_node", "arguments": {"task_id": task["task_id"], "question": "Bill test traffic?", "context": "Internal test", "paths": "billing/test.py"}}}
        completed = subprocess.run([sys.executable, "-m", "bridge", "mcp", "--db", self.store.path], input=json.dumps(message) + "\n", capture_output=True, text=True, timeout=10, check=True, env=env)
        node = json.loads(json.loads(completed.stdout)["result"]["content"][0]["text"])
        self.assertEqual(node["owner"], "Wes")
        self.assertEqual(node["status"], "pending")
        self.store.answer(node["node_id"], {"answer": "Exclude test traffic"})
        message = {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "bridge_get_decision", "arguments": {"decision_id": node["node_id"]}}}
        response = dispatch(self.store, message)
        self.assertEqual(json.loads(response["result"]["content"][0]["text"])["answer"], "Exclude test traffic")
        tree = json.loads(dispatch(self.store, {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "bridge_get_tree", "arguments": {"task_id": task["task_id"]}}})["result"]["content"][0]["text"])
        self.assertEqual(tree["nodes"][0]["status"], "answered")


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.store = Store(Path(cls.temp.name) / "http.db")
        cls.server = make_server(cls.store, 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def test_state_static_and_guarded_writes(self):
        with urlopen(self.url + "/api/state") as response:
            state = json.load(response)
        token = state["csrf_token"]
        mcp = state["mcp_config"]["mcpServers"]["bridge"]
        self.assertEqual(mcp, {"type": "http", "url": self.url + "/mcp"})
        request = Request(mcp["url"], data=json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode(),
            headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            self.assertEqual(json.load(response)["result"]["protocolVersion"], "2025-06-18")
        with self.assertRaises(HTTPError) as removed:
            urlopen(self.url + "/api/mcp/tools")
        self.assertEqual(removed.exception.code, 404)
        removed.exception.close()
        old_call = Request(self.url + "/api/mcp/call", data=b'{}',
                           headers={"Content-Type": "application/json", "X-Bridge-CSRF": token})
        with self.assertRaises(HTTPError) as removed:
            urlopen(old_call)
        self.assertEqual(removed.exception.code, 404)
        removed.exception.close()
        with urlopen(self.url) as response:
            self.assertIn(b"Judgment inbox", response.read())
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            self.assertIn("connect-src 'self' http://127.0.0.1:7334;", response.headers["Content-Security-Policy"])
            self.assertNotIn("127.0.0.1:*", response.headers["Content-Security-Policy"])
        for logo in ('claude', 'cursor', 'openai'):
            with urlopen(self.url + '/' + logo + '.svg') as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b'<svg', response.read())
        request = Request(self.url + "/api/owners", data=b'{"name":"Alex","team":"Infra"}', headers={"Content-Type": "application/json", "X-Bridge-CSRF": token})
        with urlopen(request) as response:
            self.assertEqual(json.load(response)["name"], "Alex")
        for headers in [{"Content-Type": "application/json"}, {"Host": "evil.example"}, {"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}]:
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(self.url + "/api/owners", data=b'{}', headers=headers))
            self.assertEqual(error.exception.code, 403)

    def test_malformed_json_and_unknown_paths(self):
        with urlopen(self.url + "/api/state") as response:
            token = json.load(response)["csrf_token"]
        for body in [b'not json', b'[]']:
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(self.url + "/api/runs", data=body, headers={"Content-Type": "application/json", "X-Bridge-CSRF": token}))
            self.assertEqual(error.exception.code, 400)
        with self.assertRaises(HTTPError) as error:
            urlopen(self.url + "/../bridge/store.py")
        self.assertEqual(error.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
