"""Database connections and the small SQL dialect boundary used by Bridge.

SQLite remains dependency-free. PostgreSQL uses psycopg, native full-text
indexes, and an advisory transaction lock for the existing read/check/write
transactions. The lock deliberately preserves their single-writer semantics.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

WRITE_LOCK = 724193801
WORKER_LOCK = 724193802
REPLACE_KEYS = {
    "changes": ("repo", "sha"),
    "change_people": ("repo", "sha", "engineer", "role"),
    "listings": ("repo", "kind", "pattern", "person", "role"),
    "model_cache": ("repo", "kind", "key"),
    "blame_lines": ("repo", "rev", "path", "engineer"),
    "gh_users": ("login",),
    "gh_pulls": ("repo", "number"),
}


def is_postgres(target):
    return str(target).startswith(("postgresql://", "postgres://"))


def location(target):
    return str(target) if is_postgres(target) else str(Path(target).expanduser().resolve())


def display_location(target):
    if is_postgres(target):
        from urllib.parse import urlsplit
        parts = urlsplit(target)
        return f"postgresql://{parts.hostname}:{parts.port or 5432}{parts.path}"
    return str(target)


def connect(target, *, autocommit=False):
    if is_postgres(target):
        return PostgresConnection(target, autocommit=autocommit)
    db = sqlite3.connect(target, timeout=30, isolation_level=None if autocommit else "")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    return db


class Row:
    """The mapping/index/iteration contract of sqlite3.Row."""
    def __init__(self, columns, values):
        self.columns, self.values = columns, values

    def keys(self):
        return self.columns

    def __getitem__(self, key):
        return self.values[self.columns.index(key)] if isinstance(key, str) else self.values[key]

    def __iter__(self):
        return iter(self.values)

    def __len__(self):
        return len(self.values)


def row_factory(cursor):
    columns = [c.name for c in cursor.description] if cursor.description else []
    return lambda values: Row(columns, values)


def postgres_sql(statement):
    """Translate only Bridge's shared SQL conventions; literals stay untouched."""
    sql = statement.strip().rstrip(";")
    replace = re.match(r"INSERT OR REPLACE INTO (\w+)\s*\(([^)]+)\)", sql, re.I)
    ignore = bool(re.match(r"INSERT OR IGNORE\b", sql, re.I))
    sql = re.sub(r"INSERT OR (?:IGNORE|REPLACE) INTO", "INSERT INTO", sql, flags=re.I)
    if replace:
        table, columns = replace.groups()
        keys = REPLACE_KEYS[table]
        columns = [c.strip() for c in columns.split(",")]
        updates = [f"{c}=excluded.{c}" for c in columns if c not in keys]
        sql += f" ON CONFLICT ({','.join(keys)}) DO UPDATE SET {','.join(updates)}"
    elif ignore:
        sql += " ON CONFLICT DO NOTHING"
    # Split SQL string literals before translating placeholders or type names.
    chunks = re.split(r"('(?:''|[^'])*')", sql)
    for i in range(0, len(chunks), 2):
        part = chunks[i].replace("?", "%s")
        part = re.sub(r"INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY", part, flags=re.I)
        part = re.sub(r"\bBLOB\b", "BYTEA", part)
        part = re.sub(r"\bREAL\b", "DOUBLE PRECISION", part)
        chunks[i] = part
    return "".join(chunks)


class PostgresConnection:
    dialect = "postgres"

    def __init__(self, target, *, autocommit=False):
        try:
            import psycopg
        except ImportError as error:
            raise RuntimeError("PostgreSQL requires: pip install -r requirements-postgres.txt") from error
        self.raw = psycopg.connect(target, autocommit=True, row_factory=row_factory, connect_timeout=10)
        self.autocommit = autocommit

    @property
    def in_transaction(self):
        from psycopg.pq import TransactionStatus
        return self.raw.info.transaction_status != TransactionStatus.IDLE

    def begin(self):
        if not self.in_transaction:
            self.raw.execute("BEGIN")
            self.raw.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))

    def execute(self, sql, params=()):
        import psycopg
        sql = sql.strip()
        if sql.upper() == "BEGIN IMMEDIATE":
            self.begin()
            return self.raw.cursor()
        info = re.fullmatch(r"PRAGMA table_info\((\w+)\)", sql, re.I)
        if info:
            return self.raw.execute("SELECT column_name AS name FROM information_schema.columns "
                                    "WHERE table_schema=current_schema() AND table_name=%s "
                                    "ORDER BY ordinal_position", (info[1],))
        if sql.upper().startswith("PRAGMA "):
            return self.raw.cursor()
        if not self.autocommit and re.match(r"(?:INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b", sql, re.I):
            self.begin()
        try:
            return self.raw.execute(postgres_sql(sql.replace("%", "%%") if params else sql), params or None)
        except psycopg.IntegrityError as error:
            # Existing callers catch this public exception after rollback.
            raise sqlite3.IntegrityError(str(error)) from error

    def executemany(self, sql, rows):
        for row in rows:
            self.execute(sql, row)

    def executescript(self, script):
        # Shared schema scripts contain no procedural SQL or semicolons in literals.
        for sql in script.split(";"):
            if sql.strip():
                self.execute(sql)

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        if self.in_transaction:
            self.raw.execute("ROLLBACK" if kind else "COMMIT")

    def close(self):
        self.raw.close()


def migrate_postgres(db):
    """Versioned PG-only additions after the shared relational migrations."""
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    db.execute('CREATE INDEX IF NOT EXISTS change_paths_binary_path ON change_paths(repo, path COLLATE "C")')
    if db.execute("SELECT 1 FROM schema_migrations WHERE version=1").fetchone():
        return
    for table in ("decisions", "ownership", "decision_revisions"):
        db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS rowid BIGSERIAL UNIQUE")
    for table, fields in (("decisions", ("question", "context", "answer", "rationale")),
                          ("intents", ("title", "body"))):
        expression = " || ' ' || ".join(f"coalesce({field}, '')" for field in fields)
        db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS search_vector TSVECTOR "
                   f"GENERATED ALWAYS AS (to_tsvector('english', {expression})) STORED")
        db.execute(f"CREATE INDEX IF NOT EXISTS {table}_search_gin ON {table} USING GIN(search_vector)")
    db.execute("INSERT INTO schema_migrations(version) VALUES(1)")
