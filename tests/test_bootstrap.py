"""The development login and the headless workspace setup in
bridge/bootstrap.py: a credential is handed out only while the workspace
is unclaimed and the switch is on, and a workspace is created headlessly
only on an operator's or a local admin's authority."""

import os
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase

from bridge import bootstrap
from bridge.auth import Auth
from bridge.store import Store

NO_CREDENTIAL = "No valid development credential. Run ./dev up to initialize the workspace."


class DevLoginTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "dev.db")
        self.addCleanup(self.store.graph.close)
        self.token_file = Path(self.temp.name) / "dev-login-token"
        env = patch.dict(os.environ, {"BRIDGE_LOGIN_FILE": str(self.token_file)})
        env.start()
        self.addCleanup(env.stop)

    def human_token(self):
        with self.store.graph.transaction():
            pid = self.store.graph.add_person("Local developer", email="developer@bridge.local", role="admin")
        return Auth(self.store, enabled=True).create_token(pid, "Docker development login", kind="human")["token"]

    def test_without_a_credential_file_nothing_is_handed_out(self):
        self.assertEqual(bootstrap.dev_login(self.store), NO_CREDENTIAL)

    def test_a_token_the_database_does_not_know_is_not_handed_out(self):
        self.token_file.write_text("not-a-token\n")
        self.assertEqual(bootstrap.dev_login(self.store), NO_CREDENTIAL)

    def test_a_valid_credential_is_handed_out_with_the_setup_url(self):
        # The credential is the setup credential Create your workspace asks
        # for, so it points there, not at the sign-in form.
        token = self.human_token()
        self.token_file.write_text(token + "\n")
        said = bootstrap.dev_login(self.store)
        self.assertTrue(said.startswith(token + "\n"))
        self.assertIn("http://localhost:7333/auth/setup", said)
        with patch.dict(os.environ, {"BRIDGE_PUBLIC_URL": "https://raven.acme.test/"}):
            self.assertIn("https://raven.acme.test/auth/setup", bootstrap.dev_login(self.store))

    def test_the_switch_turns_it_off_even_with_a_valid_credential(self):
        self.token_file.write_text(self.human_token() + "\n")
        with patch.dict(os.environ, {"BRIDGE_DEV_LOGIN": "0"}):
            self.assertEqual(bootstrap.dev_login(self.store),
                             "Development login is disabled. Sign in with your account.")

    def test_a_claimed_workspace_hands_out_nothing(self):
        self.token_file.write_text(self.human_token() + "\n")
        with self.store.graph.transaction():
            pid = self.store.graph.add_person("Owner", email="owner@acme.example")
            self.store.graph.db.execute("INSERT INTO account_passwords(person_id, password_hash) VALUES(?, ?)",
                                        (pid, "hash"))
        self.assertEqual(bootstrap.dev_login(self.store), "Workspace already claimed. Sign in with your account.")


class InitializeWorkspaceTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "ws.db")
        self.addCleanup(self.store.graph.close)

    def test_no_name_means_no_setup(self):
        bootstrap.initialize_workspace(self.store, "   ")
        self.assertEqual(self.store.graph.get_setting("workspace_name", ""), "")

    def test_headless_setup_needs_an_admin_credential(self):
        with self.assertRaisesRegex(ValueError, "BRIDGE_ADMIN_TOKEN"):
            bootstrap.initialize_workspace(self.store, "Acme")
        self.assertEqual(self.store.graph.get_setting("workspace_name", ""), "")
        self.assertEqual(self.store.graph.count_events("workspace_created"), 0)

    def test_the_operator_token_creates_the_workspace_once(self):
        with patch.dict(os.environ, {"BRIDGE_ADMIN_TOKEN": "operator-secret"}):
            bootstrap.initialize_workspace(self.store, "  Acme Billing  ")
            bootstrap.initialize_workspace(self.store, "Someone else")
        self.assertEqual(self.store.graph.get_setting("workspace_name"), "Acme Billing")
        self.assertEqual(self.store.graph.get_setting("workspace_profile_pending"), "")
        self.assertEqual(self.store.graph.count_events("workspace_created"), 1)

    def test_a_local_admin_account_is_enough_and_the_name_is_bounded(self):
        with self.store.graph.transaction():
            self.store.graph.add_person("Local developer", email="developer@bridge.local", role="admin")
        bootstrap.initialize_workspace(self.store, "x" * 120)
        self.assertEqual(self.store.graph.get_setting("workspace_name"), "x" * 100)
