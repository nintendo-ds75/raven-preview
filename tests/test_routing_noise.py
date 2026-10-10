"""Routing ignores two kinds of noise found in a replay of pypa/packaging:
a question word that happens to be a licence file's stem, and commits that
only ran a linter or spelling tool over the code."""

from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase

from bridge.resolve import ROOT_WORD_WHEN_ANCHORED, resolve_paths
from bridge.routing import route
from bridge.signals import _MECHANICAL
from bridge.store import Store

NOW = "2026-06-20T00:00:00+00:00"
CODE = "src/pkg/metadata.py"


class RoutingNoiseTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "noise.db")
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.graph.set_source("app", "git_now", NOW)
        for path in ("LICENSE.BSD", "Makefile", CODE, "src/pkg/other.py"):
            self.graph.upsert_artifact("app", path)

    def change(self, sha, path, author, subject="", ts=NOW):
        self.graph.add_change("app", sha, ts, [path], [(author, "", "author")], subject=subject)

    def test_a_licence_file_named_by_a_word_is_not_the_area_once_the_agent_named_code(self):
        for i in range(3):
            self.change(f"lic-{i}", "LICENSE.BSD", "Licence Author", "Relicense")
        for i in range(6):
            self.change(f"meta-{i}", CODE, "Metadata Author", f"Support metadata field {i}")
        question = "Should License-Expression reject unknown SPDX identifiers?"
        hits = resolve_paths(self.graph, "app", question, path=CODE)
        self.assertEqual([h.path for h in hits], [CODE])
        self.assertEqual(route(self.graph, "app", question, path=CODE)[0], "Metadata Author")

    def test_without_a_path_the_licence_file_still_counts(self):
        self.change("lic-0", "LICENSE.BSD", "Licence Author", "Relicense")
        hits = resolve_paths(self.graph, "app", "Should we change the BSD license text?")
        self.assertIn("LICENSE.BSD", [h.path for h in hits])

    def test_other_root_files_named_by_a_word_become_secondary(self):
        self.change("make-0", "Makefile", "Build Author", "Add install target")
        self.change("meta-0", CODE, "Metadata Author", "Support metadata 2.3")
        hits = {h.path: h for h in resolve_paths(self.graph, "app", "Should the Makefile run the metadata checks?",
                                                 path=CODE)}
        self.assertEqual(hits[CODE].weight, 1.0)
        self.assertAlmostEqual(hits["Makefile"].weight, 0.85 * ROOT_WORD_WHEN_ANCHORED)
        self.assertIn("secondary to the path the agent named", hits["Makefile"].why)

    def seed_sweeps(self, subjects):
        for i in range(3):
            self.change(f"design-{i}", CODE, "Module Designer", f"Parse enriched metadata, part {i}")
        for i, subject in enumerate(subjects):
            self.change(f"sweep-{i}", CODE, "Tooling Sweeper", subject)

    def test_tooling_sweeps_do_not_outrank_the_people_who_wrote_the_code(self):
        self.seed_sweeps(["Apply ruff rules (RUF)", "Fix typos found by codespell", "pyupgrade/black/isort/flake8 -> ruff",
                          "Modernise type annotations using FA rules from ruff", "pre-commit autoupdate"])
        picked = route(self.graph, "app", "Who should decide how metadata validates licences?", path=CODE)
        self.assertEqual(picked[0], "Module Designer")

    def test_the_same_history_without_tooling_subjects_favours_the_frequent_author(self):
        # Control: five ordinary changes do outrank three, so the test above
        # is measuring the subject, not the counts.
        self.seed_sweeps([f"Support new metadata field {i}" for i in range(5)])
        picked = route(self.graph, "app", "Who should decide how metadata validates licences?", path=CODE)
        self.assertEqual(picked[0], "Tooling Sweeper")

    def test_the_tooling_pattern_leaves_ordinary_subjects_alone(self):
        for subject in ("Apply ruff rules (RUF)", "Fix typo in Version __str__", "Update our linters",
                        "Modernise type annotations using FA rules from ruff", "Run pre-commit autoupdate"):
            self.assertTrue(_MECHANICAL.search(subject), subject)
        for subject in ("PEP 639: Implement License-Expression and License-File", "Support enriched metadata",
                        "Add a typed Metadata class", "Parse raw metadata", "Format version strings consistently"):
            self.assertFalse(_MECHANICAL.search(subject), subject)

    def test_ingested_changes_keep_their_subject(self):
        self.change("abc", CODE, "Metadata Author", "Support metadata 2.3")
        row = self.graph.db.execute("SELECT subject FROM changes WHERE repo='app' AND sha='abc'").fetchone()
        self.assertEqual(row["subject"], "Support metadata 2.3")


class DominantBlameTests(OfflineCase):
    """Whoever wrote most of a file's surviving lines stays a candidate after
    their commits age out of the recent window."""

    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "blame.db")
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.graph.set_source("app", "git_now", NOW)
        self.graph.upsert_artifact("app", CODE)

    def test_the_builder_of_a_file_clears_the_floor_after_a_quiet_year(self):
        old = "2025-05-01T00:00:00+00:00"
        for i in range(2):
            self.graph.add_change("app", f"build-{i}", old, [CODE], [("Module Builder", "", "author")],
                                  subject=f"Parse metadata, part {i}")
        for i in range(3):
            self.graph.add_change("app", f"recent-{i}", NOW, [CODE], [(f"Passer By {i}", "", "author")],
                                  subject=f"Small fix {i}")
        # Still active elsewhere in the repository, so not a departed author.
        self.graph.upsert_artifact("app", "docs/index.rst")
        self.graph.add_change("app", "builder-docs", NOW, ["docs/index.rst"], [("Module Builder", "", "author")],
                              subject="Document metadata")
        lines = {"Module Builder": 700, "Passer By 0": 30, "Passer By 1": 30, "Passer By 2": 40}
        blame = {CODE: {name: (name, "", n / 800) for name, n in lines.items()}}
        with patch("bridge.ingest.fetch_blame", return_value=blame):
            picked = route(self.graph, "app", "How should metadata validation treat licence fields?", path=CODE)
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0], "Module Builder")
