"""Conventional-commit CI against local Git repositories, without network access."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.check_commit_headers import ZERO_SHA, valid


CHECKER = Path(__file__).resolve().parents[1] / "scripts" / "check_commit_headers.py"


class CommitHeaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="raven-headers-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origin = self.root / "origin"
        self.origin.mkdir()
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                    "GIT_AUTHOR_NAME": "Test Author", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                    "GIT_COMMITTER_NAME": "Test Author", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            self.env.pop(key, None)
        self.git(self.origin, "init", "-b", "main")

    def git(self, cwd, *args, check=True):
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, capture_output=True,
                              text=True, check=check)

    def commit(self, title):
        target = self.origin / "example.txt"
        target.write_text(title + "\n")
        self.git(self.origin, "add", "example.txt")
        self.git(self.origin, "-c", "commit.gpgsign=false", "commit", "-m", title)
        return self.git(self.origin, "rev-parse", "HEAD").stdout.strip()

    def clone(self):
        checkout = self.root / "checkout"
        self.git(self.root, "clone", "--no-local", str(self.origin), str(checkout))
        return checkout

    def snapshots(self, title="fix: replace the snapshot"):
        before = self.commit("Historical subject outside the new range")
        self.git(self.origin, "checkout", "--orphan", "replacement")
        after = self.commit(title)
        self.git(self.origin, "branch", "-M", "main")
        checkout = self.clone()
        self.assertNotEqual(self.git(checkout, "cat-file", "-e", before, check=False).returncode, 0)
        return before, after, checkout

    def check_event(self, checkout, event, kind="push"):
        payload = self.root / "event.json"
        payload.write_text(json.dumps(event))
        return subprocess.run([sys.executable, str(CHECKER)], cwd=checkout,
                              env={**self.env, "GITHUB_EVENT_PATH": str(payload), "GITHUB_EVENT_NAME": kind},
                              capture_output=True, text=True)

    def push(self, before, after, **extra):
        return {"before": before, "after": after, "repository": {"default_branch": "main"}, **extra}

    def pull(self, base, head, title="fix: validate this change"):
        return {"pull_request": {"title": title, "base": {"sha": base}, "head": {"sha": head}}}

    def test_header_validation(self):
        for title in ("fix: validate headers", "feat(auth)!: require scope", "docs: explain setup"):
            self.assertTrue(valid(title), title)
        for title in ("Update headers", "fix: trailing period.", "Fix: wrong case", "fix: " + "x" * 100):
            self.assertFalse(valid(title), title)

    def test_normal_push_checks_only_new_commits(self):
        before = self.commit("Historical nonconventional title")
        after = self.commit("fix: validate the pushed change")
        result = self.check_event(self.clone(), self.push(before, after))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Fetched", result.stdout)

    def test_parentless_push_recovers_exact_before_sha(self):
        before, after, checkout = self.snapshots()
        result = self.check_event(checkout, self.push(before, after))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Fetched missing push before commit {before}", result.stdout)

    def test_recovered_range_still_rejects_invalid_new_subject(self):
        before, after, checkout = self.snapshots("Invalid replacement title")
        result = self.check_event(checkout, self.push(before, after))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Commit subject: Invalid replacement title", result.stderr)

    def test_unrecoverable_before_fails_closed_without_traceback(self):
        head = self.commit("fix: retain the valid tip")
        result = self.check_event(self.clone(), self.push("1" * 40, head))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Cannot resolve push before commit", result.stderr)
        self.assertIn("No replacement commit range was checked", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("follow Conventional Commits", result.stdout)

    def test_missing_pull_request_base_is_fetched(self):
        base, head, checkout = self.snapshots()
        result = self.check_event(checkout, self.pull(base, head), "pull_request")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Fetched missing pull-request base commit {base}", result.stdout)

    def test_missing_pull_request_head_is_fetched(self):
        base = self.commit("Historical nonconventional title")
        checkout = self.clone()
        head = self.commit("feat: add the requested behavior")
        result = self.check_event(checkout, self.pull(base, head), "pull_request")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Fetched missing pull-request head commit {head}", result.stdout)

    def test_pull_request_title_is_checked_after_recovery(self):
        base, head, checkout = self.snapshots()
        result = self.check_event(checkout, self.pull(base, head, "Invalid PR title"), "pull_request")
        self.assertEqual(result.returncode, 1)
        self.assertIn("PR title: Invalid PR title", result.stderr)

    def test_new_branch_excludes_shared_legacy_history(self):
        base = self.commit("Historical nonconventional title")
        checkout = self.clone()
        head = self.commit("feat: add a branch change")
        self.git(checkout, "fetch", "origin", head)
        result = self.check_event(checkout, self.push(ZERO_SHA, head))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(checkout, "rev-parse", "origin/main").stdout.strip(), base)

    def test_new_disconnected_history_is_checked(self):
        before, head, checkout = self.snapshots("Invalid new root")
        self.git(checkout, "fetch", "origin", before)
        self.git(checkout, "update-ref", "refs/remotes/origin/main", before)
        result = self.check_event(checkout, self.push(ZERO_SHA, head))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Commit subject: Invalid new root", result.stderr)

    def test_deleted_branch_has_no_commit_range(self):
        before = self.commit("Historical title")
        result = self.check_event(self.clone(), self.push(before, ZERO_SHA, deleted=True))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_malformed_event_sha_is_not_a_git_option(self):
        head = self.commit("fix: retain the tip")
        result = self.check_event(self.clone(), self.push("--all", head))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Invalid push before commit SHA", result.stderr)


if __name__ == "__main__":
    unittest.main()
