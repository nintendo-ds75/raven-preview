"""Bounded retrieval and paginated lists: above the whole-scan cutoff a
question scores a bounded candidate set and still finds the decision it
shares words with; every list the inbox reads pages on the server."""

import json
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen

from fixtures import OfflineCase

from bridge import graph as graph_mod
from bridge.llm import embed
from fixtures import ready_server as make_server
from bridge.store import Store

REPO = "acme/platform"


class BoundedRetrievalTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "scale.db")
        self.graph = self.store.graph
        self.run = self.store.add_run({"title": "seed", "agent": "test", "repo": REPO})
        self.addCleanup(setattr, graph_mod, "FULL_SCAN_MAX", graph_mod.FULL_SCAN_MAX)

    def seed(self, n):
        with self.graph.transaction():
            for i in range(n):
                q = f"Should exports batch {i} rows for the nightly report number {i}?"
                self.graph.db.execute(
                    "INSERT INTO decisions(id, run_id, question, context, path, routing_reason, status, answer, rationale, "
                    "answered_by, created_at, updated_at, kind, repo, signoff, signed_by, embedding) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"seed{i:06d}", self.run["id"], q, "", "exports/x.py", "seed", "approved", f"{i} rows", "seeded",
                     "Seed Owner", f"2026-01-01T00:00:{i % 60:02d}+00:00", f"2026-01-01T00:00:{i % 60:02d}+00:00",
                     "evidence", REPO, "signed", "Seed Owner", graph_mod._f32blob(embed(q))))
            needle = "Which rate applies to Globex usage above the included quota on the enterprise plan?"
            self.graph.db.execute(
                "INSERT INTO decisions(id, run_id, question, context, path, routing_reason, status, answer, rationale, "
                "answered_by, created_at, updated_at, kind, repo, signoff, signed_by, embedding) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("needle000001", self.run["id"], needle, "", "billing/rates.py", "seed", "approved", "The enterprise rate card",
                 "seeded", "Priya Natarajan", "2025-06-01T00:00:00+00:00", "2025-06-01T00:00:00+00:00", "evidence", REPO,
                 "signed", "Priya Natarajan", graph_mod._f32blob(embed(needle))))

    def test_a_bounded_pass_finds_what_the_question_shares_words_with(self):
        self.seed(120)
        q = "Which rate applies to Initech usage above the included quota on the enterprise plan?"
        whole = self.graph.similar_answered(embed(q), top_k=1, min_score=0.0, repo=REPO, query=q)
        self.assertEqual(whole[0][1].id, "needle000001")
        graph_mod.FULL_SCAN_MAX = 50
        graph_mod.CANDIDATE_RECENT = 20
        try:
            bounded = self.graph.similar_answered(embed(q), top_k=1, min_score=0.0, repo=REPO, query=q)
            self.assertEqual(bounded[0][1].id, "needle000001")
            rows = self.graph._bounded_rows(graph_mod.MEMORY_STATUSES, REPO, q)
            self.assertLess(len(rows), 121)
            if self.graph.has_fts:
                self.assertTrue(any(r["id"] == "needle000001" for r in rows))
            found = self.graph.memory_search(q, limit=3, repo=REPO)
            self.assertEqual(found[0]["id"], "needle000001")
            # No query at all: the newest rows bound the pass, and the pass still works.
            recent = self.graph.similar_answered(embed(q), top_k=1, min_score=0.0, repo=REPO)
            self.assertEqual(len(recent), 1)
        finally:
            graph_mod.CANDIDATE_RECENT = 500


class PaginationTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "pages.db")
        self.store.add_owner({"name": "Priya Natarajan", "team": "Billing", "patterns": "billing/*"})
        self.runs = [self.store.add_run({"title": f"task {i}", "agent": "test", "repo": REPO}) for i in range(7)]
        for i, run in enumerate(self.runs):
            self.store.request({"run_id": run["id"], "question": f"Question {i} about billing?", "context": "c",
                                "path": "billing/x.py"})

    def test_lists_page_on_the_server(self):
        first = self.store.list_decisions(page=1, size=3)
        self.assertEqual((first["total"], len(first["items"]), first["page"]), (7, 3, 1))
        third = self.store.list_decisions(page=3, size=3)
        self.assertEqual(len(third["items"]), 1)
        self.assertEqual(self.store.list_decisions(page=1, size=50, q="Question 4")["total"], 1)
        self.assertEqual(self.store.list_decisions(page=1, size=50, status="approved")["total"], 0)
        self.assertEqual(self.store.list_decisions(page=1, size=50, run_id=self.runs[0]["id"])["total"], 1)
        runs = self.store.list_runs(page=2, size=5)
        self.assertEqual((runs["total"], len(runs["items"])), (7, 2))
        self.assertEqual(self.store.list_runs(page=1, size=5, status="needs_judgment")["total"], 7)
        server = make_server(self.store, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))
        url = f"http://127.0.0.1:{server.server_port}"
        with urlopen(url + "/api/decisions?page=2&size=4&q=billing") as resp:
            page = json.load(resp)
        self.assertEqual((page["total"], len(page["items"]), page["page"]), (7, 3, 2))
        with urlopen(url + "/api/runs?size=2") as resp:
            self.assertEqual(len(json.load(resp)["items"]), 2)


if __name__ == "__main__":
    unittest.main()
