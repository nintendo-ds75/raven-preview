"""The graph layer, the hybrid memory search, and the inbox integration of
the ladder (ask): kinds, dedupe against pending questions, and the
protocol surface."""

import json
import unittest
from pathlib import Path

from fixtures import OfflineCase

from bridge.config import Config
from bridge.ladder import ask
from bridge.mcp import dispatch
from bridge.store import Invalid, Store
from test_ladder import seed_changes


class GraphCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "graph.db")
        self.graph = self.store.graph
        self.owner = self.store.add_owner({"name": "Priya Natarajan", "team": "Billing", "patterns": "billing/*,metering/*"})
        self.run = self.store.add_run({"title": "Add usage pricing", "agent": "eval", "repo": "acme/platform"})

    def signed(self, question, answer, rationale="", path="billing/x.py"):
        d = self.store.request({"run_id": self.run["id"], "question": question, "context": "c", "path": path})
        self.store.answer(d["id"], {"answer": answer, "rationale": rationale or "r"})
        return d["id"]


class MigrationTests(GraphCase):
    def test_migration_is_additive_and_idempotent(self):
        cols = {r["name"] for r in self.graph.db.execute("PRAGMA table_info(decisions)")}
        for name in ("kind", "category", "source", "evidence", "owner_evidence", "superseded_by", "supersedes",
                     "options", "repo", "embedding"):
            self.assertIn(name, cols)
        tables = {r["name"] for r in self.graph.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for name in ("engineers", "artifacts", "intents", "ownership", "connector_sources"):
            self.assertIn(name, tables)
        Store(self.store.path)  # reopening runs the migration again without error
        self.assertEqual(self.store.state()["graph"]["intents"], 0)
        indexes = {r["name"] for r in self.graph.db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        for name in ("events_decision", "events_run", "ownership_key"):
            self.assertIn(name, indexes)

    def test_prefix_queries_match_the_prefix_and_nothing_beside_it(self):
        """changes_under's range on (repo, path) covers exactly the paths
        under the prefix: a sibling directory sharing its characters is
        outside it. get_decision(exact=True) never expands a prefix."""
        g = self.graph
        g.add_change("qemu", "a1", "2026-01-01T00:00:00+00:00", ["hw/riscv/virt.c"], [("Ana", "a@x", "author")])
        g.add_change("qemu", "b2", "2026-01-02T00:00:00+00:00", ["hw/riscv-extra/foo.c"], [("Bo", "b@x", "author")])
        self.assertEqual({r["sha"] for r in g.changes_under("qemu", "hw/riscv/")}, {"a1"})
        self.assertEqual({r["sha"] for r in g.changes_under("qemu", "hw/riscv")}, {"a1", "b2"})
        did = self.signed("Should test traffic be billed?", "No")
        self.assertEqual(g.get_decision(did[:6]).id, did)
        self.assertIsNone(g.get_decision(did[:6], exact=True))
        self.assertEqual(g.get_decision(did, exact=True).id, did)

    def test_transaction_rolls_back_and_never_nests(self):
        g = self.graph
        with self.assertRaises(RuntimeError):
            with g.transaction():
                g.upsert_artifact("platform", "a.py")
                raise RuntimeError("boom")
        self.assertEqual(g.artifact_paths("platform"), [])
        self.assertFalse(g.db.in_transaction)
        with g.transaction():
            g.upsert_artifact("platform", "a.py")
            with g.transaction():  # a no-op inside the outer transaction
                g.upsert_artifact("platform", "b.py")
            self.assertTrue(g.db.in_transaction)
        self.assertFalse(g.db.in_transaction)
        self.assertEqual(sorted(g.artifact_paths("platform")), ["a.py", "b.py"])
        # An error that already ended the transaction (a full disk does
        # that) surfaces itself, not a failed rollback.
        with self.assertRaises(RuntimeError):
            with g.transaction():
                g.upsert_artifact("platform", "c.py")
                g.db.execute("ROLLBACK")
                raise RuntimeError("disk full")
        self.assertFalse(g.db.in_transaction)
        self.assertEqual(sorted(g.artifact_paths("platform")), ["a.py", "b.py"])

    def test_close_leaves_no_wal_behind_the_freshness_probe(self):
        """The memo's freshness probe opens its own connection; close()
        takes it down too, so no WAL or shm file outlives the graph and
        a copy over the file reopens clean (the bench reuses one path)."""
        import shutil
        from bridge.store import Store
        base = Path(self.temp.name) / "base.db"
        Store(base).graph.close()
        path = Path(self.temp.name) / "reused.db"
        g = Store(path).graph
        g.upsert_artifact("platform", "a.py")
        g.data_version()
        g.close()
        self.assertFalse(path.with_name(path.name + "-wal").exists())
        self.assertFalse(path.with_name(path.name + "-shm").exists())
        shutil.copyfile(base, path)
        again = Store(path).graph
        self.assertEqual(again.artifact_paths("platform"), [])
        again.close()

    def test_legacy_rows_keep_working(self):
        """A decision row created by the inbox before the columns existed
        reads back through the graph with defaults."""
        did = self.signed("Should test traffic be billed?", "No")
        d = self.graph.get_decision(did)
        self.assertEqual(d.status, "approved")
        self.assertEqual(d.answer, "No")
        self.assertEqual(d.owner, "Priya Natarajan")
        # The repository identity is the owner and the name, never the
        # name alone: other-company/platform is another repository.
        self.assertEqual(d.repo, "acme/platform")
        self.assertEqual(d.superseded_by, "")
        self.assertNotIn("embedding", self.store.get_decision(did))

    def test_ownership_validity_windows(self):
        g = self.graph
        g.set_ownership("platform", "billing/", "Priya", "blame", 0.5, "50%")
        g.set_ownership("platform", "billing/", "Priya", "blame", 0.55, "55%")  # drift, in place
        self.assertEqual(len(g.ownership_for("platform", "billing/usage.py")), 1)
        g.set_ownership("platform", "billing/", "Priya", "blame", 0.9, "90%")  # material change
        live = g.ownership_for("platform", "billing/usage.py")
        self.assertEqual(len(live), 1)
        self.assertAlmostEqual(live[0]["weight"], 0.9)
        closed = g.db.execute("SELECT count(*) c FROM ownership WHERE valid_to IS NOT NULL").fetchone()["c"]
        self.assertEqual(closed, 1)
        self.assertEqual(g.count_events("ownership_invalidated"), 1)


class MemorySearchTests(GraphCase):
    def test_stems_and_fields_are_searched(self):
        self.signed("Should invoices be rounded to whole cents?",
                    "Compute in integer cents; round half-up at the line-item level, never at the invoice total.")
        for q in ("invoice rounding", "rounding", "half-up per line item", "integer cents invoice total"):
            self.assertTrue(self.graph.memory_search(q), q)
        self.assertEqual(self.graph.memory_search("service token lifetime"), [])

    def test_search_excludes_pending_and_superseded(self):
        old = self.signed("How long do we keep raw metering events?", "90 days.")
        pending = self.store.request({"run_id": self.run["id"], "question": "How long do we keep raw metering logs?",
                                      "context": "c", "path": "billing/y.py"})
        new = self.store.request({"run_id": self.run["id"], "question": "What is the retention period for raw metering events?",
                                  "context": "c", "path": "billing/z.py"})
        self.store.answer(new["id"], {"answer": "30 days raw; 13 months aggregates.", "rationale": "PR-97",
                                      "supersedes": old})
        ids = [m["id"] for m in self.graph.memory_search("raw metering events retention")]
        self.assertIn(new["id"], ids)
        self.assertNotIn(old, ids)
        self.assertNotIn(pending["id"], ids)
        self.assertEqual(self.store.get_decision(old)["superseded_by"], new["id"])
        self.assertEqual(self.store.get_decision(new["id"])["supersedes"], old)
        with self.assertRaises(Invalid):
            self.store.answer(new["id"], {"answer": "x", "rationale": "y", "supersedes": "nope"})

    def test_correction_supersedes_and_prediction_is_cleared(self):
        prior = self.signed("Should test traffic be billed?", "Exclude tagged traffic")
        later = self.store.request({"run_id": self.run["id"], "question": "Should test traffic be billed?",
                                    "context": "c", "path": "billing/usage.py"})
        self.assertEqual(later["prediction"], "Exclude tagged traffic")
        self.assertEqual(later["kind"], "prediction")
        self.assertIsNone(self.store.get_decision(prior)["prediction"])
        self.store.answer(prior, {"answer": "Exclude only verified internal tests"})
        self.assertEqual(self.graph.memory_search("test traffic billed")[0]["answer"],
                         "Exclude only verified internal tests")

    def test_recency_breaks_more_than_exact_ties(self):
        old = self.signed("Should refunds for duplicate charges be automatic?", "Yes, automatic within 24 hours.")
        new = self.signed("Should duplicate charge refunds be automatic under 500 dollars?",
                          "Yes under 500 dollars; manual review above.")
        self.graph.db.execute("UPDATE decisions SET updated_at='2025-09-01T00:00:00+00:00' WHERE id=?", (old,))
        top = self.graph.memory_search("automatic refunds for duplicate charges")[0]
        self.assertEqual(top["id"], new)


    def test_same_subject_newer_record_outranks_an_older_exact_match(self):
        """A superseded rule and its replacement rarely share the same
        question text; the newer one still ranks first."""
        old = self.signed("How long do we keep raw metering events?", "90 days.")
        new = self.signed("What is the retention period for raw metering events?", "30 days raw; 13 months aggregates.")
        self.graph.db.execute("UPDATE decisions SET updated_at='2026-02-01T12:00:00+00:00' WHERE id=?", (old,))
        self.graph.db.execute("UPDATE decisions SET updated_at='2026-06-10T12:00:00+00:00' WHERE id=?", (new,))
        hits = self.graph.memory_search("How long do we keep raw metering events?")
        self.assertEqual([m["id"] for m in hits[:2]], [new, old])
        self.assertEqual(hits[0]["demoted_by"], "")
        self.assertEqual(hits[1]["demoted_by"], new)

    def test_superseded_by_newer_is_never_suggested_to_the_inbox(self):
        """An open ticket asking to extend a retired setting must not carry
        the retired answer as its suggestion; the newer same-subject answer
        demotes it out of the suggestion pool."""
        owner = self.store.add_owner({"name": "Dana Okafor", "team": "Auth", "patterns": "auth/*"})
        old = self.signed("How long should service tokens live?", "Service tokens expire after 7 days.", path="auth/tokens.py")
        new = self.signed("What is the maximum service token lifetime?",
                          "24 hours. Security baseline SB-3 caps service token lifetime at 24 hours; the 7-day setting is retired.",
                          path="auth/tokens.py")
        self.graph.db.execute("UPDATE decisions SET updated_at='2026-04-01T12:00:00+00:00' WHERE id=?", (old,))
        self.graph.db.execute("UPDATE decisions SET updated_at='2026-05-05T12:00:00+00:00' WHERE id=?", (new,))
        row = ask(self.store, Config(), self.run["id"],
                  "Extend service token TTL to 7 days for the legacy payment integrations?",
                  context="c", path="auth/tokens.py", owner_id=owner["id"])
        self.assertNotEqual(row.get("source_id"), old)
        self.assertNotIn("7 days.", row.get("prediction") or "")


class AskTests(GraphCase):
    def test_record_is_context_and_blocks_the_run_until_answered(self):
        """Without an answerability check, retrieval supplies context to
        the owner rather than silently treating a record body as an answer."""
        g = self.graph
        g.upsert_intent("platform", "pr", "88", "Enforce 24h max service token TTL in auth/tokens.py",
                        "Security baseline SB-3 caps service token lifetime at 24 hours; the 7-day setting is retired.",
                        "Dana Okafor", "2026-05-12T00:00:00+00:00")
        row = ask(self.store, Config(), self.run["id"], "What is the maximum lifetime of a service token?",
                  context="c", path="auth/tokens.py")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["kind"], "new")
        self.assertFalse(row["answer"])
        self.assertIn("24 hours", row["evidence"])
        self.assertIn("pr 88", row["evidence"])
        self.assertTrue(row["approval_pending"])
        self.assertFalse(row["authorized"])
        with self.assertRaises(Invalid) as refused:
            self.store.update_run(self.run["id"], {"status": "completed"})
        self.assertIn("still waits on a person", str(refused.exception))

    def test_new_judgment_routes_by_graph_then_patterns(self):
        row = ask(self.store, Config(), self.run["id"], "Should we bill customer-run load tests?",
                  context="c", path="billing/usage.py")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["kind"], "new")
        self.assertEqual(row["owner_name"], "Priya Natarajan")
        # A configured owner is verified authority for their patterns: the
        # route says so, tree or no tree.
        self.assertIn("verified: Priya Natarajan decides for billing/*", row["routing_reason"])
        self.assertEqual(self.graph.get_task(self.run["id"])["status"], "needs_judgment")
        seed_changes(self.graph, "platform", [("auth/tokens.py", "Dana Okafor", "author", i) for i in range(10)])
        row2 = ask(self.store, Config(), self.run["id"], "Should service tokens rotate weekly?",
                   context="c", path="auth/tokens.py")
        self.assertEqual(row2["owner_name"], "Dana Okafor")
        self.assertIn("ownership graph", row2["routing_reason"])

    def test_a_routing_reason_is_whole_or_cut_at_a_word_and_marked(self):
        """Measured live on eb9d22d: 26 stored routing reasons ended inside
        a word ("where a more sp", "26 mon"), with no mark."""
        priya = self.graph.find_person("Priya Natarajan")
        note = ("Configured for the pricing review; the evaluation seeds this authority on purpose and it is not "
                "the upstream project's maintainer authority, so read it as a stand-in for the owner of usage "
                "pricing decisions across billing, metering and the invoice exports that depend on them")
        self.graph.add_authority("path", "billing/usage.py", "decides", person_id=priya["id"],
                                 asserted_by="Evaluation Admin", note=note)
        row = ask(self.store, Config(), self.run["id"], "Should we bill customer-run load tests?",
                  context="c", path="billing/usage.py")
        self.assertEqual(row["owner_name"], "Priya Natarajan")
        prefix = "Routed from the ownership graph: "
        reason, evidence = row["routing_reason"], " ".join((row["owner_evidence"] or "").split())
        self.assertGreater(len(evidence), 300)
        self.assertTrue(reason.startswith(prefix), reason)
        self.assertTrue(reason.endswith(" … [cut: the owner evidence has the rest]"), reason[-80:])
        kept = reason[len(prefix):].split(" … [cut:")[0]
        self.assertTrue(evidence.startswith(kept), kept[-60:])
        self.assertEqual(evidence[len(kept)], " ", f"cut inside a word: {kept[-30:]!r}")

    def test_suggestions_need_a_real_match_and_never_answer_who_or_why(self):
        """The inbox fallback attaches a signed answer as a suggestion only
        above the ladder's memory floor, from any signer, and never to a
        retrospective question, whose answer is a name and a reason."""
        self.signed("Should automatic refunds for duplicate charges be paused?",
                    "Yes. Pause automatic refunds; route every duplicate-charge refund to manual review.",
                    rationale="The March double-refund incident refunded 212 customers twice.", path="billing/refunds.py")
        batch = self.signed("What batch size should the metering exporter use?", "500 rows per request.", path="metering/exporter.py")
        # Old enough that the ladder's decayed memory score falls under its
        # floor, so the question reaches the inbox fallback.
        self.graph.db.execute("UPDATE decisions SET updated_at='2026-01-15T12:00:00+00:00' WHERE id=?", (batch,))
        other = self.store.add_owner({"name": "Yuki Tanaka", "team": "Data", "patterns": "exports/*"})
        far = ask(self.store, Config(), self.run["id"], "A customer was double-charged $320 yesterday. Is that refunded automatically?",
                  context="c", path="billing/refunds.py")
        self.assertIsNone(far["prediction"])
        retro = ask(self.store, Config(), self.run["id"], "Who decided to pause automatic refunds for duplicate charges, and why?",
                    context="c", path="billing/refunds.py")
        self.assertIsNone(retro["prediction"])
        self.assertEqual(retro["category"], "data-source")
        near = ask(self.store, Config(), self.run["id"], "What batch size is the metering exporter supposed to use?",
                   context="c", path="exports/batch.py", owner_id=other["id"])
        self.assertEqual(near["prediction"], "500 rows per request.")
        self.assertEqual(near["kind"], "prediction")

    def test_pending_question_is_never_asked_twice(self):
        first = ask(self.store, Config(), self.run["id"], "Under usage-based pricing, do we apply per-seat minimums?",
                    context="c", path="billing/pricing.py")
        again = ask(self.store, Config(), self.run["id"], "Do we apply per-seat minimums under usage-based pricing?",
                    context="c", path="billing/pricing.py")
        self.assertEqual(again["id"], first["id"])
        self.assertIn("already open", again["note"])
        pending = [d for d in self.store.state()["decisions"] if d["status"] == "pending"]
        self.assertEqual(len(pending), 1)
        tree = json.loads(dispatch(self.store, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "bridge_get_tree", "arguments": {"task_id": self.run["id"]}}})["result"]["content"][0]["text"])
        self.assertEqual([n["node_id"] for n in tree["nodes"] if n["status"] == "pending"], [first["id"]])
        self.assertEqual([n["duplicate_of"] for n in tree["nodes"] if n["status"] == "duplicate"], [first["id"]])
        self.assertEqual(tree["counts"]["pending"], 1)

    def test_mcp_exposes_the_canvas_tools_and_no_approval(self):
        names = [t["name"] for t in dispatch(self.store, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
        for name in ("bridge_add_node", "bridge_get_tree", "bridge_ingest_repo", "bridge_search_decisions"):
            self.assertIn(name, names)
        self.assertFalse(any("approve" in n for n in names))
        reply = dispatch(self.store, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "bridge_add_node", "arguments": {"task_id": self.run["id"], "question": "Should we bill customer-run load tests?",
                                                     "paths": "billing/usage.py"}}})
        body = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(body["kind"], "new")
        self.assertEqual(body["owner"], "Priya Natarajan")

    def test_ingest_tool_refuses_a_non_repo(self):
        reply = dispatch(self.store, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "bridge_ingest_repo", "arguments": {"path": self.temp.name}}})
        self.assertTrue(reply["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
