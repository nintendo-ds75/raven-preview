"""The index a repository produces does not depend on how its git reads
are batched.

index_repo reads a checkout in one read session: the revision is resolved
once, the listing files the bound tree lacks are not asked for one by one,
and the --no-lazy-fetch probe runs once per process. The digests below
were recorded from the per-read code these batches replaced; the graph
tables an ingest writes must still match them, row for row."""

import hashlib
import json
import re
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures import BUILDERS, OfflineCase, git_env, run_git

from bridge import ingest
from bridge.ingest import index_repo
from bridge.store import Store

# The tables an ingest writes. Random identifiers (uuid hex) and
# wall-clock columns are masked; the checkout's temporary path is the
# one locator that differs per run.
TABLES = ("artifacts", "change_paths", "change_people", "changes", "connector_sources", "engineers",
          "intent_paths", "intents", "listings", "ownership", "source_identities", "source_records",
          "source_versions")
_RANDOM_ID = re.compile(r"\b[0-9a-f]{32}\b")

# For each fixture unpinned (the working tree) and pinned to HEAD (the
# object store). First recorded with the per-read implementation and
# matched by the batched one; re-recorded once the rows were made
# environment-independent (commit dates spelled by git_iso, ownership
# weights rounded), identical on macOS and Linux, Python 3.11 and 3.13.
GOLDEN = {
    "nodelike": {"": "941db3236072f0cc", "HEAD": "e461dbbd827e3f70"},
    "qemulike": {"": "ea505d31a718cb34", "HEAD": "dea8b214ad02dd91"},
    "synthco": {"": "cc07ddf2c595d1a5", "HEAD": "1e3d5ae9aad4967c"},
}
GOLDEN_STATS = {
    "nodelike": {"bots_skipped": 0, "commits": 45, "files": 16, "integrators": [], "maintainers": 0,
                 "merges": 0, "owners": 16, "prs": 0, "squashed": 0, "truncated": 0},
    "qemulike": {"bots_skipped": 1, "commits": 47, "files": 13, "integrators": ["Beatrix Hale"], "maintainers": 8,
                 "merges": 6, "owners": 20, "prs": 0, "squashed": 0, "truncated": 0},
    "synthco": {"bots_skipped": 1, "commits": 38, "files": 10, "integrators": [], "maintainers": 0,
                "merges": 2, "owners": 6, "prs": 1, "squashed": 1, "truncated": 0},
}


def index_rows(graph) -> dict[str, list[str]]:
    out = {}
    for table in TABLES:
        cols = [r[1] for r in graph.db.execute(f"PRAGMA table_info({table})")]
        keep = [c for c in cols if not c.endswith("_at") and not c.startswith("valid_")]
        # Ownership weights are sums of floats; Python 3.12 made sum()
        # compensated, which moves their last digit, so they are rounded.
        rows = [tuple(round(v, 9) if isinstance(v, float) else v for v in r)
                for r in graph.db.execute(f"SELECT {', '.join(keep)} FROM {table}")]
        if table == "connector_sources":
            rows = [(repo, kind, "" if kind == "git" else locator) for repo, kind, locator in rows]
        out[table] = sorted(_RANDOM_ID.sub("<id>", json.dumps(list(r), default=str)) for r in rows)
    return out


def index_digest(rows: dict[str, list[str]]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:16]


def _is_read(command: list[str]) -> bool:
    """A git read of the checkout, as opposed to the --no-lazy-fetch probe
    or, on a Git without that option, the partial-clone inspection."""
    return command[-1] != "--version" and "--get-regexp" not in command


class IngestIdentityTests(OfflineCase):
    _indexed: dict[tuple[str, str], tuple[dict, dict]] = {}

    def ingest(self, fixture: str, rev: str, name: str):
        store = Store(Path(self.temp.name) / name)
        self.addCleanup(store.graph.close)
        stats = index_repo(store.graph, BUILDERS[fixture](), rev=rev)
        return index_rows(store.graph), stats

    def batched(self, fixture: str, rev: str):
        """One batched ingest per fixture and revision, shared by the tests."""
        if (fixture, rev) not in self._indexed:
            self._indexed[(fixture, rev)] = self.ingest(fixture, rev, f"{fixture}-{rev or 'tree'}.db")
        return self._indexed[(fixture, rev)]

    def test_batched_reads_produce_the_recorded_index(self):
        for fixture in sorted(BUILDERS):
            for rev in ("", "HEAD"):
                with self.subTest(fixture=fixture, rev=rev or "unpinned"):
                    rows, stats = self.batched(fixture, rev)
                    self.assertEqual(index_digest(rows), GOLDEN[fixture][rev])
                    self.assertEqual({k: v for k, v in stats.items() if k not in ("github", "repo")},
                                     GOLDEN_STATS[fixture])
                    self.assertEqual(stats["repo"], fixture)

    def test_session_memory_changes_no_row(self):
        """Every read answered from the session (the resolved revision,
        the listing files the tree lacks) matches the read it stands for."""
        for fixture in sorted(BUILDERS):
            for rev in ("", "HEAD"):
                with self.subTest(fixture=fixture, rev=rev or "unpinned"):
                    batched, stats = self.batched(fixture, rev)
                    with patch("bridge.ingest._session", return_value=None):
                        per_read, per_read_stats = self.ingest(fixture, rev, f"{fixture}-{rev or 'tree'}-per-read.db")
                    for table in TABLES:
                        self.assertEqual(batched[table], per_read[table], table)
                    self.assertEqual(stats, per_read_stats)

    def test_one_commit_repository_is_read_in_a_handful_of_processes(self):
        repo = Path(self.temp.name) / "one-commit"
        (repo / "billing").mkdir(parents=True)
        (repo / "billing" / "usage.py").write_text("bill_internal = True\n")
        (repo / "CODEOWNERS").write_text("/billing/ wes@contract.example\n")
        run_git(repo, "init", "-q")
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-qm", "feat: seed", env=git_env(("Wes Contract", "wes@contract.example"), 0))
        store = self.fresh_store("one.db")
        self.addCleanup(store.graph.close)
        real_run = subprocess.run
        # Unpinned: resolve HEAD, the history, the working tree, the merges.
        # Pinned: the tree comes from ls-tree and CODEOWNERS from the object store.
        for rev, budget in (("", 4), ("HEAD", 5)):
            with self.subTest(rev=rev or "unpinned"):
                commands = []

                def counting(command, *args, **kwargs):
                    commands.append(command)
                    return real_run(command, *args, **kwargs)

                with patch("bridge.ingest.subprocess.run", side_effect=counting):
                    stats = index_repo(store.graph, repo, rev=rev)
                reads = [c for c in commands if _is_read(c)]
                self.assertEqual((stats["commits"], stats["files"], stats["owners"]), (1, 2, 1))
                self.assertLessEqual(len(reads), budget, [c[-3:] for c in reads])
                # The listing files the tree lacks were never asked for.
                shown = [c[-1] for c in reads if "show" in c]
                self.assertEqual([s.split(":", 1)[1] for s in shown], ["CODEOWNERS"] if rev else [])

    def test_the_probe_is_remembered_for_the_runner_that_answered(self):
        real_run = subprocess.run
        probes = []

        def counting(command, *args, **kwargs):
            if command[-1] == "--version":
                probes.append(command)
            return real_run(command, *args, **kwargs)

        store = self.fresh_store("probe.db")
        self.addCleanup(store.graph.close)
        with patch("bridge.ingest.subprocess.run", side_effect=counting):
            index_repo(store.graph, BUILDERS["synthco"]())
            index_repo(store.graph, BUILDERS["synthco"](), rev="HEAD")
            ingest._read_at(BUILDERS["synthco"](), "CODEOWNERS", "HEAD")
        self.assertEqual(len(probes), 1, probes)


class RouteReadIdentityTests(OfflineCase):
    """A route shares one read session across the git lookups behind it;
    what it answers is what the per-read lookups answered."""

    def routes(self, name: str):
        from bridge.signals import signal_route
        store = self.warm_store("qemulike", name)
        self.addCleanup(store.graph.close)
        paths = [r["path"] for r in store.graph.db.execute(
            "SELECT path FROM artifacts WHERE repo='qemulike' ORDER BY path LIMIT 4")]
        out = []
        for path in paths:
            notes: list[str] = []
            ranked = signal_route(store.graph, "qemulike", f"Should {path} keep its current default?",
                                  path=path, notes=notes)
            out.append((path, ranked, notes))
        return out

    def test_a_route_answers_the_same_with_one_read_session(self):
        self.warm_store("qemulike", "warm.db").graph.close()  # the fixture is built before counting
        real_run = subprocess.run
        spawned = []

        def counting(command, *args, **kwargs):
            spawned.append(command)
            return real_run(command, *args, **kwargs)

        with patch("bridge.ingest.subprocess.run", side_effect=counting):
            shared = self.routes("shared.db")
            reads_shared = len([c for c in spawned if _is_read(c)])
            spawned.clear()
            with patch("bridge.ingest._session", return_value=None):
                per_read = self.routes("per-read.db")
            reads_per_read = len([c for c in spawned if _is_read(c)])
        self.assertEqual(shared, per_read)
        self.assertTrue(any(ranked for _, ranked, _ in shared), shared)
        self.assertLess(reads_shared, reads_per_read)


if __name__ == "__main__":
    unittest.main()


class GitDateSpellingTests(unittest.TestCase):
    """Git 2.46 writes a UTC offset as Z where older versions write
    +00:00. The index spells every commit date one way, so the same
    checkout indexes to the same rows whichever git read it."""

    def test_z_and_offset_spell_the_same_instant_the_same_way(self):
        self.assertEqual(ingest.git_iso("2026-07-17T10:46:00Z"), "2026-07-17T10:46:00+00:00")
        self.assertEqual(ingest.git_iso("2026-07-17T10:46:00+00:00"), "2026-07-17T10:46:00+00:00")
        self.assertEqual(ingest.git_iso("2026-07-17T12:46:00+02:00\n"), "2026-07-17T12:46:00+02:00")

    def test_an_unparseable_date_is_kept_as_read(self):
        self.assertEqual(ingest.git_iso("not a date"), "not a date")
