"""Ingestion on a deterministic synthetic monorepo (tests/fixtures.py's
synthco): blame concentrations, a deliberately stale CODEOWNERS entry,
Reviewed-by trailers, merge commits and squashed PRs as records, bots
dropped, idempotent re-runs."""

import json
import shutil
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen

from fixtures import OfflineCase, build_synthco, run_git, synthco_env, template_db

from bridge.config import Config
from bridge.ingest import index_repo, parse_codeowners
from bridge.ladder import run_task
from bridge.mcp import dispatch
from bridge.routing import route
from fixtures import ready_server as make_server
from bridge.store import Store


class IngestTests(OfflineCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = build_synthco()

    def setUp(self):
        super().setUp()
        self.store = self.warm_store("synthco", "ingest.db")
        self.graph = self.store.graph
        self.stats = template_db("synthco")[1]

    def test_codeowners_parsing(self):
        self.assertEqual(parse_codeowners(self.repo), [("billing/", ["priya"]), ("gateway/", ["sarah"])])

    def test_ownership_map_from_repo_signals(self):
        rows = self.graph.ownership_for("synthco", "billing/meter.py")
        by = {(r["engineer"], r["source"]): r for r in rows}
        self.assertIn(("priya", "codeowners"), by)
        self.assertIn(("Priya Sharma", "blame"), by)
        self.assertGreater(by[("Priya Sharma", "blame")]["weight"], 0.6)
        self.assertIn(("Priya Sharma", "blame_recent"), by)
        reviews = [r for r in self.graph.ownership_for("synthco", "") if r["source"] == "review"]
        self.assertEqual([r["engineer"] for r in reviews], ["Alex Rivera"])
        self.assertFalse(any("dependabot" in r["engineer"] for r in self.graph.ownership_for("synthco", "gateway/app.py")))
        self.assertGreaterEqual(self.stats["bots_skipped"], 1)

    def test_stale_codeowners_loses_to_live_blame(self):
        picked = route(self.graph, "synthco", "Does the gateway rate limit apply before metering?", path="gateway/rate_limit.py")
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0], "Raj Patel")
        self.assertIn("CODEOWNERS lists", "; ".join(picked[1]))
        billing = route(self.graph, "synthco", "Which currency applies to the invoice total?", path="billing/invoice.py")
        self.assertEqual(billing[0], "Priya Sharma")

    def test_records_from_merges_and_squashed_prs(self):
        refs = {r["ref"]: r for r in self.graph.db.execute("SELECT * FROM intents WHERE repo='synthco'")}
        self.assertIn("18", refs)
        self.assertIn("31", refs)
        self.assertIn("77", refs)
        self.assertEqual(refs["77"]["kind"], "pr")
        self.assertIn("roll over", refs["77"]["body"])
        self.assertEqual(refs["18"]["author"], "Priya Sharma")

    def test_ingested_record_is_cited_context_without_answerability_check(self):
        r = run_task(self.graph, Config(), "usage pricing", repo="synthco",
                     decisions=[{"question": "What counts as one billable unit of usage?", "category": "definition"}])
        d = self.graph.decisions_for_task(r.task_id)[0]
        self.assertEqual(d.status, "pending", d.evidence)
        self.assertEqual(d.answer, "")
        self.assertIn("completed assistant response", d.evidence)
        self.assertIn("18", d.evidence)

    def test_codeowners_last_rule_wins_in_file_order(self):
        """Two CODEOWNERS rules cover gateway/app.py; the later one in the
        file wins even though it sorts earlier alphabetically."""
        from bridge.signals import listed_for
        repo = Path(self.temp.name) / "mini"
        repo.mkdir()
        run_git(repo, "init", "-q", "-b", "main")
        (repo / "gateway").mkdir()
        (repo / "gateway" / "app.py").write_text("x = 1\n")
        (repo / "CODEOWNERS").write_text("gateway/ @sarah\n/gateway/app.py @alex\n")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "mini", env=synthco_env("alex", 0))
        graph = Store(Path(self.temp.name) / "mini.db").graph
        index_repo(graph, repo)
        self.assertEqual([r["ord"] for r in graph.listings("mini")], [0, 1])
        self.assertEqual([e["person"] for e in listed_for(graph, "mini", "gateway/app.py")], ["alex"])
        self.assertEqual([e["person"] for e in listed_for(graph, "mini", "gateway/routes.py")], ["sarah"])

    def test_reingest_is_idempotent(self):
        before = self.graph.counts()
        again = index_repo(self.graph, self.repo)
        after = self.graph.counts()
        self.assertEqual(before["intents"], after["intents"])
        self.assertEqual(before["ownership"], after["ownership"])
        self.assertEqual(again["commits"], self.stats["commits"])
        self.assertIn("synthco", self.store.state()["graph"]["repos"])

    def test_ownership_is_served_on_its_own_route_not_in_the_polled_state(self):
        server = make_server(self.store, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def get(path):
            with urlopen(f"http://127.0.0.1:{server.server_port}{path}") as r:
                return json.loads(r.read())

        state = get("/api/state")
        for key in ("ownership", "revisions"):
            self.assertNotIn(key, state)
        # Answer deliveries are small and the run view shows them.
        self.assertIn("deliveries", state)
        self.assertIn("synthco", state["graph"]["repos"])
        every = self.store.ownership(limit=0)
        self.assertGreater(len(every), 3)
        rows = get("/api/ownership?repo=synthco")["ownership"]
        self.assertEqual(rows, every)
        self.assertEqual(get("/api/ownership?repo=other")["ownership"], [])
        self.assertEqual(len(get("/api/ownership?limit=3")["ownership"]), 3)
        self.assertEqual(get("/api/export")["ownership"], every)
        listed = dispatch(self.store, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "bridge_list_owners", "arguments": {"repo": "synthco"}}})
        listed = json.loads(listed["result"]["content"][0]["text"])
        self.assertEqual(listed["ownership"], every)
        self.assertEqual(listed["graph"], state["graph"])
        self.assertEqual(listed["owners"], state["owners"])

    def test_reingest_retires_renamed_paths(self):
        """A rebuild drops the artifacts of paths that no longer exist, so
        a renamed file's old path stops being routable, and forgets the
        ask-time fetch marks the rebuild superseded."""
        clone = Path(self.temp.name) / "renamed"
        shutil.copytree(self.repo, clone)
        g = Store(Path(self.temp.name) / "renamed.db").graph
        index_repo(g, clone, repo_name="synthco")
        self.assertIn("billing/pricing.py", g.artifact_paths("synthco"))
        g.mark_fetched("synthco", "tree", "all")
        run_git(clone, "mv", "billing/pricing.py", "billing/prices.py")
        run_git(clone, "commit", "-q", "-m", "billing: rename pricing to prices", env=synthco_env("priya", 60))
        index_repo(g, clone, repo_name="synthco")
        paths = g.artifact_paths("synthco")
        self.assertNotIn("billing/pricing.py", paths)
        self.assertIn("billing/prices.py", paths)
        self.assertFalse(g.was_fetched("synthco", "tree", "all"))


class MachineAuthorTests(unittest.TestCase):
    def test_coding_agents_are_not_people(self):
        from bridge.ingest import _is_machine_author
        for name in ("Claude", "Claude Code", "Codex", "OpenAI Codex", "Copilot", "dependabot[bot]"):
            self.assertTrue(_is_machine_author(name), name)
        for name in ("Codexa Smith", "Joe Codex", "Claudette Ray"):
            self.assertFalse(_is_machine_author(name), name)


if __name__ == "__main__":
    unittest.main()
