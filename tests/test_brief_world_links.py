"""The world tests/brief-browser.cjs drives hands the browser suite a
task link for each person it opens the page as. A fresh workspace
notifies only, and the world's address is plain http, so without the
conversational opt-in every DM failed and two of the links were empty."""

import http.server
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase


class BriefWorldLinkTests(OfflineCase):
    def test_every_person_the_suite_opens_the_page_as_has_a_link(self):
        # The world is written as its own process: when imported it sets the
        # environment (a GitHub client id, model switches) and silences the
        # HTTP access log for every handler. Keep both out of the modules
        # that run after this one.
        with patch.dict(os.environ), patch.object(http.server.BaseHTTPRequestHandler, "log_message"):
            import brief_browser_server as world
            w = world.World(Path(self.temp.name) / "world.db")
        self.addCleanup(w.server.server_close)
        state = w.state()
        for key in ("priya", "rafael", "zoe", "val", "ada", "expired"):
            self.assertTrue(state["links"][key], f"{key} has no task link")
        self.assertFalse(w.store.delivery.list("failed"), w.store.delivery.list("failed"))


if __name__ == "__main__":
    unittest.main()
