"""The startup banner says what is on. Measured live: a fresh install
with nothing connected printed "(GitHub sync on)" because a token sat in
the environment and nothing was syncing."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import OfflineCase  # noqa: E402

from bridge.__main__ import sync_banner  # noqa: E402
from bridge.github import register  # noqa: E402
from bridge.store import Store  # noqa: E402

REPO = "acme/platform"


class BannerTests(OfflineCase):
    def test_sync_is_on_only_for_a_registered_repository_that_can_be_fetched(self):
        graph = Store(Path(self.temp.name) / "banner.db").graph
        self.assertEqual(sync_banner(graph, set(), True), "")
        self.assertEqual(sync_banner(graph, set(), False), "")
        with graph.transaction():
            register(graph, REPO)
        self.assertEqual(sync_banner(graph, set(), False), "")
        self.assertEqual(sync_banner(graph, set(), True), f" (GitHub sync on for {REPO})")
        self.assertEqual(sync_banner(graph, {REPO}, False), f" (GitHub sync on for {REPO})")
        with graph.transaction():
            for i in range(4):
                register(graph, f"acme/repo{i}")
        self.assertEqual(sync_banner(graph, set(), True), f" (GitHub sync on for {REPO}, acme/repo0, acme/repo1 and 2 more)")


if __name__ == "__main__":
    unittest.main()
