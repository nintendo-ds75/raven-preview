"""Failure classes from the qemu routing replay (bench/routing): a release
manager's merges counted as approval of every file a pull carried."""

from pathlib import Path

from fixtures import OfflineCase

from bridge.routing import route
from bridge.signals import MERGE_BASE_WEIGHT, _scope_signals
from bridge.store import Store

NOW = "2026-06-20T00:00:00+00:00"


class GatekeeperMergeTests(OfflineCase):
    """Measured on qemu: the release manager merges half of the repository's
    pulls into nearly every area, so in a file with seven changes three
    merges were 93% of its approval, over the maintainer who committed the
    patches; under docs/ ninety-eight merges outranked the committer because
    the release manager also wrote a few pages there."""

    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "gatekeeper.db")
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.graph.set_source("app", "git_now", NOW)
        for area in ("alpha", "beta", "gamma"):
            self.graph.upsert_artifact("app", f"{area}/code.py")

    def change(self, sha, area, people):
        self.graph.add_change("app", sha, NOW, [f"{area}/code.py"], [(name, "", role) for name, role in people])

    def seed(self):
        # The release manager merges every area's pulls; the beta
        # maintainer merges beta's own.
        for area in ("alpha", "gamma"):
            for i in range(6):
                self.change(f"merge-{area}-{i}", area, [("Release Manager", "merger")])
        for i in range(6):
            self.change(f"merge-beta-{i}", "beta", [("Beta Maintainer", "merger")])
        # alpha's own history: the area maintainer commits a contributor's
        # patches; the release manager wrote one patch there once.
        for i in range(8):
            self.change(f"alpha-{i}", "alpha", [("Contributor", "author"), ("Area Maintainer", "committer")])
        self.change("alpha-rm", "alpha", [("Release Manager", "author")])

    def test_a_merger_of_every_area_gets_little_credit_from_a_small_scopes_merges(self):
        self.seed()
        alpha = _scope_signals(self.graph, "app", "alpha/", 0)
        self.assertLess(alpha["merge_weights"]["release manager"], 0.7)
        self.assertGreater(alpha["merge_weights"]["release manager"], MERGE_BASE_WEIGHT - 1e-9)
        picked = route(self.graph, "app", "Should alpha retry on timeout?", path="alpha/code.py")
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0], "Area Maintainer")

    def test_a_merger_of_one_area_keeps_full_weight_there(self):
        self.seed()
        beta = _scope_signals(self.graph, "app", "beta/", 0)
        self.assertAlmostEqual(beta["merge_weights"]["beta maintainer"], 1.0)
        picked = route(self.graph, "app", "Should beta retry on timeout?", path="beta/code.py")
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0], "Beta Maintainer")

    def test_substantial_independent_work_restores_a_gatekeepers_merges(self):
        self.seed()
        for i in range(4):
            self.change(f"gamma-rm-{i}", "gamma", [("Release Manager", "author")])
        gamma = _scope_signals(self.graph, "app", "gamma/", 0)
        self.assertAlmostEqual(gamma["merge_weights"]["release manager"], 1.0)
