"""Transparent stdio trace proxy around Raven's HTTP MCP endpoint.

Only tool arguments/results are logged. The local synthetic token travels in
BRIDGE_TOKEN, never in configuration files, command arguments or traces.
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

def main():
    trace = Path(os.environ["BRIDGE_EVAL_TRACE"])
    url = os.environ["BRIDGE_EVAL_URL"].rstrip("/") + "/mcp"
    token = os.environ["BRIDGE_TOKEN"]
    # Keep the existing replay log shape; optionally add a serialized,
    # credential-redacted audit chain shared with evaluator/human events.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from evals.audit.ledger import Ledger, redact
    audit_path = os.environ.get('BRIDGE_EVAL_AUDIT')
    audit = Ledger(audit_path, secrets=(token,)) if audit_path else None
    session = uuid.uuid4().hex

    def log(direction, line):
        try:
            message = json.loads(line)
        except ValueError:
            return
        value = redact({"time": time.time(), "direction": direction, "message": message, "session": session}, (token,))
        if audit:
            audit.append('mcp_' + direction, value, actor='coding-host', session=session)
        with os.fdopen(os.open(trace, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), 'a') as out:
            out.write(json.dumps(value) + "\n")

    for line in sys.stdin:
        log("request", line)
        request = Request(url, data=line.encode(), method="POST",
                          headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        try:
            with urlopen(request, timeout=120) as response:
                body = response.read().decode()
        except (HTTPError, URLError, OSError) as error:
            try:
                request_id = json.loads(line).get('id')
            except (ValueError, AttributeError):
                request_id = None
            body = json.dumps({"jsonrpc": "2.0", "id": request_id,
                               "error": {"code": -32603, "message": str(error)}})
        if body:
            log("response", body)
            sys.stdout.write(body.rstrip("\n") + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
