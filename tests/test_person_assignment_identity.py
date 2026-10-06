"""Stable identities survive namesakes, renames, and legacy owner rows.

All transport calls use FakeSlack; these tests never contact a provider.
"""
from pathlib import Path
import unittest

from fixtures import OfflineCase
from test_delivery import FakeSlack
from bridge.authz import Actor, basis_for
from bridge.store import Invalid, Store


class PersonAssignmentIdentityTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "identity.db")
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.referrer = self.graph.add_person("Morgan Chen", email="morgan@example.test", slack_id="UMORGAN")
        self.actor = Actor.person(self.graph.get_person(self.referrer))
        self.slack = FakeSlack()
        self.delivery = self.store.connect_delivery(self.slack, fallback_channel="CTRIAGE")
        self.run = self.store.add_run({"title": "Identity assignment", "repo": "example/parser"})["id"]

    def person(self, name="Taylor Rivera", suffix=""):
        return self.graph.add_person(name, email=f"taylor{suffix}@example.test",
                                     slack_id="UTAYLOR" + suffix.upper(), merge=False)

    def question(self, owner=""):
        return self.graph.add_decision(self.run, "Should usage be billed?", "billing", "pending", owner=owner)

    def owner(self, pid):
        row = self.graph.db.execute("SELECT * FROM owners WHERE person_id=? ORDER BY created_at, id LIMIT 1", (pid,)).fetchone()
        return dict(row) if row else None

    def assigned(self, decision):
        row = self.store.get_decision(decision)
        return dict(self.graph.db.execute("SELECT * FROM owners WHERE id=?", (row["owner_id"],)).fetchone())

    def assert_standing(self, decision, target, other):
        row = self.store.get_decision(decision)
        self.assertEqual(self.assigned(decision)["person_id"], target)
        self.assertEqual(basis_for(self.graph, Actor.person(self.graph.get_person(target)), row, "answer")[0], "owner")
        self.assertEqual(basis_for(self.graph, Actor.person(self.graph.get_person(other)), row, "answer")[0], "")

    def test_new_namesake_never_rebinds_an_existing_owner(self):
        first = self.person()
        owner = self.owner(first)
        decision = self.question(owner="UTAYLOR")
        second = self.person(suffix="other")
        self.assertEqual(self.owner(first), owner)
        self.assertNotEqual(self.owner(second)["id"], owner["id"])
        self.assert_standing(decision, first, second)

    def test_owner_lookup_keeps_an_unambiguous_original_identity(self):
        first = self.person()
        second = self.person(suffix="other")
        for pid, identities in ((first, (first, "UTAYLOR", "taylor@example.test")),
                                (second, (second, "UTAYLOROTHER", "taylorother@example.test"))):
            expected = self.owner(pid)
            self.assertIsNotNone(expected)
            for identity in identities:
                with self.subTest(identity=identity):
                    self.assertEqual(self.graph.owner_id_for(identity), expected["id"])

    def test_public_slack_triage_keeps_the_mentioned_person(self):
        first, second = self.person(), self.person(suffix="other")
        decision = self.question()
        self.store.notify(decision, "ask")
        self.assertEqual(self.delivery.deliver_now(), 1)
        message = self.slack.messages[0]
        reply = self.delivery.receive(message["channel"], message["ts"], "UMORGAN", "ask <@UTAYLOR>")
        self.assertIn("Sent to Taylor Rivera", reply)
        self.assert_standing(decision, first, second)
        self.assertEqual(self.store.authority(), [])
        self.assertEqual(self.delivery.deliver_now(), 1)
        message = self.slack.messages[-1]
        self.assertEqual(message["channel"], "DUTAYLOR")
        reply = self.delivery.receive(message["channel"], message["ts"], "UTAYLOROTHER", "answer: Bill it because policy requires it")
        self.assertIn("Not permitted", reply)
        self.assertEqual(self.store.get_decision(decision)["status"], "pending")
        reply = self.delivery.receive(message["channel"], message["ts"], "UTAYLOR", "answer: Exclude it because it was internal")
        self.assertIn("Recorded as Taylor Rivera's answer", reply)
        self.assertEqual(self.store.get_decision(decision)["actor_id"], first)

    def test_scoped_referral_keeps_owner_and_authority_on_the_same_person(self):
        first, second = self.person(), self.person(suffix="other")
        decision = self.question(owner="UMORGAN")
        self.store.refer(decision, {"person": first, "scope_kind": "category", "scope": "billing"}, actor=self.actor)
        self.assert_standing(decision, first, second)
        learned = self.store.authority()
        self.assertEqual([(row["person_id"], row["scope"], row["accepted"]) for row in learned], [(first, "billing", 0)])
        self.assertEqual(self.delivery.deliver_now(), 1)
        self.assertEqual(self.slack.messages[-1]["channel"], "DUTAYLOR")

    def test_delivery_dedupe_keeps_namesake_recipients_distinct(self):
        first, second = self.person(), self.person(suffix="other")
        decision = self.question()
        self.store.claim_slack_question(decision, first, self.actor)
        self.assertEqual(self.delivery.deliver_now(), 1)
        self.store.refer(decision, {"person": second, "scope_kind": "none"},
                         actor=Actor.person(self.graph.get_person(first)))
        self.assertEqual(self.delivery.deliver_now(), 1)
        self.assertEqual([message["channel"] for message in self.slack.messages], ["DUTAYLOR", "DUTAYLOROTHER"])
        self.assertIsNone(self.store.notify(decision, "reassigned"))

    def test_retry_keeps_the_original_person_after_a_namesake_is_added(self):
        target = self.graph.add_person("Taylor Rivera", email="taylor@example.test", merge=False)
        self.delivery.fallback_channel = ""
        self.graph.set_setting("slack_fallback_channel", "")
        decision = self.question(owner="taylor@example.test")
        notice = self.store.notify(decision, "ask")
        self.assertEqual((notice["state"], notice["destination"], notice["person_id"]), ("failed", "", target))
        self.person(suffix="other")
        self.graph.add_person("Taylor Rivera", email="taylor@example.test", slack_id="UTAYLOR")
        retried = self.delivery.retry(notice["id"])
        self.assertEqual((retried["destination"], retried["person_id"]), ("UTAYLOR", target))
        self.assertEqual(self.delivery.deliver_now(), 1)
        self.assertEqual(self.slack.messages[-1]["channel"], "DUTAYLOR")

    def test_explicit_delivery_recipient_is_not_replaced_by_owner(self):
        target = self.person()
        self.person(suffix="other")
        decision = self.question(owner="UTAYLOR")
        notice = self.store.notify(decision, "signoff", to="Morgan Chen")
        self.assertEqual(notice["person_id"], self.referrer)
        self.assertNotEqual(notice["person_id"], target)
        self.assertEqual(notice["destination"], "UMORGAN")

    def test_exact_owner_lookup_rejects_missing_or_inactive_targets(self):
        target = self.person()
        self.person(suffix="other")
        self.graph.db.execute("UPDATE people SET active=0 WHERE id=?", (target,))
        self.graph._bump("")
        for identity in (target, "missing-person-id", "Taylor Rivera"):
            self.assertIsNone(self.graph.owner_id_for_person(identity))

    def test_bound_delivery_does_not_fall_back_from_inactive_person(self):
        target = self.person()
        self.person(suffix="other")
        decision = self.question(owner="UTAYLOR")
        self.graph.db.execute("UPDATE people SET active=0 WHERE id=?", (target,))
        self.graph._bump("")
        notice = self.store.notify(decision, "ask")
        self.assertEqual(notice["destination"], "CTRIAGE")
        self.assertEqual(notice["person_id"], "")

    def test_rename_preserves_bound_owner_and_assigns_the_renamed_person(self):
        other = self.person("Taylor New", "other")
        target = self.person("Taylor Old")
        owner_id = self.owner(target)["id"]
        renamed = self.graph.add_person("Taylor New", email="taylor@example.test")
        self.assertEqual(renamed, target)
        self.assertEqual(self.owner(target)["id"], owner_id)
        self.assertEqual(self.owner(target)["name"], "Taylor New")
        for method in ("claim", "refer"):
            with self.subTest(method=method):
                decision = self.question(owner="UMORGAN" if method == "refer" else "")
                if method == "claim":
                    self.store.claim_slack_question(decision, target, self.actor)
                else:
                    self.store.refer(decision, {"person": target, "scope_kind": "none"}, actor=self.actor)
                self.assert_standing(decision, target, other)

    def test_unique_legacy_unbound_owner_is_adopted(self):
        owner_id = self.graph.owner_id_for("Taylor Rivera")
        target = self.person()
        self.assertEqual(self.owner(target)["id"], owner_id)

    def test_ambiguous_legacy_unbound_owner_is_not_adopted(self):
        first, second = self.person(), self.person(suffix="other")
        # Historical unbound row: its name alone cannot establish whose it was.
        self.graph.db.execute("DELETE FROM owners WHERE person_id=?", (first,))
        self.graph.db.execute("INSERT INTO owners(id,name,team,patterns,created_at,person_id) VALUES(?,?,?,?,?,?)",
                              ("legacy-owner", "Taylor Rivera", "", "billing/*", "2000-01-01", ""))
        self.graph.add_person("Taylor Rivera", email="taylor@example.test")
        legacy = self.graph.db.execute("SELECT person_id FROM owners WHERE id='legacy-owner'").fetchone()
        self.assertEqual(legacy["person_id"], "")
        self.assertNotEqual(self.owner(first)["id"], "legacy-owner")
        self.assertIsNotNone(self.owner(second))

    def test_new_assignment_does_not_repair_historical_wrong_binding(self):
        first = self.person()
        historical_owner = self.owner(first)["id"]
        historical = self.question(owner="UTAYLOR")
        second = self.person(suffix="other")
        self.graph.db.execute("UPDATE owners SET person_id=? WHERE id=?", (second, historical_owner))
        decision = self.question()
        self.store.claim_slack_question(decision, first, self.actor)
        self.assert_standing(decision, first, second)
        self.assertEqual(self.assigned(historical)["id"], historical_owner)
        self.assertEqual(self.assigned(historical)["person_id"], second)

    def test_missing_and_inactive_targets_do_not_assign_or_learn(self):
        inactive = self.person()
        self.graph.db.execute("UPDATE people SET active=0 WHERE id=?", (inactive,))
        self.graph._bump("")
        for target in ("missing-person-id", inactive):
            for method in ("claim", "refer"):
                with self.subTest(target=target, method=method):
                    decision = self.question(owner="UMORGAN" if method == "refer" else "")
                    before = self.store.get_decision(decision)
                    with self.assertRaises(Invalid):
                        if method == "claim":
                            self.store.claim_slack_question(decision, target, self.actor)
                        else:
                            self.store.refer(decision, {"person": target, "scope_kind": "category", "scope": "billing"}, actor=self.actor)
                    after = self.store.get_decision(decision)
                    self.assertEqual(after["owner_id"], before["owner_id"])
                    self.assertEqual(after["events"], before["events"])
                    self.assertEqual(self.store.authority(), [])


if __name__ == "__main__":
    unittest.main()
