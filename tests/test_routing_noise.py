"""Routing ignores two kinds of noise found in a replay of pypa/packaging:
a question word that happens to be a licence file's stem, and commits that
only ran a linter or spelling tool over the code."""

from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase

from bridge.resolve import ROOT_WORD_WHEN_ANCHORED, resolve_paths
from bridge.routing import route, route_ranked
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


class NewFileAreaTests(OfflineCase):
    """An agent creating a new module names a path that does not exist yet;
    its directory anchors the area, and licence files stay out of it."""

    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "newfile.db")
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.graph.set_source("app", "git_now", NOW)
        for path in ("LICENSE", "LICENSE.BSD", CODE):
            self.graph.upsert_artifact("app", path)
        for i in range(3):
            self.graph.add_change("app", f"lic-{i}", NOW, ["LICENSE"], [("Licence Author", "", "author")],
                                  subject="Relicense")
        for i in range(6):
            self.graph.add_change("app", f"meta-{i}", NOW, [CODE], [("Metadata Author", "", "author")],
                                  subject=f"Support metadata field {i}")

    def test_a_new_module_path_keeps_licence_files_out_of_the_area(self):
        question = "Should the SPDX license list be vendored as generated data or fetched at runtime?"
        hits = resolve_paths(self.graph, "app", question, path="src/pkg/licenses")
        self.assertNotIn("LICENSE", [h.path for h in hits])
        self.assertEqual(route(self.graph, "app", question, path="src/pkg/licenses")[0], "Metadata Author")

    def test_a_question_about_the_licence_text_still_reaches_its_author(self):
        question = "Should we update the license text to add the new copyright holder?"
        hits = resolve_paths(self.graph, "app", question, path="src/pkg/licenses")
        self.assertIn("LICENSE", [h.path for h in hits])


class MaintainersSectionTests(OfflineCase):
    """A change to MAINTAINERS that names a section is decided by that
    section's people, not by whoever edits the MAINTAINERS file most."""

    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "sections.db")
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.graph.set_source("app", "git_now", NOW)
        files = ("MAINTAINERS", "hw/core/machine.c", "hw/core/machine-qmp.c", "target/ppc/kvm.c", "hw/ppc/spapr.c")
        for f in files:
            self.graph.upsert_artifact("app", f)
        rows = [("Machine core", "hw/core/machine*", "Core Maintainer", "maintainer"),
                ("PPC KVM CPUs", "target/ppc/kvm.c", "Kvm Maintainer", "maintainer"),
                ("sPAPR (pseries)", "hw/*/spapr*", "Spapr Maintainer", "maintainer")]
        with self.graph.transaction():
            for section, pattern, person, role in rows:
                self.graph.db.execute("INSERT INTO listings(repo, kind, pattern, person, email, role, section) "
                                      "VALUES('app', 'maintainers', ?, ?, '', ?, ?)", (pattern, person, role, section))
        for i in range(8):
            self.graph.add_change("app", f"m-{i}", NOW, ["MAINTAINERS"], [("File Gardener", "", "author")],
                                  subject=f"MAINTAINERS: update entry {i}")
        for i in range(3):
            self.graph.add_change("app", f"c-{i}", NOW, ["hw/core/machine.c"], [("Core Maintainer", "", "author")],
                                  subject=f"hw/core: machine change {i}")

    def test_naming_a_section_routes_to_its_maintainer(self):
        question = "Should MAINTAINERS add myself as a reviewer of machine core?"
        hits = resolve_paths(self.graph, "app", question, path="MAINTAINERS")
        self.assertIn("hw/core/machine.c", [h.path for h in hits])
        self.assertTrue(any("section 'Machine core'" in h.why for h in hits))
        self.assertEqual(route(self.graph, "app", question, path="MAINTAINERS")[0], "Core Maintainer")

    def test_the_best_matching_section_wins_and_filler_words_do_not_match(self):
        hits = resolve_paths(self.graph, "app", "Who should approve this change: Should MAINTAINERS add myself "
                                                "as a reviewer for PPC KVM and sPAPR?")
        sections = {h.why for h in hits if "MAINTAINERS section" in h.why}
        self.assertEqual(sections, {"the question changes the MAINTAINERS section 'PPC KVM CPUs'"})
        self.assertIn("target/ppc/kvm.c", [h.path for h in hits])

    def test_a_maintainers_change_naming_no_section_is_unchanged(self):
        hits = resolve_paths(self.graph, "app", "Should MAINTAINERS sort its entries alphabetically?", path="MAINTAINERS")
        self.assertEqual([h.path for h in hits], ["MAINTAINERS"])


class FirstContactTests(OfflineCase):
    """When no history gives a clear owner, the inferred first contact is a
    recent author of real work on the file, not of a tooling sweep."""

    def test_sweep_authors_are_not_inferred_first_contacts(self):
        store = Store(Path(self.temp.name) / "first.db")
        graph = store.graph
        self.addCleanup(graph.close)
        graph.set_source("app", "git_now", NOW)
        graph.upsert_artifact("app", CODE)
        records = [("pr", "10", "Feature Author", "Support Requires-External in metadata", "2026-05-01"),
                   ("pr", "11", "Lint Sweeper", "Apply ruff rules (RUF)", "2026-06-01"),
                   ("pr", "12", "Typo Fixer", "Fix typos found by codespell", "2026-06-10")]
        with graph.transaction():
            for kind, ref, author, title, created in records:
                graph.upsert_intent("app", kind, ref, title, title, author, created, paths=[CODE])
        ranked = route_ranked(graph, "app", "Should metadata validation reject unknown fields?", path=CODE,
                              requester="Someone Else")
        names = [name for name, _lines, _score in ranked]
        self.assertEqual(names[:1], ["Feature Author"])
        self.assertNotIn("Lint Sweeper", names)
        self.assertNotIn("Typo Fixer", names)

    def test_first_contacts_are_ordered_by_real_work_on_the_file(self):
        store = Store(Path(self.temp.name) / "order.db")
        graph = store.graph
        self.addCleanup(graph.close)
        graph.set_source("app", "git_now", NOW)
        graph.upsert_artifact("app", CODE)
        with graph.transaction():
            graph.upsert_intent("app", "pr", "20", "Default optional values to None", "", "Semantics Owner",
                                "2025-01-01", paths=[CODE])
            graph.upsert_intent("app", "pr", "21", "Add Python 3.14 classifiers", "", "Recent Passer", "2026-06-01",
                                paths=[CODE])
        for i in range(3):
            graph.add_change("app", f"sem-{i}", "2025-01-01T00:00:00+00:00", [CODE], [("Semantics Owner", "", "author")],
                             subject=f"Default optional values {i}")
        graph.add_change("app", "pass-0", "2025-02-01T00:00:00+00:00", [CODE], [("Recent Passer", "", "author")],
                         subject="Add classifiers")
        ranked = route_ranked(graph, "app", "Should metadata validation reject unknown fields?", path=CODE,
                              requester="Someone Else")
        inferred = [n for n, lines, _s in ranked if lines and lines[0].startswith("inferred first contact")]
        self.assertEqual(inferred[:2], ["Semantics Owner", "Recent Passer"])


class DottedModuleTests(OfflineCase):
    """A dotted module name finds its file in a src/ layout."""

    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "dotted.db")
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.graph.set_source("app", "git_now", NOW)
        for f in ("src/pkg/tags.py", "src/pkg/metadata.py", "tests/test_tags.py", "src/pkg/utils/__init__.py"):
            self.graph.upsert_artifact("app", f)

    def test_a_dotted_module_resolves_under_src(self):
        hits = resolve_paths(self.graph, "app", "Should pkg.tags add Emscripten platform tags?")
        self.assertEqual(hits[0].path, "src/pkg/tags.py")
        self.assertIn("pkg.tags", hits[0].why)

    def test_a_dotted_package_resolves_to_its_directory_under_src(self):
        hits = resolve_paths(self.graph, "app", "Should pkg.utils keep the old helper names?")
        self.assertIn("src/pkg/utils/", [h.path for h in hits])

    def test_a_named_module_stands_in_for_a_missing_path(self):
        with self.graph.transaction():
            self.graph.upsert_intent("app", "pr", "30", "Detect 32-bit platforms accurately", "", "Tags Author",
                                     "2026-01-01", paths=["src/pkg/tags.py"])
        names = [n for n, _l, _s in route_ranked(self.graph, "app", "Should pkg.tags add Emscripten platform tags?")]
        self.assertEqual(names[:1], ["Tags Author"])


class NamedFunctionTests(OfflineCase):
    """A question that names a function but no file is about the file that
    defines it, read from the pinned checkout."""

    def setUp(self):
        super().setUp()
        from fixtures import run_git
        from bridge.ingest import index_repo
        self.repo = Path(self.temp.name) / "proj"
        (self.repo / "src/pkg").mkdir(parents=True)
        (self.repo / "src/pkg/tags.py").write_text("def sys_tags():\n    return []\n\n\ndef mac_platforms():\n    return []\n")
        (self.repo / "src/pkg/version.py").write_text("class Version:\n    pass\n")
        (self.repo / "src/pkg/csrc.c").write_text("static int parse_cpu_model_name(const char *s)\n{\n    return 0;\n}\n")
        (self.repo / "src/pkg/boot.h").write_text("#define BOOT_MAX_CPUS 4\n\ntypedef struct RISCVBootInfo {\n"
                                                 "    int harts;\n} RISCVBootInfo;\n\nvoid fork_end(int pid);\n")
        run_git(self.repo, "init", "-q")
        run_git(self.repo, "add", ".")
        for author, path in (("Tags Author", "src/pkg/tags.py"), ("Version Author", "src/pkg/version.py"),
                             ("C Author", "src/pkg/csrc.c")):
            with open(self.repo / path, "a") as f:
                f.write("\n")
            run_git(self.repo, "add", path)
            run_git(self.repo, "-c", f"user.name={author}", "-c", "user.email=a@example.invalid", "commit", "-qm",
                    f"Work on {path}")
        self.store = Store(Path(self.temp.name) / "fn.db")
        self.addCleanup(self.store.graph.close)
        index_repo(self.store.graph, self.repo, repo_name="app")

    def test_a_named_python_function_resolves_to_its_file(self):
        hits = resolve_paths(self.store.graph, "app", "Should sys_tags() yield emscripten tags?")
        self.assertEqual(hits[0].path, "src/pkg/tags.py")
        self.assertIn("sys_tags", hits[0].why)

    def test_a_named_c_function_resolves_to_its_file(self):
        hits = resolve_paths(self.store.graph, "app", "Should parse_cpu_model_name() accept aliases?")
        self.assertIn("src/pkg/csrc.c", [h.path for h in hits])

    def test_types_macros_and_short_snake_names_resolve(self):
        for question in ("Should we add a new struct RISCVBootInfo?", "Should BOOT_MAX_CPUS be raised to 8?",
                         "Should we pass pid to fork_end?"):
            self.assertIn("src/pkg/boot.h", [h.path for h in resolve_paths(self.store.graph, "app", question)], question)

    def test_an_unknown_name_adds_nothing(self):
        hits = resolve_paths(self.store.graph, "app", "Should frobnicate_widget_now() be removed?")
        self.assertFalse(any("defined in" in h.why for h in hits))

    def test_a_named_function_stands_in_for_a_missing_path(self):
        with self.store.graph.transaction():
            self.store.graph.upsert_intent("app", "pr", "40", "Detect musl platforms", "", "Tags Author", "2026-01-01",
                                           paths=["src/pkg/tags.py"])
        names = [n for n, _l, _s in route_ranked(self.store.graph, "app", "Should sys_tags() yield emscripten tags?")]
        self.assertIn("Tags Author", names[:2])
