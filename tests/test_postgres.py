"""Run the existing behavior contracts against a real PostgreSQL server.

Opt in with BRIDGE_TEST_POSTGRES=1 and DATABASE_URL. Every SQLite filename
requested by a test is mapped to a unique temporary PG schema, including
reopens and concurrent connections. Only those test schemas are removed.
"""
import importlib
import os
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bridge import database
from bridge.store import Store


class PostgresIsolation:
    def warm_store(self, fixture, name=""):
        from fixtures import BUILDERS
        from bridge.ingest import index_repo
        store = Store(Path(self.temp.name) / (name or f"{fixture}.db"))
        index_repo(store.graph, BUILDERS[fixture]())
        return store

    def setUp(self):
        import psycopg
        from psycopg import sql
        base = os.environ["DATABASE_URL"]
        admin = psycopg.connect(base, autocommit=True)
        targets, schemas, connections = {}, [], []
        lock = threading.Lock()
        original_init = Store.__init__
        self.sqlite_init = original_init
        original_connect = database.connect

        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connections.append(connection)
            return connection

        def init(store, target):
            if not database.is_postgres(target):
                key = str(Path(target).resolve())
                with lock:
                    if key not in targets:
                        schema = "bridge_test_" + uuid.uuid4().hex
                        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                        schemas.append(schema)
                        parts = urlsplit(base)
                        query = dict(parse_qsl(parts.query))
                        query["options"] = f"-csearch_path={schema}"
                        targets[key] = urlunsplit(parts._replace(query=urlencode(query)))
                    target = targets[key]
            original_init(store, target)

        def cleanup():
            for connection in connections:
                connection.close()
            for schema in schemas:
                admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            admin.close()

        self.addCleanup(cleanup)
        p = patch.object(database, "connect", connect)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(Store, "__init__", init)
        p.start()
        self.addCleanup(p.stop)
        super().setUp()


class PostgresSpecific(PostgresIsolation, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store = Store("/tmp/bridge-pg-test.db")

    def test_seed_restart_and_fulltext(self):
        self.store.seed()
        before = self.store.state()
        reopened = Store(self.store.path)
        reopened.seed()
        self.assertEqual(len(before["runs"]), len(reopened.state()["runs"]))
        self.assertEqual(len(before["decisions"]), len(reopened.state()["decisions"]))
        self.assertTrue(reopened.graph._fts_ids("decisions_fts", ["prepaid"]))
        self.assertTrue(reopened.search("unused prepaid credits")["matches"])
        self.assertEqual(reopened.graph.db.execute("SELECT version FROM schema_migrations").fetchone()[0], 1)

    def test_rollback_and_foreign_keys(self):
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.connect() as db:
                db.execute("INSERT INTO settings(key,value) VALUES(?,?)", ("must-rollback", "yes"))
                db.execute("INSERT INTO decision_revisions(id,decision_id,answer,rationale,responder,provenance,created_at) "
                           "VALUES('bad','missing','','','','','')")
        self.assertEqual(self.store.graph.get_setting("must-rollback"), "")

    def test_literal_question_marks_and_upserts(self):
        db = self.store.graph.db
        self.assertEqual(db.execute("SELECT 'why? 100%' AS question, ? AS value", ("literal?",)).fetchone()[0], "why? 100%")
        for value in ("first", "second"):
            db.execute("INSERT OR REPLACE INTO gh_users(login,name,email,fetched_at) VALUES(?,?,?,?)",
                       ("alice", value, "", ""))
        self.assertEqual(db.execute("SELECT name FROM gh_users WHERE login='alice'").fetchone()[0], "second")

    def test_native_record_search_and_cross_connection_freshness(self):
        graph = self.store.graph
        graph.upsert_intent("repo", "doc", "retention", "Backup retention", "Keep backups thirty days", "owner", "2026-09-20")
        self.assertTrue(graph._fts_ids("intents_fts", ["backup"]))
        other = Store(self.store.path)
        other.graph.upsert_intent("repo", "doc", "retention", "Retention removed", "Use archives", "owner", "2026-09-20")
        self.assertFalse(graph._fts_ids("intents_fts", ["backup"]))
        self.assertTrue(graph._fts_ids("intents_fts", ["archive"]))

    def test_import_preserves_history_and_refuses_nonempty_destination(self):
        import tempfile
        from bridge.import_sqlite import import_database
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "existing.db"
            with patch.object(Store, "__init__", self.sqlite_init):
                old = Store(path)
                old.seed()
                old.graph.upsert_intent("acme/platform", "doc", "policy", "Credit policy", "Preserve prepaid credits", "owner", "2026-09-20")
                expected = old.state()
                old.graph.close()
            counts = import_database(path, self.store.path)
            self.assertEqual(counts["decisions"], len(expected["decisions"]))
            actual = self.store.state()
            self.assertEqual([d["id"] for d in actual["decisions"]], [d["id"] for d in expected["decisions"]])
            self.assertTrue(self.store.graph._fts_ids("intents_fts", ["prepaid"]))
            self.store.add_run({"title": "After import"})  # events sequence advanced
            with self.assertRaisesRegex(ValueError, "not empty"):
                import_database(path, self.store.path)


def load_tests(loader, tests, pattern):
    if os.environ.get("BRIDGE_TEST_POSTGRES") != "1":
        return unittest.TestSuite()
    suite = loader.loadTestsFromTestCase(PostgresSpecific)
    # Reuse the same expectations on both backends, rather than implementing
    # weaker PostgreSQL copies of the authorization and lifecycle tests.
    for name in ("test_contract", "test_rules", "test_rule_invalidation", "test_delivery", "test_auth", "test_authority",
                 "test_trust", "test_github", "test_execution", "test_accounts", "test_minimal_e2e_regressions",
                 "test_slack_discovery", "test_discovery_guidance", "test_slack_conversation", "test_reconstructed_fixes", "test_routing_peers", "test_brief",
                 "test_proof", "test_finish_diff_integrity", "test_finish_disconnect", "test_review_invariants", "test_bounded_review",
                 "test_scope_clarification", "test_interview", "test_security_isolation",
                 "test_lifecycle_security", "test_reframe", "test_routing_continuity", "test_billing_contract",
                 "test_transport_contracts", "test_slack_contract_transport", "test_teams"):
        module = importlib.import_module(name)
        for key, case in vars(module).items():
            if isinstance(case, type) and issubclass(case, unittest.TestCase) and case.__module__ == name:
                wrapped = type("Postgres" + key, (PostgresIsolation, case), {"__module__": __name__})
                suite.addTests(loader.loadTestsFromTestCase(wrapped))
    return suite
