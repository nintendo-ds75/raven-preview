import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bridge.config import Config
from bridge.execution import ExecutionService
from fixtures import ready_server as make_server
from bridge.store import Store
from bridge.webhooks import make_webhook_server
from test_execution import FakeAPI


class ExecutionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "http.db")
        self.api = FakeAPI()
        self.service = ExecutionService(self.store, self.api, cfg=Config(model_api="none"))
        self.url = self.serve(make_server(self.store, 0, self.service))
        with urlopen(self.url + "/api/state") as response:
            self.token = json.load(response)["csrf_token"]

    def serve(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(close)
        return f"http://127.0.0.1:{server.server_port}"

    def submit(self):
        data = {"task": "Add usage pricing", "repository": "billing-fixture", "submission_key": "one"}
        request = Request(self.url + "/api/tasks", data=json.dumps(data).encode(),
                          headers={"Content-Type": "application/json", "X-Bridge-CSRF": self.token})
        with urlopen(request) as response:
            return json.load(response)

    def test_task_intake_requires_csrf_and_uses_trusted_repository(self):
        with self.assertRaises(HTTPError) as failure:
            urlopen(Request(self.url + "/api/tasks", data=b'{}', headers={"Content-Type": "application/json"}))
        self.assertEqual(failure.exception.code, 403)
        self.assertEqual(self.submit()["id"], self.submit()["id"])
        self.assertEqual(len(self.store.state()["runs"]), 1)

    def test_signed_ingress_is_separate_deduplicated_and_durable(self):
        result = self.submit()
        self.service.tick()
        sid = self.service.get(result["id"])["session_id"]
        seen = []
        def verify(*, payload, headers):
            seen.append(payload)
            if headers.get("test-signature") != "valid":
                raise ValueError("Bad signature")
        webhook = self.serve(make_webhook_server(self.service, verify, 0))
        event = {"id": "event-one", "type": "agent.session.action_required", "data": {"id": sid}}
        body = json.dumps(event).encode()
        for valid in (False, True, True):
            request = Request(webhook + "/webhooks/openai", data=body, headers={"test-signature": "valid" if valid else "bad"})
            if valid:
                with urlopen(request) as response:
                    self.assertEqual(response.status, 204)
            else:
                with self.assertRaises(HTTPError) as failure:
                    urlopen(request)
                self.assertEqual(failure.exception.code, 400)
        self.assertEqual(seen, [body, body, body])
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM provider_events").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "received")
        self.assertEqual(self.store.state()["decisions"], [])
        with self.assertRaises(HTTPError) as failure:
            urlopen(webhook + "/api/state")
        self.assertEqual(failure.exception.code, 501)

    def test_artifact_ids_cannot_cross_run_boundaries(self):
        run = self.submit()
        with self.assertRaises(HTTPError) as failure:
            urlopen(self.url + f"/api/executions/{run['id']}/artifacts/foreign-artifact")
        self.assertEqual(failure.exception.code, 404)
