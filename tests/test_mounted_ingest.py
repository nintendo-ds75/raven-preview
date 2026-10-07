"""Local Git reads of an explicitly selected, differently owned checkout.

Git's own t0033-safe-directory.sh uses GIT_TEST_ASSUME_DIFFERENT_OWNER to
exercise the real ownership check without changing filesystem ownership.
"""

import contextlib
import io
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase, git_env, run_git

from bridge import bootstrap
from bridge.ingest import _git, _git_quiet, _git_rev, _read_at, blame_counts, index_repo, read_log
from bridge.store import Store


class MountedIngestTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temp.name)
        self.repo = self.make_repo("selected checkout")
        self.other = self.make_repo("unrelated")
        self.global_config = self.root / "global.gitconfig"
        self.system_config = self.root / "system.gitconfig"
        self.global_config.write_text("[user]\n\tname = Fixture\n")
        self.system_config.write_text("[advice]\n\tdetachedHead = false\n")
        self.env = patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": str(self.global_config),
            "GIT_CONFIG_SYSTEM": str(self.system_config),
            "GIT_CONFIG_COUNT": "0",
            "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1",
            "LC_ALL": "C",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = Store(self.root / "ingest.db")
        self.addCleanup(self.store.graph.close)

    def make_repo(self, name):
        repo = self.root / name
        repo.mkdir()
        run_git(repo, "init", "-q", "-b", "main")
        (repo / "payments").mkdir()
        (repo / "payments" / "charge.py").write_text("currency = 'USD'\n")
        (repo / "CODEOWNERS").write_text("payments/ @payments-team\n")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "Seed payments", env=git_env(("Fixture Author", "fixture@example.test"), 0))
        return repo

    def raw_git(self, repo, *args, **kwargs):
        return subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True, timeout=10, **kwargs)

    def assert_untrusted(self, repo):
        rejected = self.raw_git(repo, "ls-files")
        self.assertNotEqual(rejected.returncode, 0, rejected.stdout)
        self.assertIn("dubious ownership", rejected.stderr)

    def snapshot(self):
        paths = [self.global_config, self.system_config, self.repo, *self.repo.rglob("*")]
        return {str(p): (p.stat().st_mode, p.stat().st_uid, p.stat().st_gid,
                         p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None) for p in paths}

    def test_repeat_ingest_is_local_and_does_not_change_trust_or_checkout(self):
        self.assert_untrusted(self.repo)
        self.assert_untrusted(self.other)
        before = self.snapshot()
        environment = dict(os.environ)
        first = index_repo(self.store.graph, self.repo, repo_name="fixture/payments")
        counts = self.store.graph.counts()
        second = index_repo(self.store.graph, self.repo, repo_name="fixture/payments")
        self.assertEqual(first, second)
        self.assertEqual(first["commits"], 1)
        self.assertEqual(first["files"], 2)
        after = self.store.graph.counts()
        self.assertEqual(after.pop("events"), counts.pop("events") + 1)
        self.assertEqual(after, counts)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(dict(os.environ), environment)
        self.assert_untrusted(self.repo)
        self.assert_untrusted(self.other)

    def test_bootstrap_pinned_and_live_reads_share_the_selected_checkout(self):
        environment = dict(os.environ)
        before = self.snapshot()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(bootstrap._ingest_configured(self.store, self.repo, "fixture/payments"))
            self.assertTrue(bootstrap._ingest_configured(self.store, self.repo, "fixture/payments"))
        self.assertEqual(_read_at(self.repo, "CODEOWNERS", "HEAD"), "payments/ @payments-team\n")
        self.assertEqual(_read_at(self.repo, "missing", "HEAD"), "")
        self.assertIn("payments/charge.py", _git_quiet(str(self.repo), "ls-tree", "-r", "--name-only", "HEAD"))
        self.assertEqual(blame_counts(str(self.repo), "HEAD", "payments/charge.py"),
                         {("Fixture Author", "fixture@example.test"): 1})
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(dict(os.environ), environment)
        self.assert_untrusted(self.other)

    def test_environment_cannot_redirect_the_selected_checkout(self):
        with patch.dict(os.environ, {"GIT_DIR": str(self.other / ".git"),
                                    "GIT_WORK_TREE": str(self.other),
                                    "GIT_COMMON_DIR": str(self.other / ".git"),
                                    "GIT_OBJECT_DIRECTORY": str(self.other / ".git" / "objects"),
                                    "GIT_INDEX_FILE": str(self.other / ".git" / "missing-index")}):
            result = index_repo(self.store.graph, self.repo)
        self.assertEqual(result["files"], 2)
        self.assertEqual(result["commits"], 1)
        self.assert_untrusted(self.other)

    def test_repository_helpers_are_not_executed(self):
        marker = self.root / "helper-ran"
        helper = self.root / "helper"
        with os.fdopen(os.open(helper, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700), "w") as out:
            out.write(f"#!/bin/sh\nprintf ran >> '{marker}'\nexit 1\n")
        env = {**os.environ, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "0"}
        for key, value in (("core.fsmonitor", str(helper)), ("core.hooksPath", str(self.root)),
                           ("diff.external", str(helper)), ("diff.fixture.textconv", str(helper)),
                           ("log.showSignature", "true"), ("gpg.program", str(helper))):
            self.raw_git(self.repo, "config", key, value, env=env).check_returncode()
        (self.repo / ".git" / "info" / "attributes").write_text("* diff=fixture\n")
        # A synthetic signature exercises the configured verifier, without
        # invoking a real signing tool or creating credentials.
        commit = self.raw_git(self.repo, "cat-file", "commit", "HEAD", env=env).stdout
        signed = commit.replace("\n\n", "\ngpgsig -----BEGIN PGP SIGNATURE-----\n fake\n -----END PGP SIGNATURE-----\n\n", 1)
        sha = self.raw_git(self.repo, "hash-object", "-t", "commit", "-w", "--stdin", input=signed, env=env).stdout.strip()
        self.raw_git(self.repo, "update-ref", "HEAD", sha, env=env).check_returncode()
        before = self.snapshot()
        index_repo(self.store.graph, self.repo)
        self.assertIn("payments/charge.py", _git(self.repo, "show", "HEAD", "--", "payments/charge.py"))
        self.assertEqual(_read_at(self.repo, "CODEOWNERS", "HEAD"), "payments/ @payments-team\n")
        self.assertTrue(blame_counts(str(self.repo), "HEAD", "payments/charge.py"))
        self.assertFalse(marker.exists())
        self.assertEqual(self.snapshot(), before)
        # Establish that this real Git installation would run the configured
        # monitor without the read helper's per-command restriction.
        self.raw_git(self.repo, "ls-files", env=env)
        self.assertTrue(marker.exists())
        marker.unlink()
        self.raw_git(self.repo, "log", "-1", env=env)
        self.assertTrue(marker.exists())

    def test_selected_path_never_grants_trust_to_its_parent(self):
        # A subdirectory must not turn Git's parent discovery into implicit trust.
        with self.assertRaises(RuntimeError):
            _git(self.repo / "payments", "ls-files")
        self.assert_untrusted(self.repo)
        self.assert_untrusted(self.other)

    def test_missing_promisor_objects_fail_without_fetching(self):
        env = {**os.environ, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "0"}
        marker = self.root / "fetch-ran"
        helper = self.root / "fetch-helper"
        with os.fdopen(os.open(helper, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700), "w") as out:
            out.write(f"#!/bin/sh\nprintf ran >> '{marker}'\nexit 1\n")
        for key, value in (("remote.origin.promisor", "true"),
                           ("remote.origin.url", f"ext::{helper}"), ("protocol.ext.allow", "always")):
            self.raw_git(self.repo, "config", key, value, env=env).check_returncode()
        blob = self.raw_git(self.repo, "rev-parse", "HEAD:CODEOWNERS", env=env).stdout.strip()
        (self.repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "git show failed|partial-clone/promisor"):
            index_repo(self.store.graph, self.repo, rev="HEAD")
        self.assertEqual(self.store.graph.artifact_paths(self.repo.name), [])
        self.assertFalse(marker.exists())
        self.assertEqual(self.snapshot(), before)
        self.raw_git(self.repo, "show", "HEAD:CODEOWNERS",
                     env={**env, "GIT_NO_LAZY_FETCH": "0", "GIT_ALLOW_PROTOCOL": "ext"})
        self.assertTrue(marker.exists())

    def test_missing_history_does_not_replace_a_previous_index(self):
        index_repo(self.store.graph, self.repo)
        env = {**os.environ, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "0"}
        head = self.raw_git(self.repo, "rev-parse", "HEAD", env=env).stdout.strip()
        (self.repo / ".git" / "objects" / head[:2] / head[2:]).unlink()
        before = self.store.graph.counts()
        with self.assertRaisesRegex(RuntimeError, "git rev-parse --verify failed"):
            index_repo(self.store.graph, self.repo)
        self.assertEqual(self.store.graph.counts(), before)

    def test_checkout_named_wildcard_does_not_expand_trust(self):
        wildcard = self.root / "*"
        self.repo.rename(wildcard)
        with self.assertRaisesRegex(RuntimeError, "wildcard"):
            index_repo(self.store.graph, wildcard)
        self.assert_untrusted(self.other)

    def test_linked_worktree_is_selected_by_its_own_path(self):
        linked = self.root / "linked-checkout"
        env = {**os.environ, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "0"}
        self.raw_git(self.repo, "worktree", "add", "-q", "-b", "linked", str(linked), env=env).check_returncode()
        self.assertTrue((linked / ".git").is_file())
        before = self.snapshot()
        result = index_repo(self.store.graph, linked, rev="HEAD")
        self.assertEqual(result["files"], 2)
        self.assertEqual(result["commits"], 1)
        self.assertEqual(self.snapshot(), before)
        self.assert_untrusted(linked)
        self.assert_untrusted(self.repo)

    def test_injected_trust_is_reset_for_each_reader(self):
        with patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory",
                                    "GIT_CONFIG_VALUE_0": "*", "GIT_CONFIG_PARAMETERS": "'safe.directory=*'"}):
            values = _git(self.repo, "config", "--get-all", "safe.directory").splitlines()
            self.assertEqual(values, ["", str(self.repo)])
            result = index_repo(self.store.graph, self.repo)
            self.assertEqual(result["commits"], 1)
        self.assert_untrusted(self.other)

    def test_option_like_revisions_cannot_enable_repository_helpers(self):
        marker = self.root / "revision-helper-ran"
        helper = self.root / "revision-helper"
        with os.fdopen(os.open(helper, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700), "w") as out:
            out.write(f"#!/bin/sh\nprintf ran >> '{marker}'\nexit 1\n")
        env = {**os.environ, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "0"}
        self.raw_git(self.repo, "config", "gpg.program", str(helper), env=env).check_returncode()
        commit = self.raw_git(self.repo, "cat-file", "commit", "HEAD", env=env).stdout
        signed = commit.replace("\n\n", "\ngpgsig -----BEGIN PGP SIGNATURE-----\n fake\n -----END PGP SIGNATURE-----\n\n", 1)
        sha = self.raw_git(self.repo, "hash-object", "-t", "commit", "-w", "--stdin", input=signed, env=env).stdout.strip()
        self.raw_git(self.repo, "update-ref", "HEAD", sha, env=env).check_returncode()
        before = self.snapshot()
        for revision in ("--show-signature", "--ext-diff", "--textconv", "--all"):
            with self.subTest(revision=revision):
                with self.assertRaises(RuntimeError):
                    read_log(self.repo, revision, [], [], strict=True)
                with self.assertRaises(RuntimeError):
                    index_repo(self.store.graph, self.repo, rev=revision)
                with self.assertRaises(RuntimeError):
                    _read_at(self.repo, "CODEOWNERS", revision)
                self.assertEqual(blame_counts(str(self.repo), revision, "payments/charge.py"), {})
                self.store.graph.set_source("fixture/payments", "git", str(self.repo))
                self.store.graph.set_source("fixture/payments", "git_rev", revision)
                with self.assertRaises(RuntimeError):
                    _git_rev(self.store.graph, "fixture/payments")
                self.assertFalse(marker.exists())
        self.assertEqual(self.snapshot(), before)
        # The same repository really does invoke its verifier if Git is
        # allowed to interpret --show-signature as an option.
        self.raw_git(self.repo, "log", "--show-signature", "-1", env=env)
        self.assertTrue(marker.exists())

    def test_branch_tag_and_sha_revisions_resolve_to_the_same_commit(self):
        env = {**os.environ, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "0"}
        head = self.raw_git(self.repo, "rev-parse", "HEAD", env=env).stdout.strip()
        self.raw_git(self.repo, "tag", "fixture-release", env=env).check_returncode()
        self.raw_git(self.repo, "tag", "-a", "annotated-release", "-m", "Fixture release",
                     env={**env, **git_env(("Fixture Author", "fixture@example.test"), 0)}).check_returncode()
        for revision in ("main", "fixture-release", "annotated-release", head):
            with self.subTest(revision=revision):
                result = index_repo(self.store.graph, self.repo, rev=revision)
                self.assertEqual(result["commits"], 1)
                self.assertEqual(self.store.graph.get_source(self.repo.name, "git_rev"), head)
                self.assertEqual(_git_rev(self.store.graph, self.repo.name), head)
                self.assertEqual(read_log(self.repo, revision, [], [], strict=True)[0].sha, head)
                self.assertEqual(_read_at(self.repo, "CODEOWNERS", revision), "payments/ @payments-team\n")
                self.assertTrue(blame_counts(str(self.repo), revision, "payments/charge.py"))

    def test_revision_must_resolve_to_a_commit(self):
        index_repo(self.store.graph, self.repo)
        before = self.store.graph.counts()
        for revision in ("HEAD^{tree}", "HEAD:CODEOWNERS"):
            with self.subTest(revision=revision), self.assertRaises(RuntimeError):
                index_repo(self.store.graph, self.repo, rev=revision)
        self.assertEqual(self.store.graph.counts(), before)

    @contextlib.contextmanager
    def older_git_without_no_lazy_fetch(self):
        """Only emulate the missing capability; all repo operations use Git."""
        real_run = subprocess.run
        protected_commands = []

        def older_run(command, **kwargs):
            if command == ["git", "--no-lazy-fetch", "--version"]:
                return subprocess.CompletedProcess(command, 129, "", "unknown option: --no-lazy-fetch")
            if kwargs.get("env", {}).get("GIT_ALLOW_PROTOCOL") == "":
                kwargs["env"] = dict(kwargs["env"])
                kwargs["env"].pop("GIT_NO_LAZY_FETCH", None)
                self.assertNotIn("--no-lazy-fetch", command)
                protected_commands.append(command)
            return real_run(command, **kwargs)

        with patch("bridge.ingest.subprocess.run", side_effect=older_run):
            yield protected_commands

    def test_older_git_rejects_promisor_reads_before_object_access_or_writes(self):
        with self.older_git_without_no_lazy_fetch() as commands:
            self.test_missing_promisor_objects_fail_without_fetching()
        self.assertTrue(commands)
        self.assertTrue(all(command[-4:-1] == ["config", "--name-only", "--get-regexp"]
                            for command in commands), commands)

    def test_older_git_still_reads_full_checkouts(self):
        with self.older_git_without_no_lazy_fetch():
            self.test_repeat_ingest_is_local_and_does_not_change_trust_or_checkout()

    def test_older_git_rejects_each_partial_clone_configuration(self):
        env = {**os.environ, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "0"}
        for key, value in (("remote.backup.promisor", "true"),
                           ("remote.backup.partialclonefilter", "blob:none"),
                           ("extensions.partialClone", "backup")):
            with self.subTest(key=key):
                self.raw_git(self.repo, "config", key, value, env=env).check_returncode()
                before = self.snapshot()
                with self.older_git_without_no_lazy_fetch(), self.assertRaisesRegex(RuntimeError, "partial-clone/promisor"):
                    index_repo(self.store.graph, self.repo)
                self.assertEqual(self.snapshot(), before)
                self.raw_git(self.repo, "config", "--unset", key, env=env).check_returncode()

    def test_unpinned_listings_reject_external_symlinks(self):
        index_repo(self.store.graph, self.repo)
        before = self.store.graph.counts()
        sentinel = self.root / "outside-listing"
        sentinel.write_text("payments/ @outside-sentinel\n")
        for rel in ("CODEOWNERS", "MAINTAINERS", ".github/CODEOWNERS", "docs/CODEOWNERS"):
            with self.subTest(path=rel):
                target = self.repo / rel
                target.parent.mkdir(exist_ok=True)
                original = target.read_bytes() if target.exists() else None
                if original is not None:
                    target.unlink()
                target.symlink_to(sentinel)
                with self.assertRaisesRegex(RuntimeError, "outside the selected checkout"):
                    _read_at(self.repo, rel)
                target.unlink()
                if original is not None:
                    target.write_bytes(original)
        (self.repo / "CODEOWNERS").unlink()
        (self.repo / "CODEOWNERS").symlink_to(sentinel)
        with self.assertRaisesRegex(RuntimeError, "outside the selected checkout"):
            index_repo(self.store.graph, self.repo)
        self.assertEqual(self.store.graph.counts(), before)

    def test_unpinned_listings_allow_internal_symlinks_but_not_external_parent(self):
        (self.repo / "local-listing").write_text("payments/ @internal\n")
        (self.repo / "MAINTAINERS").symlink_to("local-listing")
        self.assertEqual(_read_at(self.repo, "MAINTAINERS"), "payments/ @internal\n")
        outside = self.root / "outside-directory"
        outside.mkdir()
        (outside / "CODEOWNERS").write_text("payments/ @outside-sentinel\n")
        (self.repo / ".github").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "outside the selected checkout"):
            _read_at(self.repo, ".github/CODEOWNERS")
