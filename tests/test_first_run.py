"""A first run follows README.md and docs/getting-started.md and meets the
terminal output they describe. Recorded on a fresh install: the Docker
stack on a spare port, then the browser flow and the connect commands."""
import argparse
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.setup import configure, install_host_hooks, repository_name

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "A Person", "GIT_AUTHOR_EMAIL": "a@example.test",
           "GIT_COMMITTER_NAME": "A Person", "GIT_COMMITTER_EMAIL": "a@example.test"}


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, env=GIT_ENV, capture_output=True)


def _checkout(root, remote=None):
    project = Path(root) / "project"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "commit", "-q", "--allow-empty", "-m", "start")
    if remote:
        _git(project, "remote", "add", "origin", remote)
    return project


class ConnectProjectTests(unittest.TestCase):
    """`./setup --configure --project` names the repository after the
    checkout's origin remote. Only GitHub's `git@github.com:` spelling
    was read; a GitLab or self-hosted remote was recorded whole."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_every_remote_spelling_yields_owner_and_name(self):
        for remote, expected in (
            ("git@github.com:acme/widgets.git", "acme/widgets"),
            ("git@gitlab.example:acme/widgets.git", "acme/widgets"),
            ("org-123@git.example:acme/widgets", "acme/widgets"),
            ("ssh://git@github.com/acme/widgets.git", "acme/widgets"),
            ("https://github.com/acme/widgets.git", "acme/widgets"),
            ("https://gitlab.example.com/acme/widgets", "acme/widgets"),
            ("", ""),
        ):
            with self.subTest(remote=remote):
                self.assertEqual(repository_name(remote), expected)

    def test_hooks_record_the_repository_of_a_non_github_ssh_remote(self):
        import json
        project = _checkout(self.directory.name, "git@gitlab.example:acme/widgets.git")
        install_host_hooks(project, str(project), "http://localhost:7333/mcp", "test-agent-secret", ["claude"])
        config = json.loads((project / ".raven/host-config.json").read_text())
        self.assertEqual(config["repo"], "acme/widgets")

    def test_a_checkout_without_a_remote_says_where_to_name_the_repository(self):
        project = _checkout(self.directory.name)
        with self.assertRaises(ValueError) as stopped:
            install_host_hooks(project, "/Users/dev/code", "http://localhost:7333/mcp", "test-agent-secret", ["claude"])
        message = str(stopped.exception)
        self.assertIn("BRIDGE_INGEST_REPO='owner/name'", message)
        self.assertIn(".env", message)
        self.assertIn("./setup --configure --project /Users/dev/code", message)
        self.assertFalse((project / ".raven").exists())


class ConfigureWithoutTerminalTests(unittest.TestCase):
    """Piped or scripted, `./setup --configure` asked nothing, printed
    "Settings saved", and the banner then told the user to run
    `./setup --configure`: a loop with no explanation."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / ".env"

    def run_config(self, interactive, **flags):
        args = argparse.Namespace(demo=False, repo=None, repo_name=None, port=None, **flags)
        with contextlib.redirect_stdout(io.StringIO()) as said:
            configure(args, self.path, interactive)
        return said.getvalue()

    def test_configure_without_a_terminal_says_it_asked_nothing(self):
        said = self.run_config(False, configure=True, yes=False)
        self.assertIn("asked nothing", said)
        self.assertIn("interactive terminal", said)

    def test_the_notice_is_not_given_when_nothing_was_going_to_be_asked(self):
        self.assertNotIn("asked nothing", self.run_config(False, configure=True, yes=True))
        self.assertNotIn("asked nothing", self.run_config(False, configure=False, yes=False))
        with patch("builtins.input", side_effect=["", "n", "n", "n", "n"]), patch("getpass.getpass", return_value=""):
            self.assertNotIn("asked nothing", self.run_config(True, configure=True, yes=False))



if __name__ == "__main__":
    unittest.main()
