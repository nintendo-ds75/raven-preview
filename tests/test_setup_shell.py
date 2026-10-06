"""Exercise the launcher itself, including macOS Bash's empty-array behavior."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == "posix" and Path("/bin/bash").exists(), "requires Bash")
@unittest.skipUnless((Path(__file__).resolve().parents[1] / "setup").exists(),
                     "host launcher is not packaged in the application image")
class SetupShellTests(unittest.TestCase):
    def run_launcher(self, interactive, arguments):
        import pty
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(Path(__file__).resolve().parents[1] / "setup", root / "setup")
            (root / "dev").write_text("#!/bin/sh\nexit 0\n")
            (root / "dev").chmod(0o755)
            (root / "docker").write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$SETUP_TEST_LOG"\ncase "$*" in *printenv*) echo http://localhost:7333;; esac\n')
            (root / "docker").chmod(0o755)
            (root / "open").write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$SETUP_TEST_LOG"\n')
            (root / "open").chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                       SETUP_TEST_LOG=str(root / "calls"))
            project = root / 'customer code'
            project.mkdir()
            command = ["/bin/bash", str(root / "setup"), *[a.replace('{project}', str(project)) for a in arguments]]
            if interactive:
                master, slave = pty.openpty()
                try:
                    result = subprocess.run(command, stdin=slave, stdout=slave, stderr=slave,
                                            env=env, timeout=20)
                finally:
                    os.close(slave)
                    os.close(master)
            else:
                result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                        env=env, timeout=20)
            self.assertEqual(result.returncode, 0)
            return (root / "calls").read_text().splitlines()

    def test_interactive_no_arguments(self):
        calls = self.run_launcher(True, [])
        self.assertIn("bridge.setup", calls)
        self.assertNotIn("-T", calls[:calls.index("bridge.setup")])
        self.assertIn("http://localhost:7333/#connect", calls)

    def test_noninteractive_no_arguments(self):
        calls = self.run_launcher(False, [])
        self.assertIn("-T", calls)
        self.assertIn("bridge.setup", calls)
        root = Path(__file__).resolve().parents[1]
        self.assertIn("ANTHROPIC_WORKSPACE_ID: ${ANTHROPIC_WORKSPACE_ID:-}",
                      (root / "compose.yaml").read_text())
        self.assertIn("# ANTHROPIC_WORKSPACE_ID=", (root / ".env.example").read_text())

    def test_project_is_not_forwarded_to_the_connection_wizard(self):
        calls = self.run_launcher(False, ["--yes", "--configure", "--project", "{project}"])
        self.assertNotIn('--project', calls)
        self.assertNotIn('/agent-project', calls)

    def test_arguments_are_forwarded(self):
        calls = self.run_launcher(True, ["--yes", "--port", "7444"])
        self.assertIn("--port", calls)
        self.assertIn("7444", calls)
        self.assertNotIn("http://localhost:7333/#connect", calls)

    def test_windows_wrapper_uses_the_same_configuration_wizard(self):
        root = Path(__file__).resolve().parents[1]
        self.assertIn('& wsl bash "$bridgeRoot/setup" @bridgeArgs', (root / "setup.ps1").read_text())
        calls = self.run_launcher(True, ["--configure"])
        self.assertIn("bridge.setup", calls)
        self.assertIn("--configure", calls)
        self.assertNotIn("--yes", calls[:calls.index("bridge.setup") + 2])
