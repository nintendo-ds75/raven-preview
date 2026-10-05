"""Minimal MCP stdio client that talks to bridge_mcp.py exactly as an agent client would."""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


class BridgeMCP:
    def __init__(self, db):
        self.proc = subprocess.Popen([sys.executable, str(REPO / "bridge_mcp.py"), "--db", db],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, text=True)
        self.n = 0
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "eval", "version": "0"}})
        self.instructions = init["result"].get("instructions")
        self.notify("notifications/initialized")
        self.tools = self.rpc("tools/list")["result"]["tools"]

    def notify(self, method, params=None):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()

    def rpc(self, method, params=None):
        self.n += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def call(self, name, **args):
        r = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        text = r["content"][0]["text"]
        if r.get("isError"):
            return {"error": text}
        return json.loads(text)

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=5)
