"""A first run follows README.md and docs/getting-started.md and meets the
terminal output they describe. Recorded on a fresh install: the Docker
stack on a spare port, then the browser flow and the connect commands."""
import argparse
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import OfflineCase  # noqa: E402

from bridge.auth import Auth  # noqa: E402
from bridge.bootstrap import dev_login  # noqa: E402
from bridge.setup import configure, install_host_hooks, repository_name  # noqa: E402
from bridge.store import Store  # noqa: E402

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


class SetupCredentialTests(OfflineCase):
    """Measured on a fresh install: `./dev login` printed the credential
    with "Sign in at …/auth/login". That page is an email and password
    form; the credential belongs on Create your workspace."""

    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "first.db")
        self.addCleanup(self.store.graph.close)
        person = self.store.graph.add_person("Local developer", role="admin")
        self.token = Auth(self.store, enabled=True).create_token(person, "development", kind="human")["token"]
        path = Path(self.temp.name) / "token"
        path.write_text(self.token)
        self.env = patch.dict(os.environ, {"BRIDGE_LOGIN_FILE": str(path), "BRIDGE_PUBLIC_URL": "http://localhost:7444/"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_the_credential_is_sent_to_create_your_workspace_not_to_sign_in(self):
        said = dev_login(self.store)
        self.assertIn(self.token, said)
        self.assertIn("setup credential", said)
        self.assertIn("http://localhost:7444/auth/setup", said)
        self.assertIn("Create your workspace", said)
        self.assertNotIn("/auth/login", said)

    def test_after_step_one_it_points_at_the_profile_step(self):
        self.store.graph.set_setting("workspace_name", "Acme")
        self.store.graph.set_setting("workspace_profile_pending", "1")
        said = dev_login(self.store)
        self.assertIn("http://localhost:7444/auth/profile", said)
        self.assertIn("Create your profile", said)


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
