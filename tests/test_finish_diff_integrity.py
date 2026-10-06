"""Keep host-submitted patch bytes intact and reject transport damage early."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_contract import ContractCase
from bridge import canvas, proof
from bridge.mcp import INSTRUCTIONS, TOOLS, call_tool
from bridge.store import Invalid


DIFF = ("diff --git a/billing/usage.py b/billing/usage.py\n"
        "--- a/billing/usage.py\n+++ b/billing/usage.py\n"
        "@@ -1 +1 @@\n-rate = 1\n+rate = 2\n")


def sha256(diff):
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()


class FinishDiffIntegrityTests(ContractCase):
    def signed_task(self):
        task = self.start()
        node = self.node(task)
        self.answer(node["node_id"], "Bill at two cents per call.")
        call_tool(self.store, "bridge_get_tree", {"task_id": task})
        return task

    def finish(self, task, **args):
        with patch("bridge.llm.check_conformance", return_value=None):
            return call_tool(self.store, "bridge_finish_task", {"task_id": task, **args})

    def assert_rejected_without_completion(self, task, message, **args):
        before = dict(self.store.graph.get_task(task))
        events = list(self.store.graph.db.execute(
            "SELECT id FROM events WHERE run_id=? ORDER BY id", (task,)))
        with patch.object(self.store, "update_run", wraps=self.store.update_run) as update, \
                patch("bridge.canvas._review") as review, \
                patch("bridge.proof.create") as create:
            with self.assertRaisesRegex(Invalid, message):
                self.finish(task, **args)
            update.assert_not_called()
            review.assert_not_called()
            create.assert_not_called()
        self.assertEqual(dict(self.store.graph.get_task(task)), before)
        self.assertEqual(list(self.store.graph.db.execute(
            "SELECT id FROM events WHERE run_id=? ORDER BY id", (task,))), events)
        with self.assertRaisesRegex(Invalid, "No change proof saved"):
            proof.export(self.store, {"task_id": task})

    def test_missing_final_lf_rejected_then_exact_retry_finishes(self):
        task = self.signed_task()
        self.assert_rejected_without_completion(task, "final newline", diff=DIFF[:-1],
                                                 checks="pytest: passed")
        done = self.finish(task, diff=DIFF)
        self.assertEqual(done["status"], "completed")
        change = proof.export(self.store, {"task_id": task})["bundle"]["payload"]["change"]
        self.assertEqual(change["diff"].encode("utf-8"), DIFF.encode("utf-8"))
        self.assertEqual(change["sha256"], sha256(DIFF))

    def test_plain_unified_patch_missing_final_lf_is_also_rejected(self):
        task = self.signed_task()
        unified = DIFF.split("\n", 1)[1]
        self.assert_rejected_without_completion(task, "final newline", diff=unified[:-1])
        self.assertEqual(self.finish(task, diff=unified)["status"], "completed")

    def test_git_metadata_patch_missing_final_lf_is_rejected(self):
        task = self.signed_task()
        diff = "diff --git a/billing/usage.py b/billing/usage.py\nold mode 100644\nnew mode 100755\n"
        self.assert_rejected_without_completion(task, "final newline", diff=diff[:-1])
        self.assertEqual(self.finish(task, diff=diff)["status"], "completed")

    def test_original_file_without_newline_keeps_git_marker_and_patch_lf(self):
        task = self.signed_task()
        diff = DIFF.replace("-rate = 1\n", "-rate = 1\n\\ No newline at end of file\n")
        diff = diff.replace("+rate = 2\n", "+rate = 2\n\\ No newline at end of file\n")
        self.assertEqual(self.finish(task, diff=diff, diff_sha256=sha256(diff))["status"], "completed")
        change = proof.export(self.store, {"task_id": task})["bundle"]["payload"]["change"]
        self.assertEqual(change["diff"], diff)
        self.assertEqual(change["bytes"], len(diff.encode("utf-8")))

    def test_expected_hash_uses_exact_utf8_bytes_and_preserves_whitespace(self):
        task = self.signed_task()
        # Unicode, CRLF in the changed file, trailing spaces and extra final LF
        # must survive exactly; none may be normalized to make a hash match.
        diff = DIFF.replace("+rate = 2\n", "+rate = 2  # café ☕  \r\n") + "\n"
        for expected in (sha256(diff), sha256(diff).upper()):
            result = self.finish(task, diff=diff, diff_sha256=expected)
            self.assertEqual(result["proof"]["diff_sha256"], sha256(diff))
            self.assertFalse(result["verified"])
        exported = proof.export(self.store, {"task_id": task})
        change = exported["bundle"]["payload"]["change"]
        self.assertEqual(change["diff"].encode("utf-8"), diff.encode("utf-8"))
        self.assertEqual(change["bytes"], len(diff.encode("utf-8")))
        self.assertFalse(exported["integrity"]["authenticity_verified"])

    def test_expected_hash_mismatch_is_rejected_before_any_finish_write(self):
        task = self.signed_task()
        changed = DIFF.replace("+rate = 2", "+rate = 3")
        self.assert_rejected_without_completion(task, "diff_sha256.*does not match", diff=changed,
                                                 diff_sha256=sha256(DIFF), checks="pytest: passed")
        self.assertEqual(self.finish(task, diff=DIFF, diff_sha256=sha256(DIFF))["status"], "completed")

    def test_matching_hash_does_not_make_a_truncated_patch_acceptable(self):
        task = self.signed_task()
        diff = DIFF[:-1]
        self.assert_rejected_without_completion(task, "final newline", diff=diff, diff_sha256=sha256(diff))

    def test_malformed_expected_hashes_are_not_trimmed_or_ignored(self):
        task = self.signed_task()
        for expected in ("", " ", "a" * 63, "a" * 65, "g" * 64, " " + sha256(DIFF),
                         sha256(DIFF) + "\n", "sha256:" + sha256(DIFF)):
            with self.subTest(expected=repr(expected)):
                self.assert_rejected_without_completion(task, "diff_sha256.*64 hexadecimal", diff=DIFF,
                                                         diff_sha256=expected)

    def test_expected_hash_requires_a_submitted_diff(self):
        task = self.signed_task()
        self.assert_rejected_without_completion(task, "diff_sha256.*requires.*diff", diff_sha256=sha256(""))

    def test_non_utf8_text_is_rejected_before_completion(self):
        task = self.signed_task()
        self.assert_rejected_without_completion(task, "diff.*UTF-8", diff=DIFF.replace("rate = 2", "\ud800"))

    def test_legacy_calls_without_hash_or_diff_still_work(self):
        task = self.signed_task()
        self.assertEqual(self.finish(task)["status"], "completed")
        with self.assertRaisesRegex(Invalid, "No change proof saved"):
            proof.export(self.store, {"task_id": task})

    def test_legacy_advisory_fragments_remain_exact_without_final_lf(self):
        for fragment in ("+rate = 2", "diff --git a/billing/usage.py b/billing/usage.py\n+rate = 2"):
            with self.subTest(fragment=fragment):
                task = self.signed_task()
                self.assertEqual(self.finish(task, diff=fragment)["status"], "completed")
                change = proof.export(self.store, {"task_id": task})["bundle"]["payload"]["change"]
                self.assertEqual(change["diff"], fragment)
                self.assertEqual(change["sha256"], sha256(fragment))

    def test_mcp_guidance_describes_transport_integrity_without_attestation(self):
        tool = next(t for t in TOOLS if t["name"] == "bridge_finish_task")
        props = tool["inputSchema"]["properties"]
        self.assertNotIn("diff_sha256", tool["inputSchema"]["required"])
        self.assertEqual(props["diff_sha256"]["type"], "string")
        self.assertIn("not independent", props["diff_sha256"]["description"])
        for wording in ("final newline", "new files", "diff_sha256", "exact"):
            self.assertIn(wording, INSTRUCTIONS)


@unittest.skipUnless(shutil.which("git"), "git is needed to verify real patch syntax")
class GitPatchSyntaxTests(unittest.TestCase):
    def test_real_git_patch_loses_validity_when_normal_final_lf_is_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                   "GIT_CONFIG_NOSYSTEM": "1", "HOME": directory}

            def git(*args, raw=None, check=True):
                return subprocess.run(["git", "-C", directory, *args], input=raw,
                                      capture_output=True, check=check, env=env)

            git("init", "-q")
            path = root / "example.txt"
            for before, after in ((b"before\n", b"after\n"), (b"before", b"after")):
                with self.subTest(original_has_lf=before.endswith(b"\n")):
                    path.write_bytes(before)
                    git("add", "example.txt")
                    path.write_bytes(after)
                    raw = git("diff", "--no-ext-diff", "--no-color").stdout
                    self.assertTrue(raw.endswith(b"\n"))
                    path.write_bytes(before)
                    git("apply", "--check", "-", raw=raw)
                    if before.endswith(b"\n"):
                        bad = git("apply", "--check", "-", raw=raw[:-1], check=False)
                        self.assertNotEqual(bad.returncode, 0)
                        self.assertIn(b"corrupt patch", bad.stderr)
                    else:
                        self.assertIn(b"\\ No newline at end of file\n", raw)


if __name__ == "__main__":
    unittest.main()
