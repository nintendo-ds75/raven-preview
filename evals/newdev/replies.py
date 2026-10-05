"""What a person actually types, and whether Raven does the right thing.

Until now a Slack reply had to be phrased in Raven's words: `answer: X
because Y`, `sign off`, `not me @person`, `rule if ...`. Anything else,
including every ordinary way a busy person says what they decided, was
refused with a syntax reminder. That is the single largest friction on
the human side of the loop, and nothing measured it.

This panel is the phrasings, with what Raven must do for each. Two
kinds of mistake are not the same size. Refusing a real answer costs a
round trip. Recording something as a person's decision when they did not
decide it is the one mistake the whole contract exists to prevent, so
every case that is not plainly a decision must end with nothing
recorded.

    python3 -m evals.newdev.replies                 # deterministic only
    BRIDGE_MODEL_API=claude-cli python3 -m evals.newdev.replies --semantic

Exits non-zero if any case behaves unsafely. The `--semantic` arm needs
a model backend; without one the model column reads as the
deterministic one, which is the honest baseline.
"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))

QUESTION = "What should the new legacy-handling feature toggle default to, and which values may it take?"
# The agent's proposal, deliberately not what the owner will say: a reply
# that states a decision has to be recorded as what they said, and only a
# reply that agrees with this may be recorded as agreeing with it.
ON_TABLE = "A boolean: on or off. It ships off."

# phrasing, what has to happen, why this one is here
CASES = [
    ("answer: three states, off by default because we need reporting before we break anyone",
     "recorded", "Raven's own words still work"),
    ("sign off", "signed", "the shortest confirmation"),
    ("not me @wes", "handed on", "an explicit handoff"),
    ("Not a boolean. Go with three states, off, log and block, and ship it defaulting to off.",
     "recorded", "a plain statement of a different decision, no because, no prefix"),
    ("three states: off, log, block. off by default.",
     "recorded", "how somebody actually writes it in a hurry"),
    ("yeah that's right", "signed", "agreeing with the answer on the table"),
    ("lgtm", "signed", "the shortest agreement there is"),
    ("this isn't mine, wes owns the toggle registry", "handed on", "a handoff in prose"),
    ("what happens to plugins that already read the map?",
     "nothing", "a question back is not a decision"),
    ("hmm, I can see arguments both ways here", "nothing", "thinking aloud is not a decision"),
    ("ok", "nothing", "an acknowledgement is not agreement with anything"),
    ("I'll look at this tomorrow", "nothing", "a promise is not a decision"),
    ("we usually ship these things off by default",
     "nothing", "a general observation is not this decision"),
]


def outcome(store, delivery, decision_id: str, before: dict, reply: str) -> str:
    """What the reply did to the decision, in the terms the panel scores."""
    after = store.get_decision(decision_id)
    if (after.get("owner_name") or "") != (before.get("owner_name") or ""):
        return "handed on"
    if after.get("status") == "approved" and before.get("status") != "approved":
        return "signed" if (after.get("answer") or "") == (before.get("answer") or "") else "recorded"
    if (after.get("answer") or "") != (before.get("answer") or ""):
        return "recorded"
    if (after.get("signed_by") or "") != (before.get("signed_by") or ""):
        return "signed"
    return "nothing"


def run(semantic: bool) -> list[dict]:
    os.environ["BRIDGE_SEMANTIC"] = "1" if semantic else "0"
    os.environ.setdefault("BRIDGE_LIVE", "0")
    if not semantic:
        os.environ["BRIDGE_MODEL_API"] = "none"
    from fixtures import template_db  # noqa: E402
    from bridge import canvas  # noqa: E402
    from bridge.config import Config  # noqa: E402
    from bridge.store import Store  # noqa: E402
    from tests.test_delivery import FakeSlack  # noqa: E402

    import shutil
    out = []
    template, _ = template_db("qemulike")
    for phrasing, want, why in CASES:
        with tempfile.TemporaryDirectory(prefix="bridge-replies-") as tmp:
            db = Path(tmp) / "replies.db"
            shutil.copyfile(template, db)
            store = Store(db)
            slack = FakeSlack()
            store.delivery.transport = slack
            with store.graph.transaction():
                pid = store.graph.add_person("Engineer 24f6f6", email="paul@qemu.example", slack_id="UPAUL")
                store.graph.add_person("Wes Chen", email="wes@qemu.example", slack_id="UWES")
                store.graph.add_authority("path", "hw/net/*", "decides", person_id=pid)
            task = canvas.start_task(store, Config(model_api="none"), {
                "title": "Add the legacy-handling toggle", "repo": "qemulike", "agent": "eval",
                "requester": "Daniel Barboza", "paths": "hw/net/virtio-net.c"})
            node = canvas.add_node(store, Config(model_api="none"), {
                "task_id": task["task_id"], "question": QUESTION, "paths": "hw/net/virtio-net.c"})
            canvas.settle_node(store, {"task_id": task["task_id"], "node_id": node["node_id"],
                                       "answer": ON_TABLE, "rationale": "the agent's proposal"})
            store.delivery.deliver_now()
            sent = [m for m in slack.messages if m.get("thread_ts") is None or True]
            if not sent:
                out.append({"phrasing": phrasing, "want": want, "got": "no message", "ok": False, "why": why})
                continue
            thread = sent[-1]["ts"]
            channel = sent[-1]["channel"]
            before = store.get_decision(node["node_id"])
            said = store.delivery.receive(channel, thread, "UPAUL", phrasing, event_id="e1")
            got = outcome(store, store.delivery, node["node_id"], before, phrasing)
            # A reading Raven offers is taken by the person's `yes`; the
            # panel scores the pair, because that is what a person does.
            if got == "nothing" and "Reply `yes`" in (said or ""):
                said = store.delivery.receive(channel, thread, "UPAUL", "yes", event_id="e2")
                got = outcome(store, store.delivery, node["node_id"], before, "yes")
            out.append({"phrasing": phrasing, "want": want, "got": got, "ok": got == want,
                        "why": why, "said": (said or "")[:160]})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--semantic", action="store_true", help="let Raven read a reply with the model")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    results = run(args.semantic)
    width = max(len(r["phrasing"]) for r in results)
    for r in results:
        print(f"  {'ok  ' if r['ok'] else 'FAIL'}  {r['phrasing']:{width}}  want {r['want']:10} got {r['got']}")
        if not r["ok"]:
            print(f"        {r['why']}: {r.get('said', '')}")
    failed = [r for r in results if not r["ok"]]
    unsafe = [r for r in failed if r["want"] == "nothing"]
    print(f"\n{len(results) - len(failed)} of {len(results)} phrasings handled "
          f"({'model reading on' if args.semantic else 'deterministic only'})")
    if unsafe:
        print(f"{len(unsafe)} of them recorded something the person did not decide, which is the unsafe direction")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, indent=2) + "\n")
    return 1 if unsafe else 0


if __name__ == "__main__":
    raise SystemExit(main())
