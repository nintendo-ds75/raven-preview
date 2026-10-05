"""Loopback GitHub replay, synthetic Slack Web API and Jira search server.

Reads a fixture with `responses` keyed by GitHub REST path and `jira_issues`.
The GitHub list is deliberately paginated in pairs. `/control` can inject a
single HTTP failure by exact URL path. No Authorization headers are logged.
This is a network double, not a replacement for Bridge connector logic.
"""
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--port", type=int, default=17438)
    args = parser.parse_args()
    fixture = json.load(open(args.fixture))
    lock = threading.Lock()
    failures = {}
    counter = 0

    class Handler(BaseHTTPRequestHandler):
        def send_json(self, status, payload, headers=None):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_request_body(self, payload=None):
            nonlocal counter
            with lock:
                counter += 1
                timestamp = f"{int(time.time())}.{counter:06d}"
                with open(args.log, "a") as stream:
                    stream.write(json.dumps({"method": self.command, "url": self.path,
                                             "payload": payload, "ts": timestamp}) + "\n")
            return timestamp

        def maybe_fail(self, path):
            with lock:
                status = failures.pop(path, None)
            if status:
                self.send_json(status, {"message": "Injected transient evaluation failure"})
                return True
            return False

        def do_GET(self):
            self.log_request_body()
            url = urlsplit(self.path)
            if self.maybe_fail(url.path):
                return
            if url.path == "/jira/rest/api/3/search/jql":
                return self.send_json(200, {"issues": fixture.get("jira_issues", []), "isLast": True})
            if url.path.startswith("/github/"):
                path = url.path.removeprefix("/github")
                if path == "/repos/urllib3/urllib3/pulls":
                    items = [v for k, v in fixture["responses"].items()
                             if k.startswith(path + "/") and k.count("/") == 5]
                    items.sort(key=lambda x: x["updated_at"], reverse=True)
                    page = int(parse_qs(url.query).get("page", ["1"])[0])
                    headers = {}
                    if page * 2 < len(items):
                        headers["Link"] = (f'<http://{self.headers["Host"]}{url.path}?page={page+1}>; rel="next"')
                    return self.send_json(200, items[(page-1)*2:page*2], headers)
                if path in fixture["responses"]:
                    return self.send_json(200, fixture["responses"][path])
            self.send_json(404, {"message": "No recorded response for this path"})

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            timestamp = self.log_request_body(data)
            path = urlsplit(self.path).path
            if self.maybe_fail(path):
                return
            if path == "/control":
                with lock:
                    failures.update(data.get("fail_once", {}))
                return self.send_json(200, {"ok": True})
            if path == "/slack/api/conversations.open":
                return self.send_json(200, {"ok": True, "channel": {"id": "D"+data["users"]}})
            if path == "/slack/api/chat.postMessage":
                return self.send_json(200, {"ok": True, "channel": data["channel"], "ts": timestamp})
            if path == "/slack/api/users.lookupByEmail":
                return self.send_json(200, {"ok": False, "error": "users_not_found"})
            if path == "/teams":
                return self.send_json(200, {"ok": True})
            self.send_json(404, {"ok": False, "error": "unsupported_method"})

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Connector double listening on loopback:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
