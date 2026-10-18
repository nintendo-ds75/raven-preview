"""The shared SQL Raven writes for SQLite is translated for PostgreSQL:
placeholders, upserts, type names, and the string functions the two
spell differently. Measured live: a routing query using SQLite's instr()
errored on every PostgreSQL contract."""
import unittest

from bridge.database import postgres_sql


class TranslationTests(unittest.TestCase):
    def test_instr_becomes_strpos_outside_string_literals(self):
        sql = postgres_sql("SELECT CASE WHEN instr(path, '/') > 0 THEN substr(path, 1, instr(path, '/') - 1) "
                           "ELSE 'instr(x)' END FROM t WHERE repo = ?")
        self.assertEqual(sql.count("strpos("), 2)
        self.assertIn("'instr(x)'", sql)
        self.assertIn("repo = %s", sql)

    def test_upsert_and_types_still_translate(self):
        self.assertIn("ON CONFLICT DO NOTHING", postgres_sql("INSERT OR IGNORE INTO t(a) VALUES(?)"))
        self.assertIn("DOUBLE PRECISION", postgres_sql("CREATE TABLE t (w REAL)"))
