"""The world tests/brief-browser.cjs drives: one task with every standing
the task page has, served with sign-in on, and a small control server
the test reads the store through.

    python3 tests/brief_browser_server.py DB_PATH

prints one JSON line (the app's URL, the control URL, the people, the
decisions and each person's task link) and serves until killed.

Offline and deterministic: Slack is a fake that records what it would
post, no model is called and no key is read.

The task: Ayla asked for per-tenant limits on remote-write. Seven
decisions route by the authority map:
  config   Mei, answered and signed
  tsdb     Tomas handed it on to Rafael; CODEOWNERS also lists @kmarsh,
           whom the workspace has no person for
  remote   Priya, waiting, with options (the decision their DM names)
  remote2  Priya, waiting, no options; carries a prediction
  handon   Priya, waiting (they hand it on from the page)
  settled  Priya's, settled by the agent, wanting their sign-off
  ui       Mei answered and signed; Zoe decides web/ too, and adds their signature
An earlier task holds Tomas's long signed answer, which the remote
decision shows as a precedent. Val is a viewer; Ada administers the workspace and holds a link that makes no
login. GitHub sign-in is configured with made-up credentials that never
reach GitHub."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import fixtures  # noqa: E402,F401  (strips a deployment's environment)

os.environ.update({"BRIDGE_MODEL_API": "none", "BRIDGE_SEMANTIC": "0", "BRIDGE_LIVE": "0",
                   "BRIDGE_CLAUDE_BIN": "/nonexistent/claude-offline", "BRIDGE_SECRET": "brief-browser-not-a-secret"})
os.environ.pop("ANTHROPIC_API_KEY", None)
# GitHub sign-in is configured (made-up credentials, never used to reach
# GitHub), so the account dialog offers it as the other way in.
os.environ.update({"BRIDGE_GITHUB_CLIENT_ID": "brief-browser-client", "BRIDGE_GITHUB_CLIENT_SECRET": "not-a-secret"})

import http.server  # noqa: E402

http.server.BaseHTTPRequestHandler.log_message = lambda *args: None

from bridge import briefing, canvas  # noqa: E402
from bridge.auth import SESSION_COOKIE, Auth  # noqa: E402
from bridge.authz import Actor  # noqa: E402
from bridge.config import Config  # noqa: E402
from bridge.server import make_server  # noqa: E402
from bridge.store import Store  # noqa: E402

CFG = Config(model_api="none")
REPO = "acme/telemetry"
PEOPLE = {
    # key: (name, email, github login, slack id, role)
    "priya": ("Priya Raman", "priya@example.org", "praman", "UCAL", "member"),
    "rafael": ("Rafael Ortega", "rafael@example.org", "rortega", "UBRY", "member"),
    "tomas": ("Tomas Novak", "tomas@example.org", "tnovak", "UGAN", "member"),
    # No email: the account dialog asks for one.
    "sam": ("Sam Okafor", "", "sokafor", "UJES", "member"),
    "mei": ("Mei Lin", "mei@example.org", "meilin", "UJUL", "member"),
    "zoe": ("Zoe Reviewer", "zoe@example.org", "zoerev", "UZOE", "member"),
    "ayla": ("Ayla Chen", "ayla@example.org", "aylachen", "UAYL", "member"),
    "val": ("Val Viewer", "val@example.org", "valview", "UVAL", "viewer"),
    "ada": ("Ada Admin", "ada@example.org", "adaadmin", "UADA", "admin"),
}
AUTHORITY = [("storage/remote/*", "decides", "priya"), ("tsdb/*", "decides", "tomas"), ("tsdb/*", "knows", "sam"),
             ("config/*", "decides", "mei"), ("web/*", "decides", "mei"), ("web/*", "decides", "zoe")]
PRIOR_ANSWER = ("Reject them with an error and count them in prometheus_tsdb_out_of_order_samples_total. Never drop "
                "samples silently: a sample the database refuses has to show up somewhere an operator looks, which "
                "means a counter with the tenant as a label, an error returned to the client that sent it, and a "
                "line in the documentation that says which of the two the operator should alert on. We tried a "
                "silent drop in an earlier release and it took three weeks and a customer escalation to notice "
                "that a whole region's samples were missing from the long-term store.")
LONG_PRIOR_QUESTION = "Should out-of-order samples older than the allowed window be dropped silently or rejected?"


class FakeSlack:
    name = "slack"
    supports_dm = True

    def __init__(self):
        self.messages: list[dict] = []

    def open_dm(self, user_id):
        return "D" + user_id

    def post_message(self, channel, text, blocks=None, thread_ts=""):
        self.messages.append({"channel": channel, "text": text})
        return f"1800000000.{len(self.messages):06d}"


class World:
    def __init__(self, db: Path):
        self.store = Store(db)
        self.graph = self.store.graph
        self.graph.set_setting("workspace_name", "Telemetry maintainers")
        self.slack = FakeSlack()
        self.auth = Auth(self.store, enabled=True)
        self.server = make_server(self.store, port=0, auth=self.auth)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.store.connect_delivery(self.slack, base_url=self.base)
        self.ids: dict[str, str] = {}
        self.nodes: dict[str, str] = {}
        self.seed()

    def person(self, key):
        return self.graph.get_person(self.ids[key])

    def answer(self, key, who, answer, rationale):
        d = self.store.get_decision(self.nodes[key] if key in self.nodes else key)
        self.store.answer(d["id"], {"answer": answer, "rationale": rationale, "expected_updated_at": d["updated_at"],
                                    "signed_by": self.person(who)["name"], "source": f"slack: {self.person(who)['name']}"},
                          actor=Actor.person(self.person(who)))

    def seed(self):
        g = self.graph
        with g.transaction():
            for key, (name, email, login, slack, role) in PEOPLE.items():
                self.ids[key] = g.add_person(name, email=email, github_login=login, slack_id=slack, role=role)
            for scope, role, key in AUTHORITY:
                g.add_authority("path", scope, role, person_id=self.ids[key], repo=REPO)
            # CODEOWNERS names a maintainer the workspace has no person for.
            g.add_listing(REPO, "codeowners", "/tsdb/", "@kmarsh", ord=1)
        prior = canvas.start_task(self.store, CFG, {"title": "Reject out-of-order samples", "repo": REPO,
                                                    "requester": "Sam Okafor", "paths": "tsdb/head.go"})["task_id"]
        old = canvas.add_node(self.store, CFG, {"task_id": prior, "question": LONG_PRIOR_QUESTION,
                                                "paths": "tsdb/head.go"})["node_id"]
        self.prior_node = old
        self.store.delivery.deliver_now()
        self.answer(old, "tomas", PRIOR_ANSWER, "A silent drop is invisible until someone notices a gap.")
        self.task = canvas.start_task(self.store, CFG, {
            "title": "Add per-tenant sample limits to remote-write", "repo": REPO, "requester": "Ayla Chen",
            "agent": "Claude Code", "paths": "storage/remote/queue_manager.go",
            "goal": "One noisy tenant keeps starving the shared remote-write queue. Add a per-tenant "
                    "samples-per-second limit, and keep single-tenant setups behaving as today."})["task_id"]
        specs = {
            "config": {"question": "Where should the per-tenant limit be configured: per remote_write endpoint or "
                                   "in a global tenants block?", "paths": "config/config.go",
                       "options": "Per remote_write endpoint | New global tenants block"},
            "tsdb": {"question": "Should the WAL watcher track per-tenant sample counts, or only the queue manager?",
                     "paths": "tsdb/wlog/watcher.go", "context": "Tracking in the watcher costs memory per tenant."},
            "remote": {"question": "When a tenant exceeds its limit, should remote-write drop the excess samples, "
                                   "requeue them with backoff, or block the shard?",
                       "paths": "storage/remote/queue_manager.go",
                       "context": "Dropping keeps other tenants flowing but loses data unless a metric counts it.",
                       "options": "Drop excess and count it in a metric | Requeue with backoff, capped | Block the "
                                  "shard (today's behaviour)"},
            "remote2": {"question": "Should the write handler return 429 to a tenant over its limit?",
                        "paths": "storage/remote/write_handler.go"},
            "handon": {"question": "Should the limiter's token bucket refill per second or per scrape interval?",
                       "paths": "storage/remote/limiter.go"},
            "settled": {"question": "Should the per-tenant limit default to unlimited?",
                        "paths": "storage/remote/client.go"},
            "ui": {"question": "Should the status page show each tenant's current sample rate?",
                   "paths": "web/ui/tenants.tsx"},
        }
        for key, spec in specs.items():
            self.nodes[key] = canvas.add_node(self.store, CFG, {"task_id": self.task, **spec})["node_id"]
        self.store.delivery.deliver_now()
        self.answer("config", "mei", "Per remote_write endpoint, next to queue_config.",
                    "Operators reason about limits per endpoint.")
        self.answer("ui", "mei", "Yes, beside the queue's shard count.", "Operators look there first.")
        tsdb = self.store.get_decision(self.nodes["tsdb"])
        self.store.refer(self.nodes["tsdb"], {"person": self.ids["rafael"], "by": "Tomas Novak",
                                              "expected_updated_at": tsdb["updated_at"],
                                              "note": "Rafael owns the watcher memory work"},
                         actor=Actor.person(self.person("tomas")))
        canvas.settle_node(self.store, {"task_id": self.task, "node_id": self.nodes["settled"],
                                        "answer": "Yes: unlimited unless a limit is configured.",
                                        "rationale": "Single-tenant setups must behave as today."})
        with g.transaction():
            # A guess the page must label as one.
            g.update_decision(self.nodes["remote2"], prediction="Return 429 with a Retry-After header.")
            # Enough history that the page folds the oldest of it.
            for i in range(6):
                g.append_event("task_note", {"task_id": self.task, "by": ("Ayla Chen", "Mei Lin")[i % 2],
                                             "text": f"Seeded context line {i + 1}."})
        self.store.delivery.deliver_now()
        with g.transaction():
            # Links the page opens on, made as a message would carry them.
            self.links = {
                "zoe": briefing.mint(g, self.ids["zoe"], self.task, self.nodes["ui"], "seeded"),
                "val": briefing.mint(g, self.ids["val"], self.task, self.nodes["remote"], "seeded"),
                # An administrator's link makes no login: the page offers "Sign in".
                "ada": briefing.mint(g, self.ids["ada"], self.task, "", "seeded"),
                "expired": briefing.mint(g, self.ids["priya"], self.task, self.nodes["remote"], "seeded"),
            }
            g.db.execute("UPDATE brief_links SET expires_at=? WHERE token_hash=?",
                         (int(time.time()) - 60, briefing._hash(self.links["expired"])))
        self.links["priya"] = self.dm_link("priya", "remote")
        self.links["rafael"] = self.dm_link("rafael", "tsdb")

    def dm_link(self, key, node_key):
        """The newest link in this person's DMs that names that decision."""
        for m in reversed(self.slack.messages):
            if m["channel"] != "D" + PEOPLE[key][3]:
                continue
            for token in re.findall(r"/brief#(rvn_[A-Za-z0-9_-]+)", m["text"]):
                link = briefing.resolve(self.graph, token)
                if link is not None and link.decision_id == self.nodes[node_key]:
                    return token
        return ""

    def decision(self, key):
        d = self.store.get_decision(self.nodes.get(key, key))
        return {**{k: d.get(k) for k in ("id", "status", "answer", "rationale", "answered_by", "owner_name", "signoff",
                                         "signed_by", "authorized", "updated_at", "prediction")},
                "signatures": canvas._live_signatures(d)}

    def state(self):
        return {"base": self.base, "task": self.task, "nodes": self.nodes, "ids": self.ids, "links": self.links,
                "prior_node": self.prior_node, "people": {k: v[0] for k, v in PEOPLE.items()},
                "emails": {k: v[1] for k, v in PEOPLE.items()},
                "admin_cookie": self.auth.session_cookie(self.ids["ada"]).split(";", 1)[0].split("=", 1)[1],
                "cookie_name": SESSION_COOKIE}


def control(world: World):
    """What the test reads the store through, on a port of its own. Not
    part of the product: it exists only in this test process."""
    g = world.graph

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/decision":
                    out = world.decision(q["id"])
                elif url.path == "/notes":
                    out = briefing._notes(g, world.task, everything=True)
                elif url.path == "/tree-notes":
                    out = [n["text"] for n in canvas.task_notes(world.store, world.task)]
                elif url.path == "/setting":
                    out = {"value": g.get_setting(q["key"]), "mode": briefing.mode(g)}
                elif url.path == "/messages":
                    world.store.delivery.deliver_now()
                    out = world.slack.messages
                elif url.path == "/dm-link":
                    world.store.delivery.deliver_now()
                    out = {"token": world.dm_link(q["person"], q["node"])}
                elif url.path == "/links":
                    out = [dict(r) for r in g.db.execute(
                        "SELECT id, decision_id, notification_id, revoked_at FROM brief_links WHERE person_id=? "
                        "ORDER BY created_at", (world.ids[q["person"]],))]
                elif url.path == "/account":
                    pid = world.ids[q["person"]]
                    row = g.db.execute("SELECT 1 FROM account_passwords WHERE person_id=?", (pid,)).fetchone()
                    out = {"claimed": row is not None, "email": world.graph.get_person(pid).get("email") or ""}
                else:
                    return self.reply(404, {"error": "unknown"})
            except Exception as error:  # the test reads the message
                return self.reply(500, {"error": f"{type(error).__name__}: {error}"})
            return self.reply(200, out)

        def reply(self, code, value):
            body = json.dumps(value, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    world = World(Path(sys.argv[1]))
    ctl = control(world)
    print(json.dumps({**world.state(), "control": f"http://127.0.0.1:{ctl.server_port}"}), flush=True)
    try:
        world.server.serve_forever()
    finally:
        world.server.server_close()
        ctl.shutdown()


if __name__ == "__main__":
    main()
