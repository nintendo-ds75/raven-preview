"""A record that describes the state of the code does not veto a signed
standing rule; only a record that decides the same question differently
does, and then the node quotes the record's claim and says why the rule
was withheld.

Measured on the Parcelwise dry run (Nov 2, task 638ea3c58127): PR #220,
"web: labels and assistant answers this month on the Plan page", whose body
says assistant answers have "no allowance and no charge for those yet",
turned three signed rules (what counts as one use, which table billing
reads, where charges post) into CONFLICTED sign-offs; PR #71, older than
the rule it was set against and describing how the raw table is written,
did the same; every card quoted the PR title as the contrary statement;
and each fit reason read "Jordan did not make it a standing rule" beside
a row with reusable=1 and rule_scope='any'."""
import datetime
from types import SimpleNamespace
from unittest.mock import patch

from fixtures import OfflineCase
from raven import ladder, llm
from raven.config import Config
from raven.judgment import fit_reason

REPO = "parcelwise/parcelwise-app"
USE_Q = "What counts as one billable use of the assistant: a question, a completed answer, or tokens?"
USE_A = ("One answer to one question is one use (PR #80). Tokens, requests and streamed chunks are not units of use; "
         "the event is `assistant` with the merchant and the request id, the same shape as a label.")
USE_ASKED = "What counts as one billable assistant use?"
TABLE_Q = "Which table does billing read usage from: usage_daily or the raw usage_events?"
TABLE_A = ("usage_daily (ADR 0005, PR #96): one row per merchant, feature and day. The raw usage_events table stays "
           "inside app/usage; it has replays and our own test shops in it.")
TABLE_ASKED = "Which table does assistant billing read usage from?"
POST_Q = "Do usage charges post at purchase time, or through the nightly billing job?"
POST_A = ("Through the nightly billing job, app/billing/nightly.py (ADR 0006, PR #113). Nothing charges at purchase "
          "time; a new metered feature is a plan entry and a usage feature, not a code path of its own in billing.")
POST_ASKED = "Where do assistant charges post?"
PR_220_TITLE = "web: labels and assistant answers this month on the Plan page (#220)"
PR_220_BODY = ("The Plan page shows what the month has used: labels against the allowance, and assistant answers (no "
               "allowance\nand no charge for those yet). Reads usage_daily through month_to_date(), by cursor like "
               "everything else.\n\nReviewed-by: Jordan Okafor <jordan@parcelwise.example>")
PR_71_TITLE = "usage: usage_events, one row per label bought (#71)"
PR_71_BODY = ("One table for everything we may ever count: append-only, one row per thing a merchant did, with the "
              "merchant,\nthe feature, the request id and when. Label purchases write a row from the route; "
              "merchants.labels_this_month\nis derived from it from here on instead of bumped in place. Nothing reads "
              "the raw table for a number.\n\nReviewed-by: Marcus Lee <marcus@parcelwise.example>")
ADR_TITLE = "ADR 0009: a billable assistant use is a question asked"
ADR_BODY = ("Status: accepted. We decided that one billable assistant use is one question a merchant asks the "
            "assistant, whether or not an answer completes. Tokens and completed answers are not units of use.\n\n"
            "Reviewed-by: Priya Shah <priya@parcelwise.example>")


def semantic_on():
    return patch.object(Config, "semantic_retrieval", property(lambda self: not self.deterministic))


def model(verdict="CONFLICT", composed=""):
    """A scripted model: the conflict rung answers `verdict` for every
    record (the dry run's model read PR #220 as CONFLICT), every selector
    prefers the memory candidate, and the grounded composer answers
    `composed` or refuses."""
    def complete(self, purpose, system, prompt, **kw):
        if purpose == "conflict":
            return verdict
        if purpose == "compose" and composed:
            return composed
        if purpose == "compose_joint":
            return "RECORDS DO NOT ANSWER"
        return "RECORD DOES NOT ANSWER"

    def complete_json(self, purpose, system, prompt, **kw):
        if purpose in ("select", "precedent"):
            return {"pick": "m1"}
        if purpose == "expand":
            return {"phrases": []}
        if purpose == "followup":
            return {"needs_context": False, "question": ""}
        return {"same": False}
    return [patch.object(llm.Client, "complete", complete), patch.object(llm.Client, "complete_json", complete_json)]


class ConflictCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store("record-conflict.db")
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.store.add_owner({"name": "Jordan Okafor", "team": "Platform", "patterns": "app/usage/*"})
        self.graph.set_setting("auto_rules", "1")

    def rule(self, question, answer, by="Jordan Okafor", path="app/usage/"):
        task = self.store.add_run({"title": "Earlier call", "repo": REPO})["id"]
        did = self.graph.add_decision(task, question, "policy", "pending", repo=REPO, owner=by, path=path)
        self.store.answer(did, {"answer": answer, "signed_by": by, "source": f"inbox: {by}"})
        self.store.make_rule(did, {"by": by, "scope": "any",
                                   "expected_updated_at": self.store.get_decision(did)["updated_at"]})
        return did

    def record(self, kind, ref, title, body, days_from_now, author="Lena Park", paths=("app/usage/",)):
        when = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days_from_now)).isoformat()
        self.graph.upsert_intent(REPO, kind, ref, title, body, author, when, paths=list(paths))

    def ask(self, question, path="app/usage/store.py", verdict="CONFLICT", composed=""):
        task = self.store.add_run({"title": "Charge for the assistant", "repo": REPO})["id"]
        patches = [semantic_on()] + model(verdict, composed)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return ladder.ask(self.store, Config(), task, question, path=path, category="definition")

    def events(self, kind):
        return [r for r in self.graph.db.execute("SELECT detail FROM events WHERE kind=?", (kind,)).fetchall()]


class StatusRecordTests(ConflictCase):
    def test_a_pr_that_describes_what_shipped_does_not_veto_a_signed_rule(self):
        """The dry run's first case: PR #220 is newer than Jordan's rule and
        the model read it as CONFLICT; its body only says nothing is
        charged yet. The rule settles and the PR is noted beside it."""
        source = self.rule(USE_Q, USE_A)
        self.record("pr", "220", PR_220_TITLE, PR_220_BODY, days_from_now=1)
        row = self.ask(USE_ASKED)
        self.assertEqual((row["status"], row["source"], row["signoff"]), ("resolved", "memory", "rule"), row["evidence"])
        self.assertEqual((row["source_id"], row["answer"]), (source, USE_A))
        self.assertTrue(row["authorized"])
        self.assertNotIn("CONFLICTED", row["answer"])
        self.assertIn('pr 220 covers this too: "The Plan page shows what the month has used: labels against the '
                      'allowance, and assistant answers (no allowance and no charge for those yet).";', row["evidence"])
        self.assertIn("describes what has shipped, not a different rule", row["evidence"])
        self.assertNotIn(PR_220_TITLE, row["evidence"])
        self.assertIn("nobody needed to be asked", row["fit_reason"])
        self.assertNotIn("did not make it a standing rule", row["fit_reason"])
        self.assertFalse(self.events("memory_record_conflict"))
        self.assertTrue(any("pr 220" in e["detail"] for e in self.events("rule_over_record")))

    def test_a_record_older_than_the_rule_does_not_veto_it(self):
        """The dry run's second case: PR #71 predates Jordan's rule on which
        table billing reads, and describes how the raw table is written."""
        source = self.rule(TABLE_Q, TABLE_A)
        self.record("pr", "71", PR_71_TITLE, PR_71_BODY, days_from_now=-30, author="Jordan Okafor")
        # A near match (the dry run measured 0.80): the composer grounds the rule's answer in the question.
        row = self.ask(TABLE_ASKED, path="app/usage/daily.py", composed=TABLE_A)
        self.assertEqual((row["status"], row["signoff"], row["source_id"]), ("resolved", "rule", source), row["evidence"])
        self.assertEqual(row["answer"], TABLE_A)
        self.assertIn("pr 71 covers this too:", row["evidence"])
        self.assertIn("the rule is the newer call", row["evidence"])
        self.assertIn("nobody needed to be asked", row["fit_reason"])
        self.assertFalse(self.events("memory_record_conflict"))

    def test_a_rule_reached_by_meaning_keeps_the_note_in_its_evidence(self):
        """The dry run's third case: Marcus's rule on where charges post was
        reached by the semantic rung, and PR #220 vetoed it too."""
        source = self.rule(POST_Q, POST_A, by="Marcus Lee", path="app/billing/")
        self.record("pr", "220", PR_220_TITLE, PR_220_BODY, days_from_now=1, paths=("app/billing/", "app/usage/"))
        row = self.ask(POST_ASKED, path="app/billing/nightly.py", composed=POST_A)
        self.assertEqual((row["status"], row["signoff"], row["source_id"]), ("resolved", "rule", source), row["evidence"])
        self.assertIn("reused from decision", row["evidence"])
        self.assertNotIn("CONFLICTED", row["answer"])
        self.assertIn("pr 220 covers this too:", row["evidence"])
        self.assertIn("nobody needed to be asked", row["fit_reason"])
        self.assertFalse(self.events("memory_record_conflict"))

    def test_a_newer_decision_record_that_decides_differently_is_a_conflict_quoting_its_claim(self):
        source = self.rule(USE_Q, USE_A)
        self.record("doc", "ADR-0009", ADR_TITLE, ADR_BODY, days_from_now=1, author="Priya Shah")
        row = self.ask(USE_ASKED)
        self.assertEqual((row["status"], row["source_id"]), ("partial", source), row["evidence"])
        self.assertNotEqual(row["signoff"], "rule")
        self.assertFalse(row["authorized"])
        self.assertTrue(row["answer"].startswith("CONFLICTED, do not act on either side alone"), row["answer"])
        # The card quotes the sentence of the body that decides, not the title.
        self.assertIn('doc ADR-0009 states otherwise: "We decided that one billable assistant use is one question a '
                      'merchant asks the assistant, whether or not an answer completes."', row["answer"])
        self.assertNotIn(ADR_TITLE, row["answer"])
        self.assertNotIn("Reviewed-by", row["answer"])
        self.assertIn("a record disagrees, so a person still signs it here", row["fit_reason"])
        self.assertNotIn("did not make it a standing rule", row["fit_reason"])
        self.assertIn("A record disagrees with the answer", row["evidence"])
        self.assertTrue(any("ADR-0009" in e["detail"] for e in self.events("memory_record_conflict")))

    def test_a_record_the_model_reads_as_status_is_no_conflict_at_all(self):
        """With the tightened prompt the model answers STATUS for PR #220;
        the rule settles with the PR cited beside it."""
        source = self.rule(USE_Q, USE_A)
        self.record("pr", "220", PR_220_TITLE, PR_220_BODY, days_from_now=1)
        row = self.ask(USE_ASKED, verdict="STATUS")
        self.assertEqual((row["status"], row["signoff"], row["source_id"]), ("resolved", "rule", source), row["evidence"])
        self.assertNotIn("CONFLICTED", row["answer"])
        self.assertFalse(self.events("memory_record_conflict"))


class VerdictTests(OfflineCase):
    def test_the_prompt_asks_for_status_as_its_own_verdict_and_for_the_body(self):
        self.assertIn("STATUS", llm.CONFLICT_SYSTEM)
        self.assertIn("CONFLICT", llm.CONFLICT_SYSTEM)
        self.assertIn("read the body", llm.CONFLICT_SYSTEM)

    def test_only_a_conflict_verdict_disagrees(self):
        record = {"kind": "pr", "ref": "220", "title": PR_220_TITLE, "body": PR_220_BODY}
        with semantic_on():
            for verdict, expected in (("STATUS", False), ("status.", False), ("AGREE", False), ("UNRELATED", False),
                                      ("CONFLICT", True), ("Conflict: different rule", True), ("", False)):
                with self.subTest(verdict=verdict), patch.object(llm.Client, "complete", return_value=verdict):
                    self.assertEqual(ladder._disagrees(Config(), USE_ASKED, USE_A, record), expected)
            with patch.object(llm.Client, "complete", side_effect=llm.LLMError("offline")):
                self.assertEqual(ladder._record_verdict(Config(), USE_ASKED, USE_A, record), "")

    def test_the_model_is_given_the_record_body(self):
        record = {"kind": "pr", "ref": "220", "title": PR_220_TITLE, "body": PR_220_BODY}
        with semantic_on(), patch.object(llm.Client, "complete", return_value="STATUS") as complete:
            ladder._record_verdict(Config(), USE_ASKED, USE_A, record)
        prompt = complete.call_args.args[2]
        self.assertIn("no charge for those yet", prompt)


class StructuralGuardTests(OfflineCase):
    """The guard does not rest on the prompt: a pull request that describes
    state, or any record older than the rule, does not contest a rule even
    when the model said CONFLICT; different figures always do."""

    def rule(self, answer, when="2026-06-01T23:40:00+00:00"):
        from raven.graph import iso_to_ts
        return SimpleNamespace(answer=answer, updated_at=iso_to_ts(when), reusable=1, authorized=True)

    def record(self, kind, body, when, title="a record", status=""):
        return {"kind": kind, "ref": "1", "title": title, "body": body, "created_at": when, "status": status,
                "resolved": 1}

    def test_a_pr_describing_state_never_contests_a_rule(self):
        rule = self.rule(USE_A)
        self.assertTrue(ladder._describes_state(self.record("pr", PR_220_BODY, "2026-10-06T10:50:00-07:00")))
        self.assertFalse(ladder._contests_rule(rule, self.record("pr", PR_220_BODY, "2026-10-06T10:50:00-07:00")))
        self.assertFalse(ladder._contests_rule(rule, self.record("commit", "for now nothing is charged for answers",
                                                                  "2026-10-06T10:50:00-07:00")))

    def test_a_decision_record_newer_than_the_rule_contests_it(self):
        rule = self.rule(USE_A)
        adr = self.record("doc", ADR_BODY, "2026-10-06T10:50:00-07:00")
        self.assertFalse(ladder._describes_state(adr))
        self.assertTrue(ladder._contests_rule(rule, adr))
        # A pull request whose body states a different rule, with no status wording, contests it too.
        pr = self.record("pr", "One billable use is one question asked, answered or not.", "2026-10-06T10:50:00-07:00")
        self.assertTrue(ladder._contests_rule(rule, pr))
        # The same ADR, older than the rule's signature, does not: the rule is the newer call.
        self.assertFalse(ladder._contests_rule(rule, self.record("doc", ADR_BODY, "2026-05-01T00:00:00+00:00")))
        # Status wording in a document is not what makes a pull request a status report.
        self.assertFalse(ladder._describes_state(self.record("doc", "not charged yet", "2026-10-06T10:50:00-07:00")))

    def test_different_figures_always_contest(self):
        rule = self.rule("Retain retry error summaries for 11 days.")
        pr = self.record("pr", "For now we retain retry error summaries for 19 days.", "2026-01-01T00:00:00+00:00")
        self.assertTrue(ladder._describes_state(pr))
        self.assertTrue(ladder._contests_rule(rule, pr))

    def test_an_open_or_void_record_does_not_contest(self):
        rule = self.rule(USE_A)
        self.assertFalse(ladder._contests_rule(rule, self.record("ticket", ADR_BODY, "2026-10-06T10:50:00-07:00",
                                                                  status="In Progress")))
        self.assertFalse(ladder._contests_rule(rule, self.record("ticket", ADR_BODY, "2026-10-06T10:50:00-07:00",
                                                                  status="Cancelled")))

    def test_the_claim_quoted_is_the_sentence_that_bears_on_the_answer(self):
        pr = self.record("pr", PR_220_BODY, "2026-10-06T10:50:00-07:00", title=PR_220_TITLE)
        claim = ladder._record_claim(pr, USE_A)
        self.assertEqual(claim, "The Plan page shows what the month has used: labels against the allowance, and "
                                "assistant answers (no allowance and no charge for those yet).")
        self.assertEqual(ladder._record_claim(pr, TABLE_A),
                         "Reads usage_daily through month_to_date(), by cursor like everything else.")
        # No body: the title is all there is to quote.
        self.assertEqual(ladder._record_claim(self.record("pr", "", "2026-10-06", title=PR_220_TITLE), USE_A), PR_220_TITLE)
        # A long sentence is cut at a word and marked.
        long = self.record("doc", "Decided: " + "usage " * 80 + "ends here.", "2026-10-06")
        self.assertLessEqual(len(ladder._record_claim(long, TABLE_A)), 240)
        self.assertNotIn("usage usage usage usage usage usage usage usage usage usage usage usage usage usage usage usage "
                         "usage usage usage usage usage usage usage usage usage usage usage usage usage usage usage usage "
                         "usage usage usage usage usage usage usage usage ends", ladder._record_claim(long, TABLE_A))


class FitReasonTests(OfflineCase):
    def item(self, reusable):
        return SimpleNamespace(answered_by="Jordan Okafor", signed_by="", rule_scope="any", reusable=reusable)

    def test_a_withheld_rule_names_the_reason_not_its_owner(self):
        reason = fit_reason(self.item(1), "direct", False, when="2026-06-01", withheld="a record disagrees")
        self.assertEqual(reason, "The same question Jordan Okafor already answered on 2026-06-01; Jordan made it a "
                                 "standing rule, but a record disagrees, so a person still signs it here.")
        self.assertIn("but the answer combines sources, so a person still signs it here",
                      fit_reason(self.item(1), "semantic", False, withheld="the answer combines sources"))
        self.assertIn("but it does not fit this question, so a person still signs it here",
                      fit_reason(self.item(1), "near match", False))

    def test_a_row_that_is_not_a_rule_still_says_so(self):
        self.assertIn("Jordan did not make it a standing rule, so a person still signs it here",
                      fit_reason(self.item(0), "direct", False))
        self.assertIn("so nobody needed to be asked", fit_reason(self.item(1), "direct", True))
