import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bridge.config import Config
from bridge.execution import ExecutionService
from bridge.store import Invalid, Store

OFFLINE = Config(model_api="none")


class FakeAPI:
    def __init__(self):
        self.sessions = {}
        self.saved_items = {}
        self.saved_turns = {}
        self.sent = []
        self.accept_then_timeout = False
        self.timeout_launch = False

    def start_task(self, **kwargs):
        sid = "session_" + str(len(self.sessions) + 1)
        self.sessions[sid] = {"id": sid, "status": "in_progress", "required_actions": [],
                              "metadata": kwargs["metadata"], "environment": {"type": "openai_hosted"}}
        self.saved_items[sid] = []
        self.saved_turns[sid] = [{"id": "turn_" + sid, "status": "in_progress", "subagent_id": None}]
        if self.timeout_launch:
            raise TimeoutError("Accepted, but connection lost")
        return copy.deepcopy(self.sessions[sid])

    def retrieve_session(self, sid):
        return copy.deepcopy(self.sessions[sid])

    def find_sessions(self, metadata):
        return [copy.deepcopy(s) for s in self.sessions.values() if s["metadata"] == metadata]

    def items(self, sid):
        return copy.deepcopy(self.saved_items[sid])

    def turns(self, sid):
        return copy.deepcopy(self.saved_turns[sid])

    def artifacts(self, sid):
        return []

    def send_events(self, sid, events, key):
        self.sent.append((sid, events, key))
        for event in events:
            if event["type"] == "agent.session.input.tool_result":
                self.saved_items[sid].append({"id": key, "type": "function_call_output", "turn_id": event["turn_id"],
                    "call_id": event["call_id"], "output": event.get("output"), "error": event.get("error")})
                self.sessions[sid]["required_actions"] = [a for a in self.sessions[sid]["required_actions"]
                    if (a.get("turn_id"), a.get("call_id")) != (event["turn_id"], event["call_id"])]
            else:
                self.saved_items[sid].append({"id": key, "type": "message", "role": "user", **event["input"][0]})
        if self.accept_then_timeout:
            raise TimeoutError("Accepted, but connection lost")

    def ask(self, sid, call_id="call_one", **changes):
        action = {"type": "function_call", "turn_id": "turn_" + sid, "call_id": call_id,
            "name": "request_judgment", "arguments": {
                "question": "Bill this load-test spike?", "context": "The new spike is not covered by policy.",
                "path": "billing/usage.py", "evidence": "usage.json: test_run september-13, 11x spike",
                "options": "Bill all traffic or exclude verified test traffic; revenue differs.",
                "blocked_work": "Treatment of the spike", "independent_work": "Aggregation tests"}, **changes}
        self.sessions[sid]["required_actions"].append(action)
        self.sessions[sid]["status"] = "requires_action"
        return action


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.db")
        self.store.add_owner({"name": "Fixture owner", "team": "Billing", "patterns": "billing/*"})
        self.api = FakeAPI()
        self.service = ExecutionService(self.store, self.api, cfg=OFFLINE)

    def launch(self, key="submission-one"):
        result = self.service.submit({"task": "Add usage-based pricing", "repository": "billing-fixture", "submission_key": key})
        self.service.tick()
        return self.service.get(result["id"])

    def question(self, run):
        self.api.ask(run["session_id"])
        self.service.tick()
        return self.store.state()["decisions"][-1]

    def answer(self, decision, value="Exclude only verified internal test traffic"):
        return self.store.answer(decision["id"], {"answer": value, "rationale": "Synthetic fixture policy",
                                               "expected_updated_at": decision["updated_at"]})

    def scoped_followup(self):
        first = self.launch()
        original = self.question(first)
        self.store.graph.db.execute('UPDATE decisions SET facts=? WHERE id=?',
                                    (json.dumps({'customer': 'acme'}), original['id']))
        self.answer(original)
        self.service.tick()
        second = self.launch(key='second-scoped-task')
        action = self.api.ask(second['session_id'])
        self.service.tick()
        outputs = [e for sid, events, _ in self.api.sent if sid == second['session_id'] for e in events]
        clarification = json.loads(outputs[-1]['output'])
        return second, action, clarification

    def test_scope_clarification_returns_to_managed_host_and_explicit_retry_clears_it(self):
        run, action, result = self.scoped_followup()
        self.assertEqual(result['status'], 'needs_scope_clarification')
        self.assertIsNone(self.service.get(run['run_id'])['last_error'])
        args = {**action['arguments'], 'facts': 'customer=acme', 'scope_request': result['request_key']}
        self.api.ask(run['session_id'], call_id='clarified', arguments=args)
        self.service.tick()
        self.assertFalse(self.store.graph.db.execute('SELECT 1 FROM scope_clarifications WHERE task_id=?',
                                                      (run['run_id'],)).fetchone())
        node = self.store.graph.db.execute('SELECT * FROM decisions WHERE run_id=?', (run['run_id'],)).fetchone()
        self.assertIsNotNone(node)
        self.assertNotEqual(node['signoff'], 'signed')

    def test_unresolved_scope_prevents_managed_result_ready(self):
        run, _, result = self.scoped_followup()
        self.assertEqual(result['status'], 'needs_scope_clarification')
        self.api.saved_turns[run['session_id']][-1]['status'] = 'completed'
        self.service.tick()
        self.assertEqual(self.service.get(run['run_id'])['status'], 'review_required')

    def test_concurrent_submission_deduplicates_and_rejects_key_reuse(self):
        data = {"task": "Add usage-based pricing", "repository": "billing-fixture", "submission_key": "one"}
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(self.service.submit, [data] * 4))
        self.assertEqual(len({r["id"] for r in results}), 1)
        with self.assertRaises(Invalid):
            self.service.submit({**data, "task": "A different task"})
        self.service.tick()
        self.assertEqual(len(self.api.sessions), 1)

    def test_launch_timeout_recovers_without_second_session(self):
        self.api.timeout_launch = True
        run = self.launch()
        self.assertIsNone(run["session_id"])
        self.service.tick()
        self.assertEqual(len(self.api.sessions), 1)
        self.assertEqual(self.service.get(run["run_id"])["launch_state"], "linked")

    def test_unrecoverable_launch_never_relaunches(self):
        run = self.launch()
        self.service.update(run["run_id"], session_id=None, launch_state="launching")
        self.api.sessions.clear()
        self.service.tick()
        self.service.tick()
        self.assertEqual(len(self.api.sessions), 0)
        self.assertEqual(self.service.get(run["run_id"])["status"], "recovery_required")

    def test_pending_call_survives_restart_and_answer_is_queued_atomically(self):
        run = self.launch()
        question = self.question(run)
        self.service.tick()
        self.assertEqual(len(self.store.state()["decisions"]), 1)
        self.assertEqual(self.api.sent, [])
        self.assertIn("Evidence:", question["context"])
        self.store = Store(self.store.path)
        self.service = ExecutionService(self.store, self.api, cfg=OFFLINE)
        answer = self.answer(question)
        self.assertEqual(self.service.get(run["run_id"])["status"], "needs_judgment")
        self.assertEqual(self.store.state()["deliveries"][0]["state"], "queued")
        self.assertEqual(answer["revision"]["provenance"], "recorded by local operator")
        self.service = ExecutionService(Store(self.store.path), self.api, cfg=OFFLINE)
        self.service.tick()
        self.assertEqual(self.store.state()["deliveries"][0]["state"], "delivered")
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(self.api.sent[0][0], run["session_id"])

    def test_accepted_result_timeout_reconciles_without_replay(self):
        run = self.launch()
        self.answer(self.question(run))
        self.api.accept_then_timeout = True
        self.service.tick()
        self.assertEqual(self.store.state()["deliveries"][0]["state"], "uncertain")
        self.service = ExecutionService(Store(self.store.path), self.api, cfg=OFFLINE)
        self.service.tick()
        self.assertEqual(self.store.state()["deliveries"][0]["state"], "delivered")
        self.assertEqual(len(self.api.sent), 1)

    def test_correction_supersedes_queued_revision(self):
        run = self.launch()
        answer = self.answer(self.question(run), "Bill all usage")
        self.answer(answer, "Exclude the tagged spike")
        self.service.tick()
        self.assertEqual([j["state"] for j in self.store.state()["deliveries"]], ["superseded", "delivered"])
        output = json.loads(self.api.sent[0][1][0]["output"])
        self.assertEqual(output["answer"], "Exclude the tagged spike")

    def test_correction_after_delivery_queues_recheck_and_retains_sent_revision(self):
        run = self.launch()
        answer = self.answer(self.question(run), "Bill all usage")
        self.service.tick()
        self.answer(answer, "Exclude the tagged spike")
        self.service.tick()
        jobs = self.store.state()["deliveries"]
        self.assertEqual([j["kind"] for j in jobs], ["result", "correction"])
        self.assertIn('Bill all usage', jobs[0]["payload"])
        self.assertEqual(self.service.get(run["run_id"])["review_required"], 1)

    def test_completed_session_is_not_restarted_on_correction(self):
        run = self.launch()
        answer = self.answer(self.question(run))
        self.service.tick()
        self.api.saved_turns[run["session_id"]][0]["status"] = "completed"
        self.api.sessions[run["session_id"]]["status"] = "idle"
        self.service.tick()
        self.answer(answer, "Recheck the result")
        self.service.tick()
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(self.store.state()["deliveries"][-1]["state"], "stopped")
        self.assertEqual(self.service.get(run["run_id"])["status"], "review_required")

    def test_cancelled_session_stays_cancelled_when_answer_arrives(self):
        run = self.launch()
        question = self.question(run)
        self.api.saved_turns[run["session_id"]][0]["status"] = "cancelled"
        self.api.sessions[run["session_id"]]["status"] = "idle"
        self.service.tick()
        self.answer(question)
        self.service.tick()
        self.assertEqual(self.service.get(run["run_id"])["status"], "cancelled")
        self.assertEqual(self.api.sent, [])

    def test_environment_and_unknown_tool_do_not_create_questions(self):
        run = self.launch()
        self.api.sessions[run["session_id"]]["required_actions"] = [{"type": "environment_connection", "environment_id": "env_one"}]
        self.service.tick()
        self.assertEqual(self.store.state()["decisions"], [])
        self.api.ask(run["session_id"], name="approve_answer", arguments={"answer": "Yes"})
        self.service.tick()
        self.assertFalse(self.api.sent[0][1][0]["success"])
        self.assertEqual(self.store.state()["decisions"], [])

    def test_invalid_arguments_cannot_supply_owner_or_run(self):
        run = self.launch()
        self.api.ask(run["session_id"], arguments={"run_id": "another", "owner_id": "fake"})
        self.service.tick()
        self.assertEqual(self.store.state()["decisions"], [])
        self.assertFalse(self.api.sent[0][1][0]["success"])

    def test_multiple_questions_and_sessions_are_isolated(self):
        first, second = self.launch(), self.launch("submission-two")
        self.api.ask(first["session_id"], "call_a")
        self.api.ask(first["session_id"], "call_b")
        self.api.ask(second["session_id"], "call_a")
        self.service.tick()
        question = next(d for d in self.store.state()["decisions"] if d["run_id"] == first["run_id"])
        self.answer(question)
        self.service.tick()
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(self.api.sent[0][0], first["session_id"])
        self.assertEqual(self.service.get(first["run_id"])["status"], "needs_judgment")
        self.assertEqual(self.service.get(second["run_id"])["status"], "needs_judgment")

    def test_managed_status_cannot_be_overridden_by_mcp(self):
        run = self.launch()
        with self.assertRaises(Invalid):
            self.store.update_run(run["run_id"], {"status": "completed"})

    def test_managed_reviews_require_concurrency_token(self):
        run = self.launch()
        question = self.question(run)
        with self.assertRaises(Invalid):
            self.store.answer(question["id"], {"answer": "Do not proceed"})
        self.assertEqual(self.store.state()["deliveries"], [])

    def test_correction_during_send_preserves_old_result_and_queues_new_revision(self):
        run = self.launch()
        answer = self.answer(self.question(run), "First answer")
        original = self.api.send_events
        def race(sid, events, key):
            self.answer(answer, "Corrected while the old answer was sending")
            original(sid, events, key)
        self.api.send_events = race
        self.service.tick()
        jobs = self.store.state()["deliveries"]
        self.assertEqual([j["state"] for j in jobs], ["delivered", "queued"])
        self.assertIn("First answer", jobs[0]["payload"])
        self.assertIn("Corrected while", jobs[1]["payload"])

    def test_worker_lock_prevents_duplicate_dispatchers(self):
        service = ExecutionService(self.store, self.api, cfg=OFFLINE)
        service.start()
        self.addCleanup(service.close)
        with self.assertRaises(Invalid):
            ExecutionService(self.store, self.api, cfg=OFFLINE).start()

    def test_events_are_deduplicated_and_retrieval_is_authoritative(self):
        run = self.launch()
        event = {"id": "event-one", "type": "agent.session.idle", "data": {"id": run["session_id"]}}
        self.service.receive_event(event)
        self.service.receive_event(event)
        self.service.tick()
        self.assertEqual(self.service.get(run["run_id"])["status"], "working")
        with self.store.connect() as db:
            events = db.execute("SELECT * FROM provider_events").fetchall()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["state"], "processed")


class ManagedCanvasTests(unittest.TestCase):
    """Managed execution is an adapter on the canvas: the same kickoff,
    the same nodes, the same contract as an MCP agent."""

    setUp = ExecutionTests.setUp
    launch = ExecutionTests.launch
    question = ExecutionTests.question
    answer = ExecutionTests.answer

    def test_a_submitted_task_is_kicked_off_on_the_canvas(self):
        run = self.launch()
        row = self.store.graph.db.execute("SELECT * FROM runs WHERE id=?", (run["run_id"],)).fetchone()
        # A managed task names no paths, so a task whose words name no area
        # is unplaced rather than passed; the verdict is on the run either way.
        self.assertIn(row["verdict"], ("engage", "pass", "unplaced"))
        self.assertTrue(row["verdict_why"])
        self.assertEqual(row["client_key"], "managed:submission-one")
        kinds = [r["kind"] for r in self.store.graph.db.execute("SELECT kind FROM events WHERE run_id=?", (run["run_id"],))]
        self.assertIn("task_started", kinds)

    def test_a_question_is_a_node_through_the_ladder_and_a_signature_delivers_it(self):
        from bridge import canvas
        run = self.launch()
        question = self.question(run)
        node = canvas.node_view(self.store, question["id"])
        self.assertEqual(node["task_id"], run["run_id"])
        self.assertEqual(node["client_ref"], f"turn_{run['session_id']}:call_one")
        self.assertEqual(node["status"], "pending")
        self.assertEqual(node["owner"], "Fixture owner")
        # The provider asking twice for the same call is one node, one call.
        self.api.ask(run["session_id"])
        self.service.tick()
        self.assertEqual(len(self.store.state()["decisions"]), 1)
        # The agent settling the node itself is not an answer for the
        # hosted agent: it waits for the owner's signature, which delivers.
        canvas.settle_node(self.store, {"task_id": run["run_id"], "node_id": question["id"],
                                        "answer": "Exclude verified test traffic", "rationale": "policy"})
        self.service.tick()
        self.assertEqual(self.service.get(run["run_id"])["status"], "needs_judgment")
        self.assertEqual(self.store.state()["deliveries"], [])
        current = self.store.get_decision(question["id"])
        canvas.sign_off(self.store, question["id"], {"by": "Fixture owner", "expected_updated_at": current["updated_at"]})
        jobs = self.store.state()["deliveries"]
        self.assertEqual([j["state"] for j in jobs], ["queued"])
        self.assertIn("Exclude verified test traffic", jobs[0]["payload"])
        self.assertIn("signed by Fixture owner", jobs[0]["payload"])
        self.service.tick()
        self.assertEqual(self.store.state()["deliveries"][0]["state"], "delivered")
        self.assertEqual(self.service.get(run["run_id"])["status"], "working")

    def test_a_duplicate_question_in_another_session_is_settled_by_one_answer(self):
        first = self.launch("one")
        second = self.launch("two")
        self.api.ask(first["session_id"])
        self.api.ask(second["session_id"])
        self.service.tick()
        decisions = self.store.state()["decisions"]
        statuses = sorted(d["status"] for d in decisions)
        self.assertEqual(statuses, ["duplicate", "pending"])
        canonical = next(d for d in decisions if d["status"] == "pending")
        self.answer(canonical)
        self.service.tick()
        self.assertEqual(sorted(s for (_, _, s) in [(j["kind"], j["state"], j["state"]) for j in self.store.state()["deliveries"]]),
                         ["delivered", "delivered"])
        self.assertEqual({s for (s, _, _) in self.api.sent}, {first["session_id"], second["session_id"]})

    def test_a_configured_checkout_is_packaged_from_what_git_tracks(self):
        import subprocess
        from bridge.execution import checkout_config, parse_repositories
        root = Path(self.temp.name) / "checkout"
        root.mkdir()
        env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "a",
               "GIT_COMMITTER_EMAIL": "a@x", "HOME": str(root)}
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, env=env)
        (root / "src").mkdir()
        (root / "src" / "app.py").write_text("print('hi')\n")
        (root / "logo.png").write_bytes(b"\x89PNG\x00\x00binary")
        (root / "untracked.txt").write_text("not committed\n")
        subprocess.run(["git", "-C", str(root), "add", "src/app.py", "logo.png"], check=True, env=env)
        subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "baseline"], check=True, env=env)
        repos = parse_repositories([f"app={root}"])
        self.assertEqual([r["id"] for r in repos], ["billing-fixture", "app"])
        config = checkout_config("gpt-6-astra", repos[1])
        paths = [f["path"] for f in config["environment"]["files"]]
        self.assertEqual(paths, ["/workspace/repo/src/app.py"])
        self.assertEqual(config["skipped"], ["logo.png"])
        self.assertEqual(config["repository"], "app")
        for bad in ("app", "app=/nonexistent/path", "billing-fixture=" + str(root)):
            with self.assertRaises(Invalid):
                parse_repositories([bad])
        service = ExecutionService(self.store, self.api, repositories=repos, cfg=OFFLINE)
        result = service.submit({"task": "Add a greeting", "repository": "app", "submission_key": "app-one"})
        self.assertEqual(json.loads(service.get(result["id"])["config"])["repository"], "app")
        with self.assertRaises(Invalid):
            service.submit({"task": "x", "repository": "other", "submission_key": "app-two"})
