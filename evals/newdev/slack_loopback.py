"""Minimal local Slack Web API double for a running Bridge stack.

No real Slack workspace or person is contacted. Log only synthetic payloads,
never Authorization headers. Set SLACK_API_BASE on the disposable Bridge app.
"""
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=17436)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    lock = threading.Lock()
    count = 0

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            nonlocal count
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method = self.path.rsplit("/", 1)[-1]
            with lock:
                count += 1
                timestamp = f"{int(time.time())}.{count:06d}"
                with args.out.open("a") as stream:
                    stream.write(json.dumps({"method": method, "payload": data, "ts": timestamp}) + "\n")
            if method == "conversations.open":
                result = {"ok": True, "channel": {"id": "D" + data["users"]}}
            elif method == "chat.postMessage":
                result = {"ok": True, "channel": data["channel"], "ts": timestamp}
            elif method == "users.lookupByEmail":
                result = {"ok": False, "error": "users_not_found"}
            else:
                result = {"ok": False, "error": "unsupported_method"}
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Local Slack double listening on {args.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
