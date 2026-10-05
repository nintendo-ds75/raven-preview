"""Independent behavioral oracle for the fresh-user redirect evaluation.

Run with the evaluated urllib3 checkout on PYTHONPATH. Uses only loopback HTTP
servers and synthetic header values; never contacts an external service.
The expected policy is the final answer recorded by the disposable test owner.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from urllib3 import PoolManager
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.util.retry import Retry


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", parse_qs(parsed.query)["to"][0])
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            body = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *_):
        pass


def main():
    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    origin, other = [f"http://127.0.0.1:{s.server_port}" for s in servers]
    results = {}

    def check(name, operation):
        try:
            actual = operation()
            results[name] = {"passed": actual is True, "actual": actual}
        except Exception as error:
            results[name] = {"passed": False, "error": str(error)}

    try:
        with PoolManager(timeout=2) as http:
            def sent(target, headers, retries=None):
                kw = {} if retries is None else {"retries": retries}
                response = http.request("GET", origin + "/redirect?" + urlencode({"to": target}),
                                        headers=headers, **kw)
                assert response.status == 200, response.status
                return response.json()

            check("exact final default set", lambda: Retry().remove_headers_on_redirect == {
                "cookie", "authorization", "proxy-authorization", "x-api-key"})
            for spelling in ("X-Api-Key", "x-api-key", "X-aPi-kEy"):
                check("cross-port strips " + spelling,
                      lambda key=spelling: "x-api-key" not in sent(other + "/echo", {key: "synthetic"}))
            check("same origin retains API key", lambda:
                  sent(origin + "/echo", {"X-Api-Key": "synthetic"}).get("x-api-key") == "synthetic")
            check("different hostname strips API key", lambda:
                  "x-api-key" not in sent(f"http://localhost:{servers[0].server_port}/echo",
                                          {"X-Api-Key": "synthetic"}))
            headers = {"Authorization": "synthetic", "Cookie": "test=synthetic",
                       "Proxy-Authorization": "synthetic", "X-Api-Key": "synthetic",
                       "X-Auth-Token": "preserve", "X-Api-Token": "preserve", "X-Trace-Id": "preserve"}
            received = sent(other + "/echo", headers)
            check("existing sensitive defaults still stripped", lambda:
                  all(k not in received for k in ("authorization", "cookie", "proxy-authorization")))
            check("correction respected and ordinary headers retained", lambda:
                  all(received.get(k) == "preserve" for k in ("x-auth-token", "x-api-token", "x-trace-id")))
            check("empty override still disables stripping", lambda:
                  sent(other + "/echo", headers, Retry(remove_headers_on_redirect=[])).get("x-api-key") == "synthetic")
            custom = sent(other + "/echo", {**headers, "X-API-Secret": "synthetic"},
                          Retry(remove_headers_on_redirect=["X-API-Secret"]))
            check("custom list replaces rather than extends defaults", lambda:
                  "x-api-secret" not in custom and custom.get("x-api-key") == "synthetic"
                  and custom.get("authorization") == "synthetic")
            optout = Retry(remove_headers_on_redirect=Retry.DEFAULT_REMOVE_HEADERS_ON_REDIRECT - {"X-Api-Key"})
            result = sent(other + "/echo", headers, optout)
            check("targeted opt-out preserves other protections", lambda:
                  result.get("x-api-key") == "synthetic" and "authorization" not in result)
            check("caller headers not mutated", lambda:
                  headers.get("X-Api-Key") == "synthetic" and
                  sent(origin + "/echo", headers).get("x-api-key") == "synthetic")
            check("Retry.new preserves override", lambda:
                  optout.new().remove_headers_on_redirect == optout.remove_headers_on_redirect)
            pool = HTTPSConnectionPool("example.invalid", 443)
            check("effective default port equivalent", lambda:
                  pool.is_same_host("https://example.invalid/path") and
                  pool.is_same_host("https://example.invalid:443/path"))
            check("port and scheme remain origin boundaries", lambda:
                  not pool.is_same_host("https://example.invalid:8443/path") and
                  not pool.is_same_host("http://example.invalid/path"))
            pool.close()
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    print(json.dumps(results, indent=2))
    return 0 if all(r["passed"] for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
