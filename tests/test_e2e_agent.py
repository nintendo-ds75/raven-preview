"""The loop the site describes, end to end, with a scripted host agent
speaking MCP to Raven: kickoff once, a question reaches its owner in
Slack, the answer comes back to the same task while the agent waits,
the change follows the answer, sign-off still gates finishing. Then the
things that go wrong on a real day: the agent restarts, the reply comes
hours late, an answer is corrected after another task reused it, the
owner cannot be reached. Each has an operator-visible recovery.

The agent is the JSON-RPC client of bridge/mcp.py: in process for the
scenarios (a fake Slack stands in for the network), as a real stdio
subprocess for the restart, and over HTTP for the shared Raven's
bounded wait."""

import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from fixtures import OfflineCase, git_env, run_git, workdir

from bridge import canvas
from bridge.config import Config
from bridge.delivery import handle_slack_event
from bridge.ingest import index_repo
from bridge.mcp import dispatch
from fixtures import ready_server as make_server
from bridge.store import Invalid, Store
from test_delivery import FakeSlack

ROOT = Path(__file__).resolve().parent.parent
CFG = Config(model_api="none")
REPO = "acme/ledger"
PRIYA = ("Priya Natarajan", "priya@acme.example")
MARISOL = ("Marisol Vega", "marisol@acme.example")
WES = ("Wes Chen", "wes@acme.example")


def build_ledger() -> Path:
    """A small repository with two areas: billing (Priya's) and api
    (Marisol's), enough history for git to agree with the map."""
    repo = workdir() / "ledger"
    if (repo / ".built").exists():
        return repo
    repo.mkdir(exist_ok=True)
    run_git(repo, "init", "-q", "-b", "main")
    files = {"billing/rates.py": "RATE = 0.01\n", "billing/invoice.py": "def total(): return 0\n",
             "api/routes.py": "ROUTES = []\n", "README.md": "# ledger\n"}
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-q", "-m", "baseline", env=git_env(WES, 0))
    for day in range(1, 5):
        (repo / "billing/rates.py").write_text(f"RATE = 0.0{day + 1}\n")
        run_git(repo, "commit", "-q", "-am", f"billing: rate change {day}\n\nReviewed-by: {WES[0]} <{WES[1]}>",
                env=git_env(PRIYA, day))
        (repo / "api/routes.py").write_text(f"ROUTES = [{day}]\n")
        run_git(repo, "commit", "-q", "-am", f"api: route {day}", env=git_env(MARISOL, day))
    (repo / ".built").write_text("")
    return repo


class ToolError(Exception):
    pass


class Agent:
    """A scripted host agent: MCP JSON-RPC over dispatch()."""

    def __init__(self, store):
        self.store = store
        self.n = 0

    def rpc(self, method, params=None):
        self.n += 1
        return dispatch(self.store, {"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})

    def call(self, name, **args):
        response = self.rpc("tools/call", {"name": name, "arguments": args})
        result = response["result"]
        text = result["content"][0]["text"]
        if result["isError"]:
            raise ToolError(text)
        return json.loads(text)


def thread_reply(channel, thread_ts, user, text, event_id):
    return {"type": "event_callback", "event_id": event_id,
            "event": {"type": "message", "channel": channel, "thread_ts": thread_ts, "user": user, "text": text}}


class E2ECase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.repo = build_ledger()
        self.store = Store(Path(self.temp.name) / "e2e.db")
        self.graph = self.store.graph
        index_repo(self.graph, self.repo, repo_name=REPO)
        self.slack = FakeSlack()
        self.delivery = self.store.connect_delivery(self.slack, base_url="https://bridge.acme.test")
        with self.graph.transaction():
            self.priya = self.graph.add_person(*PRIYA, slack_id="UPRI")
            self.marisol = self.graph.add_person(*MARISOL)
            self.wes = self.graph.add_person(*WES, slack_id="UWES")
            self.graph.add_authority("path", "billing/*", "decides", person_id=self.priya, repo=REPO,
                                     source="config", asserted_by="test", accepted=True)
            self.graph.add_authority("path", "api/*", "decides", person_id=self.marisol, repo=REPO,
                                     source="config", asserted_by="test", accepted=True)

    def kickoff(self, agent, key="cc-task-1", title="Add usage-based pricing for overage"):
        return agent.call("bridge_start_task", title=title, repo=REPO, requester=WES[1],
                          paths="billing/rates.py", client_key=key, agent="Claude Code")


class TheLoopTests(E2ECase):
    def test_kickoff_question_in_slack_answer_change_and_signoff(self):
        agent = Agent(self.store)
        init = agent.rpc("initialize")["result"]
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        self.assertIn("bridge_wait", init["instructions"])
        names = [t["name"] for t in agent.rpc("tools/list")["result"]["tools"]]
        self.assertIn("bridge_import_record", names)
        self.assertIn("bridge_connection_status", names)
        self.assertIn("bridge_wait", names)

        # Kickoff once: the task speaks of pricing in Priya's area.
        kick = self.kickoff(agent)
        self.assertEqual(kick["verdict"], "engage", kick["why"])
        task = kick["task_id"]
        self.assertEqual(self.kickoff(agent)["task_id"], task)
        self.assertTrue(self.kickoff(agent)["repeated"])

        # The question reaches its owner in Slack.
        node = agent.call("bridge_add_node", task_id=task, question="Which rate applies to Globex usage above the included quota?",
                          context="Globex is on the enterprise plan; the contract names no overage rate",
                          paths="billing/rates.py", client_ref="n1", options="the list rate|the enterprise rate card")
        self.assertEqual(node["status"], "pending")
        self.assertEqual(node["owner"], PRIYA[0])
        self.assertFalse(node["authorized"])
        self.assertEqual(self.delivery.deliver_now(), 1)
        ask = self.slack.messages[-1]
        self.assertEqual(ask["channel"], "DUPRI")
        self.assertIn("Which rate applies to Globex", ask["text"])
        self.assertIn("Reply in this thread", ask["text"])
        self.assertIn("the enterprise rate card", ask["text"])

        # The agent does its independent work, then waits; nobody has answered.
        waited = agent.call("bridge_wait", task_id=task, timeout="0.2")
        self.assertTrue(waited["timed_out"])
        self.assertEqual([w["owner"] for w in waited["waiting_on"]], [PRIYA[0]])
        self.assertIn("still waits on Priya Natarajan", waited["next"])
        self.assertEqual(waited["changed"], [])
        last_seen = waited["observed_at"]

        # The agent's session restarts: a new process, the same key.
        store2 = Store(self.store.path)
        delivery2 = store2.connect_delivery(self.slack, base_url="https://bridge.acme.test")
        agent2 = Agent(store2)
        again = self.kickoff(agent2)
        self.assertEqual(again["task_id"], task)
        self.assertTrue(again["repeated"])
        tree = agent2.call("bridge_get_tree", task_id=task)
        self.assertEqual(tree["counts"]["blocking"], 1)
        self.assertIn("1 waiting on Priya Natarajan", tree["next"])

        # Hours later Priya answers in the thread; the answer carries the task id back.
        reply = handle_slack_event(delivery2, thread_reply("DUPRI", ask["ts"], "UPRI",
                                   "The enterprise rate card: 0.02 per unit over quota because the contract references it", "Ev1"))
        self.assertEqual(reply, {"ok": True})
        ack = self.slack.messages[-1]
        self.assertEqual(ack["thread_ts"], ask["ts"])
        self.assertIn("Recorded as Priya Natarajan's answer", ack["text"])
        self.assertIn(task, ack["text"])
        self.assertIn("bridge_get_tree", ack["text"])

        # The agent resumes: what happened since it last looked comes back at once.
        resumed = agent2.call("bridge_wait", task_id=task, timeout="5", since=last_seen)
        self.assertFalse(resumed["timed_out"])
        self.assertLess(resumed["waited_seconds"], 1)
        change = resumed["changed"][0]
        self.assertEqual(change["node_id"], node["node_id"])
        self.assertEqual(change["to"], "answered")
        self.assertTrue(change["authorized"])
        self.assertIn("0.02 per unit", change["answer"])
        self.assertEqual(change["answered_by"], PRIYA[0])
        self.assertIn("act on the answer", change["next"])
        self.assertEqual(resumed["waiting_on"], [])

        # The change follows the answer: a decision the agent settles under it.
        child = agent2.call("bridge_add_node", task_id=task, question="Round the overage charge to whole cents?",
                            parent_id=node["node_id"], client_ref="n2", paths="billing/rates.py",
                            context="Applying the 0.02 rate produces fractional cents on the invoice")
        settled = agent2.call("bridge_settle_node", task_id=task, node_id=child["node_id"],
                              answer="Round half up to whole cents", rationale="Matches how invoice totals are shown")
        self.assertEqual(settled["status"], "resolved")
        self.assertEqual(settled["signoff"], "required")
        self.assertFalse(settled["authorized"])
        self.assertTrue(settled["blocking"])
        self.assertEqual(settled["owner"], PRIYA[0])
        self.assertGreaterEqual(delivery2.deliver_now(), 1)
        signoff = self.slack.messages[-1]
        self.assertEqual(signoff["channel"], "DUPRI")
        self.assertIn("Sign-off wanted from Priya Natarajan", signoff["text"])
        self.assertIn("Round half up to whole cents", signoff["text"])

        # Sign-off still gates shipping.
        with self.assertRaises(ToolError) as refused:
            agent2.call("bridge_finish_task", task_id=task)
        self.assertIn(child["node_id"], str(refused.exception))
        self.assertIn("evidence, not sign-off", str(refused.exception))
        observed = agent2.call("bridge_get_tree", task_id=task)["observed_at"]

        # Priya signs in the thread; the waiting agent sees it and finishes.
        handle_slack_event(delivery2, thread_reply("DUPRI", signoff["ts"], "UPRI", "sign off", "Ev2"))
        self.assertIn("Signed off by Priya Natarajan", self.slack.messages[-1]["text"])
        signed = agent2.call("bridge_wait", task_id=task, timeout="5", since=observed)
        self.assertEqual([c["node_id"] for c in signed["changed"]], [child["node_id"]])
        self.assertTrue(signed["changed"][0]["authorized"])
        self.assertEqual(signed["changed"][0]["signed_by"], PRIYA[0])
        done = agent2.call("bridge_finish_task", task_id=task)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["counts"]["blocking"], 0)

        # The record: every step is an event on the task, and the decision
        # is memory the next task can reuse only as Priya's signed answer.
        kinds = [r["kind"] for r in self.graph.db.execute("SELECT kind FROM events WHERE run_id=? ORDER BY id", (task,))]
        for kind in ("task_started", "node_added", "notification_queued", "notification_sent", "reply_received",
                     "owner_approved", "signoff", "run_updated"):
            self.assertIn(kind, kinds, kinds)
        found = agent2.call("bridge_search_decisions", query="rate for usage above the included quota", repo=REPO)
        self.assertTrue(any(c["id"] == node["node_id"] for c in found["matches"]))

    def test_wait_reports_a_follow_up_question_and_a_hand_on(self):
        agent = Agent(self.store)
        task = self.kickoff(agent)["task_id"]
        node = agent.call("bridge_add_node", task_id=task, question="Which rate applies to Globex usage above the included quota?",
                          paths="billing/rates.py", client_ref="n1")
        self.delivery.deliver_now()
        seen = agent.call("bridge_get_tree", task_id=task)["observed_at"]
        # Priya is not the one: she hands it to Marisol from Slack; the
        # agent learns the node moved, and to whom.
        ask = self.slack.messages[-1]
        handle_slack_event(self.delivery, thread_reply("DUPRI", ask["ts"], "UPRI", "not me Marisol Vega", "Ev1"))
        moved = agent.call("bridge_wait", task_id=task, timeout="5", since=seen)
        self.assertEqual(moved["changed"][0]["owner"], MARISOL[0])
        self.assertEqual(moved["changed"][0]["to"], "pending")
        self.assertIn(MARISOL[0], moved["next"])
        # Marisol answers in the inbox and adds the next level's question.
        row = self.store.get_decision(node["node_id"])
        self.store.answer(node["node_id"], {"answer": "The enterprise rate card", "rationale": "contract",
                                            "signed_by": MARISOL[0], "expected_updated_at": row["updated_at"]})
        canvas.add_followups(self.store, CFG, node["node_id"], {"questions": "Does the rate card cover partner accounts?",
                                                                 "by": MARISOL[0]})
        answered = agent.call("bridge_wait", task_id=task, timeout="5", since=moved["observed_at"])
        by_id = {c["node_id"]: c for c in answered["changed"]}
        self.assertEqual(by_id[node["node_id"]]["to"], "answered")
        suggested = [c for c in answered["changed"] if c.get("new")]
        self.assertEqual(len(suggested), 1)
        self.assertEqual(suggested[0]["to"], "suggested")
        self.assertIn("adopt", suggested[0]["next"])
        adopted = agent.call("bridge_add_node", task_id=task, question="Does the rate card cover partner accounts?",
                             adopt=suggested[0]["node_id"], client_ref="n2")
        self.assertEqual(adopted["parent_id"], node["node_id"])
        self.assertEqual(adopted["origin"], "human")


class CorrectionTests(E2ECase):
    def test_a_correction_reaches_the_task_that_reused_the_answer(self):
        question = "Which rate applies to Globex usage above the included quota?"
        first = Agent(self.store)
        task_a = self.kickoff(first, key="cc-a")["task_id"]
        node_a = first.call("bridge_add_node", task_id=task_a, question=question, paths="billing/rates.py", client_ref="a1")
        row = self.store.get_decision(node_a["node_id"])
        self.store.answer(node_a["node_id"], {"answer": "The enterprise rate card: 0.02 per unit", "rationale": "contract",
                                              "signed_by": PRIYA[0], "expected_updated_at": row["updated_at"]})
        first.call("bridge_get_tree", task_id=task_a)
        first.call("bridge_finish_task", task_id=task_a)

        # A later task asks the same thing: Priya's signed answer resolves
        # it as evidence, still waiting on her signature for this task.
        second = Agent(self.store)
        task_b = self.kickoff(second, key="cc-b", title="Show overage pricing on the invoice")["task_id"]
        node_b = second.call("bridge_add_node", task_id=task_b, question=question, paths="billing/rates.py", client_ref="b1")
        self.assertEqual(node_b["status"], "resolved")
        self.assertEqual(node_b["kind"], "evidence")
        self.assertIn("0.02 per unit", node_b["answer"])
        self.assertFalse(node_b["authorized"])
        self.assertEqual(node_b["owner"], PRIYA[0])
        row_b = self.store.get_decision(node_b["node_id"])
        self.assertEqual(row_b["source_id"], node_a["node_id"])
        canvas.sign_off(self.store, node_b["node_id"], {"by": PRIYA[0], "expected_updated_at": row_b["updated_at"]})
        self.assertTrue(second.call("bridge_get_tree", task_id=task_b)["nodes"][0]["authorized"])
        seen = second.call("bridge_get_tree", task_id=task_b)["observed_at"]
        self.slack.messages.clear()

        # Priya corrects the original: task B's node is in doubt, its
        # agent is told, Priya hears about it, and B cannot finish on it.
        row_a = self.store.get_decision(node_a["node_id"])
        canvas.sign_off(self.store, node_a["node_id"], {"by": PRIYA[0], "answer": "The list rate: 0.03 per unit",
                                                        "rationale": "the rate card was renegotiated",
                                                        "expected_updated_at": row_a["updated_at"]})
        doubt = second.call("bridge_wait", task_id=task_b, timeout="5", since=seen)
        self.assertEqual(doubt["changed"][0]["node_id"], node_b["node_id"])
        self.assertTrue(doubt["changed"][0]["needs_review"])
        self.assertFalse(doubt["changed"][0]["authorized"])
        self.assertIn("needs review", doubt["changed"][0]["next"])
        self.delivery.deliver_now()
        review = next(m for m in self.slack.messages if "Needs your review" in m["text"])
        self.assertEqual(review["channel"], "DUPRI")
        self.assertIn("corrected", review["text"])
        # The requester is not told by default (the agent reads the tree);
        # a team that wants the copy turns notify_requester on.
        self.assertFalse(any(m["channel"] == "DUWES" and "was answered" in m["text"] for m in self.slack.messages))
        with self.assertRaises(ToolError) as refused:
            second.call("bridge_finish_task", task_id=task_b)
        self.assertIn(node_b["node_id"], str(refused.exception))

        # Priya corrects B in the thread with the new answer; B finishes.
        handle_slack_event(self.delivery, thread_reply("DUPRI", review["ts"], "UPRI",
                                                       "The list rate: 0.03 per unit because the rate card was renegotiated", "Ev9"))
        self.assertIn("Corrected and signed by Priya Natarajan", self.slack.messages[-1]["text"])
        fixed = second.call("bridge_wait", task_id=task_b, timeout="5", since=doubt["observed_at"])
        self.assertTrue(fixed["changed"][0]["authorized"])
        self.assertIn("0.03", fixed["changed"][0]["answer"])
        self.assertEqual(second.call("bridge_finish_task", task_id=task_b)["status"], "completed")


class UnreachableOwnerTests(E2ECase):
    def test_an_unreachable_owner_is_visible_and_recoverable(self):
        agent = Agent(self.store)
        task = self.kickoff(agent, title="Add a partner accounts endpoint")["task_id"]
        node = agent.call("bridge_add_node", task_id=task, question="Should the partner accounts endpoint require an API key?",
                          paths="api/routes.py", client_ref="n1")
        self.assertEqual(node["status"], "pending")
        self.assertEqual(node["owner"], MARISOL[0])
        # Marisol has no Slack id and there is no fallback channel: the
        # notification fails where an operator sees it, naming the fix.
        self.assertEqual(self.delivery.deliver_now(), 0)
        failed = self.delivery.list("failed")
        self.assertEqual(len(failed), 1)
        self.assertIn("Marisol Vega has no Slack id", failed[0]["last_error"])
        self.assertEqual(failed[0]["decision_id"], node["node_id"])
        waited = agent.call("bridge_wait", task_id=task, timeout="0.1")
        self.assertTrue(waited["timed_out"])
        self.assertEqual(waited["waiting_on"][0]["owner"], MARISOL[0])

        # Recovery one: a fallback channel, and a retry from Deliveries.
        self.store.update_settings({"slack_fallback_channel": "C0BRIDGE"})
        retried = self.delivery.retry(failed[0]["id"])
        self.assertEqual(retried["state"], "queued")
        self.assertEqual(self.delivery.deliver_now(), 1)
        posted = self.slack.messages[-1]
        self.assertEqual(posted["channel"], "C0BRIDGE")
        self.assertIn("Marisol Vega has no Slack id yet; posted here instead", posted["text"])
        self.assertIn("partner accounts endpoint", posted["text"])

        # Recovery two: Marisol never appears, so the coordinator hands
        # the decision to Priya, who is reachable and answers.
        seen = agent.call("bridge_get_tree", task_id=task)["observed_at"]
        handed = self.store.refer(node["node_id"], {"person": self.priya, "by": WES[0]})
        self.assertIn("Handed to Priya Natarajan", handed["notice"])
        self.assertEqual(self.delivery.deliver_now(), 1)
        dm = self.slack.messages[-1]
        self.assertEqual(dm["channel"], "DUPRI")
        self.assertIn("Handed to you", dm["text"])
        moved = agent.call("bridge_wait", task_id=task, timeout="5", since=seen)
        self.assertEqual(moved["changed"][0]["owner"], PRIYA[0])
        handle_slack_event(self.delivery, thread_reply("DUPRI", dm["ts"], "UPRI", "Yes, an API key because partners are external", "Ev3"))
        answered = agent.call("bridge_wait", task_id=task, timeout="5", since=moved["observed_at"])
        self.assertEqual(answered["changed"][0]["to"], "answered")
        self.assertTrue(answered["changed"][0]["authorized"])
        self.assertEqual(agent.call("bridge_finish_task", task_id=task)["status"], "completed")

    def test_wait_validates_its_arguments(self):
        agent = Agent(self.store)
        task = self.kickoff(agent)["task_id"]
        empty = agent.call("bridge_wait", task_id=task, timeout="0.1")
        self.assertFalse(empty["timed_out"])
        self.assertIn("no node yet", empty["next"])
        with self.assertRaises(ToolError):
            agent.call("bridge_wait", task_id=task, timeout="soon")
        with self.assertRaises(ToolError):
            agent.call("bridge_wait", task_id=task, timeout="-1")
        with self.assertRaises(ToolError):
            agent.call("bridge_wait", task_id=task, node_id="nope")
        capped = canvas.wait(self.store, {"task_id": task, "timeout": "99999"}, cap=0.05)
        self.assertEqual(capped["timeout_applied"], 0.05)
        self.assertIn("capped", capped["notice"])


class StdioRestartTests(E2ECase):
    """The real transport: bridge_mcp.py as a subprocess on stdio, killed
    and started again on the same database."""

    def launch(self):
        env = {**os.environ, "BRIDGE_SEMANTIC": "0", "BRIDGE_LIVE": "0", "PYTHONUNBUFFERED": "1"}
        env.pop("ANTHROPIC_API_KEY", None)
        proc = subprocess.Popen([sys.executable, str(ROOT / "bridge_mcp.py"), "--db", str(self.store.path)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=str(ROOT), env=env)
        self.addCleanup(self.stop, proc)
        return proc

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            pipe.close()

    def rpc(self, proc, method, params=None, id=1):
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}) + "\n").encode())
        proc.stdin.flush()
        line = proc.stdout.readline()
        self.assertTrue(line, proc.stderr.read().decode() if proc.poll() is not None else "no response")
        return json.loads(line)

    def call(self, proc, name, **args):
        response = self.rpc(proc, "tools/call", {"name": name, "arguments": args})
        result = response["result"]
        self.assertFalse(result["isError"], result["content"][0]["text"])
        return json.loads(result["content"][0]["text"])

    def test_a_killed_agent_process_resumes_the_same_task(self):
        first = self.launch()
        init = self.rpc(first, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test"}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "bridge")
        kick = self.call(first, "bridge_start_task", title="Add usage-based pricing for overage", repo=REPO,
                         requester=WES[1], paths="billing/rates.py", client_key="stdio-1")
        task = kick["task_id"]
        node = self.call(first, "bridge_add_node", task_id=task, question="Which rate applies above the included quota?",
                         paths="billing/rates.py", client_ref="n1")
        self.assertEqual(node["status"], "pending")
        waited = self.call(first, "bridge_wait", task_id=task, timeout="0.2")
        self.assertTrue(waited["timed_out"])
        first.kill()
        first.wait(timeout=10)

        second = self.launch()
        self.rpc(second, "initialize")
        again = self.call(second, "bridge_start_task", title="Add usage-based pricing for overage", repo=REPO,
                          requester=WES[1], paths="billing/rates.py", client_key="stdio-1")
        self.assertEqual(again["task_id"], task)
        self.assertTrue(again["repeated"])
        tree = self.call(second, "bridge_get_tree", task_id=task)
        self.assertEqual(tree["nodes"][0]["node_id"], node["node_id"])
        self.assertEqual(tree["counts"]["blocking"], 1)

        # The answer lands in the inbox (another process) while the agent waits.
        def answer_later():
            time.sleep(0.5)
            row = self.store.get_decision(node["node_id"])
            self.store.answer(node["node_id"], {"answer": "The enterprise rate card", "rationale": "contract",
                                                "signed_by": PRIYA[0], "expected_updated_at": row["updated_at"]})
        threading.Thread(target=answer_later, daemon=True).start()
        resumed = self.call(second, "bridge_wait", task_id=task, timeout="10", since=tree["observed_at"])
        self.assertFalse(resumed["timed_out"])
        self.assertEqual(resumed["changed"][0]["to"], "answered")
        self.assertTrue(resumed["changed"][0]["authorized"])
        self.assertLess(resumed["waited_seconds"], 8)
        self.assertEqual(self.call(second, "bridge_finish_task", task_id=task)["status"], "completed")


class SharedWaitTests(E2ECase):
    """A shared Raven over HTTP applies its configured request bound."""

    def setUp(self):
        super().setUp()
        self.server = make_server(self.store, port=0, wait_cap=0.3)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def close():
            self.server.shutdown()
            self.server.server_close()
            thread.join()
        self.addCleanup(close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def call_http(self, name, arguments):
        request = Request(self.base + "/mcp", method="POST", headers={"Content-Type": "application/json"},
                          data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": name, "arguments": arguments}}).encode())
        with urlopen(request) as response:
            result = json.loads(response.read())["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            raise Invalid(text)
        return json.loads(text)

    def test_a_client_that_takes_progress_waits_the_whole_timeout(self):
        """Measured live on a397f1c: every HTTP wait was cut to 50 seconds,
        so one task took 28 wait calls and the host ran out of budget, and
        Claude Code aborted two calls that "sent no response or progress".
        A client that asks for progress now gets a stream: a notification
        while it waits, then the result, past the request bound."""
        from unittest.mock import patch
        kick = self.call_http("bridge_start_task", {"title": "Add usage-based pricing for overage", "repo": REPO,
                                                     "requester": WES[1], "paths": "billing/rates.py",
                                                     "client_key": "http-stream"})
        self.call_http("bridge_add_node", {"task_id": kick["task_id"], "question": "Which rate applies above the quota?",
                                           "paths": "billing/rates.py", "client_ref": "n1"})
        request = Request(self.base + "/mcp", method="POST",
                          headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                          data=json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
                              "name": "bridge_wait", "arguments": {"task_id": kick["task_id"], "timeout": "2.5"},
                              "_meta": {"progressToken": "wait-1"}}}).encode())
        with patch("bridge.mcp.PROGRESS_EVERY", 0.3), urlopen(request) as response:
            self.assertEqual(response.headers["Content-Type"], "text/event-stream")
            body = response.read().decode()
        events = [json.loads(chunk.split("data: ", 1)[1]) for chunk in body.split("\n\n") if "data: " in chunk]
        progress = [e for e in events if e.get("method") == "notifications/progress"]
        self.assertGreaterEqual(len(progress), 2, body[:500])
        self.assertEqual({e["params"]["progressToken"] for e in progress}, {"wait-1"})
        marks = [e["params"]["progress"] for e in progress]
        self.assertEqual(marks, sorted(marks))
        final = events[-1]
        self.assertEqual(final["id"], 7)
        waited = json.loads(final["result"]["content"][0]["text"])
        self.assertTrue(waited["timed_out"])
        self.assertEqual(waited["timeout_applied"], 2.5)
        self.assertNotIn("notice", waited)

    def test_a_server_stopping_under_a_streamed_wait_says_so_at_once(self):
        """Measured live on eb9d22d: Raven was restarted during a streamed
        wait, and the host heard nothing until its idle limit cut the call
        329 seconds later. Stopping now answers every open wait first."""
        import time as _time
        from unittest.mock import patch
        kick = self.call_http("bridge_start_task", {"title": "Add usage-based pricing for overage", "repo": REPO,
                                                     "requester": WES[1], "paths": "billing/rates.py",
                                                     "client_key": "http-stop"})
        self.call_http("bridge_add_node", {"task_id": kick["task_id"], "question": "Which rate applies above the quota?",
                                           "paths": "billing/rates.py", "client_ref": "n1"})
        request = Request(self.base + "/mcp", method="POST",
                          headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                          data=json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {
                              "name": "bridge_wait", "arguments": {"task_id": kick["task_id"], "timeout": "60"},
                              "_meta": {"progressToken": "wait-stop"}}}).encode())
        got = {}

        def read():
            with patch("bridge.mcp.PROGRESS_EVERY", 0.3), urlopen(request) as response:
                got["body"] = response.read().decode()
        reader = threading.Thread(target=read)
        reader.start()
        _time.sleep(1.0)
        started = _time.monotonic()
        self.assertEqual(self.server.stop_waits(grace=5.0), 0)
        reader.join(timeout=5)
        self.assertFalse(reader.is_alive())
        self.assertLess(_time.monotonic() - started, 3.0)
        events = [json.loads(chunk.split("data: ", 1)[1]) for chunk in got["body"].split("\n\n") if "data: " in chunk]
        final = events[-1]
        self.assertEqual(final["id"], 9)
        waited = json.loads(final["result"]["content"][0]["text"])
        self.assertEqual(waited["interrupted"], "the Raven server is stopping")
        self.assertFalse(waited["timed_out"])
        self.assertIn("Call bridge_wait again in a few seconds", waited["next"])
        # A plain wait, bounded by the server, is told the same way.
        self.assertEqual(self.call_http("bridge_wait", {"task_id": kick["task_id"], "timeout": "0.3"})["interrupted"],
                         "the Raven server is stopping")

    def test_a_finish_that_asks_for_progress_gets_it_while_the_diff_is_read(self):
        """Measured live on 5e967e4: the finish timed out at 60 seconds while
        the diff was read. A client that asks for progress is kept alive."""
        import time as _time
        from unittest.mock import patch
        kick = self.call_http("bridge_start_task", {"title": "Add usage-based pricing for overage", "repo": REPO,
                                                     "requester": WES[1], "paths": "billing/rates.py",
                                                     "client_key": "http-finish"})
        node = self.call_http("bridge_add_node", {"task_id": kick["task_id"], "question": "Which rate applies above the quota?",
                                                  "paths": "billing/rates.py", "client_ref": "n1"})
        row = self.store.get_decision(node["node_id"])
        self.store.answer(node["node_id"], {"answer": "The enterprise rate card", "rationale": "r",
                                            "expected_updated_at": row["updated_at"]})
        self.call_http("bridge_get_tree", {"task_id": kick["task_id"]})

        def slow(*args, **kwargs):
            _time.sleep(1.2)
            return {"verdict": "follows", "why": "w", "requirements": []}
        request = Request(self.base + "/mcp", method="POST",
                          headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                          data=json.dumps({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {
                              "name": "bridge_finish_task", "arguments": {"task_id": kick["task_id"],
                                                                          "diff": "+ rate_card = 'enterprise'"},
                              "_meta": {"progressToken": "finish-1"}}}).encode())
        with patch("bridge.mcp.PROGRESS_EVERY", 0.3), patch("bridge.llm.check_conformance", side_effect=slow), \
                urlopen(request) as response:
            self.assertEqual(response.headers["Content-Type"], "text/event-stream")
            body = response.read().decode()
        events = [json.loads(chunk.split("data: ", 1)[1]) for chunk in body.split("\n\n") if "data: " in chunk]
        progress = [e for e in events if e.get("method") == "notifications/progress"]
        self.assertGreaterEqual(len(progress), 2, body[:400])
        self.assertEqual({e["params"]["message"] for e in progress}, {"reading the diff against what was signed"})
        done = json.loads(events[-1]["result"]["content"][0]["text"])
        self.assertEqual(done["review"]["status"], "done")
        self.assertEqual([f["verdict"] for f in done["follows"]], ["follows"])

    def test_a_wait_over_stdio_sends_progress_to_a_client_that_asked(self):
        from unittest.mock import patch
        kick = canvas.start_task(self.store, None, {"title": "Add usage-based pricing for overage", "repo": REPO,
                                                    "paths": "billing/rates.py"})
        canvas.add_node(self.store, None, {"task_id": kick["task_id"], "question": "Which rate applies above the quota?",
                                           "paths": "billing/rates.py"})
        notes = []
        with patch("bridge.mcp.PROGRESS_EVERY", 0.3):
            response = dispatch(self.store, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "bridge_wait", "arguments": {"task_id": kick["task_id"], "timeout": "2.2"},
                "_meta": {"progressToken": 11}}}, notify=notes.append)
        self.assertGreaterEqual(len(notes), 2)
        self.assertEqual({n["params"]["progressToken"] for n in notes}, {11})
        self.assertTrue(json.loads(response["result"]["content"][0]["text"])["timed_out"])

    def test_a_wait_over_http_respects_the_request_cap(self):
        kick = self.call_http("bridge_start_task", {"title": "Add usage-based pricing for overage", "repo": REPO,
                                                     "requester": WES[1], "paths": "billing/rates.py", "client_key": "http-1"})
        task = kick["task_id"]
        node = self.call_http("bridge_add_node", {"task_id": task, "question": "Which rate applies above the included quota?",
                                                   "paths": "billing/rates.py", "client_ref": "n1"})
        started = time.monotonic()
        waited = self.call_http("bridge_wait", {"task_id": task, "timeout": "1"})
        self.assertTrue(waited["timed_out"])
        self.assertLess(time.monotonic() - started, 0.8)
        self.assertGreaterEqual(waited["waited_seconds"], 0.25)
        self.assertIn("capped", waited["notice"])
        self.assertEqual(waited["timeout_applied"], 0.3)

        def answer_later():
            time.sleep(0.1)
            row = self.store.get_decision(node["node_id"])
            self.store.answer(node["node_id"], {"answer": "The enterprise rate card", "rationale": "contract",
                                                "signed_by": PRIYA[0], "expected_updated_at": row["updated_at"]})
        threading.Thread(target=answer_later, daemon=True).start()
        resumed = self.call_http("bridge_wait", {"task_id": task, "timeout": "10"})
        self.assertFalse(resumed["timed_out"])
        self.assertEqual(resumed["changed"][0]["to"], "answered")
        self.assertLess(resumed["waited_seconds"], 5)
        with self.assertRaises(Invalid):
            self.call_http("bridge_wait", {"task_id": task, "timeout": "later"})


if __name__ == "__main__":
    unittest.main()
