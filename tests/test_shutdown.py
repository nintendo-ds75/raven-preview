"""Stopping the server is observable to an agent waiting on it.

Measured live on eb9d22d: `docker compose restart` sent Raven SIGTERM
during a streamed bridge_wait, the process ended without answering, and
the host heard nothing until its own idle limit cut the call 329 seconds
later. On SIGTERM (or Ctrl-C) Raven now answers every open wait first."""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ShutdownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.port = free_port()
        db = Path(self.temp.name) / "bridge.db"
        sys.path.insert(0, str(ROOT))
        from bridge.store import Store
        # An existing workspace, as the feature tests' ready_server sets up.
        store = Store(db)
        store.graph.set_setting("workspace_name", "Test workspace")
        store.graph.db.close()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC", "BRIDGE_", "SLACK", "GITHUB",
                                                                        "DATABASE_URL", "TEAMS"))}
        env["BRIDGE_MODEL_API"] = "none"
        self.proc = subprocess.Popen([sys.executable, "-m", "bridge", "serve", "--port", str(self.port),
                                      "--db", str(db)],
                                     cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.1)
        self.fail("bridge serve did not start: " + (self.proc.stderr.read() if self.proc.poll() is not None else ""))

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self.proc.stderr.close()

    def call(self, name, arguments):
        request = Request(f"http://127.0.0.1:{self.port}/mcp", method="POST", headers={"Content-Type": "application/json"},
                          data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": name, "arguments": arguments}}).encode())
        with urlopen(request, timeout=10) as response:
            result = json.loads(response.read())["result"]
        return json.loads(result["content"][0]["text"])

    def test_sigterm_answers_an_open_streamed_wait_before_the_process_ends(self):
        kick = self.call("bridge_start_task", {"title": "Decide the overage rate", "repo": "acme/ledger",
                                               "paths": "billing/rates.py"})
        node = self.call("bridge_add_node", {"task_id": kick["task_id"], "question": "Which rate applies above the quota?",
                                             "paths": "billing/rates.py"})
        self.assertTrue(node["blocking"], node)
        request = Request(f"http://127.0.0.1:{self.port}/mcp", method="POST",
                          headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                          data=json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
                              "name": "bridge_wait", "arguments": {"task_id": kick["task_id"], "timeout": "120"},
                              "_meta": {"progressToken": "t"}}}).encode())
        got = {}

        def read():
            with urlopen(request, timeout=30) as response:
                got["body"] = response.read().decode()
        reader = threading.Thread(target=read)
        reader.start()
        time.sleep(1.5)
        sent = time.monotonic()
        self.proc.send_signal(signal.SIGTERM)
        reader.join(timeout=15)
        self.assertFalse(reader.is_alive(), "the wait was not answered")
        self.assertLess(time.monotonic() - sent, 5.0)
        events = [json.loads(chunk.split("data: ", 1)[1]) for chunk in got["body"].split("\n\n") if "data: " in chunk]
        waited = json.loads(events[-1]["result"]["content"][0]["text"])
        self.assertEqual(waited["interrupted"], "the Raven server is stopping")
        self.assertIn("Call bridge_wait again", waited["next"])
        self.assertEqual(self.proc.wait(timeout=10), 0)


if __name__ == "__main__":
    unittest.main()
