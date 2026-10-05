"""Check the evaluated patch's claim that same-origin redirects keep every header.

Uses synthetic headers and one loopback server. Both the untouched and patched
urllib3 remove body-related headers when a 303 redirect changes the method.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from urllib3 import PoolManager


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(303)
        self.send_header("Location", "/echo")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        body = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    headers = {"X-Api-Key": "synthetic", "Content-Type": "application/json"}
    with PoolManager(timeout=2) as http:
        r = http.request("POST", f"http://127.0.0.1:{server.server_port}/redirect",
                         headers=headers, body=b"{}")
        received = r.json()
    print(json.dumps({
        "claim": "Same-origin redirects, including relative ones, forward every header unchanged.",
        "status": r.status,
        "redirect": "same-origin relative 303, POST to GET",
        "sent": headers,
        "received": received,
        "claim_holds": all(received.get(k.lower()) == v for k, v in headers.items()),
        "credential_preserved": received.get("x-api-key") == "synthetic",
    }, indent=2))
finally:
    server.shutdown()
    server.server_close()
