"""People, identity, the authority map and the coordinator.

Routing has two layers: what git history suggests, and what the
organization verified. The verified layer wins: a person the authority
map says decides a path, a decision category or a repository is the
owner whatever blame says; a team CODEOWNERS names is its members once
the team is known; a bare first name resolves to the one engineer it
starts, so nobody is asked their own question; and what nobody is
verified to own goes to the coordinator, never to a guess."""

import json
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fixtures import OfflineCase

from bridge import canvas, identity
from bridge.config import Config
from bridge.mcp import dispatch
from bridge.routing import route, route_ranked
from bridge.scopes import decision_scopes
from fixtures import ready_server as make_server
from bridge.store import Invalid, Store

CFG = Config(model_api="none")


class IdentityTests(unittest.TestCase):
    def test_same_person_across_spellings(self):
        P = identity.Person
        self.assertTrue(identity.same_person(P(name="Tamsin P. Berrangé"), P(name="Tamsin Berrange")))
        self.assertTrue(identity.same_person(P(name="Ran Benita", email="ran@x"), P(email="RAN@X")))
        self.assertTrue(identity.same_person(P(handle="bluetech"), P(name="Ran Benita", handle="Bluetech")))
        self.assertTrue(identity.same_person(P(name="Kwame Asante"), P(handle="kwameasante")))
        self.assertTrue(identity.same_person(P(email="1234+octocat@users.noreply.github.com"), P(handle="octocat")))
        self.assertTrue(identity.same_person(P(name="Priya Natarajan", aliases=("priya.n@old.example",)),
                                             P(email="priya.n@old.example")))
        self.assertFalse(identity.same_person(P(name="Priya Natarajan"), P(name="Priya Sharma")))
        self.assertFalse(identity.same_person(P(name="Wes"), P(name="Wes Chen")))

    def test_parse_person_forms(self):
        self.assertEqual(identity.parse_person("Wes Chen <wes@acme.example>"), identity.Person(name="Wes Chen", email="wes@acme.example"))
        self.assertEqual(identity.parse_person("wes@acme.example"), identity.Person(email="wes@acme.example"))
        self.assertEqual(identity.parse_person("@wchen"), identity.Person(handle="wchen"))
        self.assertEqual(identity.parse_person("Wes"), identity.Person(name="Wes"))

    def test_a_first_name_resolves_only_when_unique(self):
        names = ["Priya Natarajan", "Yuki Tanaka", "Priya Sharma"]
        self.assertEqual(identity.first_name_match("Yuki", names), "Yuki Tanaka")
        self.assertEqual(identity.first_name_match("Priya", names), "Priya")
        self.assertEqual(identity.first_name_match("Nobody", names), "Nobody")
        self.assertEqual(identity.first_name_match("Yuki Tanaka", names), "Yuki Tanaka")

    def test_decision_scopes_name_categories(self):
        self.assertEqual(decision_scopes("Should we bill the usage spike, or exclude it as a load test?")[0], "billing")
        self.assertIn("pricing", decision_scopes("Do we apply per-seat minimums under usage-based pricing?"))
        self.assertIn("security", decision_scopes("Can we shorten service token expiry to 24 hours?"))
        self.assertEqual(decision_scopes("Should hw/riscv/virt.c add the imsics compatible string?"), [])
        self.assertEqual(decision_scopes("anything", category="pricing")[0], "pricing")
        self.assertEqual(decision_scopes("anything", category="policy"), [])


class PeopleTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "people.db")
        self.graph = self.store.graph

    def test_a_person_is_one_row_however_they_are_named(self):
        with self.graph.transaction():
            pid = self.graph.add_person("Wes Chen", email="wes@acme.example", github_login="wchen", team="Billing")
            same = self.graph.add_person("W. Chen", email="WES@acme.example", slack_id="U123")
        self.assertEqual(pid, same)
        p = self.graph.get_person(pid)
        self.assertEqual((p["name"], p["github_login"], p["slack_id"]), ("W. Chen", "wchen", "U123"))
        self.assertIn("Wes Chen", p["aliases"])
        for probe in ("wes@acme.example", "@wchen", "U123", "Wes Chen", "W. Chen", pid):
            self.assertEqual(self.graph.find_person(probe)["id"], pid, probe)
        # One inbox owner row, linked to the person.
        owners = self.graph.db.execute("SELECT * FROM owners WHERE person_id=?", (pid,)).fetchall()
        self.assertEqual(len(owners), 1)
        self.assertEqual(self.graph.owner_id_for("wes@acme.example"), owners[0]["id"])
        self.assertEqual(self.graph.resolve_engineer("@wchen"), "W. Chen")

    def test_a_first_name_finds_the_one_person_it_starts(self):
        with self.graph.transaction():
            self.graph.add_person("Priya Natarajan", email="priya@acme.example")
            self.graph.add_person("Yuki Tanaka", email="yuki@acme.example")
        self.assertEqual(self.graph.find_person("Yuki")["name"], "Yuki Tanaka")
        with self.graph.transaction():
            self.graph.add_person("Priya Sharma", email="ps@acme.example")
        self.assertIsNone(self.graph.find_person("Priya"))
        self.assertIsNone(self.graph.find_person("Nobody Known"))

    def test_store_api_validates_and_links(self):
        p = self.store.add_person({"name": "Marisol Vega", "email": "marisol@acme.example", "aliases": "mvega, @marisolv",
                                   "team": "Pricing"})
        self.assertEqual(p["aliases"], ["mvega", "@marisolv"])
        self.assertEqual(self.graph.find_person("@marisolv")["id"], p["id"])
        with self.assertRaises(Invalid):
            self.store.add_person({"name": "X", "role": "king"})
        with self.assertRaises(Invalid):
            self.store.add_authority({"person": "Nobody", "scope_kind": "path", "scope": "billing/*"})
        a = self.store.add_authority({"person": "marisol@acme.example", "scope_kind": "category", "scope": "pricing",
                                      "role": "decides", "by": "Wes Chen"})
        self.assertEqual((a["who"], a["scope"], a["role"], a["asserted_by"]), ("Marisol Vega", "pricing", "decides", "Wes Chen"))
        with self.assertRaises(Invalid):
            self.store.add_authority({"person": p["id"], "scope_kind": "category", "scope": "not-a-category"})
        ended = self.store.end_authority(a["id"])
        self.assertTrue(ended["ended"])
        self.assertEqual([x["id"] for x in self.store.authority()], [])


class AuthorityRoutingTests(OfflineCase):
    """Routing on the qemu-like repository, with the authority map on top."""

    def setUp(self):
        super().setUp()
        self.store = self.warm_store("qemulike", "authority.db")
        self.graph = self.store.graph

    def ask(self, question, path="hw/riscv/virt.c", **extra):
        t = canvas.start_task(self.store, CFG, {"title": "Add usage-based pricing", "repo": "qemulike",
                                                "requester": "Tamsin Reed <tamsin@synthco.example>", "paths": path})
        return canvas.add_node(self.store, CFG, {"task_id": t["task_id"], "question": question, "paths": path, **extra})

    def test_git_alone_routes_a_billing_question_to_the_file_owner(self):
        n = self.ask("Should we bill the usage spike, or exclude it as a load test?")
        self.assertEqual(n["owner"], "Oriel Vance", n["owner_evidence"])
        self.assertNotIn("verified", n["owner_evidence"])

    def test_authority_many_people_share_says_so_rather_than_naming_one(self):
        """A CODEOWNERS team expanded into its members gives all of them
        the same authority for the same area, and the winner's evidence
        then reads like a finding about that person. Measured on Grafana:
        139 people were recorded as deciding one area, all within 0.02 of
        each other, and which came first was very nearly arbitrary."""
        with self.graph.transaction():
            for i in range(14):
                pid = self.graph.add_person(f"Squad Member {i}", email=f"m{i}@qemu.example")
                self.graph.add_authority("path", "hw/riscv/*", "decides", person_id=pid, source="codeowners")
        n = self.ask("Should hw/riscv/virt.c change the default interrupt controller?")
        self.assertIn("people are recorded as deciding for that same area", n["owner_evidence"])
        self.assertIn("rather than a decider the map picks out", n["owner_evidence"])

    def test_a_small_team_sharing_an_area_is_a_real_answer(self):
        with self.graph.transaction():
            for i in range(3):
                pid = self.graph.add_person(f"Small Squad {i}", email=f"s{i}@qemu.example")
                self.graph.add_authority("path", "hw/riscv/*", "decides", person_id=pid, source="codeowners")
        n = self.ask("Should hw/riscv/virt.c change the default interrupt controller?")
        self.assertNotIn("are recorded as deciding for that same area", n["owner_evidence"])

    def test_a_category_authority_outranks_the_file_owner(self):
        with self.graph.transaction():
            pid = self.graph.add_person("Marisol Vega", email="marisol@acme.example", team="Pricing")
            self.graph.add_authority("category", "billing", "decides", person_id=pid, source="config",
                                     asserted_by="Wes Chen")
        n = self.ask("Should we bill the usage spike, or exclude it as a load test?")
        self.assertEqual(n["owner"], "Marisol Vega", n["owner_evidence"])
        self.assertTrue(n["owner_evidence"].startswith("verified: Marisol Vega decides for billing decisions"))
        self.assertIn("asserted by Wes Chen", n["owner_evidence"])
        # The category authority says nothing about a board question.
        m = self.ask("Should hw/riscv/virt.c change the default interrupt controller?")
        self.assertEqual(m["owner"], "Oriel Vance")

    def test_a_path_authority_outranks_blame_and_survives_inactivity(self):
        with self.graph.transaction():
            pid = self.graph.add_person("Old Timer", email="old@past.example")
            self.graph.add_authority("path", "hw/riscv/*", "approves", person_id=pid, source="config")
        n = self.ask("Should hw/riscv/virt.c change the default interrupt controller?")
        self.assertEqual(n["owner"], "Old Timer", n["owner_evidence"])
        self.assertTrue(n["owner_evidence"].startswith("verified: Old Timer must approve for hw/riscv/*"))
        self.assertEqual(n["ranked"][1]["owner"], "Oriel Vance")

    def test_the_narrowest_path_authority_owns_the_file(self):
        """Nested ownership is how a large repository is actually laid
        out, and CODEOWNERS' own rule is that the last, most specific
        matching pattern owns the file. A team listed for the directory
        above does not also decide inside the directory another team is
        listed for; it stays on the list, below them."""
        with self.graph.transaction():
            broad = self.graph.add_person("Platform Pat", email="pat@acme.example")
            narrow = self.graph.add_person("Board Bea", email="bea@acme.example")
            self.graph.add_authority("path", "hw/", "decides", person_id=broad, source="codeowners")
            self.graph.add_authority("path", "hw/riscv/", "decides", person_id=narrow, source="codeowners")
        n = self.ask("Should hw/riscv/virt.c change the default interrupt controller?")
        self.assertEqual(n["owner"], "Board Bea", n["owner_evidence"])
        self.assertIn("decides for hw/riscv/", n["owner_evidence"])
        self.assertNotIn("Platform Pat", n["owner_evidence"])
        self.assertIn("Platform Pat", [r["owner"] for r in n["ranked"]])
        # Outside the narrower rule the broader one still decides.
        m = self.ask("Should hw/net/virtio-net.c drop the legacy header?", path="hw/net/virtio-net.c")
        self.assertEqual(m["owner"], "Platform Pat", m["owner_evidence"])

    def test_a_narrower_approver_does_not_displace_the_decider(self):
        """Measured live on eb9d22d: Mira decides util/*, Theo must approve
        util/retry.py, and five of seven questions about retry.py went to
        Theo. Deciding and approving are two responsibilities: the decider
        is asked, the approver signs."""
        with self.graph.transaction():
            mira = self.graph.add_person("Mira Runtime", email="mira@acme.example")
            theo = self.graph.add_person("Theo Release", email="theo@acme.example")
            self.graph.add_authority("path", "hw/riscv/*", "decides", person_id=mira, source="config")
            self.graph.add_authority("path", "hw/riscv/virt.c", "approves", person_id=theo, source="config")
        n = self.ask("Which unit should the new timeout option on hw/riscv/virt.c use?")
        self.assertEqual(n["owner"], "Mira Runtime", n["owner_evidence"])
        self.assertTrue(n["owner_evidence"].startswith("verified: Mira Runtime decides for hw/riscv/*"),
                        n["owner_evidence"])
        self.assertNotIn("more specific rule", n["owner_evidence"].split(";")[0])
        self.assertEqual(n["required_signers"], ["Theo Release"])
        # An approver whose own history adds to their score is still not
        # the decider: Oriel wrote most of virt.c and must approve it.
        with self.graph.transaction():
            oriel = self.graph.add_person("Oriel Vance", email="oriel@riscv.example")
            self.graph.add_authority("path", "hw/riscv/virt.c", "approves", person_id=oriel, source="config")
        a = self.ask("Should hw/riscv/virt.c keep the legacy boot order for old guests?")
        self.assertEqual(a["owner"], "Mira Runtime", a["owner_evidence"])
        self.assertIn("Oriel Vance must approve here and signs as a required approver; Mira Runtime decides "
                      "it, so Raven asks Mira Runtime", a["owner_evidence"])
        self.assertIn("Oriel Vance", a["required_signers"])
        # With nobody who decides, the person who must approve is asked.
        m = self.ask("Should hw/riscv/boot.c load the device tree before the firmware?", path="hw/riscv/boot.c")
        self.assertEqual(m["owner"], "Mira Runtime", m["owner_evidence"])
        with self.graph.transaction():
            self.graph.add_authority("path", "hw/net/virtio-net.c", "approves", person_id=theo, source="config")
        k = self.ask("Should hw/net/virtio-net.c drop the legacy header?", path="hw/net/virtio-net.c")
        self.assertEqual(k["owner"], "Theo Release", k["owner_evidence"])

    def test_the_decider_of_what_a_decision_is_about_outranks_the_decider_of_its_files(self):
        """Measured live on 5e967e4: "May Retry-After diagnostics contain
        bearer credentials?", category security, went to the person who
        decides util/*, and the security decider was not among the signers."""
        with self.graph.transaction():
            mira = self.graph.add_person("Mira Runtime", email="mira@acme.example")
            sam = self.graph.add_person("Sam Security", email="sam@acme.example")
            self.graph.add_authority("path", "hw/riscv/*", "decides", person_id=mira, source="config")
            self.graph.add_authority("category", "security", "decides", person_id=sam, source="config")
        n = self.ask("May the boot diagnostics on hw/riscv/virt.c contain bearer credentials?", category="security")
        self.assertEqual(n["owner"], "Sam Security", n["owner_evidence"])
        self.assertIn("Sam Security decides security decisions, which this one is about; Mira Runtime decides the "
                      "files it lands in, so Raven asks Sam Security", n["owner_evidence"])
        # A question about the files themselves stays with their decider.
        m = self.ask("Which unit should the new boot delay option on hw/riscv/virt.c use?")
        self.assertEqual(m["owner"], "Mira Runtime", m["owner_evidence"])

    def test_the_decider_for_any_path_a_node_names_is_found(self):
        """Measured live on eb9d22d: a decision about connectionpool.py and
        util/retry.py went to the team listed for the first file, past the
        person the map says decides the second."""
        with self.graph.transaction():
            mira = self.graph.add_person("Mira Runtime", email="mira@acme.example")
            self.graph.add_authority("path", "hw/riscv/*", "decides", person_id=mira, source="config")
        n = self.ask("Should the network device and the board share one reset budget?",
                     path="hw/net/virtio-net.c,hw/riscv/virt.c")
        self.assertEqual(n["owner"], "Mira Runtime", n["owner_evidence"])
        self.assertIn("decides for hw/riscv/* (matches hw/riscv/virt.c)", n["owner_evidence"])

    def test_a_pathless_question_routes_on_its_category(self):
        with self.graph.transaction():
            pid = self.graph.add_person("Marisol Vega", email="marisol@acme.example")
            self.graph.add_authority("category", "pricing", "decides", person_id=pid)
        picked = route(self.graph, "qemulike", "Do we apply per-seat minimums under usage-based pricing?")
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0], "Marisol Vega")
        self.assertIn("routed on the decision's category", "; ".join(picked[1]))

    def test_a_repo_wide_and_an_expired_authority(self):
        with self.graph.transaction():
            pid = self.graph.add_person("Beatrix Hale", email="beatrix@integrator.example")
            self.graph.add_authority("repo", "", "decides", person_id=pid, effective_to="2000-01-01T00:00:00+00:00")
        n = self.ask("Should hw/riscv/virt.c change the default interrupt controller?")
        self.assertEqual(n["owner"], "Oriel Vance")
        with self.graph.transaction():
            self.graph.add_authority("repo", "", "decides", person_id=pid)
        n = self.ask("Should hw/riscv/virt.c move the flash base address?")
        self.assertEqual(n["owner"], "Beatrix Hale")
        self.assertIn("repository-wide", n["owner_evidence"])

    def test_an_unaccepted_referral_is_a_candidate_not_the_owner(self):
        with self.graph.transaction():
            pid = self.graph.add_person("Marisol Vega", email="marisol@acme.example")
            self.graph.add_authority("category", "billing", "decides", person_id=pid, source="referral",
                                     asserted_by="Wes Chen", accepted=False)
        ranked = route_ranked(self.graph, "qemulike", "Should we bill the usage spike, or exclude it as a load test?",
                              path="hw/riscv/virt.c", requester="Tamsin Reed")
        self.assertEqual(ranked[0][0], "Oriel Vance")
        self.assertIn("Marisol Vega", [r[0] for r in ranked])
        marisol = next(ev for name, ev, _score in ranked if name == "Marisol Vega")
        self.assertIn("not yet accepted", "; ".join(marisol))

    def test_a_team_authority_routes_to_its_members(self):
        with self.graph.transaction():
            a = self.graph.add_person("Marisol Vega", email="marisol@acme.example")
            b = self.graph.add_person("Wes Chen", email="wes@acme.example")
            tid = self.graph.add_team("Pricing", handle="acme/pricing")
            self.graph.set_team_members(tid, [a, b])
            self.graph.add_authority("category", "pricing", "decides", team_id=tid)
        ranked = route_ranked(self.graph, "qemulike", "Do we apply per-seat minimums under usage-based pricing?")
        self.assertEqual({r[0] for r in ranked[:2]}, {"Marisol Vega", "Wes Chen"})
        self.assertIn("as a member of Pricing", ranked[0][1][0])

    def test_the_requester_is_routed_to_when_the_decision_is_theirs(self):
        with self.graph.transaction():
            pid = self.graph.add_person("Tamsin Reed", email="tamsin@synthco.example")
            self.graph.add_authority("category", "billing", "decides", person_id=pid)
        n = self.ask("Should we bill the usage spike, or exclude it as a load test?")
        self.assertEqual(n["owner"], "Tamsin Reed", n["owner_evidence"])

    def test_a_first_name_requester_is_never_asked_their_own_question(self):
        picked = route(self.graph, "qemulike", "Should hw/riscv/virt.c add the imsics compatible string?",
                       path="hw/riscv/virt.c", requester="Oriel")
        self.assertIsNotNone(picked)
        self.assertNotEqual(picked[0], "Oriel Vance", picked[1])
        # Through the people table too: an alias names the same person.
        with self.graph.transaction():
            self.graph.add_person("Oriel Vance", email="oriel@riscv.example", aliases=("alf",))
        picked = route(self.graph, "qemulike", "Should hw/riscv/virt.c add the imsics compatible string?",
                       path="hw/riscv/virt.c", requester="alf")
        self.assertIsNotNone(picked)
        self.assertNotEqual(picked[0], "Oriel Vance", picked[1])


class TeamCodeownersTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.warm_store("nodelike", "teams.db")
        self.graph = self.store.graph

    def test_a_known_team_in_codeowners_is_its_members(self):
        before = route(self.graph, "nodelike", "Should sqlite expose the changes count?", path="lib/sqlite.js")
        self.assertIsNotNone(before)
        self.assertIn("team @nodejs/sqlite", "; ".join(before[1]))
        with self.graph.transaction():
            pid = self.graph.add_person("Casimir Vale", email="casimir@node.example", github_login="cjihrig")
            tid = self.graph.add_team("SQLite", handle="nodejs/sqlite")
            self.graph.set_team_members(tid, [pid])
        after = route(self.graph, "nodelike", "Should sqlite expose the changes count?", path="lib/sqlite.js")
        self.assertEqual(after[0], "Casimir Vale")
        self.assertIn("CODEOWNERS lists the team @nodejs/sqlite for /lib/sqlite.js; Casimir Vale is a member", "; ".join(after[1]))


class ReadinessTests(OfflineCase):
    """A repository's public history names authors, not deciders. A pilot
    that ingests one and stops finds every question landing on the
    coordinator, and nothing said so."""

    def setUp(self):
        super().setUp()
        self.store = self.warm_store("qemulike", "readiness.db")
        self.graph = self.store.graph

    def keys(self):
        return {r["key"] for r in self.store.readiness()}

    def test_a_fresh_ingest_says_what_is_missing(self):
        found = self.keys()
        self.assertIn("no_people", found)
        self.assertIn("no_authority", found)
        self.assertIn("no_coordinator", found)
        blockers = [r for r in self.store.readiness() if r["level"] == "blocker"]
        self.assertTrue(blockers)
        for r in self.store.readiness():
            self.assertTrue(r["what"] and r["do"], r)

    def test_setting_the_map_up_clears_them(self):
        with self.graph.transaction():
            pid = self.graph.add_person("Oriel Vance", email="oriel@qemu.example", slack_id="UALI")
            self.graph.add_authority("path", "hw/riscv/*", "decides", person_id=pid)
            self.graph.set_setting("coordinator", pid)
        found = self.keys()
        self.assertNotIn("no_people", found)
        self.assertNotIn("no_authority", found)
        self.assertNotIn("no_coordinator", found)

    def test_authority_for_another_repository_does_not_clear_this_one(self):
        """The blocker is about the repositories that are actually here.
        A row recorded for something else covers nothing of theirs, and
        clearing it on the count of rows in the table said the map was
        set up while every question still fell to the coordinator."""
        with self.graph.transaction():
            pid = self.graph.add_person("Wes Chen", email="wes@qemu.example", slack_id="UWES")
            self.graph.add_authority("path", "billing/*", "decides", person_id=pid, repo="acme/other")
        note = next((r for r in self.store.readiness() if r["key"] == "no_authority"), None)
        self.assertIsNotNone(note, self.store.readiness())
        self.assertEqual(note["level"], "warning")
        self.assertIn("qemulike", note["what"])

    def test_an_expired_authority_does_not_clear_it_either(self):
        """A row whose effective_to has passed decides nothing today."""
        with self.graph.transaction():
            pid = self.graph.add_person("Wes Chen", email="wes@qemu.example", slack_id="UWES")
            self.graph.add_authority("path", "hw/riscv/*", "decides", person_id=pid, effective_to="2020-01-01")
        self.assertIn("no_authority", self.keys())
        with self.graph.transaction():
            self.graph.add_authority("path", "hw/riscv/*", "decides", person_id=pid, effective_to="2099-01-01")
        self.assertNotIn("no_authority", self.keys())

    def test_knowing_an_area_is_not_deciding_for_it(self):
        with self.graph.transaction():
            pid = self.graph.add_person("Wes Chen", email="wes@qemu.example", slack_id="UWES")
            self.graph.add_authority("path", "hw/riscv/*", "knows", person_id=pid)
        self.assertIn("no_authority", self.keys())

    def test_a_second_repository_with_nobody_is_reported_without_hiding_the_first(self):
        with self.graph.transaction():
            pid = self.graph.add_person("Oriel Vance", email="oriel@qemu.example", slack_id="UALI")
            self.graph.add_authority("path", "hw/riscv/*", "decides", person_id=pid, repo="qemulike")
            self.graph.set_ownership("acme/other", "docs/", "Oriel Vance", "codeowners", 1.0, "CODEOWNERS")
        found = {r["key"]: r for r in self.store.readiness()}
        gap = next((v for k, v in found.items() if k.startswith("no_authority")), None)
        self.assertIsNotNone(gap, found)
        self.assertEqual(gap["level"], "warning")
        self.assertIn("acme/other", gap["what"])

    def test_a_team_handle_is_refused_as_a_person(self):
        """Pasting a CODEOWNERS entry in makes a person nobody can reach:
        messages go nowhere and the decision sits."""
        with self.assertRaises(ValueError) as refused:
            with self.graph.transaction():
                self.graph.add_person("@grafana/dataviz-squad")
        self.assertIn("is a team, not a person", str(refused.exception))
        # A person whose name happens to contain a slash is still a person,
        # and a team handle with a real identity attached is somebody.
        with self.graph.transaction():
            self.graph.add_person("Dana O/Brien", email="dana@qemu.example")
            self.graph.add_person("grafana/oncall", email="oncall@qemu.example")
        self.assertIsNotNone(self.graph.find_person("dana@qemu.example"))

    def test_teams_already_stored_as_people_are_reported(self):
        with self.graph.transaction():
            self.graph.db.execute(
                "INSERT INTO people(id, name, email, github_login, slack_id, aliases, team, role, active, "
                "source, created_at, updated_at, github_id) "
                "VALUES('t1','@grafana/dataviz-squad','','','','[]','','member',1,'config','','','')")
        note = next((r for r in self.store.readiness() if r["key"] == "teams_as_people"), None)
        self.assertIsNotNone(note, self.store.readiness())
        self.assertEqual(note["level"], "blocker")
        self.assertIn("reaches nobody", note["what"])

    def test_questions_falling_through_to_the_coordinator_are_counted(self):
        with self.graph.transaction():
            pid = self.graph.add_person("Wes Chen", email="wes@qemu.example", slack_id="UWES")
            self.graph.set_setting("coordinator", pid)
        for i in range(3):
            t = canvas.start_task(self.store, CFG, {"title": f"Task {i}", "repo": "qemulike",
                                                     "client_key": f"k{i}", "paths": "docs/notes.md",
                                                     "requester": "Tamsin Reed"})
            canvas.add_node(self.store, CFG, {"task_id": t["task_id"], "client_ref": "q",
                                              "question": f"Should the pricing tier {i} change?",
                                              "paths": "docs/notes.md"})
        note = next((r for r in self.store.readiness() if r["key"] == "falling_through"), None)
        self.assertIsNotNone(note, self.store.readiness())
        self.assertIn("reached the coordinator", note["what"])


class CoordinatorTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.warm_store("qemulike", "coordinator.db")
        self.graph = self.store.graph
        with self.graph.transaction():
            self.coord = self.graph.add_person("Sam Coordinator", email="sam@acme.example")

    def node(self, question, **extra):
        t = canvas.start_task(self.store, CFG, {"title": "Something", "repo": "qemulike",
                                                "requester": "Tamsin Reed", **({"paths": extra.pop("paths")} if "paths" in extra else {})})
        return canvas.add_node(self.store, CFG, {"task_id": t["task_id"], "question": question, **extra})

    def test_what_nobody_owns_goes_to_the_coordinator(self):
        n = self.node("Should the release notes mention the pricing change?")
        self.assertEqual(n["status"], "unrouted")
        self.store.update_settings({"coordinator": "sam@acme.example"})
        n = self.node("Should the changelog mention the new billing tier?")
        self.assertEqual((n["status"], n["owner"]), ("pending", "Sam Coordinator"))
        self.assertIn("coordinator fallback", n["owner_evidence"])

    def test_an_owner_nobody_can_reach_goes_to_the_coordinator(self):
        """Measured live on 5e967e4: the only verified owner had expired, the
        route went to a contributor from git history who is not one of
        Raven's people, and the message to them failed."""
        self.store.update_settings({"coordinator": self.coord})
        n = self.node("Should hw/riscv/virt.c change the default interrupt controller?", paths="hw/riscv/virt.c")
        self.assertEqual(n["owner"], "Sam Coordinator", n["owner_evidence"])
        self.assertIn("Oriel Vance leads on git history but is not one of the people in Raven",
                      n["owner_evidence"])
        self.assertIn("candidate from git history: Oriel Vance", n["owner_evidence"])
        # Once they are one of its people, the history routes to them.
        with self.graph.transaction():
            self.graph.add_person("Oriel Vance", email="oriel@riscv.example")
        m = self.node("Should hw/riscv/virt.c move the flash base address?", paths="hw/riscv/virt.c")
        self.assertEqual(m["owner"], "Oriel Vance", m["owner_evidence"])

    def test_pilot_mode_sends_inferred_routes_to_the_coordinator_with_candidates(self):
        self.store.update_settings({"coordinator": self.coord, "require_verified_route": True})
        n = self.node("Should hw/riscv/virt.c change the default interrupt controller?", paths="hw/riscv/virt.c")
        self.assertEqual(n["owner"], "Sam Coordinator", n["owner_evidence"])
        self.assertIn("candidate from git history: Oriel Vance", n["owner_evidence"])
        with self.graph.transaction():
            pid = self.graph.add_person("Oriel Vance", email="oriel@riscv.example")
            self.graph.add_authority("path", "hw/riscv/*", "decides", person_id=pid)
        n = self.node("Should hw/riscv/virt.c move the flash base address?", paths="hw/riscv/virt.c")
        self.assertEqual(n["owner"], "Oriel Vance", n["owner_evidence"])
        self.assertEqual(self.store.settings()["coordinator"]["name"], "Sam Coordinator")

    def test_a_per_repository_coordinator_wins(self):
        with self.graph.transaction():
            other = self.graph.add_person("Repo Lead", email="lead@acme.example")
        self.store.update_settings({"coordinator": self.coord})
        self.store.update_settings({"coordinator": other, "repo": "qemulike"})
        self.assertEqual(self.graph.coordinator("qemulike")["name"], "Repo Lead")
        self.assertEqual(self.graph.coordinator("elsewhere")["name"], "Sam Coordinator")


class SurfaceTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "surface.db")

    def test_rest_round_trip(self):
        server = make_server(self.store, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        port = server.server_port
        base = f"http://127.0.0.1:{port}"

        def get(path):
            with urlopen(Request(base + path, headers={"Host": f"127.0.0.1:{port}"})) as r:
                return json.loads(r.read())

        csrf = get("/api/state")["csrf_token"]

        def post(path, data):
            req = Request(base + path, data=json.dumps(data).encode(), method="POST",
                          headers={"Content-Type": "application/json", "X-Bridge-CSRF": csrf, "Host": f"127.0.0.1:{port}"})
            with urlopen(req) as r:
                return json.loads(r.read())

        p = post("/api/people", {"name": "Marisol Vega", "email": "marisol@acme.example", "github_login": "mvega", "team": "Pricing"})
        w = post("/api/people", {"name": "Wes Chen", "email": "wes@acme.example"})
        t = post("/api/teams", {"name": "Pricing", "handle": "acme/pricing", "members": "marisol@acme.example, Wes Chen"})
        self.assertEqual(set(t["members"]), {p["id"], w["id"]})
        a = post("/api/authority", {"person": "@mvega", "scope_kind": "category", "scope": "pricing", "role": "decides",
                                    "by": "Wes Chen", "note": "owns commercial exceptions"})
        self.assertEqual(a["who"], "Marisol Vega")
        s = post("/api/settings", {"coordinator": "Wes Chen", "require_verified_route": True})
        self.assertEqual(s["coordinator"]["name"], "Wes Chen")
        self.assertTrue(s["require_verified_route"])
        listing = get("/api/people")
        self.assertEqual([x["name"] for x in listing["people"]], ["Marisol Vega", "Wes Chen"])
        self.assertEqual(listing["people"][0]["teams"], ["Pricing"])
        self.assertEqual(listing["authority"][0]["scope"], "pricing")
        with self.assertRaises(HTTPError) as bad:
            post("/api/authority", {"person": "Unknown Person", "scope_kind": "path", "scope": "x/*"})
        self.assertEqual(bad.exception.code, 400)
        ended = post(f"/api/authority/{a['id']}/end", {})
        self.assertTrue(ended["ended"])
        self.assertEqual(get("/api/people")["authority"], [])

    def test_mcp_list_owners_carries_the_verified_layer(self):
        self.store.add_person({"name": "Marisol Vega", "email": "marisol@acme.example"})
        self.store.add_authority({"person": "Marisol Vega", "scope_kind": "category", "scope": "billing", "role": "decides"})
        self.store.update_settings({"coordinator": "Marisol Vega"})
        r = dispatch(self.store, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "bridge_list_owners", "arguments": {}}})
        out = json.loads(r["result"]["content"][0]["text"])
        self.assertEqual(out["people"][0]["name"], "Marisol Vega")
        self.assertEqual(out["authority"][0]["role"], "decides")
        self.assertEqual(out["coordinator"], "Marisol Vega")


if __name__ == "__main__":
    unittest.main()
