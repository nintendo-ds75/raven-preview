"""A signed standing rule that answers a question in part is not set aside
for the records. The owner's other standing rules are read with it through
the joint path and settle it together when they cover the whole (sign-off
`rule`, both cited, the fit reason naming both calls); otherwise the rule's
answer is served as the partial for its owner to sign, with the records
cited as context, ahead of any composition from pull requests. A question
with no rule still composes from records; a rule whose conditions do not
hold settles nothing.

Measured on the Parcelwise dry run (Nov 2, task ec4272c763e4, decision
6ee6ac89dcf7): "What counts as one billable assistant use: a completed
answer, or every model attempt?" had Jordan's Jun 1 rule (one answer to one
question is one use) and his Sep 15 rule (every attempt counts in
usage_daily). The model read PR #216 as disagreeing with the Jun 1 rule,
the selector rejected both the rule and the record because neither answers
the whole question, and the ladder composed PR #80, ADR 0005 and PR #216
into a partial that asked Jordan to sign what he had already ruled on."""
import re
from unittest.mock import patch

from fixtures import OfflineCase
from raven import ladder, llm
from raven.config import Config

REPO = "parcelwise/parcelwise-app"
USE_Q = "What counts as one billable use of the assistant: a question, a completed answer, or tokens?"
USE_A = ("One answer to one question is one use (PR #80). Tokens, requests and streamed chunks are not units of use; "
         "the event is `assistant` with the merchant and the request id, the same shape as a label.")
RETRY_Q = "How are retries and failed assistant attempts counted: does a retried or timed-out attempt count as a use?"
RETRY_A = ("Yes to both since PR #216: every attempt we make is a usage row with its status and attempt number, and "
           "usage_daily counts attempts. The counter is what the model cost us, not what the merchant saw.")
ASKED = "What counts as one billable assistant use: a completed answer, or every model attempt?"
CONTEXT = ("#80 (Jordan): \"one answer to one question is one use\". #216 (Jordan): every attempt is a usage row and "
           "\"usage_daily counts attempts.\" app/usage/store.py:22-27 rollup counts all rows with status <> 'voided', so "
           "timed_out and failed attempts count. ADR 0005 says billing reads usage_daily.\n"
           "Paths: app/usage/store.py, app/usage/daily.py, app/assistant/chat.py")
# The grounded composer's reading of the Jun 1 rule alone: one side of the question, the other left open.
RULE_PARTIAL = ("One answer to one question is one use (PR #80); tokens, requests and streamed chunks are not units of "
                "use. The earlier answer does not establish: whether every model attempt counts as a use.")
# The joint composer's reading of the Jun 1 and Sep 15 rules together, within their words.
RULES_JOINT = ("One answer to one question is one use (PR #80), and since PR #216 every attempt is a usage row and "
               "usage_daily counts attempts: the counter is what the model cost us, not what the merchant saw.")
# The joint composer's reading of the pull requests, as the dry run stored it.
RECORDS_JOINT = ("PR #80 set one usage event per completed answer; PR #216 changed this: each retry attempt is its own "
                 "usage row and usage_daily counts attempts rather than what the merchant saw. usage_daily is what "
                 "billing reads.")
PR_80 = ("assistant: report one usage event when an answer completes (#80)",
         "Jordan's call on the review: one answer to one question is one use. Tokens, requests and streamed chunks are\n"
         "not units of use. The event is `assistant` with the merchant and the request id, the same shape as a label.\n\n"
         "Reviewed-by: Jordan Okafor <jordan@parcelwise.example>", "2026-06-01T16:15:00-07:00")
PR_216 = ("assistant: retry the model once on a timeout; every attempt is a usage row (#216)",
          "Timeouts are a third of the answers merchants never get; one retry with the same request id fixes most of\n"
          "them. Jordan's call on the review: every attempt we make is a usage row, with its status and attempt number,\n"
          "and usage_daily counts attempts. The counter is what the model cost us, not what the merchant saw.\n\n"
          "Reviewed-by: Jordan Okafor <jordan@parcelwise.example>", "2026-09-15T13:15:00-07:00")
PR_96 = ("usage: daily counts per merchant and feature, read by cursor (#96)",
         "ADR 0005. Readers (billing, the dashboard) read usage_daily, never usage_events: the raw table has replays and\n"
         "our own test shops in it. Reads page by cursor, never by offset, because the rollup inserts while a reader "
         "walks the table; a reader that wants a period total walks every\npage.\n\n"
         "Reviewed-by: Marcus Lee <marcus@parcelwise.example>", "2026-06-09T13:50:00-07:00")
PR_220 = ("web: labels and assistant answers this month on the Plan page (#220)",
          "The Plan page shows what the month has used: labels against the allowance, and assistant answers (no "
          "allowance\nand no charge for those yet). Reads usage_daily through month_to_date(), by cursor like "
          "everything else.\n\nReviewed-by: Jordan Okafor <jordan@parcelwise.example>", "2026-10-06T10:50:00-07:00")
ADR_0005 = ("ADR 0005: Usage is counted in usage_events and read from usage_daily, by cursor",
            "One append-only table, usage_events: one row per thing a merchant did that we may ever count. A nightly "
            "rollup folds it into usage_daily, one row per merchant, feature and day, and that is what billing and the "
            "dashboard read. PR #96.", "2026-06-09T21:00:00+00:00")


def semantic_on():
    return patch.object(Config, "semantic_retrieval", property(lambda self: not self.deterministic))


def model(select="none"):
    """The dry run's model, scripted: the conflict rung reads PR #216 as
    CONFLICT against an answer that does not speak of attempts and AGREE
    against one that does; the selector answers `select` (the dry run's
    "none": neither the rule nor the record answers the whole question);
    the grounded composer answers the Jun 1 rule in part and refuses any
    other row; the joint composer answers from the two rules when it is
    shown both, from the pull requests when it is shown those, and refuses
    otherwise; the support check finds every claim stated."""
    def complete(self, purpose, system, prompt, **kw):
        if purpose == "compose":
            return RULE_PARTIAL if re.search(r"Record \(earlier answer \w+, author/assignee: Jordan Okafor\):\nOne answer", prompt) \
                else "RECORD DOES NOT ANSWER"
        if purpose == "compose_joint":
            labels = {m.group(2): m.group(1) for m in re.finditer(r"(r\d) \((decision \w+|pr 80|pr 216|doc adr-0005)", prompt)}
            rules = [label for cit, label in labels.items() if cit.startswith("decision")]
            if len(rules) >= 2 and "one use (PR #80)" in prompt and "since PR #216" in prompt:
                return RULES_JOINT + "\nUSED_SOURCES: " + ",".join(rules[:2]) + "\nCOVERAGE: FULL"
            if "pr 80" in labels and "pr 216" in labels:
                return RECORDS_JOINT + "\nUSED_SOURCES: " + ",".join(labels[c] for c in ("pr 80", "pr 216")) + "\nCOVERAGE: PARTIAL"
            return "RECORDS DO NOT ANSWER"
        if purpose == "conflict":
            if "Record pr 216" not in prompt:
                return "UNRELATED"
            on_file = prompt.split("Answer on file:", 1)[1].split("\n\nRecord", 1)[0]
            return "AGREE" if "attempt" in on_file else "CONFLICT"
        return "RECORD DOES NOT ANSWER"

    def complete_json(self, purpose, system, prompt, **kw):
        if purpose == "support":
            return {"claims": [], "answer": "", "open": []}
        if purpose == "memory_rerank":
            first = re.search(r"\[([0-9a-f]+)\] scored", prompt)
            return {"id": first.group(1), "why": "the closest"} if first else {}
        if purpose in ("select", "precedent"):
            return {"pick": select}
        if purpose == "expand":
            return {"phrases": []}
        if purpose == "followup":
            return {"needs_context": False, "question": ""}
        return {}
    return [patch.object(llm.Client, "complete", complete), patch.object(llm.Client, "complete_json", complete_json)]


class PartialRuleCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store("partial-rule.db")
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.store.add_owner({"name": "Jordan Okafor", "team": "Platform", "patterns": "app/usage/*"})
        self.graph.set_setting("auto_rules", "1")
        # The dry run's records around the question: the lexical rung weighs PR #216 against the rule.
        for ref, kind, (title, body, when) in (("80", "pr", PR_80), ("216", "pr", PR_216), ("adr-0005", "doc", ADR_0005),
                                               ("96", "pr", PR_96), ("220", "pr", PR_220)):
            self.graph.upsert_intent(REPO, kind, ref, title, body, "Maya Chen" if kind == "pr" else "Jordan Okafor", when,
                                     status="decided" if kind == "doc" else "", paths=["app/usage/", "app/assistant/"])

    def rule(self, question, answer, when, by="Jordan Okafor", conditions=""):
        """A signed standing rule (scope any) by `by`, recorded at `when`, as
        the dry run's seeded rows were."""
        task = self.store.add_run({"title": "Earlier call", "repo": REPO})["id"]
        did = self.graph.add_decision(task, question, "data", "pending", repo=REPO, owner=by, path="app/usage/")
        self.store.answer(did, {"answer": answer, "signed_by": by, "source": f"inbox: {by}"})
        self.store.make_rule(did, {"by": by, "scope": "any", "conditions": conditions,
                                   "expected_updated_at": self.store.get_decision(did)["updated_at"]})
        self.graph.db.execute("UPDATE decisions SET created_at=?, updated_at=? WHERE id=?", (when, when, did))
        return did

    def jun1(self, conditions=""):
        return self.rule(USE_Q, USE_A, "2026-06-01T23:40:00+00:00", conditions=conditions)

    def sep15(self):
        return self.rule(RETRY_Q, RETRY_A, "2026-09-15T21:45:00+00:00")

    def ask(self, select="none"):
        self.task = self.store.add_run({"title": "Charge for the assistant", "repo": REPO})["id"]
        for p in [semantic_on()] + model(select):
            p.start()
            self.addCleanup(p.stop)
        return ladder.ask(self.store, Config(), self.task, ASKED, context=CONTEXT, path="app/usage/store.py",
                          category="data")

    def events(self, kind):
        return [r["detail"] for r in self.graph.db.execute("SELECT detail FROM events WHERE kind=?", (kind,))]

    def cited(self, did):
        return {r["related_id"] for r in self.graph.db.execute(
            "SELECT related_id FROM decision_links WHERE decision_id=? AND kind='derived'", (did,))}

    def record_roles(self, did):
        """The records pinned on the decision, by reference, with their role."""
        return {r["ref"]: r["role"] for r in self.graph.db.execute(
            "SELECT i.ref, e.role FROM decision_source_edges e JOIN source_records s ON s.id=e.record_id "
            "JOIN intents i ON i.id=s.intent_id WHERE e.decision_id=? AND e.active=1", (did,))}


class PartialRuleTests(PartialRuleCase):
    def test_a_rule_that_answers_in_part_settles_with_the_owners_other_rule(self):
        jun1, sep15 = self.jun1(), self.sep15()
        row = self.ask()
        self.assertEqual((row["status"], row["source"], row["signoff"]), ("resolved", "memory", "rule"), row["evidence"])
        self.assertTrue(row["authorized"])
        self.assertEqual((row["source_id"], row["signed_by"], row["answer"]), (jun1, "Jordan Okafor", RULES_JOINT))
        self.assertEqual(row["fit_reason"], "The same two questions Jordan Okafor already answered on Jun 1 and Sep 15; "
                                            "Jordan said both apply anywhere, so nobody needed to be asked.")
        self.assertEqual(self.cited(row["id"]), {jun1, sep15})
        self.assertIn(f"the rules Jordan Okafor made of decisions {jun1} and {sep15}, together", row["evidence"])
        self.assertIn("a standing rule is the closest call; read for the part it answers", row["evidence"])
        self.assertNotIn("CONFLICTED", row["answer"])
        self.assertNotIn("composed across records", row["evidence"])
        # The record the selector weighed against the rule is cited as context beside it.
        self.assertEqual(self.record_roles(row["id"]), {"216": "context"})
        read = self.events("rule_read_in_part")
        self.assertTrue(read and jun1 in read[0] and "pr 216" in read[0] and '"disputes": true' in read[0], read)
        self.assertIn('pr 216 covers this too: "Jordan\'s call on the review: every attempt we make is a usage row', row["evidence"])
        self.assertIn("read against the answer as served, it does not decide it differently", row["evidence"])
        self.assertFalse(self.events("memory_record_conflict"))
        self.assertFalse(any('"kind": "joint-composition"' in e for e in self.events("resolve")))

    def test_without_a_second_rule_the_rule_is_served_in_part_to_its_owner(self):
        jun1 = self.jun1()
        row = self.ask()
        self.assertEqual((row["status"], row["source"], row["source_id"]), ("partial", "memory", jun1), row["evidence"])
        self.assertNotEqual(row["signoff"], "rule")
        self.assertEqual(row["answered_by"], "Jordan Okafor")
        # The rule's own words, with what it leaves open noted beside them, not a reading of the pull requests.
        self.assertEqual(row["answer"], "One answer to one question is one use (PR #80); tokens, requests and streamed "
                                        "chunks are not units of use.")
        self.assertIn("whether every model attempt counts as a use", row["gaps"])
        self.assertNotIn("CONFLICTED", row["answer"])
        self.assertIn("settles this only in part, so a person still signs it here", row["fit_reason"])
        self.assertIn("Jordan said that answer applies anywhere", row["fit_reason"])
        self.assertIn("answers this only in part, so a person signs it", row["evidence"])
        self.assertNotIn("composed across records", row["evidence"])
        self.assertEqual(self.cited(row["id"]), {jun1})
        self.assertEqual(self.record_roles(row["id"]), {"216": "context"})
        self.assertFalse(any('"kind": "joint-composition"' in e for e in self.events("resolve")))
        self.assertFalse(self.events("memory_record_conflict"))

    def test_without_a_rule_the_records_still_compose(self):
        row = self.ask()
        self.assertEqual((row["status"], row["source"], row["answer"]), ("partial", "record", RECORDS_JOINT), row["evidence"])
        self.assertFalse(row["authorized"])
        self.assertTrue(row["evidence"].startswith("composed across records: "), row["evidence"])
        self.assertIn("answers this only in part", row["evidence"])
        self.assertTrue(any('"kind": "joint-composition"' in e for e in self.events("resolve")))
        self.assertFalse(self.events("rule_read_in_part"))

    def test_a_rule_whose_conditions_do_not_hold_settles_nothing(self):
        jun1 = self.jun1(conditions="plan=enterprise")
        self.sep15()
        row = self.ask()
        self.assertNotEqual(row["signoff"], "rule")
        self.assertFalse(row["authorized"])
        self.assertNotIn(jun1, self.cited(row["id"]))
        self.assertFalse(self.events("rule_read_in_part"))
        # The records answer in their place, as before.
        self.assertEqual((row["status"], row["source"]), ("partial", "record"), row["evidence"])
        self.assertTrue(row["evidence"].startswith("composed across records: "), row["evidence"])

    def test_a_selector_that_keeps_the_rule_settles_the_same_way(self):
        """The other way the pass can go: the selector prefers the rule, and
        the owner's other rule completes it all the same."""
        jun1, sep15 = self.jun1(), self.sep15()
        row = self.ask(select="m1")
        self.assertEqual((row["status"], row["signoff"], row["source_id"], row["answer"]), ("resolved", "rule", jun1, RULES_JOINT), row["evidence"])
        self.assertEqual(self.cited(row["id"]), {jun1, sep15})
        self.assertFalse(self.events("rule_read_in_part"))


class OwnerRuleTests(OfflineCase):
    def test_the_owners_other_current_rules_complete_a_rule_same_owner_and_scope_only(self):
        from types import SimpleNamespace
        mk = lambda id_, by="Jordan Okafor", scope="any", reusable=1, authorized=True, conditions="": SimpleNamespace(  # noqa: E731
            id=id_, answered_by=by, rule_by=by, rule_scope=scope, reusable=reusable, authorized=authorized,
            rule_conditions=conditions, rule_expires="", source_reuse_uncertain=False)
        item = mk("a1")
        cands = [(0.9, 0.9, mk("a1")), (0.8, 0.8, mk("b2")), (0.7, 0.7, mk("c3", by="Marcus Lee")),
                 (0.6, 0.6, mk("d4", scope="same")), (0.5, 0.5, mk("e5", reusable=0)), (0.4, 0.4, mk("f6", authorized=False)),
                 (0.3, 0.3, mk("g7", conditions="plan=enterprise")), (0.2, 0.2, mk("h8")), (0.1, 0.1, mk("b2"))]
        self.assertEqual([p.id for p in ladder._owner_rules(item, cands, ASKED, "", {}, REPO)], ["b2", "h8"])
        self.assertEqual([p.id for p in ladder._owner_rules(item, cands, ASKED, "", {"plan": "enterprise"}, REPO)],
                         ["b2", "g7", "h8"])
        self.assertEqual([p.id for p in ladder._owner_rules(item, cands, ASKED, "", {}, REPO, limit=1)], ["b2"])
