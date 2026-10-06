import argparse
import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from bridge.setup import _mcp_clients, configure, read_settings, save_settings


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / ".env"
        self.args = argparse.Namespace(demo=False, repo=None, repo_name=None,
                                       port=None, configure=False)

    def run_config(self, interactive=False):
        with contextlib.redirect_stdout(io.StringIO()):
            configure(self.args, self.path, interactive)

    def test_repeat_preserves_existing_configuration_exactly(self):
        self.path.write_text("# my settings\nOPENAI_API_KEY='private-value'\nBRIDGE_PORT=7444\n"
                             "BRIDGE_PUBLIC_URL='http://localhost:7444'\n")
        self.run_config()
        self.assertIn("BRIDGE_DEMO='0'", self.path.read_text())
        before = self.path.read_bytes()
        self.run_config()
        self.assertEqual(self.path.read_bytes(), before)

    def test_the_published_port_and_the_address_people_open_are_the_same(self):
        """Docker publishes BRIDGE_PORT and the container always binds
        7333, so BRIDGE_PUBLIC_URL is the only thing that tells the
        server which port people reach it on. When the two disagree the
        printed link returns 403, so setup does not leave them that
        way."""
        self.path.write_text("BRIDGE_PORT=17433\n")
        self.run_config()
        self.assertIn("BRIDGE_PUBLIC_URL='http://localhost:17433'", self.path.read_text())

    def test_a_public_hostname_someone_set_is_not_rewritten(self):
        """A name that is not loopback means something terminates in
        front of this port; its address is not ours to correct."""
        self.path.write_text("BRIDGE_PORT=17433\nBRIDGE_PUBLIC_URL='https://bridge.acme.test'\n")
        self.run_config()
        self.assertIn("BRIDGE_PUBLIC_URL='https://bridge.acme.test'", self.path.read_text())

    def test_repository_and_port_update_without_losing_unrelated_settings(self):
        self.path.write_text("# retained\nGITHUB_TOKEN=existing-secret\nBRIDGE_PORT=7333\n")
        self.args.repo = "/home/developer/My Repos/platform"
        self.args.repo_name = "acme/platform"
        self.args.port = 7444
        self.run_config()
        text = self.path.read_text()
        self.assertIn("GITHUB_TOKEN=existing-secret", text)
        self.assertIn("BRIDGE_REPOS_DIR='/home/developer/My Repos'", text)
        self.assertIn("BRIDGE_PUBLIC_URL='http://localhost:7444'", text)
        self.assertEqual(text.count("BRIDGE_PORT="), 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_missing_keys_do_not_enable_optional_paid_backends(self):
        with patch.dict(os.environ, {}, clear=True), patch("builtins.input", side_effect=["", "y", "", "n", "n", "y"]), patch("getpass.getpass", return_value=""):
            self.run_config(interactive=True)
        text = self.path.read_text()
        self.assertNotIn("BRIDGE_AGENTS", text)
        self.assertNotIn("BRIDGE_MODEL_API", text)

    def test_invalid_configuration_does_not_touch_existing_file(self):
        self.path.write_text("BRIDGE_PORT=7333\n")
        self.args.port = 70000
        with self.assertRaises(ValueError):
            self.run_config()
        self.assertEqual(self.path.read_text(), "BRIDGE_PORT=7333\n")

    def test_multiline_credentials_are_rejected_without_writing(self):
        self.path.write_text("# original\n")
        with self.assertRaises(ValueError):
            save_settings(self.path, {"GITHUB_TOKEN": "value\nBRIDGE_DEMO=0"})
        self.assertEqual(self.path.read_text(), "# original\n")

    def test_cancelled_wizard_does_not_save_partial_credentials(self):
        with patch("builtins.input", side_effect=["", "y", KeyboardInterrupt]), patch("getpass.getpass", return_value="secret"):
            with self.assertRaises(KeyboardInterrupt):
                self.run_config(interactive=True)
        self.assertFalse(self.path.exists())

    def test_anthropic_workspace_prompt_saves_explicit_selection_without_logging_key(self):
        key = "synthetic-api-key-do-not-log"
        with patch.dict(os.environ, {}, clear=True), \
                patch("builtins.input", side_effect=["", "y", "  wrkspc_fixture_selected  ", "n", "n", "n"]) as prompt, \
                patch("getpass.getpass", return_value=key) as secret, \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            configure(self.args, self.path, interactive=True)
        settings = read_settings(self.path)
        self.assertEqual(settings["ANTHROPIC_WORKSPACE_ID"], "wrkspc_fixture_selected")
        self.assertEqual(settings["ANTHROPIC_API_KEY"], key)
        self.assertEqual(settings["BRIDGE_MODEL_API"], "anthropic")
        self.assertIn("ANTHROPIC_WORKSPACE_ID='wrkspc_fixture_selected'", self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        secret.assert_called_once_with("Anthropic API key: ")
        workspace_prompt = prompt.call_args_list[2].args[0]
        self.assertIn("Anthropic workspace ID", workspace_prompt)
        self.assertIn("optional", workspace_prompt)
        self.assertIn("multi-workspace", workspace_prompt)
        self.assertIn("Enter to keep/skip", workspace_prompt)
        self.assertNotIn(key, output.getvalue() + errors.getvalue() + str(prompt.call_args_list) + str(secret.call_args_list))

    def test_blank_anthropic_workspace_stays_unset(self):
        for blank in ("", "   "):
            with self.subTest(blank=blank), patch.dict(os.environ, {}, clear=True), \
                    patch("builtins.input", side_effect=["", "y", blank, "n", "n", "n"]), \
                    patch("getpass.getpass", return_value="synthetic-api-key"):
                self.run_config(interactive=True)
                self.assertNotIn("ANTHROPIC_WORKSPACE_ID", read_settings(self.path))
                self.assertEqual(read_settings(self.path)["BRIDGE_MODEL_API"], "anthropic")

    def test_blank_anthropic_workspace_keeps_saved_key_and_selection(self):
        original = "# retained\nANTHROPIC_API_KEY='synthetic-saved-key'\nANTHROPIC_WORKSPACE_ID='wrkspc_fixture_saved'\n"
        for blank in ("", "   "):
            self.path.write_text(original)
            with self.subTest(blank=blank), patch.dict(os.environ, {}, clear=True), \
                    patch("builtins.input", side_effect=["", "y", blank, "n", "n", "n"]), \
                    patch("getpass.getpass", return_value=""):
                self.run_config(interactive=True)
                self.assertTrue(self.path.read_text().startswith(original))

    def test_workspace_can_be_updated_without_reentering_saved_key(self):
        self.path.write_text("ANTHROPIC_API_KEY='synthetic-saved-key'\nANTHROPIC_WORKSPACE_ID='wrkspc_fixture_old'\nBRIDGE_MODEL_API='anthropic'\n")
        with patch.dict(os.environ, {}, clear=True), \
                patch("builtins.input", side_effect=["", "y", "wrkspc_fixture_new", "n", "n", "n"]), \
                patch("getpass.getpass", return_value=""):
            self.run_config(interactive=True)
        settings = read_settings(self.path)
        self.assertEqual(settings["ANTHROPIC_API_KEY"], "synthetic-saved-key")
        self.assertEqual(settings["ANTHROPIC_WORKSPACE_ID"], "wrkspc_fixture_new")
        self.assertEqual(settings["BRIDGE_MODEL_API"], "anthropic")
        self.assertEqual(self.path.read_text().count("ANTHROPIC_WORKSPACE_ID="), 1)

    def test_workspace_alone_does_not_enable_inference(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("builtins.input", side_effect=["", "y", "wrkspc_fixture_only", "n", "n", "n"]), \
                patch("getpass.getpass", return_value=""):
            self.run_config(interactive=True)
        settings = read_settings(self.path)
        self.assertEqual(settings["ANTHROPIC_WORKSPACE_ID"], "wrkspc_fixture_only")
        self.assertNotIn("ANTHROPIC_API_KEY", settings)
        self.assertNotIn("BRIDGE_MODEL_API", settings)

    def test_blank_workspace_keeps_runtime_selection_without_copying_environment(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "synthetic-runtime-key",
                                     "ANTHROPIC_WORKSPACE_ID": "wrkspc_fixture_runtime"}, clear=True), \
                patch("builtins.input", side_effect=["", "y", "", "n", "n", "n"]), \
                patch("getpass.getpass", return_value=""):
            self.run_config(interactive=True)
            self.assertEqual(os.environ["ANTHROPIC_WORKSPACE_ID"], "wrkspc_fixture_runtime")
        settings = read_settings(self.path)
        self.assertNotIn("ANTHROPIC_WORKSPACE_ID", settings)
        self.assertNotIn("ANTHROPIC_API_KEY", settings)
        self.assertEqual(settings["BRIDGE_MODEL_API"], "anthropic")

    def test_skipped_and_noninteractive_setup_preserve_anthropic_settings(self):
        original = "ANTHROPIC_API_KEY='synthetic-saved-key'\nANTHROPIC_WORKSPACE_ID='wrkspc_fixture_saved'\nBRIDGE_MODEL_API='anthropic'\n"
        for interactive in (False, True):
            self.path.write_text(original)
            with self.subTest(interactive=interactive), \
                    patch("builtins.input", side_effect=["", "n", "n", "n", "n"]) as prompt, \
                    patch("getpass.getpass") as secret:
                self.run_config(interactive=interactive)
                self.assertTrue(self.path.read_text().startswith(original))
                secret.assert_not_called()
                if not interactive:
                    prompt.assert_not_called()

    def test_cancelled_workspace_prompt_preserves_existing_file(self):
        original = "# retained\nANTHROPIC_API_KEY='synthetic-saved-key'\nANTHROPIC_WORKSPACE_ID='wrkspc_fixture_saved'\n"
        for error in (EOFError, KeyboardInterrupt):
            for after_selection in (False, True):
                self.path.write_text(original)
                answers = ["", "y"] + (["wrkspc_fixture_new"] if after_selection else []) + [error]
                with self.subTest(error=error, after_selection=after_selection), \
                        patch("builtins.input", side_effect=answers), \
                        patch("getpass.getpass", return_value="synthetic-new-key"):
                    with self.assertRaises(error):
                        self.run_config(interactive=True)
                    self.assertEqual(self.path.read_text(), original)

    def test_demo_requires_explicit_flag(self):
        self.run_config()
        self.assertIn("BRIDGE_DEMO='0'", self.path.read_text())
        self.args.demo = True
        self.run_config()
        self.assertIn("BRIDGE_DEMO='1'", self.path.read_text())

    def test_selected_clients_preserve_unrelated_servers_and_have_no_secret(self):
        project = Path(self.directory.name) / "project"
        project.mkdir()
        (project / ".mcp.json").write_text('{"mcpServers":{"other":{"command":"other"}}}')
        cursor = project / ".cursor/mcp.json"
        cursor.parent.mkdir()
        cursor.write_text('{"mcpServers":{"other":{"command":"other"}}}')
        codex = project / ".codex/config.toml"
        codex.parent.mkdir()
        codex.write_text('[other]\nvalue = 3\n\n[mcp_servers.bridge]\ncommand = "old"\n')
        for _ in range(2):
            _mcp_clients(project, "http://localhost:7333/mcp", "test-agent-secret", ["claude", "cursor", "codex"])
        import json
        for path in (project / ".mcp.json", cursor):
            config = json.loads(path.read_text())["mcpServers"]
            self.assertEqual(config["other"]["command"], "other")
            self.assertEqual(config["bridge"]["url"], "http://localhost:7333/mcp")
            self.assertEqual(config["bridge"]["headers"]["Authorization"], "Bearer test-agent-secret")
            self.assertNotIn("/local/bridge", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        import tomllib
        parsed = tomllib.loads(codex.read_text())
        self.assertEqual(parsed["other"]["value"], 3)
        self.assertEqual(parsed["mcp_servers"]["bridge"]["url"], "http://localhost:7333/mcp")
        self.assertEqual(parsed["mcp_servers"]["bridge"]["http_headers"]["Authorization"], "Bearer test-agent-secret")
        self.assertEqual(codex.read_text().count("[mcp_servers.bridge]"), 1)

    def test_http_mcp_config_has_no_launcher(self):
        project = Path(self.directory.name) / "project"
        _mcp_clients(project, "http://localhost:7333/mcp", "test-agent-secret", ["claude"])
        import json
        entry = json.loads((project / ".mcp.json").read_text())["mcpServers"]["bridge"]
        self.assertEqual(entry["type"], "http")
        self.assertNotIn("command", entry)

    def test_generated_mcp_credential_is_locally_git_excluded(self):
        project = Path(self.directory.name) / "project"
        project.mkdir()
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        _mcp_clients(project, "http://localhost:7333/mcp", "test-agent-secret", ["claude", "cursor", "codex"])
        excluded = (project / ".git/info/exclude").read_text()
        for filename in ("/.mcp.json", "/.cursor/mcp.json", "/.codex/config.toml"):
            self.assertIn(filename, excluded)
        status = subprocess.run(["git", "-C", str(project), "status", "--porcelain"],
                                capture_output=True, text=True, check=True)
        self.assertEqual(status.stdout, "")

    def test_tracked_mcp_configuration_is_not_given_a_token(self):
        project = Path(self.directory.name) / "project"
        project.mkdir()
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        path = project / ".mcp.json"
        path.write_text('{"mcpServers":{}}\n')
        subprocess.run(["git", "-C", str(project), "add", ".mcp.json"], check=True)
        with self.assertRaisesRegex(ValueError, "tracked file"):
            _mcp_clients(project, "http://localhost:7333/mcp", "test-agent-secret", ["claude"])
        self.assertNotIn("test-agent-secret", path.read_text())

    def test_github_connection_no_longer_asks_for_a_token_or_repository(self):
        with patch.dict(os.environ, {}, clear=True), patch("builtins.input", side_effect=["", "n", "n", "n", "n"]), patch("getpass.getpass", return_value="") as secret:
            self.run_config(interactive=True)
        self.assertFalse(secret.called)
        self.assertNotIn("GITHUB_TOKEN", self.path.read_text())
        self.assertNotIn("BRIDGE_GITHUB_REPOS", self.path.read_text())


class StartupIngestTests(unittest.TestCase):
    """Measured on a fresh install: a checkout the container could not
    read put Raven in a restart loop, with the reason only in the logs."""

    def test_an_unreadable_checkout_starts_bridge_without_it_and_readiness_says_so(self):
        from bridge import bootstrap
        from bridge.store import Store
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "bridge.db"))
            self.addCleanup(store.graph.close)
            broken = Path(tmp) / "repos" / "acme"
            (broken / ".git").mkdir(parents=True)
            with contextlib.redirect_stdout(io.StringIO()) as said:
                self.assertFalse(bootstrap._ingest_configured(store, broken, "acme/platform"))
            self.assertIn("could not ingest acme/platform", said.getvalue())
            finding = [f for f in store.readiness() if f["key"] == "ingest_failed"]
            self.assertEqual(len(finding), 1)
            self.assertIn("acme/platform", finding[0]["what"])
            # A checkout that reads clears it.
            good = Path(tmp) / "repos" / "good"
            good.mkdir(parents=True)
            env = {**os.environ, "GIT_AUTHOR_NAME": "A Person", "GIT_AUTHOR_EMAIL": "a@example.test",
                   "GIT_COMMITTER_NAME": "A Person", "GIT_COMMITTER_EMAIL": "a@example.test"}
            for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "start"]):
                subprocess.run(["git", "-C", str(good), *args], check=True, env=env, capture_output=True)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(bootstrap._ingest_configured(store, good, "acme/good"))
            self.assertFalse([f for f in store.readiness() if f["key"] == "ingest_failed"])
