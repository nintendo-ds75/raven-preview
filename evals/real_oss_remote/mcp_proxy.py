"""Transparent stdio trace proxy around Raven's HTTP MCP endpoint.

Only tool arguments/results are logged. The local synthetic token travels in
BRIDGE_TOKEN, never in configuration files, command arguments or traces.
"""
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

def main():
    trace = Path(os.environ["BRIDGE_EVAL_TRACE"])
    url = os.environ["BRIDGE_EVAL_URL"].rstrip("/") + "/mcp"
    token = os.environ["BRIDGE_TOKEN"]

    def log(direction, line):
        try:
            message = json.loads(line)
        except ValueError:
            return
        with trace.open("a") as out:
            out.write(json.dumps({"time": time.time(), "direction": direction, "message": message}) + "\n")

    for line in sys.stdin:
        log("request", line)
        request = Request(url, data=line.encode(), method="POST",
                          headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        try:
            with urlopen(request, timeout=120) as response:
                body = response.read().decode()
        except (HTTPError, URLError, OSError) as error:
            body = json.dumps({"jsonrpc": "2.0", "id": None,
                               "error": {"code": -32603, "message": str(error)}})
        if body:
            log("response", body)
            sys.stdout.write(body.rstrip("\n") + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
