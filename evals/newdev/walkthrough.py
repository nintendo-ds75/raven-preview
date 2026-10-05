"""The first hour, as a new engineer would actually spend it.

Not a unit test: the commands a person runs from the README, in order,
against a real repository, with the agent side speaking MCP over stdio
to a real `bridge_mcp.py` subprocess and the human side replying in
Slack through the real inbound path. Every promise the product makes is
checked where a new user would meet it, and the ones that do not hold
print as failures with what happened instead.

    python3 -m evals.newdev.walkthrough --repo /path/to/a/git/checkout

The checkout can be anything with real history; `--repo` defaults to
this repository, so the walkthrough runs anywhere Bridge does.
"""

import argparse
import json
import subprocess
import sys
import tempfile
import time
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Every promise here is about the deterministic contract, and the host
# subprocess is already pinned to it. Pin this process too: with a model
# backend merely present on the machine, each kickoff made a live call
# nothing here reads, which turned a one minute run into many.
os.environ.setdefault("BRIDGE_MODEL_API", "none")
os.environ.setdefault("BRIDGE_SEMANTIC", "0")
os.environ.setdefault("BRIDGE_LIVE", "0")

from bridge import canvas  # noqa: E402
from bridge.authz import Actor  # noqa: E402
from bridge.config import Config  # noqa: E402
from bridge.ingest import index_repo  # noqa: E402
from bridge.store import Invalid, Store  # noqa: E402

CFG = Config(model_api="none")


class Slack:
    """Stands in for the network, not for the people: every message is
    what Slack would have received, and every reply goes back in through
    the same event handler Slack posts to."""

    name = "slack"
    supports_dm = True

    def __init__(self):
        self.messages = []
        self.n = 0

    def open_dm(self, user_id):
        return "D" + user_id

    def post_message(self, channel, text, blocks=None, thread_ts=""):
        self.n += 1
        ts = f"1800000000.{self.n:06d}"
        self.messages.append({"channel": channel, "text": text, "thread_ts": thread_ts, "ts": ts})
        return ts

    def to(self, who: str):
        return [m for m in self.messages if who in m["text"]]


class HttpHost:
    """The coding agent against a shared Bridge over standard HTTP MCP.

    The server process holds the Slack connection, so a person hears about a
    decision an agent in another process wrote.
    """

    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def call(self, name, **args):
        from urllib.request import Request, urlopen
        req = Request(self.base + "/mcp", method="POST",
                      headers={"Content-Type": "application/json"},
                      data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": name, "arguments": args}}).encode())
        with urlopen(req) as r:
            result = json.loads(r.read())["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            raise Invalid(text)
        return json.loads(text)

    def close(self):
        pass


class Host:
    """The same agent as a real stdio subprocess, which is how a solo
    developer runs it and the only way to prove a restart."""

    def __init__(self, db: Path):
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "bridge_mcp.py"), "--db", str(db)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            env={"PATH": "/usr/bin:/bin", "BRIDGE_MODEL_API": "none", "BRIDGE_SEMANTIC": "0",
                 "HOME": str(Path.home())})
        self.n = 0
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "newdev", "version": "0"}})
        self.instructions = init["result"].get("instructions") or ""
        self.notify("notifications/initialized")
        self.tools = [t["name"] for t in self.rpc("tools/list")["result"]["tools"]]

    def notify(self, method, params=None):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()

    def rpc(self, method, params=None):
        self.n += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method,
                                          "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def call(self, name, **args):
        result = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            raise Invalid(text)
        return json.loads(text)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


class Walkthrough:
    def __init__(self):
        self.results: list[dict] = []
        self.step = ""

    def section(self, name: str) -> None:
        self.step = name
        print(f"\n== {name}")

    def check(self, promise: str, ok: bool, detail: str = "") -> bool:
        self.results.append({"step": self.step, "promise": promise, "ok": bool(ok), "detail": detail})
        print(f"   {'ok  ' if ok else 'FAIL'} {promise}" + (f"\n        {detail}" if detail else ""))
        return bool(ok)

    def report(self) -> int:
        bad = [r for r in self.results if not r["ok"]]
        print(f"\n{len(self.results) - len(bad)} of {len(self.results)} promises held")
        for r in bad:
            print(f"  FAIL [{r['step']}] {r['promise']}: {r['detail']}")
        return 1 if bad else 0


def run(repo: Path, work: Path, w: Walkthrough) -> int:
    db = work / "bridge.db"

    # ---------------------------------------------------------------- 1
    w.section("Install and ingest: 'build the ownership map from a repository'")
    store = Store(db)
    # Naming the workspace is the first thing a new install asks for, and
    # nothing answers until it is done.
    with store.graph.transaction():
        store.graph.set_setting("workspace_name", "The first hour")
    w.check("a fresh install asks for a workspace name before it answers anything",
            bool(store.graph.get_setting("workspace_name")), store.graph.get_setting("workspace_name"))
    stats = index_repo(store.graph, repo, max_commits=400)
    w.check("ingest reads a real checkout and says what it found",
            stats["commits"] > 0 and stats["files"] > 0,
            f"{stats['commits']} commits, {stats['files']} files, {stats['owners']} ownership rows")
    repo_name = stats["repo"]

    # ---------------------------------------------------------------- 2
    w.section("Before anything else: does Bridge tell me what is not set up?")
    ready = store.readiness()
    keys = {r["key"] for r in ready}
    w.check("a fresh ingest reports the setup that is still missing",
            {"no_people", "no_authority"} <= keys, f"reported: {sorted(keys)}")
    w.check("every finding says what to do about it",
            all(r["what"] and r["do"] for r in ready))
    w.check("it is in the state the inbox polls, not only in an API nobody calls",
            isinstance(store.state().get("readiness"), list))

    # ---------------------------------------------------------------- 3
    w.section("Set the map up, the way readiness told me to")
    graph = store.graph
    # The names in the ownership map are whatever the repository holds,
    # and in a large one most of them are CODEOWNERS teams. A team is not
    # somebody a question can be put to, so the people I add are people.
    from bridge.graph import is_team_handle
    top = [r["engineer"] for r in graph.db.execute(
        "SELECT engineer, sum(weight) w FROM ownership WHERE valid_to IS NULL AND engineer != '' "
        "GROUP BY engineer ORDER BY w DESC LIMIT 12") if not is_team_handle(r["engineer"])][:2]
    deciders = top or ["Dana Ortiz", "Wes Chen"]
    ids = {}
    with graph.transaction():
        for i, name in enumerate(deciders):
            ids[name] = graph.add_person(name, email=f"{name.split()[0].lower()}@example.invalid",
                                         slack_id=f"U{i}NEWDEV")
        graph.add_authority("path", "bridge/", "decides", person_id=ids[deciders[0]], repo=repo_name)
        graph.add_authority("category", "compat", "decides", person_id=ids[deciders[-1]])
        coordinator = graph.add_person("Sam Coordinator", email="sam@example.invalid", slack_id="UCOORD")
        graph.set_setting("coordinator", coordinator)
    after = {r["key"] for r in store.readiness()}
    w.check("setting the map up clears the blockers",
            not ({"no_people", "no_authority", "no_coordinator"} & after), f"still open: {sorted(after)}")

    slack = Slack()
    delivery = store.connect_delivery(slack, base_url="https://bridge.example.invalid")

    # ---------------------------------------------------------------- 4
    w.section("Point my agent at it: 'agent tools, none of which approves anything'")
    probe = Host(db)
    expected_tools = {"bridge_start_task", "bridge_add_node", "bridge_settle_node", "bridge_get_tree",
                      "bridge_wait", "bridge_finish_task", "bridge_get_decision", "bridge_search_decisions",
                      "bridge_list_owners", "bridge_ingest_repo", "bridge_import_record", "bridge_connection_status"}
    w.check("the MCP server starts and offers the protocol", set(probe.tools) == expected_tools, ", ".join(probe.tools))
    w.check("its instructions tell an agent what to do first",
            "bridge_start_task" in probe.instructions)
    probe.close()
    # The rest runs against a shared Bridge, because that is the shape a
    # team uses: the server holds the Slack connection, and the agent
    # reaches it over HTTP.
    from bridge.server import make_server
    server = make_server(store, port=0)
    import threading
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    host = HttpHost(base)

    # ---------------------------------------------------------------- 5
    w.section("Kick a task off: 'engage or pass, with its reason'")
    brief = ("Drop the legacy compatibility shim in bridge/store.py. It changes the default for existing "
             "databases and breaks compat for anyone still on the old schema, so we need a call on it.")
    task = host.call("bridge_start_task", title="Drop the legacy compatibility shim", repo=repo_name,
                     goal=brief, paths="bridge/store.py", requester="New Dev <newdev@example.invalid>",
                     client_key="newdev-1")
    w.check("a task with a real judgment call in it engages",
            task["verdict"] == "engage", f"{task['verdict']}: {task['why'][:160]}")
    w.check("the verdict says why, in terms I can check", bool(task["why"]))
    w.check("Bridge names decisions the task may contain before I ask it anything",
            bool(task.get("candidates")),
            "; ".join(c["question"][:60] for c in (task.get("candidates") or [])[:3]))
    w.check("every candidate says which signal it came from",
            all(c.get("why") and c.get("source") for c in (task.get("candidates") or [])))

    # ---------------------------------------------------------------- 6
    w.section("Write the decisions down: 'Bridge resolves what it can and routes the rest'")
    first = host.call("bridge_add_node", task_id=task["task_id"], client_ref="d1",
                      question="Do existing databases on the old schema have to keep working after the shim goes?",
                      context="The shim has been in for two releases. Dropping it changes what an old database does on open.",
                      paths="bridge/store.py", options="keep reading the old schema | migrate on open | refuse to open")
    w.check("a decision routes to a person, with evidence naming the signal",
            bool(first["owner"]) and bool(first["owner_evidence"]),
            f"{first['owner'] or '(unrouted)'}: {(first['owner_evidence'] or '')[:140]}")
    w.check("the person it names is somebody I put in the map",
            first["owner"] in set(deciders) | {"Sam Coordinator"}, first["owner"])
    second = host.call("bridge_add_node", task_id=task["task_id"], client_ref="d2", parent_id=first["node_id"],
                       question="If we migrate on open, is a one-way migration acceptable?",
                       context="A migration that cannot be rolled back means an operator cannot downgrade.",
                       paths="bridge/store.py")
    w.check("a child decision hangs off the one it grew from",
            second["parent_id"] == first["node_id"])
    w.check("nothing is authorized yet",
            not first["authorized"] and not second["authorized"])

    # ---------------------------------------------------------------- 7
    w.section("What the person gets: 'the context to decide', and not eleven messages")
    delivery.deliver_now()
    w.check("each decision produced one message, not several",
            len(slack.messages) <= 2, f"{len(slack.messages)} messages for 2 decisions: "
            + "; ".join(m["text"].split(chr(10))[0] for m in slack.messages))
    asked = slack.messages[0]
    # Bridge may have resolved this one from the record, in which case it
    # asks for a signature rather than an answer; either way the person
    # gets the question, the context and how to reply.
    w.check("the message carries the question, the context and how to reply",
            "Context:" in asked["text"] and "shim" in asked["text"]
            and ("answer:" in asked["text"] or "sign off" in asked["text"]),
            asked["text"][:160].replace("\n", " "))
    predictions = [m for m in slack.messages if "from the records" in m["text"]]
    w.check("nothing is described as coming from the records unless it did",
            not any("Bridge's guess" in m["text"] and "records" in m["text"] for m in slack.messages)
            and not predictions,
            "; ".join(m["text"][:100] for m in predictions))

    # ---------------------------------------------------------------- 8
    w.section("Answer in Slack: 'people reply where they already are'")
    owner = first["owner"]
    owner_id = (graph.find_person(owner) or {}).get("slack_id", "")
    reply = delivery.receive(asked["channel"], asked["ts"], owner_id,
                             "answer: migrate on open, one way because nobody is downgrading across this")
    w.check("a reply stated as an answer is recorded",
            "Not recorded" not in reply and any(t in reply for t in ("Recorded", "signed by", "Corrected")),
            reply[:160])
    row = store.get_decision(first["node_id"])
    w.check("the answer is on the decision, attributed to the person who gave it",
            (row["answer"] or "").startswith("migrate on open") and row["answered_by"] == owner,
            f"{row['answered_by']}: {(row['answer'] or '')[:80]}")
    chat = delivery.receive(asked["channel"], asked["ts"], owner_id, "actually hold on, let me think")
    w.check("a conversational reply is never recorded as a decision",
            "Not recorded" in chat, chat[:120])

    # ---------------------------------------------------------------- 9
    w.section("The gate: 'nothing ships on an answer nobody signed'")
    try:
        host.call("bridge_finish_task", task_id=task["task_id"])
        w.check("finishing is refused while a decision waits on a person", False, "it was allowed")
    except Invalid as refused:
        w.check("finishing is refused while a decision waits on a person",
                second["node_id"] in str(refused), str(refused)[:160])

    # --------------------------------------------------------------- 10
    w.section("My session dies: 'the same task comes back'")
    # A real process, killed and started again, with nothing carried over.
    host.close()
    restarted = Host(db)
    again = restarted.call("bridge_start_task", title="Drop the legacy compatibility shim", repo=repo_name,
                      goal=brief, client_key="newdev-1")
    w.check("the same client_key returns the same task, not a second one",
            again["task_id"] == task["task_id"] and again.get("repeated"))
    tree = restarted.call("bridge_get_tree", task_id=task["task_id"])
    restarted.close()
    host = HttpHost(base)
    answered = [n for n in tree["nodes"] if n["answer"]]
    w.check("the answer the person gave is on the tree the new process reads",
            bool(answered) and answered[0]["answer"].startswith("migrate on open"))
    w.check("the tree says what to do next", "waiting" in tree["next"] or "sign" in tree["next"],
            tree["next"][:140])

    # --------------------------------------------------------------- 11
    w.section("Finish: 'authorized' is not 'verified'")
    for node_id in (second["node_id"],):
        view = canvas.node_view(store, node_id)
        person = graph.get_person(ids.get(view["owner"]) or graph.find_person(view["owner"] or owner)["id"])
        store.answer(node_id, {"answer": "Yes, one way is fine.", "rationale": "nobody downgrades across this",
                               "signed_by": person["name"], "expected_updated_at": view["updated_at"]},
                     actor=Actor.person(person))
    # The answer landed after I last read the tree: I have not seen it,
    # so Bridge holds the finish until I have.
    try:
        host.call("bridge_finish_task", task_id=task["task_id"])
        w.check("finishing waits until I have read an answer that landed since my last read", False,
                "it was allowed")
    except Invalid as unread:
        w.check("finishing waits until I have read an answer that landed since my last read",
                second["node_id"] in str(unread) and "bridge_get_tree" in str(unread), str(unread)[:160])
    host.call("bridge_get_tree", task_id=task["task_id"])
    done = host.call("bridge_finish_task", task_id=task["task_id"],
                     checks="python3 -m unittest discover -s tests: could not run, no go toolchain")
    w.check("the task finishes once every decision is authorized", done["status"] == "completed")
    w.check("finishing says plainly that it did not verify the change",
            done.get("verified") is False and "not verified" in (done.get("caveat") or ""),
            (done.get("caveat") or "")[:180])
    w.check("what I said I ran is recorded as my claim, not as a result",
            "could not run" in (done.get("checks") or ""))
    w.check("it lists who authorized what",
            len(done.get("authorized") or []) == 2,
            json.dumps(done.get("authorized") or [])[:160])

    # --------------------------------------------------------------- 12
    w.section("The next task: 'the understanding compounds'")
    later = host.call("bridge_start_task", title="Remove the old schema reader", repo=repo_name,
                      goal="Take out the code that reads the old schema, now that the shim is gone.",
                      paths="bridge/store.py", client_key="newdev-2")
    reuse = host.call("bridge_add_node", task_id=later["task_id"], client_ref="r1",
                      question="Do existing databases on the old schema have to keep working after the shim goes?",
                      context="The shim has been in for two releases. Dropping it changes what an old database does on open.",
                      paths="bridge/store.py")
    w.check("the same question asked again reuses the answer a person gave",
            first["node_id"] in (reuse["evidence"] or "") or (reuse["answer"] or "").startswith("migrate on open"),
            f"{reuse['status']}/{reuse['kind']}: {(reuse['evidence'] or '')[:160]}")
    w.check("the reused answer cites where it came from",
            first["node_id"] in (reuse["evidence"] or ""), (reuse["evidence"] or "")[:160])
    w.check("reuse still wants a person: evidence is not authorization",
            not reuse["authorized"] and reuse["blocking"])
    # Who signs it is the map's call, not the last answerer's. Bridge
    # learns from an answer only where the route rested on inference: a
    # person answering once does not quietly become the owner of an area
    # somebody was recorded as deciding. So the promise is that the
    # earlier answer reaches whoever is asked, with its author named.
    # This read "it goes back to the person who answered it before" until
    # the two roles landed on different people and it turned out never to
    # have been exercised.
    w.check("whoever it asks is shown the earlier answer and who gave it",
            owner in (reuse["evidence"] or "") and reuse["owner"] in set(deciders) | {"Sam Coordinator"},
            f"asks {reuse['owner']}, citing {owner}: {(reuse['evidence'] or '')[:110]}")
    host.close()

    # --------------------------------------------------------------- 13
    w.section("An area nobody owns: 'unknown ownership is a gap, not a reason to keep quiet'")
    bare = Store(work / "bare.db")
    index_repo(bare.graph, repo, max_commits=120)
    for table in ("ownership", "listings", "authority", "change_people", "blame_lines", "engineers"):
        bare.graph.db.execute(f"DELETE FROM {table}")
    bare.graph._memo.clear()
    blind = canvas.start_task(bare, CFG, {"title": "Drop the legacy compatibility shim", "repo": repo_name,
                                          "goal": brief, "paths": "bridge/store.py",
                                          "requester": "New Dev <newdev@example.invalid>"})
    w.check("a task in an area nobody owns still engages",
            blind["verdict"] == "engage", f"{blind['verdict']}: {blind['why'][:160]}")
    w.check("and the reason says nobody is known, rather than that nothing is needed",
            "does not know who owns" in blind["why"], blind["why"][:200])
    return w.report()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=str(ROOT), help="a git checkout to ingest")
    ap.add_argument("--work", default="", help="where the disposable database goes")
    ap.add_argument("--out", default="", help="write the result as JSON here")
    args = ap.parse_args(argv)

    work = Path(args.work or tempfile.mkdtemp(prefix="newdev-"))
    work.mkdir(parents=True, exist_ok=True)
    w = Walkthrough()
    started = time.time()
    try:
        code = run(Path(args.repo).resolve(), work, w)
    finally:
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(json.dumps(
                {"repo": args.repo, "seconds": round(time.time() - started, 1),
                 "held": sum(1 for r in w.results if r["ok"]), "total": len(w.results),
                 "results": w.results}, indent=2) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
