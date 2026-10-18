"""The shape of a Slack message a person reads on a phone, over the
demo decisions and ten synthetic ones with long context and options:
the question within the first three lines, at most 1,200 characters
before the options, how to reply before the recorded scope, and no
paragraph said twice outside the stored context the message carries
verbatim. Measured before this shape: the scope table sat between the
link and the brief, the options were printed twice and the reply line
was seventeen lines down."""

import json

import unittest

from fixtures import OfflineCase

from bridge.approval_scope import transport_text
from bridge.delivery import LEAD_CONTEXT, render

LINK = "https://raven.example/brief#rvn_" + "0" * 32
PROSE = ("The retry path in billing/usage.py currently clamps the computed wait at the configured cap and then adds "
         "jitter, so near the cap the effective wait can exceed it by the jitter window. The metering change drafted "
         "for this task moves the clamp after the jitter so the cap is a hard ceiling. Operators at two enterprise "
         "accounts asked for the ceiling to hold exactly; the SDK team prefers the current behaviour because it keeps "
         "backoff spread under load. Both are plausible; the agent cannot settle it from the code alone. ")
OPTIONS = ["Clamp after jitter", "Jitter after clamp", "Keep today's behaviour and document it"]
KINDS = ("ask", "signoff", "review", "reassigned")


def synthetic(i: int) -> dict:
    """A decision as the store hands it to render: the context carries
    the paths and options canvas.add_node appends to it."""
    context = (PROSE * (1 + i % 3)).strip() + f"\nPaths: billing/usage_{i}.py\nOptions: " + " | ".join(OPTIONS)
    return {"id": f"d{i:011d}", "run_id": "r1", "repo": "acme/platform", "run_title": f"Synthetic task {i}",
            "requester": "Priya Natarajan", "run_agent": "Claude Code", "status": "pending",
            "question": f"Should billing/usage_{i}.py clamp the jittered wait at the cap (case {i})?",
            "context": context, "options": json.dumps(OPTIONS),
            "brief": ("The agent asks whether the cap is a hard ceiling. Two accounts want it exact; the SDK team "
                      "wants today's spread." if i % 2 else ""),
            "owner_evidence": "verified: Wes Chen decides for billing/* (config); area: billing",
            "evidence": ("memory: best match scored 0.3, under the 0.6 floor; records: retrieved context slack C1:1.2: "
                         "partner accounts are billed at list rate"),
            "answer": "Clamp after jitter." if i % 2 else "", "answered_by": "", "kind": "agent" if i % 2 else "new",
            "facts": json.dumps({"customer": f"acct-{i}", "environment": "production"}),
            "review_reason": "The upstream retention answer changed.", "prediction": "", "named_records": []}


def repeated_lines(text: str, context: str) -> list[str]:
    """Lines said twice, outside the stored context the message carries
    verbatim (its appended Options line repeats the structured one by
    contract: nothing a person authored is hidden)."""
    body = text.replace(context, "") if context else text
    seen: dict[str, int] = {}
    for line in body.split("\n"):
        line = line.strip()
        if len(line) >= 40:
            seen[line] = seen.get(line, 0) + 1
    return [line for line, n in seen.items() if n > 1]


class Shape:
    def check(self, text: str, question: str, context: str, has_options: bool):
        lines = text.split("\n")
        q = next((i for i, line in enumerate(lines) if line == f"*{transport_text(question)}*"), None)
        self.assertIsNotNone(q, text)
        self.assertLessEqual(q, 2, "the question is not within the first three lines:\n" + text)
        if has_options:
            opt = next(i for i, line in enumerate(lines) if line.startswith("Options: "))
            before = "\n".join(lines[:opt])
            self.assertLessEqual(len(before), 1200, f"{len(before)} characters before the options:\n{before}")
        reply = next(i for i, line in enumerate(lines) if line.startswith("Reply "))
        scope = next(i for i, line in enumerate(lines) if line.startswith("Decision scope "))
        self.assertLess(reply, scope, "how to reply comes after the recorded scope:\n" + text)
        self.assertTrue(lines[-1].startswith("_Raven is an AI coordination assistant"), lines[-1])
        self.assertEqual(repeated_lines(text, transport_text(context)), [], text)


class SyntheticShapeTests(unittest.TestCase, Shape):
    def test_ten_long_decisions_read_in_order_for_every_kind(self):
        for i in range(10):
            row = synthetic(i)
            for kind in KINDS:
                with self.subTest(case=i, kind=kind):
                    text = render(row, kind, "Wes Chen", "https://raven.example", "", task_link=LINK,
                                  has_account=False, rule_hint=False)["text"]
                    self.check(text, row["question"], row["context"], True)
                    # The complete context is in the message once the lead cut it.
                    self.assertIn(transport_text(row["context"]), text)
                    if not row["brief"]:
                        self.assertIn("the task page has the rest", text)

    def test_a_short_context_is_said_once_and_a_brief_replaces_it_in_the_lead(self):
        row = {**synthetic(0), "context": "Usage jumped 11x on enterprise-two.\nOptions: Bill it | Exclude the load test"}
        text = render(row, "ask", "Wes Chen", "", "")["text"]
        self.assertEqual(text.count("Usage jumped 11x"), 1)
        self.assertLessEqual(len(row["context"]), LEAD_CONTEXT)
        lines = text.split("\n")
        self.assertLess(lines.index("Context: Usage jumped 11x on enterprise-two."),
                        lines.index("Options: Bill it | Exclude the load test"))
        briefed = render({**row, "brief": "The spike came from a tagged load test."}, "ask", "Wes Chen", "", "")["text"]
        briefed_lines = briefed.split("\n")
        self.assertIn("The spike came from a tagged load test.", briefed_lines)
        self.assertLess(briefed_lines.index("The spike came from a tagged load test."),
                        briefed_lines.index("Decision scope \\u0028recorded fields; no facts inferred from prose\\u0029:"))
        # With a brief, the context moves whole into the appendix, after the scope.
        self.assertGreater(briefed_lines.index("Context: Usage jumped 11x on enterprise-two."),
                           next(i for i, line in enumerate(briefed_lines) if line.startswith("Reply in this thread: `answer:")))

    def test_a_context_that_is_only_appended_paths_and_options_does_not_lead(self):
        row = {**synthetic(0), "context": "Paths: billing/usage.py\nOptions: Bill it | Exclude the load test",
               "options": json.dumps(["Bill it", "Exclude the load test"])}
        lines = render(row, "ask", "Wes Chen", "", "")["text"].split("\n")
        options = lines.index("Options: Bill it | Exclude the load test")
        self.assertFalse([line for line in lines[:options] if line.startswith("Context: ")], lines[:options])
        # Whole in the appendix: a reply is checked against the complete context.
        self.assertIn("Context: Paths: billing/usage.py", lines[options + 1:])
        self.assertEqual(sum("Exclude the load test" in line for line in lines), 2)

    def test_a_brief_that_only_repeats_the_context_is_not_said_twice(self):
        row = {**synthetic(0), "context": "Usage jumped 11x on enterprise-two.", "brief": "Usage jumped 11x on enterprise-two."}
        text = render(row, "ask", "Wes Chen", "", "")["text"]
        self.assertEqual(text.count("Usage jumped 11x"), 1)

    def test_the_reply_line_is_one_line_that_fits_the_kind(self):
        for kind in KINDS:
            text = render(synthetic(1), kind, "Wes Chen", "", "", rule_hint=False)["text"]
            reply = [line for line in text.split("\n") if line.startswith("Reply ")]
            self.assertEqual(len(reply), 1, text)
            self.assertLess(len(reply[0]), 200, reply[0])
            self.assertIn("`answer: <the decision> because <why>`", reply[0])
            self.assertNotIn("inference backend", reply[0])


class DemoShapeTests(OfflineCase, Shape):
    """The decisions a demo workspace seeds, sent through the outbox."""

    class Slack:
        name = "slack"

        def __init__(self):
            self.messages = []

        def open_dm(self, user_id):
            return "D" + user_id

        def post_message(self, channel, text, blocks=None, thread_ts=""):
            self.messages.append({"channel": channel, "text": text})
            return f"1.{len(self.messages)}"

    def test_the_demo_decisions_read_in_order(self):
        store = self.fresh_store("demo.db")
        store.graph.set_setting("slack_interaction_mode", "conversation")
        slack = self.Slack()
        store.connect_delivery(slack, base_url="https://raven.example")
        # The demo owners have no Slack ids; their messages reach the
        # triage channel with a note saying who was meant.
        store.update_settings({"slack_fallback_channel": "C-RAVEN"})
        store.seed()
        store.delivery.deliver_now()
        self.assertGreaterEqual(len(slack.messages), 4)
        self.assertTrue(all("posted here instead" in m["text"] for m in slack.messages))
        decisions = store.graph.db.execute("SELECT question, context, options FROM decisions").fetchall()
        for m in slack.messages:
            row = next(d for d in decisions if f"*{transport_text(d['question'])}*" in m["text"].split("\n"))
            with self.subTest(question=row["question"]):
                self.check(m["text"], row["question"], row["context"] or "", bool(json.loads(row["options"] or "[]")))


if __name__ == "__main__":
    unittest.main()
