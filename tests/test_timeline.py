"""What's been done: the task timeline, one plain sentence per stored
event, folded so a task with twenty calls reads in a dozen rows. The
templates are checked one kind at a time on synthetic events, then the
module is run over a real task through the canvas and the HTTP route."""
import http.client
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import OfflineCase, ready_server  # noqa: E402

from bridge import canvas, timeline  # noqa: E402
from bridge.config import Config  # noqa: E402

RUN = {"id": "task1", "title": "Start charging for Pilot", "agent": "claude", "requester": "Alex Moreno", "status": "needs_judgment"}
T = "2026-11-02T11:5{}:{:02d}+00:00"


def at(minute, second=0):
    return T.format(minute, second)


def decision(did, question, **extra):
    base = {"id": did, "run_id": "task1", "question": question, "status": "pending", "signoff": "", "source": "",
            "source_id": "", "owner_name": "", "answered_by": "", "signed_by": "", "created_at": at(3), "kind": "new",
            "superseded_by": "", "draft": 0, "rationale": "", "fit_reason": "", "owner_evidence": ""}
    return {**base, **extra}


def prior(did, by, day, run="old", **extra):
    return {"id": did, "run_id": run, "question": "earlier", "answered_by": by, "created_at": f"2026-{day}T12:00:00+00:00",
            "status": "approved", "source": "human", "source_revision": "", **extra}


def event(eid, name, minute, second=0, did="", **detail):
    """A stored event of this `name` (a detail of its own may be `kind`)."""
    body = {"task_id": "task1", **({"decision_id": did} if did else {}), **detail}
    return {"id": eid, "kind": name, "decision_id": did, "detail": json.dumps(body), "at": at(minute, second)}


def texts(rows):
    return [r["text"] for r in rows]


class TemplateTests(OfflineCase):
    """Each event kind renders one sentence a person can read."""

    def build(self, decisions, events, priors=None, meetings=None, run=RUN):
        return timeline.build(run, {d["id"]: d for d in decisions}, events, priors or {}, meetings or {})

    def test_the_kickoff_names_the_requester_and_the_count_once_filed(self):
        ds = [decision("a", "Which table?"), decision("b", "Which plan?"), decision("c", "dup", status="duplicate")]
        rows = self.build(ds, [event(1, "task_started", 2, requester="Alex Moreno", verdict="engage", paths=[])])
        self.assertEqual(texts(rows), ["Engaged: Alex's prompt read; 2 judgment calls identified"])
        self.assertEqual(rows[0]["kind"], "engaged")
        self.assertEqual(rows[0]["at"], at(2))
        self.assertEqual(rows[0]["people"], ["Alex Moreno"])
        # Nothing filed yet: said, not counted as zero.
        rows = self.build([], [event(1, "task_started", 2, requester="Alex Moreno", verdict="engage")])
        self.assertIn("the agent is writing down its judgment calls", rows[0]["text"])
        rows = self.build([], [event(1, "task_started", 2, verdict="pass", why="a typo fix")])
        self.assertEqual(rows[0]["text"], "Passed: nothing in this task needs a person's call; a typo fix")

    def test_settled_from_an_earlier_call_names_the_call_and_whether_anyone_was_asked(self):
        p = prior("p1", "Raj Patel", "01-22")
        d = decision("a", "Which table is Pilot usage read from?", status="resolved", signoff="rule", source="memory", source_id="p1")
        rows = self.build([d], [event(1, "resolve", 3, did="a", source="memory", cited="p1", answered_by="Raj Patel", signed=True)], {"p1": p})
        self.assertEqual(texts(rows), ["Settled “Which table is Pilot usage read from” from Raj's Jan 22 call; nobody asked"])
        self.assertEqual(rows[0]["refs"], [{"kind": "call", "id": "p1", "run_id": "old", "label": "Raj's Jan 22 call"}])
        # A partial answer still wants its owner's signature, and says so.
        part = {**d, "status": "partial", "signoff": "required", "owner_name": "Raj Patel"}
        rows = self.build([part], [event(1, "rule_over_record", 3, did="a", memory="p1", record="merge 46"),
                                   event(2, "resolve", 3, 1, did="a", source="memory", cited="p1", signed=True)], {"p1": p})
        self.assertEqual(texts(rows), ["Settled “Which table is Pilot usage read from” in part from Raj's Jan 22 call; PR #46 agrees; Raj signs the rest"])
        self.assertIn({"kind": "record", "id": "PR #46", "label": "PR #46"}, rows[0]["refs"])
        # A record that only covers the same ground (a PR describing what
        # shipped, or one older than the rule) is not said to agree.
        rows = self.build([d], [event(1, "rule_over_record", 3, did="a", memory="p1", record="pr 220", covers=True),
                                event(2, "resolve", 3, 1, did="a", source="memory", cited="p1", signed=True)], {"p1": p})
        self.assertEqual(texts(rows), ["Settled “Which table is Pilot usage read from” from Raj's Jan 22 call; "
                                       "PR #220 covers this too; nobody asked"])
        self.assertIn({"kind": "record", "id": "PR #220", "label": "PR #220"}, rows[0]["refs"])

    def test_settled_from_two_calls_of_one_person_names_both(self):
        p1, p2 = prior("p1", "Jordan Okafor", "06-09"), prior("p2", "Jordan Okafor", "06-10")
        d = decision("a", "Which table does assistant billing read usage from, and how are reads paged?",
                     status="resolved", signoff="rule", source="memory", source_id="p1")
        rows = self.build([d], [event(1, "resolve", 3, did="a", source="memory", cited="p1", also=["p2"],
                                      answered_by="Jordan Okafor", signed=True)], {"p1": p1, "p2": p2})
        shown = timeline.topic(d["question"])
        self.assertEqual(texts(rows), [f"Settled “{shown}” from Jordan's Jun 9 and Jun 10 calls; nobody asked"])
        self.assertEqual([r["id"] for r in rows[0]["refs"]], ["p1", "p2"])
        self.assertEqual(rows[0]["people"], ["Jordan Okafor"])
        # Two people's calls are named one by one, and the composition still wants a signature.
        p2 = prior("p2", "Marcus Lee", "06-10")
        signed = {**d, "signoff": "required", "owner_name": "Jordan Okafor"}
        rows = self.build([signed], [event(1, "resolve", 3, did="a", source="memory", cited="p1", also=["p2"], signed=True)],
                          {"p1": p1, "p2": p2})
        self.assertEqual(texts(rows), [f"Settled “{shown}” from Jordan's Jun 9 call and Marcus's Jun 10 call; Jordan signs it"])

    def test_settled_from_records_lists_the_records_as_short_labels(self):
        d = decision("a", "Is Northwind charged for Pilot?", status="proposed", signoff="required", source="record", owner_name="Noah Kim")
        cited = "meeting meeting:m1:chunk:00000 [transcript]; commit 387e508c8d5d; merge 36; slack pricing-2026-09-24 [decided]; ticket BILL-201 (resolved): title; doc adr-0002: title"
        rows = self.build([d], [event(1, "resolve", 6, did="a", source="record", cited=cited, kind="joint-composition")],
                          meetings={"m1": "Pilot pricing sync, Oct 29"})
        # An answer its owner still signs is found, not settled: "Settled ...;
        # Noah signs it" contradicted itself on the Parcelwise dry run.
        self.assertEqual(texts(rows), ["Found an answer in the repo's own record for “Is Northwind charged for Pilot”: the Pilot pricing sync, Oct 29 · "
                                       "commit 387e508 · PR #36 · #pricing · BILL-201 · ADR-0002; Noah signs it"])
        self.assertEqual([r["kind"] for r in rows[0]["refs"]], ["meeting", "commit", "pr", "slack", "ticket", "doc"])
        part = {**d, "status": "partial"}
        rows = self.build([part], [event(1, "resolve", 6, did="a", source="record", cited="merge 36", kind="joint-composition")])
        self.assertEqual(texts(rows), ["Found a partial answer in the repo's own record for “Is Northwind charged for Pilot”: PR #36; Noah signs the rest"])
        # Nobody signs it: settled.
        rule = {**d, "status": "resolved", "signoff": "rule"}
        rows = self.build([rule], [event(1, "resolve", 6, did="a", source="record", cited="merge 36", kind="joint-composition")])
        self.assertEqual(texts(rows), ["Settled “Is Northwind charged for Pilot” from the repo's own record: PR #36; nobody asked"])
        # Two found within two minutes fold, and still say who signs.
        e = decision("b", "Which plans are metered?", status="proposed", signoff="required", source="record", owner_name="Noah Kim")
        rows = self.build([d, e], [event(1, "resolve", 6, did="a", source="record", cited="merge 36", kind="joint-composition"),
                                   event(2, "resolve", 6, 30, did="b", source="record", cited="doc adr-0002: plans", kind="joint-composition")])
        self.assertEqual(texts(rows), ["Found answers in the repo's own record for 2, Noah signs them: Is Northwind charged for Pilot (PR #36) · Which plans are metered (ADR-0002)"])
        rows = self.build([rule, {**e, "status": "resolved", "signoff": "rule"}],
                          [event(1, "resolve", 6, did="a", source="record", cited="merge 36", kind="joint-composition"),
                           event(2, "resolve", 6, 30, did="b", source="record", cited="doc adr-0002: plans", kind="joint-composition")])
        self.assertEqual(texts(rows), ["Settled 2 from the repo's own record: Is Northwind charged for Pilot (PR #36) · Which plans are metered (ADR-0002)"])

    def test_settled_from_a_meeting_quotes_the_speaker(self):
        d = decision("a", "Which plans are charged per use for Pilot?", status="resolved", signoff="rule", source="meeting",
                     answered_by="Dana Whitfield", rationale="Right, so we meter it. Enterprise only.",
                     fit_reason="From the Oct 29 Pilot pricing sync (Dana, Elena): Dana said it")
        rows = self.build([d], [event(1, "meeting_precedent", 3, did="a", kind="settle", meeting_id="m1", speaker="Dana Whitfield")],
                          meetings={"m1": "Pilot pricing sync (Dana, Elena), Oct 29"})
        self.assertEqual(texts(rows), ["Settled “Which plans are charged per use for Pilot” from the Pilot pricing sync (Dana, Elena), Oct 29: "
                                       "Dana said “Right, so we meter it. Enterprise only.”; nobody asked"])
        # Without the meeting row, the fit reason names it.
        rows = self.build([d], [event(1, "meeting_precedent", 3, did="a", kind="settle", meeting_id="m1", speaker="Dana Whitfield")])
        self.assertTrue(rows[0]["text"].startswith("Settled “Which plans are charged per use for Pilot” from the Oct 29 Pilot pricing sync (Dana, Elena): "), rows[0]["text"])
        # A prediction from a meeting is Raven's guess, confirmed by its owner.
        g = decision("b", "From which period are orgs charged?", status="proposed", signoff="required", owner_name="Dana Whitfield")
        rows = self.build([g], [event(1, "meeting_precedent", 4, did="b", kind="predict", meeting_id="m1", speaker="Dana Whitfield")],
                          meetings={"m1": "Pilot pricing sync, Oct 29"})
        self.assertEqual(texts(rows), ["Raven's guess on “From which period are orgs charged” from what Dana said in the Pilot pricing sync, Oct 29; Dana confirms"])

    def test_checking_then_confirmed_or_flagged(self):
        p = prior("p1", "Raj Patel", "09-17")
        d = decision("a", "How are retries counted?", status="resolved", signoff="rule", source="memory", source_id="p1")
        rows = self.build([d], [event(1, "checking", 3, did="a", cited="p1", kind="near match"),
                                event(2, "resolve", 4, 55, did="a", source="memory", cited="p1", answered_by="Raj Patel", signed=True)], {"p1": p})
        self.assertEqual(texts(rows), ["Checking “How are retries counted” against Raj's Sep 17 call; nobody asked yet",
                                       "Confirmed: Raj's Sep 17 call covers “How are retries counted”; nobody asked"])
        # The model pass withdrew the match and routed it instead: the route is the row.
        w = decision("b", "What is the per-use rate?", owner_name="Dana Whitfield",
                     owner_evidence="verified: Dana Whitfield decides for app/billing/plans.py (matches); verified: Dana Whitfield decides for pricing decisions (config)")
        rows = self.build([w], [event(1, "checking", 3, did="b", cited="p1", kind="near match"),
                                event(2, "ask_drafted", 5, did="b", owner="Dana Whitfield", score=2.9)], {"p1": p})
        self.assertEqual(texts(rows)[1], "Flagged “What is the per-use rate” to Dana, who decides for pricing decisions; waiting on Dana")
        self.assertEqual(rows[1]["refs"], [{"kind": "decision", "id": "b", "run_id": "task1", "label": "What is the per-use rate"}])

    def test_confirmed_against_two_calls_says_they_cover_it(self):
        # Measured on the Parcelwise demo (Nov 2): "Jordan's Sep 15 and Jun 1 calls covers ...", a plural
        # subject with a singular verb. Two calls cover; one call covers.
        p1, p2 = prior("p1", "Jordan Okafor", "09-15"), prior("p2", "Jordan Okafor", "06-01")
        d = decision("a", "Do retried, timed-out or failed assistant calls count as uses?",
                     status="resolved", signoff="rule", source="memory", source_id="p1")
        shown = timeline.topic(d["question"])
        events = [event(1, "checking", 3, did="a", cited="p1", kind="near match"),
                  event(2, "resolve", 4, 55, did="a", source="memory", cited="p1", also=["p2"], answered_by="Jordan Okafor", signed=True)]
        rows = self.build([d], events, {"p1": p1, "p2": p2})
        self.assertEqual(texts(rows)[1], f"Confirmed: Jordan's Sep 15 and Jun 1 calls cover “{shown}”; nobody asked")
        # Two people's calls, named one by one, are plural too.
        rows = self.build([d], events, {"p1": p1, "p2": prior("p2", "Marcus Lee", "06-01")})
        self.assertEqual(texts(rows)[1], f"Confirmed: Jordan's Sep 15 call and Marcus's Jun 1 call cover “{shown}”; nobody asked")
        # One call still covers.
        rows = self.build([d], [events[0], event(2, "resolve", 4, 55, did="a", source="memory", cited="p1", answered_by="Jordan Okafor", signed=True)], {"p1": p1})
        self.assertEqual(texts(rows)[1], f"Confirmed: Jordan's Sep 15 call covers “{shown}”; nobody asked")

    def test_a_static_route_the_model_pass_then_settled_is_not_a_row(self):
        p = prior("p1", "Raj Patel", "09-17")
        d = decision("a", "How are retries counted?", status="resolved", signoff="rule", source="memory", source_id="p1")
        rows = self.build([d], [event(1, "ask_drafted", 3, did="a", owner="Mei Tanaka"),
                                event(2, "resolve", 4, did="a", source="memory", cited="p1", signed=True)], {"p1": p})
        self.assertEqual([r["kind"] for r in rows], ["settled"])

    def test_a_guess_an_assumption_a_fold_and_a_conflict(self):
        p = prior("p1", "Dana Whitfield", "09-24")
        g = decision("a", "Is Northwind charged?", status="proposed", signoff="required", owner_name="Noah Kim")
        rows = self.build([g], [event(1, "proposed", 3, did="a", source="memory", cited="p1", signed=False, kind="near match")], {"p1": p})
        self.assertEqual(texts(rows), ["Raven's guess on “Is Northwind charged” from Dana's Sep 24 call; Noah confirms"])
        a = decision("b", "Where does the meter live?", status="assumed", owner_name="Dana Whitfield")
        rows = self.build([a], [event(1, "assume", 3, did="b", default="In account settings", certainty="high")])
        self.assertEqual(texts(rows), ["Assumed “Where does the meter live” (reversible): In account settings; Dana can correct it"])
        twin = decision("c", "What is the per-use rate for Pilot?", owner_name="Dana Whitfield")
        dup = decision("d", "How many cents per Pilot response?", status="duplicate", superseded_by="c")
        rows = self.build([twin, dup], [event(1, "dedup_open", 3, did="d", twin="c", question="How many cents per Pilot response?")])
        self.assertEqual(texts(rows), ["Folded “How many cents per Pilot response” into the open call “What is the per-use rate for Pilot”: one answer settles both"])
        newer = prior("p2", "Dana Whitfield", "10-29", source="meeting", answered_via="meeting", source_revision="2026-10-29")
        rows = self.build([twin], [event(1, "conflict_resolved", 3, did="c", preferred="p2", over=["p1"], rule="newest answer preferred"),
                                   event(2, "conflict_resolved", 3, 5, did="c", kept="p2", retired=["p1"])], {"p1": p, "p2": newer})
        self.assertEqual(texts(rows), ["Two earlier calls disagreed on “What is the per-use rate for Pilot”; preferred Dana's Oct 29 call over Dana's Sep 24 call; both stand"])

    def test_people_acting_a_hand_on_an_answer_a_correction_a_note(self):
        d = decision("a", "From which period are orgs charged?", owner_name="Elena Rossi", kind="prediction")
        rows = self.build([d], [event(1, "owner_changed", 3, did="a", to="Elena Rossi", by="Dana Whitfield", referral=True),
                                event(2, "route_learned", 3, did="a", person="Elena Rossi", scopes=["app/billing/*"], by="Dana Whitfield"),
                                event(3, "owner_approved", 4, did="a", answer="x", owner="Elena Rossi", actor="Elena Rossi", source="slack: reply"),
                                event(4, "precedent_corrected", 5, did="a", by="Raj Patel", scope="here", replaced="", why="doesn't apply to batch runs"),
                                event(5, "task_note", 6, by="Alex Moreno", text="Northwind is on a custom contract."),
                                event(6, "followup_added", 7, did="a", by="Dana Whitfield"),
                                event(7, "signoff", 8, did="a", by="Noah Kim", corrected=False, source="inbox: Noah Kim")])
        self.assertEqual(texts(rows), [
            "Dana handed “From which period are orgs charged” to Elena; Raven routes app/billing/* to Elena from now on",
            "Elena confirmed Raven's guess on “From which period are orgs charged” on Slack",
            "Raj corrected “From which period are orgs charged”: doesn't apply to batch runs; the earlier call stays for other cases",
            "Alex added a note: “Northwind is on a custom contract.”",
            "Dana added a follow-up: “From which period are orgs charged”",
            "Noah signed “From which period are orgs charged”",
        ])
        self.assertEqual(rows[0]["people"], ["Dana Whitfield", "Elena Rossi"])

    def test_an_own_answer_over_a_guess_and_a_correction_read_as_what_the_person_did(self):
        # Measured on a demo take: a person who typed their own answer to a
        # call Raven had a guess for read as "confirmed Raven's guess", and a
        # correction showed as a confirmation and then a correction.
        a = decision("a", "Should select all pick every order?", owner_name="Lena Park", kind="prediction")
        b = decision("b", "Do we refuse the whole batch?", owner_name="Priya Shah", kind="prediction")
        c = decision("c", "Skip orders with a live label?", owner_name="Jordan Okafor", kind="prediction")
        rows = self.build([a, b, c], [
            event(1, "owner_approved", 3, did="a", answer="This page, with a link for all", actor="Lena Park", took_guess=False),
            event(2, "owner_approved", 4, did="b", answer="Buy what fits", actor="Priya Shah", took_guess=False),
            event(3, "precedent_corrected", 4, did="b", by="Priya Shah", scope="here", replaced="", why="a batch is not one label"),
            event(4, "owner_approved", 5, did="c", answer="Skip it", actor="Jordan Okafor", took_guess=True)])
        self.assertEqual(texts(rows), [
            "Lena answered “Should select all pick every order”",
            "Priya corrected “Do we refuse the whole batch”: a batch is not one label; the earlier call stays for other cases",
            "Jordan confirmed Raven's guess on “Skip orders with a live label”",
        ])

    def test_the_agent_waiting_and_the_finish(self):
        ds = [decision("a", "Which table?", status="approved", answered_by="Raj Patel"), decision("b", "Which plan?")]
        rows = self.build(ds, [event(1, "node_settled", 3, did="b"),
                               event(2, "host_stopped_waiting", 4, waiting=1, why="idle wait cap"),
                               {"id": 3, "kind": "run_updated", "decision_id": "", "detail": "completed", "at": at(5)},
                               event(4, "task_abandoned", 6, reason="the branch was dropped")])
        self.assertEqual(texts(rows), ["Claude Code proposed an answer for “Which plan”; it waits for a signature",
                                       "Claude Code stopped, waiting on 1 open call",
                                       "Finished: 1 of 2 calls authorized by a person",
                                       "Abandoned: the branch was dropped"])

    def test_a_dependent_put_in_doubt_and_a_withdrawn_guess(self):
        d = decision("a", "Which table?")
        rows = self.build([d], [event(1, "prediction_withdrawn", 3, did="a", reason="its source was replaced"),
                                event(2, "dependent_flagged", 4, did="a", reason="the call it leaned on changed", source="p1"),
                                event(3, "legacy_source_reuse_review", 5, did="a", previous_authorization={"signoff": "rule"}, reason="unknown revisions"),
                                event(4, "legacy_source_reuse_review", 5, 1, did="a", previous_authorization={"signoff": ""}, reason="unknown revisions")])
        self.assertEqual(texts(rows), ["Withdrew Raven's guess on “Which table”: its source was replaced",
                                       "“Which table” needs another look: the call it leaned on changed",
                                       "“Which table” needs another look: the source it was settled from has no known revision, so a person signs it"])


class FoldingTests(OfflineCase):
    """Rows of one kind close together fold into one; order is by time."""

    def test_several_settles_in_a_burst_become_one_row_with_every_ref(self):
        priors = {"p1": prior("p1", "Raj Patel", "09-17"), "p2": prior("p2", "Raj Patel", "01-22"), "p3": prior("p3", "Alex Moreno", "05-14")}
        ds = [decision(k, q, status="resolved", signoff="rule", source="memory", source_id=p) for k, q, p in
              (("a", "What counts as one use?", "p1"), ("b", "Which table?", "p2"), ("c", "Where do charges post?", "p3"))]
        ds.append(decision("d", "Which orgs are excluded?", owner_name="Jonas Lindqvist"))
        events = [event(1, "task_started", 2, requester="Alex Moreno", verdict="engage"),
                  event(2, "resolve", 3, 52, did="a", source="memory", cited="p1", signed=True),
                  event(3, "ask_drafted", 4, 1, did="d", owner="Jonas Lindqvist"),
                  event(4, "resolve", 4, 23, did="b", source="memory", cited="p2", signed=True),
                  event(5, "resolve", 5, 38, did="c", source="memory", cited="p3", signed=True)]
        rows = timeline.build(RUN, {d["id"]: d for d in ds}, events, priors)
        self.assertEqual([(r["kind"], r["count"]) for r in rows], [("engaged", 1), ("settled", 3), ("flagged", 1)])
        self.assertEqual(rows[1]["text"], "Settled 3 from earlier calls, nobody asked: What counts as one use (Raj, Sep 17) · "
                                          "Which table (Raj, Jan 22) · Where do charges post (Alex, May 14)")
        self.assertEqual([r["label"] for r in rows[1]["refs"]], ["Raj's Sep 17 call", "Raj's Jan 22 call", "Alex's May 14 call"])
        self.assertEqual(rows[1]["people"], ["Raj Patel", "Alex Moreno"])
        self.assertEqual(rows[1]["at"], at(3, 52), "a folded row sits where its first event happened")
        self.assertEqual([r["at"] for r in rows], sorted(r["at"] for r in rows))

    def test_rows_far_apart_or_with_a_different_tail_stay_apart(self):
        priors = {"p1": prior("p1", "Raj Patel", "09-17"), "p2": prior("p2", "Raj Patel", "01-22")}
        ds = [decision("a", "One?", status="resolved", signoff="rule", source="memory", source_id="p1"),
              decision("b", "Two?", status="partial", signoff="required", source="memory", source_id="p2", owner_name="Raj Patel"),
              decision("c", "Three?", status="resolved", signoff="rule", source="memory", source_id="p1")]
        events = [event(1, "resolve", 3, did="a", source="memory", cited="p1", signed=True),
                  event(2, "resolve", 3, 10, did="b", source="memory", cited="p2", signed=True),
                  event(3, "resolve", 9, did="c", source="memory", cited="p1", signed=True)]
        rows = timeline.build(RUN, {d["id"]: d for d in ds}, events, priors)
        self.assertEqual([r["count"] for r in rows], [1, 1, 1])
        self.assertIn("Raj signs the rest", rows[1]["text"])

    def test_flags_fold_per_person(self):
        ds = [decision("a", "A?", owner_name="Jonas Lindqvist"), decision("b", "B?", owner_name="Jonas Lindqvist"),
              decision("c", "C?", owner_name="Dana Whitfield")]
        events = [event(1, "ask_drafted", 4, did="a", owner="Jonas Lindqvist"), event(2, "ask_drafted", 4, 6, did="b", owner="Jonas Lindqvist"),
                  event(3, "ask_drafted", 4, 9, did="c", owner="Dana Whitfield")]
        rows = timeline.build(RUN, {d["id"]: d for d in ds}, events)
        self.assertEqual(texts(rows), ["Flagged 2 to Jonas: A · B", "Flagged “C” to Dana; waiting on Dana"])

    def test_a_long_list_is_cut_and_a_long_question_is_cut_at_a_word(self):
        self.assertEqual(timeline.topic("Which organisations are left out of Pilot charges on the Enterprise plan?"),
                         "Which organisations are left out of Pilot…")
        self.assertEqual(timeline.topic("What counts as one Pilot use?"), "What counts as one Pilot use")
        ds = [decision(f"d{i}", f"Question {i}?", owner_name="Dana Whitfield") for i in range(6)]
        events = [event(i + 1, "ask_drafted", 4, i, did=f"d{i}", owner="Dana Whitfield") for i in range(6)]
        rows = timeline.build(RUN, {d["id"]: d for d in ds}, events)
        self.assertEqual(texts(rows), ["Flagged 6 to Dana: Question 0 · Question 1 · Question 2 · Question 3 · and 2 more"])

    def test_record_refs_parse_every_citation_shape(self):
        refs = timeline.record_refs("merge 7; commit b6fdd48f53bf: title; slack pricing-2026-09-24 [decided]; ticket BILL-201 (resolved): t; "
                                    "doc adr-0002: t; ownership graph; merge 7")
        self.assertEqual([(r["kind"], r["label"]) for r in refs],
                         [("pr", "PR #7"), ("commit", "commit b6fdd48"), ("slack", "#pricing"), ("ticket", "BILL-201"),
                          ("doc", "ADR-0002"), ("graph", "the ownership graph")])
        self.assertEqual(timeline.record_refs(""), [])
        self.assertEqual(timeline.when("2026-09-17T15:45:00+00:00"), "Sep 17")
        self.assertEqual(timeline.when(""), "")


class StoreTests(OfflineCase):
    """The module over a real task: what the canvas writes, read back as
    rows, through the store and the HTTP route, with no model."""

    def setUp(self):
        super().setUp()
        self.store = self.warm_store("qemulike", "timeline.db")
        self.cfg = Config()

    def test_a_started_task_with_a_routed_node_reads_as_engaged_then_flagged(self):
        task = canvas.start_task(self.store, self.cfg, {"title": "Switch the virt default to the aplic", "repo": "qemulike", "agent": "claude",
                                                        "requester": "Tamsin Reed <tamsin@synthco.example>", "paths": "hw/riscv/virt.c"})
        node = canvas.add_node(self.store, self.cfg, {"task_id": task["task_id"], "question": "Should the aplic be the default interrupt controller for virt?",
                                                      "context": "The board chooses one at init.", "paths": "hw/riscv/virt.c", "client_ref": "irq"})
        rows = timeline.rows(self.store, task["task_id"])
        self.assertEqual(rows[0]["kind"], "engaged")
        self.assertIn("1 judgment call identified", rows[0]["text"])
        kinds = [r["kind"] for r in rows]
        self.assertIn("flagged", kinds, rows)
        flagged = rows[kinds.index("flagged")]
        self.assertTrue(flagged["text"].startswith("Flagged “Should the aplic be the default interrupt…”"), flagged["text"])
        self.assertEqual(flagged["refs"][0], {"kind": "decision", "id": node["node_id"], "run_id": task["task_id"],
                                              "label": "Should the aplic be the default interrupt…"})
        for row in rows:
            self.assertRegex(row["at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", "times are ISO; the page makes them local")
        # The same rows ride on /api/state for the live task, and the
        # route serves them for any task.
        self.assertEqual(timeline.view(self.store, task["task_id"])["rows"], rows)
        state = self.store.state()
        timeline.attach(self.store, state["runs"])
        mine = next(r for r in state["runs"] if r["id"] == task["task_id"])
        self.assertEqual(mine["timeline"], rows)
        with self.assertRaises(Exception):
            timeline.rows(self.store, "nope")

    def test_the_route_serves_the_timeline(self):
        task = canvas.start_task(self.store, self.cfg, {"title": "Switch the virt default to the aplic", "repo": "qemulike", "agent": "claude",
                                                        "requester": "Tamsin Reed <tamsin@synthco.example>", "paths": "hw/riscv/virt.c"})
        server = ready_server(self.store, host="127.0.0.1", port=0)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def get(path):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
            try:
                connection.request("GET", path)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        status, body = get(f"/api/tasks/{task['task_id']}/timeline")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["task_id"], task["task_id"])
        self.assertEqual(body["rows"][0]["kind"], "engaged")
        self.assertEqual(set(body["rows"][0]), {"kind", "at", "text", "refs", "people", "count"})
        status, state = get("/api/state")
        self.assertEqual(status, 200)
        live = next(r for r in state["runs"] if r["id"] == task["task_id"])
        self.assertEqual(live["timeline"], body["rows"])
        status, body = get("/api/tasks/nope/timeline")
        self.assertEqual(status, 404, body)
