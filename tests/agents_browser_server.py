"""Offline browser fixture using the real worker, store, HTTP server, and UI."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_execution import FakeAPI
from bridge.config import Config
from bridge.execution import ExecutionService
from fixtures import ready_server as make_server
from bridge.store import Store


class BrowserAPI(FakeAPI):
    def start_task(self, **kwargs):
        session = super().start_task(**kwargs)
        self.ask(session["id"])
        return session

    def send_events(self, sid, events, key):
        super().send_events(sid, events, key)
        self.saved_turns[sid][0]["status"] = "completed"
        self.sessions[sid]["status"] = "idle"
        self.saved_items[sid].extend([
            {"id": "cmd", "type": "command_execution", "command": "python -m unittest discover -s tests",
             "exit_code": 0, "output": "Fixture: tests passed <script>alert(1)</script>"},
            {"id": "reply", "type": "message", "role": "assistant", "phase": "final_answer",
             "content": [{"text": "Synthetic task result"}]}])


if __name__ == "__main__":
    store = Store(sys.argv[1])
    store.add_owner({"name": "Browser fixture owner", "team": "Billing", "patterns": "billing/*"})
    service = ExecutionService(store, BrowserAPI(), cfg=Config(model_api="none"))
    service.start()
    server = make_server(store, 0, service)
    print(f"http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.close()
