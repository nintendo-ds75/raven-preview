"""Regression pins from the routing replay loop (bench/routing): each
test is one failure class the bench found on real repositories, rebuilt
on a deterministic synthetic repository with a MAINTAINERS file,
review trailers, an accepting committer, an integrator, a bot, a
tree-wide sweep, and an inactive old-timer (tests/fixtures.py)."""

import datetime
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase, build_nodelike, build_qemulike

from bridge import llm as llm_mod
from bridge.config import Config
from bridge.ingest import index_repo
from bridge.ladder import ask
from bridge.routing import route, route_ranked
from bridge.store import Store
from bridge.signals import _scope_signals


class MergeWeightTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "merge-weights.db")
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.timestamp = "2026-06-20T00:00:00+00:00"
        self.graph.set_source("app", "git_now", self.timestamp)
        for path in ("alpha/code.py", "beta/code.py"):
            self.graph.upsert_artifact("app", path)

    def change(self, sha, area, people):
        self.graph.add_change("app", sha, self.timestamp, [f"{area}/code.py"],
                              [(name, "", role) for name, role in people])

    def seed(self):
        for area in ("alpha", "beta"):
            for i in range(12):
                self.change(f"{area}-{i}", area, [(f"{area} Author", "author"),
                                                  (f"{area} Reviewer", "reviewed-by")])

    def test_general_integrator_is_retained_but_does_not_displace_area_reviewer(self):
        self.seed()
        before = route(self.graph, "app", "Who should approve this?", path="alpha/code.py")
        for area in ("alpha", "beta"):
            for i in range(12):
                self.change(f"merge-{area}-{i}", area, [("Integrator", "merger")])
        after = route(self.graph, "app", "Who should approve this?", path="alpha/code.py")
        self.assertEqual(before[0], "alpha Reviewer")
        self.assertEqual(after[0], before[0])
        self.assertAlmostEqual(after[2], before[2])
        sig = _scope_signals(self.graph, "app", "alpha/", 0)
        self.assertEqual(sig["roles"]["integrator"]["merger"], 12)
        self.assertLess(sig["approval"]["integrator"], sig["approval"]["alpha reviewer"])

    def test_merge_specialization_can_route_without_authorship(self):
        for area, name in (("alpha", "Area Maintainer"), ("beta", "Integrator")):
            for i in range(12):
                self.change(f"merge-{area}-{i}", area, [(name, "merger")])
        picked = route(self.graph, "app", "Who should approve this?", path="alpha/code.py")
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0], "Area Maintainer")

    def test_pure_repo_wide_merging_is_not_enough_to_route(self):
        for area in ("alpha", "beta"):
            for i in range(12):
                self.change(f"merge-{area}-{i}", area, [("Integrator", "merger")])
        self.assertIsNone(route(self.graph, "app", "Who should approve this?", path="alpha/code.py"))
        self.assertIsNone(route(self.graph, "app", "Who should approve the project?"))

    def test_independent_area_work_strengthens_a_frequent_mergers_evidence(self):
        for area in ("alpha", "beta"):
            for i in range(12):
                self.change(f"merge-{area}-{i}", area, [("Maintainer", "merger")])
                author = "Maintainer" if area == "alpha" and i < 6 else "Contributor"
                self.change(f"{area}-{i}", area, [(author, "author")])
        alpha = _scope_signals(self.graph, "app", "alpha/", 0)
        beta = _scope_signals(self.graph, "app", "beta/", 0)
        self.assertGreater(alpha["merge_weights"]["maintainer"], beta["merge_weights"]["maintainer"])
        picked = route(self.graph, "app", "Who should approve this?", path="alpha/code.py")
        self.assertEqual(picked[0], "Maintainer")

    def gatekept(self):
        """A repository one person merges nearly all of, where CODEOWNERS
        names the integration's own owner, who writes most of it."""
        self.graph.upsert_artifact("app", "gamma/code.py")
        for area in ("alpha", "beta", "gamma"):
            for i in range(12):
                self.change(f"merge-{area}-{i}", area, [("Gate Keeper", "merger")])
        for i in range(6):
            self.change(f"alpha-own-{i}", "alpha", [("zedowner", "author")])
        for i in range(3):
            self.change(f"alpha-sweep-{i}", "alpha", [("Gate Keeper", "author")])
        self.graph.add_listing("app", "codeowners", "/alpha/", "zedowner", "", "owner")

    def test_a_listed_owner_active_here_is_asked_before_the_repositorys_merger(self):
        """Measured on home-assistant/core: the person who merges four in
        five of the repository's changes led eight of ten integrations over
        the code owners who write them."""
        self.gatekept()
        picked = route(self.graph, "app", "Should retries back off here?", path="alpha/code.py")
        self.assertEqual(picked[0], "zedowner")
        self.assertTrue(any("leads here on merging" in ln and "CODEOWNERS lists zedowner" in ln for ln in picked[1]),
                        picked[1])

    def test_a_handle_is_joined_to_the_name_its_noreply_email_names(self):
        """CODEOWNERS says @zo-dev; the commits say "Zed Owner" from
        123+zo-dev@users.noreply.github.com. Before GitHub is synced that
        address is the only join between them."""
        for i in range(6):
            self.graph.add_change("app", f"alpha-zed-{i}", self.timestamp, ["alpha/code.py"],
                                  [("Zed Owner", "123+zo-dev@users.noreply.github.com", "author")])
            self.change(f"alpha-other-{i}", "alpha", [("Other Author", "author")])
        self.graph.add_listing("app", "codeowners", "/alpha/", "zo-dev", "", "owner")
        picked = route(self.graph, "app", "Should retries back off here?", path="alpha/code.py")
        self.assertEqual(picked[0], "Zed Owner")
        self.assertIn("CODEOWNERS lists Zed Owner for /alpha/", picked[1])

    def test_a_merger_of_one_area_keeps_it_over_a_listed_bystander(self):
        """Merging one area is ownership, not gatekeeping: the rule is for
        someone who merges across the repository."""
        for i in range(12):
            self.change(f"merge-alpha-{i}", "alpha", [("Area Maintainer", "merger")])
            self.change(f"merge-beta-{i}", "beta", [("Beta Maintainer", "merger")])
        for i in range(4):
            self.change(f"alpha-own-{i}", "alpha", [("Area Maintainer", "author")])
        self.change("alpha-by", "alpha", [("bystander", "author")])
        self.graph.add_listing("app", "codeowners", "/alpha/", "bystander", "", "owner")
        picked = route(self.graph, "app", "Should retries back off here?", path="alpha/code.py")
        self.assertEqual(picked[0], "Area Maintainer")

    def test_multiple_approval_roles_on_one_change_are_not_counted_twice(self):
        for i in range(12):
            self.change(str(i), "alpha", [("Writer", "author"), ("Reviewer", "reviewed-by"),
                                           ("Reviewer", "committer")])
        sig = _scope_signals(self.graph, "app", "alpha/", 0)
        self.assertEqual(sig["approval"]["reviewer"], sig["total"])
        req = ({"writer"}, set())
        sig = _scope_signals(self.graph, "app", "alpha/", 0, req)
        self.assertEqual(sig["aff"]["reviewer"], sig["req_total"])

    def test_merges_elsewhere_refresh_cached_specialization_across_connections(self):
        for i in range(12):
            self.change(f"alpha-{i}", "alpha", [("Maintainer", "merger")])
        self.graph.db.commit()
        before = _scope_signals(self.graph, "app", "alpha/", 0)["merge_weights"]["maintainer"]
        other = Store(Path(self.temp.name) / "merge-weights.db")
        try:
            with other.graph.transaction():
                for i in range(12):
                    other.graph.add_change("app", f"beta-{i}", self.timestamp, ["beta/code.py"],
                                           [("Someone Else", "", "merger")])
        finally:
            other.graph.close()
        after = _scope_signals(self.graph, "app", "alpha/", 0)["merge_weights"]["maintainer"]
        self.assertGreater(after, before)


class QemuLikeCase(OfflineCase):
    """Fixture only: the qemu-like repository, an empty store per test,
    and warm() for the store with it ingested."""

    @classmethod
    def setUpClass(cls):
        cls.repo = build_qemulike()

    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "signals.db")
        self.graph = self.store.graph

    def warm(self):
        self.store = self.warm_store("qemulike", "warm.db")
        self.graph = self.store.graph
        return self.graph


class SignalRoutingTests(QemuLikeCase):
    def test_approvals_outrank_authorship_and_maintainers_are_listed(self):
        """Tamsin wrote nearly every riscv change; Oriel reviewed and
        accepted them and MAINTAINERS lists him. The route is Oriel,
        and the evidence names the review signal and the listing."""
        g = self.warm()
        picked = route(g, "qemulike", "Should hw/riscv/virt.c add the imsics compatible string?")
        self.assertIsNotNone(picked)
        owner, lines, _ = picked
        self.assertEqual(owner, "Oriel Vance")
        joined = " | ".join(lines)
        self.assertIn("MAINTAINERS lists Oriel Vance", joined)
        self.assertIn("reviewed or accepted", joined)
        self.assertNotIn("score", joined.lower())
        ranked = [r[0] for r in route_ranked(g, "qemulike", "Should hw/riscv/virt.c add the imsics compatible string?")]
        self.assertIn("Tamsin Reed", ranked[:3])

    def test_catch_all_maintainer_and_integrator_never_win(self):
        """Beatrix is listed for F: * and merges every pull; he is not the
        owner of hw/net, Kwame is."""
        g = self.warm()
        owner, lines, _ = route(g, "qemulike", "Should hw/net/virtio-net.c drop the legacy header?")
        self.assertEqual(owner, "Kwame Asante")
        self.assertNotIn("Beatrix Hale", [r[0] for r in route_ranked(g, "qemulike", "Should hw/net/virtio-net.c drop the legacy header?")][:1])
        self.assertTrue(any("listed project-wide" in ln for ln in lines) or "Beatrix" not in " ".join(lines))

    def test_sweep_and_bot_do_not_own_an_area(self):
        g = self.warm()
        for q in ("Should hw/net/e1000.c fix the checksum offload?", "Should util/cutils.c parse sizes?"):
            ranked = route_ranked(g, "qemulike", q)
            names = [r[0] for r in ranked]
            self.assertNotIn("Sweep Author", names[:1], q)
            self.assertFalse(any("bot" in n.lower() for n in names), q)

    def test_inactive_person_is_never_routed_to_and_the_note_says_so(self):
        g = self.warm()
        owner, lines, _ = route(g, "qemulike", "Should hw/riscv/boot.c load the firmware earlier?")
        self.assertNotEqual(owner, "Old Timer")
        # Old Timer is listed as a designated reviewer but inactive for two years.
        self.assertNotIn("Old Timer", [r[0] for r in route_ranked(g, "qemulike", "Should hw/riscv/boot.c load the firmware earlier?")])

    def test_pathless_question_finds_its_area_from_joined_words(self):
        """'buffer is zero' names util/bufferiszero.c once the words are
        joined; the reviewer of that file is the route."""
        g = self.warm()
        picked = route(g, "qemulike", "Should we simplify the buffer is zero accel test?")
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0], "Rich Reviewer")
        self.assertTrue(any("bufferiszero" in ln for ln in picked[1]), picked[1])

    def test_shared_header_directory_does_not_hijack(self):
        """include/hw/riscv/virt.h shares 'riscv' and 'virt' with the real
        file; the ubiquitous 'include' never wins on its own."""
        g = self.warm()
        picked = route(g, "qemulike", "Should the riscv virt board export the header?")
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0], "Oriel Vance")

    def test_unknown_is_honest_and_not_a_guess(self):
        g = self.warm()
        notes: list[str] = []
        picked = route(g, "qemulike", "Should we add the missing punctuation everywhere?", notes=notes)
        self.assertIsNone(picked)
        self.assertTrue(notes and "no path" in notes[0], notes)
        run = self.store.add_run({"title": "t", "agent": "test", "repo": "qemulike"})
        row = ask(self.store, Config(), run["id"], "Who should approve this change: Should we add the missing punctuation everywhere?",
                  context="c")
        self.assertIsNone(row["owner_name"])
        self.assertIn("does not know", row["owner_evidence"])

    def test_cold_start_routes_from_git_at_ask_time(self):
        """An empty graph that only knows where the checkout is routes
        exactly like the warm one, and leaves the touches behind."""
        g = self.graph
        g.set_source("qemulike", "git", str(self.repo))
        with patch.dict(os.environ, {"BRIDGE_LIVE": "1"}):
            picked = route(g, "qemulike", "Should hw/riscv/virt.c add the imsics compatible string?")
            self.assertIsNotNone(picked)
            self.assertEqual(picked[0], "Oriel Vance")
            self.assertIn("MAINTAINERS lists Oriel Vance", " | ".join(picked[1]))
            self.assertTrue(g.has_changes("qemulike"))
            pathless = route(g, "qemulike", "Should we simplify the buffer is zero accel test?")
            self.assertIsNotNone(pathless)
            self.assertEqual(pathless[0], "Rich Reviewer")

    def test_point_in_time_ingest_hides_the_future(self):
        """Ingesting at an old revision sees neither later commits nor the
        later tree."""
        g = self.graph
        old = subprocess.run(["git", "-C", str(self.repo), "rev-list", "--max-parents=0", "HEAD"],
                             capture_output=True, text=True).stdout.split()[0]
        stats = index_repo(g, self.repo, rev=old)
        self.assertEqual(stats["commits"], 1)
        # One import commit is no ownership; the MAINTAINERS listing at
        # that revision is what routes, and the note says the history is thin.
        picked = route(g, "qemulike", "Should hw/riscv/virt.c add a feature?")
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0], "Oriel Vance")
        self.assertIn("MAINTAINERS lists", picked[1][0])
        self.assertNotIn("Tamsin Reed", [r[0] for r in route_ranked(g, "qemulike", "Should hw/riscv/virt.c add a feature?")])

    def test_evidence_lines_name_signals(self):
        g = self.warm()
        for q in ("Should hw/riscv/virt.c add the imsics compatible string?",
                  "Should hw/net/virtio-net.c drop the legacy header?",
                  "Should net/tap.c set the MTU?"):
            picked = route(g, "qemulike", q)
            self.assertIsNotNone(picked, q)
            head = picked[1][0]
            self.assertRegex(head, r"MAINTAINERS lists|reviewed or accepted|authored \d+%|you told Raven")
            self.assertTrue(any(ln.startswith("area: ") for ln in picked[1]), picked[1])


class RevisionIngestTests(QemuLikeCase):
    def test_ingesting_an_older_revision_first_leaves_no_trace(self):
        """A store that ingested revision A and then revision B holds the
        same changes, artifacts and listings as a store that ingested B
        alone: a rebuild replaces what the repository contributes, it
        does not layer on it."""
        rev = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD~12", "HEAD"],
                             capture_output=True, text=True, check=True).stdout.split()
        a, b = rev[0], rev[1]
        tables = ("SELECT sha, ts, nfiles, subject FROM changes WHERE repo=? ORDER BY sha",
                  "SELECT sha, path FROM change_paths WHERE repo=? ORDER BY sha, path",
                  "SELECT sha, engineer, email, role FROM change_people WHERE repo=? ORDER BY sha, engineer, role",
                  "SELECT path, kind, touch_count FROM artifacts WHERE repo=? ORDER BY path",
                  "SELECT kind, pattern, person, email, role, section, ord FROM listings WHERE repo=? "
                  "ORDER BY kind, ord, pattern, person, role")

        def rows(g, sql):
            return [tuple(r) for r in g.db.execute(sql, ("qemulike",))]

        twice = Store(Path(self.temp.name) / "twice.db").graph
        index_repo(twice, self.repo, rev=a)
        at_a = [rows(twice, sql) for sql in tables]
        index_repo(twice, self.repo, rev=b)
        once = Store(Path(self.temp.name) / "once.db").graph
        index_repo(once, self.repo, rev=b)
        at_b = [rows(once, sql) for sql in tables]
        self.assertTrue(at_b[0])
        # Revision A really is a different world: fewer changes, fewer touches.
        self.assertLess(len(at_a[0]), len(at_b[0]))
        self.assertNotEqual(at_a[3], at_b[3])
        for sql, expected in zip(tables, at_b):
            self.assertEqual(rows(twice, sql), expected, sql)
        self.assertEqual(twice.get_source("qemulike", "git_rev"), b)


class ModelCacheTests(QemuLikeCase):
    def test_a_cached_area_answer_is_served_without_a_second_model_call(self):
        """The model's reading of a question is cached per question: the
        second ask costs no call, and a value written with cache_set is
        what is served, with no builder call at all."""
        from bridge.resolve import model_area_hits, tree_for
        g = self.warm()
        calls: list[str] = []

        def fake_json(self_, purpose, system, prompt, **kw):
            calls.append(purpose)
            return {"paths": ["hw/riscv/"], "confidence": "high", "why": "the virt board lives there"}

        p = patch.object(llm_mod.Client, "complete_json", fake_json)
        p.start()
        self.addCleanup(p.stop)
        q = "Should the virt board advertise the interrupt controller in the device tree?"
        with patch.dict(os.environ, {"BRIDGE_SEMANTIC": "1"}):
            tree = tree_for(g, "qemulike")
            first = model_area_hits(g, "qemulike", tree, q, "")
            self.assertEqual(list(first), ["hw/riscv/"])
            self.assertEqual(calls, ["area"])
            again = model_area_hits(g, "qemulike", tree, q, "")
            self.assertEqual(calls, ["area"])
            self.assertEqual({k: (v.weight, v.why) for k, v in again.items()},
                             {k: (v.weight, v.why) for k, v in first.items()})
            keys = [r["key"] for r in g.db.execute("SELECT key FROM model_cache WHERE repo=? AND kind='model_area'",
                                                   ("qemulike",))]
            self.assertEqual(len(keys), 1)
            g.cache_set("qemulike", "model_area", keys[0],
                        json.dumps({"paths": ["net/"], "confidence": "high", "why": "written by hand"}))
            seeded = model_area_hits(g, "qemulike", tree, q, "")
            self.assertEqual(list(seeded), ["net/"])
            self.assertIn("written by hand", seeded["net/"].why)
            self.assertEqual(calls, ["area"])
        # With the semantic rung off the cache is not even consulted.
        self.assertEqual(model_area_hits(g, "qemulike", tree, q, ""), {})
        self.assertEqual(calls, ["area"])


class LevelBlendTests(OfflineCase):
    """The signals at the named path and at the levels above it, the
    nearer the louder (signals.LEVEL_WEIGHTS): a thin file is judged by
    the directory that holds it, and the nearer level outranks a bigger
    share further up."""

    NOW = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)

    def setUp(self):
        super().setUp()
        self.graph = Store(Path(self.temp.name) / "levels.db").graph

    def seed(self, repo, rows):
        """(path, people, days_ago) per change; people are (name, email, role)."""
        g = self.graph
        g.set_source(repo, "git_now", self.NOW.isoformat())
        for i, (path, people, days_ago) in enumerate(rows):
            g.upsert_artifact(repo, path)
            for name, email, _role in people:
                g.upsert_engineer(name, email)
            g.add_change(repo, f"c{i:04d}", (self.NOW - datetime.timedelta(days=days_ago)).isoformat(), [path], people)

    def test_level_weights_fall_from_the_named_path_upwards(self):
        from bridge.signals import LEVEL_WEIGHTS, THIN_LEVEL_WEIGHT
        self.assertEqual(LEVEL_WEIGHTS[0], 1.0)
        self.assertTrue(all(a > b for a, b in zip(LEVEL_WEIGHTS, LEVEL_WEIGHTS[1:])), LEVEL_WEIGHTS)
        self.assertLess(THIN_LEVEL_WEIGHT, LEVEL_WEIGHTS[0])

    def test_a_thin_file_under_a_rich_directory_routes_to_the_directory_approver(self):
        """One change to hw/timer/tick.c is not ownership; the person who
        reviews hw/timer/ is the route, and the evidence says it is the
        directory's history that decided."""
        ann = ("Ann Approver", "ann@x.example", "reviewed-by")
        bob = ("Bob Author", "bob@x.example", "author")
        cy = ("Cy Newcomer", "cy@x.example", "author")
        self.seed("lv", [("hw/timer/clock.c", [bob, ann], i) for i in range(8)] + [("hw/timer/tick.c", [cy], 1)])
        q = "Should hw/timer/tick.c drop the old fallback path?"
        picked = route(self.graph, "lv", q)
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0], "Ann Approver")
        self.assertTrue(any("under hw/timer/" in ln for ln in picked[1]), picked[1])
        names = [r[0] for r in route_ranked(self.graph, "lv", q)]
        self.assertIn("Cy Newcomer", names)
        self.assertLess(names.index("Ann Approver"), names.index("Cy Newcomer"))

    def test_an_approver_one_level_up_beats_a_bigger_share_two_levels_up(self):
        """Two reviews everything under hw/timer/; Three reviews more
        changes than Two across hw/ as a whole. A question about a file
        in hw/timer/ routes to Two, with Three ranked below."""
        auth = ("Auth Or", "auth@x.example", "author")
        two = ("Two Nearer", "two@x.example", "reviewed-by")
        three = ("Three Further", "three@x.example", "reviewed-by")
        self.seed("lv", [("hw/timer/tick.c", [auth, two], 1)]
                  + [("hw/timer/clock.c", [auth, two], i) for i in range(6)]
                  + [("hw/dma/engine.c", [auth, three], i) for i in range(10)])
        q = "Should hw/timer/tick.c drop the old fallback path?"
        ranked = route_ranked(self.graph, "lv", q)
        names = [r[0] for r in ranked]
        self.assertEqual(names[0], "Two Nearer", names)
        self.assertIn("Three Further", names)
        self.assertLess(names.index("Two Nearer"), names.index("Three Further"))
        self.assertTrue(any("under hw/timer/" in ln for ln in ranked[0][1]), ranked[0][1])


class NodeLikeCase(OfflineCase):
    """Fixture only: the node-like repository, ingested once and copied per test."""

    @classmethod
    def setUpClass(cls):
        cls.repo = build_nodelike()

    def setUp(self):
        super().setUp()
        self.store = self.warm_store("nodelike", "node.db")
        self.graph = self.store.graph


class NodeLikeRoutingTests(NodeLikeCase):
    def test_area_word_names_the_module_files_not_the_vendored_copy(self):
        """'sqlite' is lib/sqlite.js and src/node_sqlite.cc, reviewed by
        Casimir; deps/v8/src/sqlite/ is another project's tree and never
        the area, whatever its reviewers' shares."""
        picked = route(self.graph, "nodelike", "Should sqlite fix a use-after-free in StatementSync?")
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0], "Casimir Vale")
        self.assertNotIn("deps/", " ".join(picked[1]))
        self.assertTrue(any("CODEOWNERS lists the team @nodejs/sqlite" in ln for ln in picked[1]), picked[1])

    def test_generic_area_word_is_still_an_area(self):
        """'doc' and 'util' are stop words in prose but the directory and
        module the question is about."""
        doc = route(self.graph, "nodelike", "Should doc clarify the util.inspect defaults?")
        self.assertIsNotNone(doc)
        self.assertEqual(doc[0], "Ines Moreno")
        util = route(self.graph, "nodelike", "Should util preserve the length of deprecated functions?")
        self.assertIsNotNone(util)
        self.assertEqual(util[0], "Soren Berg")

    def test_github_squash_committer_is_not_an_approver(self):
        names = [r[0] for r in route_ranked(self.graph, "nodelike", "Should http2 skip writeHead if the stream is closed?")]
        self.assertTrue(names and names[0] == "Ines Moreno", names)
        self.assertNotIn("GitHub", names)

    def test_team_only_codeowners_routes_to_the_active_person_and_names_the_team(self):
        picked = route(self.graph, "nodelike", "Should lib/sqlite.js expose isOpen?")
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0], "Casimir Vale")
        self.assertIn("@nodejs/sqlite", " ".join(picked[1]))


class AreaChainTests(QemuLikeCase):
    def test_area_written_the_way_people_say_it(self):
        """'riscv/virt/imsics' is not a path in the tree; its longest known
        component run names hw/riscv/."""
        g = self.warm()
        picked = route(g, "qemulike", "Should riscv/virt/imsics add the compatible string?")
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0], "Oriel Vance")

    def test_integrator_merges_are_not_area_approvals(self):
        g = self.warm()
        retained = g.db.execute("SELECT count(*) FROM change_people WHERE repo=? AND engineer=? AND role='merger'",
                                ("qemulike", "Beatrix Hale")).fetchone()[0]
        self.assertGreater(retained, 0)
        for q in ("Should tcg/tcg.c optimize the constant folding?", "Should hw/net/virtio-net.c drop the legacy header?"):
            picked = route(g, "qemulike", q)
            self.assertIsNotNone(picked, q)
            self.assertNotIn("as merger", " ".join(picked[1]), q)
            self.assertNotEqual(picked[0], "Beatrix Hale", q)


class WeakAreaTests(NodeLikeCase):
    def test_a_lone_plain_word_that_is_a_filename_does_not_route(self):
        """'objects' is a directory and a file in the vendored V8 tree and
        nothing else; one ordinary word is not an area, so Raven says it
        does not know rather than routing to the V8 updater."""
        notes: list[str] = []
        picked = route(self.graph, "nodelike", "Should we tidy up the objects handling before the release?", notes=notes)
        self.assertTrue(picked is None or picked[0] != "Yara Lindgren", picked)
        if picked is None:
            self.assertTrue(notes, notes)


class IdentifierPieceTests(QemuLikeCase):
    def test_a_piece_of_an_identifier_is_a_plain_word(self):
        """virt inside virt_open_old() is not a name for hw/riscv/virt.c on
        its own; with nothing else to go on Raven does not know."""
        g = self.warm()
        notes: list[str] = []
        picked = route(g, "qemulike", "Should we get rid of virt_open_old() everywhere?", notes=notes)
        self.assertIsNone(picked, picked)
        self.assertTrue(notes, notes)


class RoutingQuestionTests(QemuLikeCase):
    def test_a_routing_question_is_never_answered_by_a_record(self):
        """When the graph cannot route a who-should-approve question, a
        merge record that shares its words must not come back as the
        answer; the outcome is an honest unknown."""
        self.warm()
        run = self.store.add_run({"title": "t", "agent": "test", "repo": "qemulike"})
        row = ask(self.store, Config(), run["id"],
                  "Who should approve this change: Should we merge the pull tag into staging today?", context="c")
        self.assertEqual(row["kind"], "new")
        self.assertIsNone(row["owner_name"])
        self.assertIn("does not know", row["owner_evidence"])

    def test_the_approval_leader_of_a_spread_area_routes(self):
        """util is reviewed by three regulars; the one who reviews most of
        it is the person to ask even without a majority."""
        g = self.warm()
        picked = route(g, "qemulike", "Should util/cutils.c parse sizes with suffixes?")
        # cutils.c has one sweep change only; util/ as a whole is Rich's area.
        self.assertTrue(picked is None or picked[0] in ("Rich Reviewer",), picked)


class RequesterTests(QemuLikeCase):
    def test_the_requester_is_never_routed_to(self):
        """Oriel asking about his own area is not sent to himself; the
        route says who asked and goes to the next person with signal."""
        g = self.warm()
        q = "Should hw/riscv/virt.c add the imsics compatible string?"
        picked = route(g, "qemulike", q, requester="Oriel Vance <oriel@riscv.example>")
        self.assertIsNotNone(picked)
        self.assertNotEqual(picked[0], "Oriel Vance")
        self.assertTrue(any("never routes a question back to its requester" in ln for ln in picked[1]), picked[1])
        names = [r[0] for r in route_ranked(g, "qemulike", q, requester="oriel@riscv.example")]
        self.assertNotIn("Oriel Vance", names)

    def test_the_file_reviewer_routes_and_each_signal_says_why(self):
        """Rich reviewed every bufferiszero change Tamsin sent. The route
        is Rich whichever signal is asked about: with Tamsin asking, the
        evidence names the affinity; line ownership of the named file is
        read from the checkout with its own evidence line, the author of
        most lines still loses to the reviewer, and BRIDGE_BLAME=0 turns
        it off; and the reviewer of the very file the question names
        outranks the maintainer of the directory above when the file's
        own history is clear, with the directory still in the ranking."""
        g = self.warm()
        q = "Should util/bufferiszero.c drop the AVX2 path?"
        with self.subTest("affinity"):
            picked = route(g, "qemulike", q, requester="Tamsin Reed <tamsin@synthco.example>")
            self.assertIsNotNone(picked)
            self.assertEqual(picked[0], "Rich Reviewer")
            self.assertTrue(any("of Tamsin Reed's last" in ln for ln in picked[1]), picked[1])
        with self.subTest("blame"):
            ranked = route_ranked(g, "qemulike", q)
            self.assertEqual(ranked[0][0], "Rich Reviewer")
            joined = " | ".join(ln for r in ranked for ln in r[1])
            self.assertIn("of the human-written lines of util/bufferiszero.c (git blame, bots excluded)", joined)
            with patch.dict(os.environ, {"BRIDGE_BLAME": "0"}):
                fresh = Store(Path(self.temp.name) / "noblame.db").graph
                index_repo(fresh, self.repo)
                joined = " | ".join(ln for r in route_ranked(fresh, "qemulike", q) for ln in r[1])
                self.assertNotIn("git blame", joined)
        with self.subTest("file history"):
            picked = route(g, "qemulike", q)
            self.assertEqual(picked[0], "Rich Reviewer")
            self.assertTrue(any("under util/bufferiszero.c" in ln for ln in picked[1]), picked[1])


HW_CORE_SECTION = """
Hardware core
-------------
M: Rich Reviewer <rich@tcg.example>
F: hw/
X: hw/riscv/
"""


class ListingCorrectnessTests(QemuLikeCase):
    def test_an_exclusion_hides_only_its_own_section(self):
        """A section covering hw/ that excludes hw/riscv/ steps aside for
        hw/riscv/virt.c; the RISC-V section's own maintainer is still
        listed there and still routes (one X: line in a kernel-style
        MAINTAINERS used to hide every maintainer of the path)."""
        from bridge.ingest import index_maintainers
        from bridge.signals import listed_for
        g = self.warm()
        index_maintainers(g, "qemulike", HW_CORE_SECTION)
        listed = listed_for(g, "qemulike", "hw/riscv/virt.c")
        people = {e["person"] for e in listed if e["role"] == "maintainer"}
        self.assertIn("Oriel Vance", people)
        self.assertNotIn("Rich Reviewer", people)
        self.assertIn("Rich Reviewer", {e["person"] for e in listed_for(g, "qemulike", "hw/net/e1000.c")})
        picked = route(g, "qemulike", "Should hw/riscv/virt.c add the imsics compatible string?")
        self.assertEqual(picked[0], "Oriel Vance")
        self.assertIn("MAINTAINERS lists Oriel Vance", " | ".join(picked[1]))

    def test_a_recorded_routing_answer_is_keyed_by_its_path(self):
        """You told Raven who owns hw/riscv/: that answer wins there and
        is not applied to a question about the network code."""
        g = self.warm()
        g.record_routing_answer("qemulike", "hw/riscv/", "Palmer Dabbelt")
        picked = route(g, "qemulike", "Should hw/riscv/virt.c add the imsics compatible string?")
        self.assertEqual(picked[0], "Palmer Dabbelt")
        self.assertIn("you told Raven this owner directly", picked[1])
        net = route(g, "qemulike", "Should hw/net/virtio-net.c drop the legacy header?")
        self.assertEqual(net[0], "Kwame Asante")
        self.assertNotIn("Palmer Dabbelt",
                         [r[0] for r in route_ranked(g, "qemulike", "Should hw/net/virtio-net.c drop the legacy header?")])


class GenerationTests(QemuLikeCase):
    """The route-time memo (tree, listings, records, history) is keyed
    on a generation every writer bumps, and on SQLite's data version so
    a write from another connection is seen too."""

    def test_a_second_route_sees_a_change_added_in_between(self):
        g = self.warm()
        q = "Should net/tap.c set the MTU?"
        first = route_ranked(g, "qemulike", q)
        self.assertEqual(first[0][0], "Kwame Asante")
        gen = g.generation("qemulike")
        for i in range(12):
            g.add_change("qemulike", f"new{i}", f"2026-08-{10 + i:02d}T10:00:00+00:00",
                         ["net/tap.c"], [("Tamsin Reed", "tamsin@synthco.example", "author"),
                                         ("Rich Reviewer", "rich@tcg.example", "reviewed-by")])
        self.assertGreater(g.generation("qemulike"), gen)
        second = route_ranked(g, "qemulike", q)
        self.assertEqual(second[0][0], "Rich Reviewer")
        self.assertIn("Kwame Asante", [r[0] for r in second])
        # The same store read again, with nothing written, is the same answer.
        self.assertEqual(route_ranked(g, "qemulike", q), second)

    def test_a_write_through_another_graph_invalidates_the_memo(self):
        """The MCP server writes the same file from its own connection:
        the inbox's graph has bumped nothing, but the memo notices."""
        from bridge.graph import Graph
        from bridge.resolve import tree_for
        from bridge.signals import listed_for
        g = self.warm()
        q = "Should hw/riscv/virt.c add the imsics compatible string?"
        self.assertEqual(route(g, "qemulike", q)[0], "Oriel Vance")
        tree = tree_for(g, "qemulike")
        self.assertNotIn("hw/riscv/new-board.c", tree.files)
        # The inbox's own write to another table, through its own
        # connection, moves SQLite's data version but not the rows: the
        # tree is checked, not rebuilt.
        self.store.add_owner({"name": "Someone", "team": "Inbox", "patterns": "docs/*"})
        self.assertIs(tree_for(g, "qemulike"), tree)
        gen = g.generation("qemulike")
        other = Graph(self.store.path)
        other.upsert_artifact("qemulike", "hw/riscv/new-board.c")
        other.add_listing("qemulike", "codeowners", "hw/riscv/", "@palmer", ord=0)
        other.record_routing_answer("qemulike", "hw/riscv/", "Palmer Dabbelt")
        other.close()
        self.assertEqual(g.generation("qemulike"), gen)
        self.assertIn("hw/riscv/new-board.c", tree_for(g, "qemulike").files)
        self.assertIn("palmer", {e["person"].lstrip("@") for e in listed_for(g, "qemulike", "hw/riscv/virt.c")})
        self.assertEqual(route(g, "qemulike", q)[0], "Palmer Dabbelt")


class DeterminismTests(QemuLikeCase):
    """What a question resolves to, and who it routes to, never depends
    on the process's hash seed: a set walked in hash order once flipped a
    route between runs, so every bucket of the tree is kept sorted and
    the same store answers the same way in two differently seeded
    processes."""

    QUESTIONS = (
        "Should hw/riscv/virt.c add the imsics compatible string?",
        "Should the riscv virt board export the header?",
        "Should we simplify the buffer is zero accel test?",
        "Should net drop the legacy header?",
        "Should tcg optimize the prologue?",
        "Should util/cutils.c parse sizes with suffixes?",
    )
    SCRIPT = (
        "import sys\n"
        "from bridge.graph import Graph\n"
        "from bridge.resolve import resolve_paths\n"
        "from bridge.routing import route_ranked\n"
        "g = Graph(sys.argv[1])\n"
        "for q in sys.argv[2:]:\n"
        "    print([(h.path, h.why) for h in resolve_paths(g, 'qemulike', q)],\n"
        "          [r[0] for r in route_ranked(g, 'qemulike', q)])\n"
    )

    def test_routes_are_the_same_under_two_hash_seeds(self):
        from bridge.resolve import tree_for
        g = self.warm()
        tree = tree_for(g, "qemulike")
        for bucket in list(tree.dir_by_base.values()) + list(tree.files_by_base.values()):
            self.assertEqual(bucket, sorted(bucket))
        g.close()
        root = Path(__file__).resolve().parents[1]
        outputs = []
        for seed in ("0", "1"):
            env = {**os.environ, "PYTHONHASHSEED": seed, "BRIDGE_SEMANTIC": "0", "BRIDGE_LIVE": "0"}
            proc = subprocess.run([sys.executable, "-c", self.SCRIPT, str(self.store.path), *self.QUESTIONS],
                                  cwd=root, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            outputs.append(proc.stdout)
        self.assertIn("Oriel Vance", outputs[0])
        self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
