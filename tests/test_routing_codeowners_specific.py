"""CODEOWNERS names one owner per file, the last matching rule: a rule for
a file overrides the rule for its directory. Measured on a two-file node
(a retry policy whose own rule named its owner, and the client under the
directory rule): the directory owner's second path outscored the file's
owner, whom the file alone had ranked first."""

from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase

from bridge.ingest import parse_codeowners_text
from bridge.routing import route_ranked
from bridge.signals import listed_for
from bridge.store import Store

NOW = "2026-08-10T00:00:00+00:00"
RETRY = "fetchkit/retry.py"
CLIENT = "fetchkit/client.py"
LIOR = ("Lior Bensimon", "lior@example.test")
DANA = ("Dana Okafor", "dana@example.test")
CODEOWNERS = """# Who decides what in fetchkit.
/fetchkit/          @lior-bensimon
/fetchkit/retry.py  @dana-okafor
/tests/             @lior-bensimon
"""
QUESTION = "Should MAX_RETRIES be raised from 3 to 5?"


class SpecificCodeownerTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "owners.db")
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.graph.set_source("app", "git_now", NOW)
        for path in ("README.md", "fetchkit/__init__.py", CLIENT, RETRY, "tests/test_retry.py", ".github/CODEOWNERS"):
            self.graph.upsert_artifact("app", path)
        for i, (pattern, owners) in enumerate(parse_codeowners_text(CODEOWNERS)):
            for owner in owners:
                self.graph.add_listing("app", "codeowners", pattern, owner, ord=i)
        # Lior builds the client and the retry policy; Dana sets the limit as
        # a product call; Lior nudges the backoff afterwards.
        self.change("c1", "2026-06-02T10:00:00+00:00", ["README.md", "fetchkit/__init__.py", CLIENT, ".github/CODEOWNERS"],
                    LIOR, "feat: add the fetch client")
        self.change("c2", "2026-06-09T11:30:00+00:00", [RETRY], LIOR, "feat: retry transient failures")
        self.change("c3", "2026-06-10T09:15:00+00:00", ["tests/test_retry.py"], LIOR, "test: cover the retry limit")
        self.change("c4", "2026-07-14T15:45:00+00:00", [RETRY], DANA, "fix: keep the retry limit at three")
        self.change("c5", "2026-08-03T13:00:00+00:00", [RETRY], LIOR, "fix: nudge backoff to 250ms")
        self.blame = {RETRY: {LIOR[0]: (*LIOR, 21 / 22), DANA[0]: (*DANA, 1 / 22)},
                      CLIENT: {LIOR[0]: (*LIOR, 1.0)}}

    def change(self, sha, ts, paths, who, subject):
        self.graph.add_change("app", sha, ts, paths, [(who[0], who[1], "author")], subject=subject)

    def ranked(self, **kw):
        with patch("bridge.ingest.fetch_blame", return_value=self.blame):
            return route_ranked(self.graph, "app", QUESTION, **kw)

    def test_the_last_matching_rule_owns_the_file(self):
        # GitHub's rule: later patterns take precedence, so the file's own
        # rule names its only owner and the directory rule covers the rest.
        self.assertEqual([e["person"] for e in listed_for(self.graph, "app", RETRY)], ["dana-okafor"])
        self.assertEqual([e["person"] for e in listed_for(self.graph, "app", CLIENT)], ["lior-bensimon"])
        self.assertEqual([e["person"] for e in listed_for(self.graph, "app", "fetchkit/")], ["lior-bensimon"])

    def test_the_file_owner_outranks_the_directory_owner_on_the_file_alone(self):
        # Dana's one product-decision commit and the file's rule, against
        # Lior's two commits and 95% of the lines: the rule names Dana.
        ranked = self.ranked(path=RETRY)
        self.assertEqual([name for name, _ev, _s in ranked][:2], [DANA[0], LIOR[0]])
        self.assertTrue(any(ln.startswith("CODEOWNERS lists Dana Okafor for /fetchkit/retry.py") for ln in ranked[0][1]))
        self.assertFalse(any("CODEOWNERS lists" in ln for ln in ranked[1][1]), ranked[1][1])

    def test_a_secondary_path_does_not_let_the_directory_owner_overtake(self):
        ranked = self.ranked(path=RETRY, also_paths=[CLIENT])
        self.assertEqual([name for name, _ev, _s in ranked][:2], [DANA[0], LIOR[0]])
        note = next((ln for ln in ranked[0][1] if ln.startswith("note: CODEOWNERS names Dana Okafor for fetchkit/retry.py")), "")
        self.assertIn("Lior Bensimon leads on the other paths it touches", note)

    def test_the_other_path_first_routes_to_its_own_owner(self):
        # The same two files the other way round: the client is the path
        # this change is about, and its rule names Lior.
        ranked = self.ranked(path=CLIENT, also_paths=[RETRY])
        self.assertEqual(ranked[0][0], LIOR[0])

    def test_a_listed_owner_without_signal_on_the_file_is_not_promoted(self):
        # Dana never touched the file: the rule alone does not beat the
        # person who wrote and keeps changing it, with or without the
        # second path.
        self.graph.db.execute("DELETE FROM change_people WHERE repo='app' AND sha='c4'")
        self.graph.db.execute("DELETE FROM change_paths WHERE repo='app' AND sha='c4'")
        self.graph.db.execute("DELETE FROM changes WHERE repo='app' AND sha='c4'")
        self.graph._bump("app")
        self.blame[RETRY] = {LIOR[0]: (*LIOR, 1.0)}
        self.assertEqual(self.ranked(path=RETRY)[0][0], LIOR[0])
        self.assertEqual(self.ranked(path=RETRY, also_paths=[CLIENT])[0][0], LIOR[0])

    def test_kickoff_discovery_and_node_routing_name_the_same_first_contact(self):
        from bridge.canvas import discover
        from bridge.config import load
        with patch("bridge.ingest.fetch_blame", return_value=self.blame):
            found = discover(self.store, load(), "app", "Raise the retry limit",
                             "Raise MAX_RETRIES from 3 to 5 in the fetch client", [RETRY, CLIENT], "")
            node = route_ranked(self.graph, "app", QUESTION, path=RETRY, also_paths=[CLIENT])
        self.assertEqual(found["people"][0]["name"], node[0][0])
