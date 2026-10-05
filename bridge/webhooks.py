"""Dedicated signed ingress. Never attach it to the browser write endpoints."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .store import Invalid


def make_webhook_server(executions, verify_signature, port):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/webhooks/openai":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    self.send_error(413)
                    return
                payload = self.rfile.read(length)
                try:
                    verify_signature(payload=payload, headers=self.headers)
                except Exception:
                    self.send_error(400, "Invalid signature")
                    return
                event = json.loads(payload)
                if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
                    raise Invalid("Invalid event")
                if event.get("type") in {"agent.session.created", "agent.session.action_required",
                        "agent.session.in_progress", "agent.session.idle", "agent.session.failed"}:
                    executions.receive_event(event)
                self.send_response(204)
                self.end_headers()
            except (ValueError, UnicodeDecodeError):
                self.send_error(400)
            except Exception:
                # No successful acknowledgement unless persistence completed.
                self.send_error(503, "Event persistence unavailable")
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def start_webhooks(executions, client, port):
    if not client.webhook_secret:
        raise Invalid("Set OPENAI_WEBHOOK_SECRET before enabling webhook ingress")
    server = make_webhook_server(executions, client.webhooks.verify_signature, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
