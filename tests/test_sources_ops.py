"""Operating the native context sources: the scheduler keeps going when one
source fails and honours a provider's Retry-After, starts and stops cleanly,
the report marks a source that has not synced lately as stale, and the
command line adds, runs, reports and disables a source."""
import contextlib
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import source_docs, sources
from bridge.store import Store
from test_sources_docs import PAGE, Site


class SourceOperationsTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.db = Path(self.temp.name) / "ops.db"
        self.store = Store(self.db)
        self.addCleanup(self.store.graph.close)
        self.good, self.bad = Site(), Site()
        self.addCleanup(self.good.close)
        self.addCleanup(self.bad.close)
        for site in (self.good, self.bad):
            site.page("/llms.txt", "- [Guide](/guide)\n", content_type="text/plain")
            site.page("/guide", PAGE, etag='"v1"')

    def add(self, site):
        return sources.register(self.store, "docs", "example/service", site.origin + "/", shared=True,
                                client=source_docs.HTTPClient())

    def test_one_failing_source_does_not_stop_the_others(self):
        good, bad = self.add(self.good), self.add(self.bad)
        self.bad.routes["/guide"] = (500, "down", {})
        results = sources.SourceSyncer(self.store, clients={"docs": source_docs.HTTPClient()}).tick()
        self.assertEqual([r["connection_id"] for r in results], [good["id"]])
        self.assertIn("HTTP 500", sources._row(self.store, bad["id"])["last_error"])
        self.assertTrue(sources._row(self.store, good["id"])["last_success_at"])

    def test_a_rate_limited_source_pauses_the_loop_for_retry_after(self):
        self.add(self.good)
        syncer = sources.SourceSyncer(self.store)
        failing = type("Limited", (), {"get": lambda self, url, headers=None: (_ for _ in ()).throw(
            sources.SourceError("rate limited", retry_after=42))})()
        syncer.clients = {"docs": failing}
        with patch.object(syncer._stop, "wait") as wait:
            self.assertEqual(syncer.tick(), [])
        wait.assert_called_once_with(42)

    def test_the_scheduler_starts_once_and_stops(self):
        syncer = sources.SourceSyncer(self.store, minutes=60, clients={"docs": source_docs.HTTPClient()})
        syncer.start()
        thread = syncer._thread
        syncer.start()
        self.assertIs(syncer._thread, thread)
        syncer.close()
        self.assertIsNone(syncer._thread)
        self.assertFalse(thread.is_alive())

    def test_a_source_that_has_not_synced_lately_is_stale_and_in_readiness(self):
        connection = self.add(self.good)
        sources.sync(self.store, connection["id"])
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        with self.store.graph.transaction():
            self.store.graph.db.execute("UPDATE source_connections SET last_success_at=? WHERE id=?",
                                        (old, connection["id"]))
        [entry] = sources.report(self.store)["sources"]
        self.assertEqual(entry["state"], "stale")
        self.assertTrue(any(r["key"] == "context_source_" + connection["id"] for r in sources.readiness(self.store)))

    def run_cli(self, *argv):
        out = io.StringIO()
        with patch("sys.argv", ["bridge.sources", *argv, "--db", str(self.db)]), contextlib.redirect_stdout(out):
            sources.main()
        return json.loads(out.getvalue())

    def test_the_command_line_adds_runs_reports_and_disables_a_source(self):
        with patch.dict(os.environ, {"JIRA_BASE_URL": "", "SLACK_BOT_TOKEN": ""}):
            added = self.run_cli("add", "docs", "--repo", "example/service", "--target", self.good.origin + "/",
                                 "--workspace-shared", "--max-pages", "5")
            self.assertEqual(added["options"], {"max_pages": 5})
            [ran] = self.run_cli("sync", "--id", added["id"])
            self.assertEqual(ran["records"], 2)
            report = self.run_cli("status")
            self.assertEqual(report["sources"][0]["records"], 2)
            self.assertEqual({s["kind"] for s in report["not_configured"]}, {"slack", "jira"})
            self.assertFalse(self.run_cli("disable", "--id", added["id"])["enabled"])

    def test_the_command_line_refuses_without_the_attestation(self):
        err = io.StringIO()
        with patch("sys.argv", ["bridge.sources", "add", "docs", "--repo", "example/service", "--target",
                                self.good.origin + "/", "--db", str(self.db)]), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as stopped:
                sources.main()
        self.assertEqual(stopped.exception.code, 1)
        self.assertIn("every reader", err.getvalue())
