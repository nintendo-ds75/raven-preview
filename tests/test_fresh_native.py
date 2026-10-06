"""The documented native install starts from no workspace or accounts."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen

from bridge.server import make_server
from bridge.store import Invalid, Store


class FreshNativeTests(unittest.TestCase):
    def test_empty_local_install_reaches_inbox_without_impossible_signup(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "new.db")
            server = make_server(store, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = "http://127.0.0.1:" + str(server.server_port)
                with urlopen(base + "/api/state") as response:
                    self.assertEqual(response.status, 200)
                    self.assertIsInstance(json.load(response), dict)
                with urlopen(base + "/") as response:
                    self.assertEqual(response.url, base + "/")
                    self.assertEqual(response.status, 200)
                self.assertEqual(store.graph.db.execute("SELECT count(*) AS n FROM account_passwords").fetchone()["n"], 0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)
                store.graph.close()

    def test_native_local_mode_cannot_bind_publicly(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "new.db")
            try:
                with self.assertRaises(Invalid):
                    make_server(store, 0, host="0.0.0.0")
            finally:
                store.graph.close()
