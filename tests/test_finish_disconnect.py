"""A lost progress stream must not discard an authorized finish's proof."""
from contextlib import contextmanager
import errno
import hashlib
import json
import socket
import threading
import time
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen

from fixtures import ready_server
from test_contract import ContractCase
from bridge import canvas, mcp, proof
from bridge.store import Invalid, Store


DIFF = ("diff --git a/billing/usage.py b/billing/usage.py\n"
        "--- a/billing/usage.py\n+++ b/billing/usage.py\n"
        "@@ -1 +1 @@\n-rate = 1\n+rate = 2  # café  \n")


class FinishDisconnectTests(ContractCase):
    def signed_task(self):
        task = self.start()
        node = self.node(task)
        self.answer(node["node_id"], "Bill at two cents per call.")
        mcp.call_tool(self.store, "bridge_get_tree", {"task_id": task})
        return task

    def arguments(self, task):
        return {"task_id": task, "diff": DIFF, "diff_sha256": hashlib.sha256(DIFF.encode()).hexdigest(),
                "checks": "pytest: 12 passed"}

    def request(self, task):
        return {"jsonrpc": "2.0", "id": "finish", "method": "tools/call", "params": {
            "name": "bridge_finish_task", "arguments": self.arguments(task),
            "_meta": {"progressToken": "finish-progress"}}}

    @contextmanager
    def held_review(self):
        release, entered = threading.Event(), threading.Event()
        workers = []

        def slow(*args):
            workers.append(threading.current_thread())
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test did not release the reader")
            return {"verdict": "follows", "why": "The signed rate is present", "requirements": []}

        def finish_reading():
            release.set()
            for worker in workers:
                worker.join(5)
                self.assertFalse(worker.is_alive())

        with patch("bridge.llm.check_conformance", side_effect=slow) as reader:
            try:
                yield reader, entered, finish_reading
            finally:
                finish_reading()

    def test_broken_progress_saves_exact_proof_and_recovers_without_restarting_review(self):
        for error in (BrokenPipeError("client closed"), ConnectionResetError("client reset")):
            with self.subTest(error=type(error).__name__), self.held_review() as (reader, entered, finish_reading):
                task = self.signed_task()
                notify = Mock(side_effect=error)
                with patch.object(mcp, "PROGRESS_EVERY", 0):
                    result = mcp.dispatch(self.store, self.request(task), notify=notify)["result"]
                self.assertFalse(result["isError"], result)
                self.assertTrue(entered.is_set())
                notify.assert_called_once()
                finished = json.loads(result["content"][0]["text"])
                self.assertEqual(finished["status"], "completed")
                self.assertEqual(finished["review"]["status"], "running")
                self.assertFalse(finished["verified"])
                self.assertEqual(finished["follows"], [])
                self.assertIn("still reading", finished["caveat"])

                # A new connection can recover the proof before the reader finishes.
                reopened = Store(self.store.path)
                try:
                    exported = mcp.call_tool(reopened, "bridge_export_proof", {"task_id": task})
                    original = exported["bundle"]
                    self.assertEqual(original["id"], finished["proof"]["id"])
                    self.assertEqual(original["payload"]["change"], {
                        "diff": DIFF, "sha256": self.arguments(task)["diff_sha256"], "bytes": len(DIFF.encode())})
                    self.assertEqual(original["payload"]["checks"]["text"], "pytest: 12 passed")
                    self.assertTrue(exported["integrity"]["valid"])
                    self.assertTrue(exported["review_pending"])
                    self.assertIn("bridge_wait", exported["next"])
                    finish_reading()
                    after = mcp.call_tool(reopened, "bridge_export_proof", {"task_id": task})
                    self.assertEqual(after["bundle"], original, "export must not rewrite the earlier snapshot")
                    self.assertEqual(after["review"]["status"], "done")
                    self.assertEqual(original["payload"]["review"]["status"], "running")
                    self.assertFalse(after["review_snapshot_current"])
                    refreshed = mcp.call_tool(reopened, "bridge_finish_task", self.arguments(task))
                    latest = mcp.call_tool(reopened, "bridge_export_proof", {"task_id": task})
                    self.assertEqual(refreshed["review"]["id"], finished["review"]["id"])
                    self.assertEqual(latest["bundle"]["payload"]["review"]["status"], "done")
                    self.assertNotEqual(latest["bundle"]["id"], original["id"])
                    self.assertEqual(reader.call_count, 1)
                    self.assertEqual(reopened.graph.count_events("conformance_started", task_id=task), 1)
                    self.assertTrue(latest["integrity"]["valid"])
                finally:
                    reopened.graph.close()

    def test_unrelated_progress_errors_are_still_visible(self):
        for error in (RuntimeError("not a disconnect"), OSError(errno.EIO, "disk failure")):
            with self.subTest(error=type(error).__name__), self.held_review():
                task = self.signed_task()
                with patch.object(mcp, "PROGRESS_EVERY", 0):
                    result = mcp.dispatch(self.store, self.request(task), notify=Mock(side_effect=error))["result"]
                self.assertTrue(result["isError"])
                self.assertIn(type(error).__name__, result["content"][0]["text"])
                self.assertIn(str(error), result["content"][0]["text"])

    def test_disconnect_handling_does_not_hide_proof_storage_failure(self):
        task = self.signed_task()
        with patch("bridge.llm.check_conformance", return_value=None), \
                patch.object(proof, "create", side_effect=ConnectionResetError("storage disconnected")):
            with self.assertRaisesRegex(ConnectionResetError, "storage disconnected"):
                mcp.call_tool(self.store, "bridge_finish_task", self.arguments(task))

    def test_disconnect_handling_does_not_bypass_authorization(self):
        task = self.start()
        self.node(task)
        notify = Mock(side_effect=BrokenPipeError("client closed"))
        with patch.object(canvas, "_review") as review, patch.object(proof, "create") as create:
            result = mcp.dispatch(self.store, self.request(task), notify=notify)["result"]
            self.assertTrue(result["isError"])
            notify.assert_not_called()
            review.assert_not_called()
            create.assert_not_called()
        self.assertNotEqual(self.store.graph.get_task(task)["status"], "completed")
        with self.assertRaisesRegex(Invalid, "No change proof saved"):
            proof.export(self.store, {"task_id": task})

    def test_http_client_disconnect_keeps_proof_available_on_another_connection(self):
        task = self.signed_task()
        server = ready_server(self.store, port=0)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        client_closed, disconnected, finished = threading.Event(), threading.Event(), threading.Event()
        progress_sleep, finish_task = mcp.progress_sleep, canvas.finish_task

        def progress(token, notify, **kwargs):
            def emit(payload):
                try:
                    notify(payload)
                except (BrokenPipeError, ConnectionResetError):
                    disconnected.set()
                    raise

            def pause(seconds):
                if not client_closed.wait(5):
                    raise RuntimeError("test client did not close")
                time.sleep(0.01)
            return progress_sleep(token, emit, every=0, sleep=pause, **kwargs)

        def finish(*args, **kwargs):
            try:
                return finish_task(*args, **kwargs)
            finally:
                finished.set()

        try:
            with self.held_review() as (reader, entered, _), \
                    patch.object(mcp, "progress_sleep", side_effect=progress), \
                    patch.object(canvas, "finish_task", side_effect=finish):
                body = json.dumps(self.request(task)).encode()
                with socket.create_connection(server.server_address, timeout=5) as client:
                    client.sendall((f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:{server.server_port}\r\n"
                                    "Content-Type: application/json\r\n"
                                    "Accept: application/json, text/event-stream\r\n"
                                    f"Content-Length: {len(body)}\r\n\r\n").encode() + body)
                    headers = b""
                    while b"\r\n\r\n" not in headers:
                        part = client.recv(4096)
                        self.assertTrue(part, headers)
                        headers += part
                    self.assertIn(b"text/event-stream", headers)
                    self.assertTrue(entered.wait(5))
                    client.shutdown(socket.SHUT_RDWR)
                client_closed.set()
                self.assertTrue(disconnected.wait(5), "the server must observe a real broken progress stream")
                self.assertTrue(finished.wait(5), "disconnect must end the wait without losing proof bookkeeping")
                request = Request(f"http://127.0.0.1:{server.server_port}/mcp", headers={
                    "Content-Type": "application/json"}, data=json.dumps({
                        "jsonrpc": "2.0", "id": "export", "method": "tools/call", "params": {
                            "name": "bridge_export_proof", "arguments": {"task_id": task}}}).encode())
                with urlopen(request, timeout=5) as response:
                    result = json.load(response)["result"]
                self.assertFalse(result["isError"], result)
                exported = json.loads(result["content"][0]["text"])
                self.assertEqual(exported["bundle"]["payload"]["change"]["diff"], DIFF)
                self.assertTrue(exported["review_pending"])
                self.assertEqual(exported["bundle"]["payload"]["review"]["status"], "running")
                self.assertTrue(exported["integrity"]["valid"])
                self.assertEqual(reader.call_count, 1)
        finally:
            client_closed.set()
            server.shutdown()
            server.server_close()
            server_thread.join(5)
