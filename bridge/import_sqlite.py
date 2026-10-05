"""Copy an existing Raven SQLite database into an empty PostgreSQL database.

The source is opened read-only in one snapshot. The import is atomic, keeps
identities/history/blobs, skips SQLite search indexes, and resets PG sequences.
"""
import argparse
import json
import os
import sqlite3
from pathlib import Path

from .database import is_postgres
from .store import Store


def import_database(source, target):
    if not is_postgres(target):
        raise ValueError("The destination must be a PostgreSQL URL")
    source_path = Path(source).expanduser().resolve(strict=True)
    src = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    store = Store(target)
    try:
        src.execute("BEGIN")
        with store.connect() as dst:
            dst.execute("BEGIN IMMEDIATE")
            tables = {r[0] for r in dst.execute("SELECT tablename FROM pg_tables WHERE schemaname=current_schema()")}
            tables.discard("schema_migrations")
            if any(dst.execute(f'SELECT 1 FROM "{t}" LIMIT 1').fetchone() for t in tables):
                raise ValueError("Destination is not empty; import into a fresh database before starting/seeding Raven")
            source_tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            pending = tables & source_tables
            counts = {}
            while pending:
                ready = [t for t in sorted(pending) if not ({r[2] for r in src.execute(f'PRAGMA foreign_key_list("{t}")')} & pending)]
                if not ready:
                    raise ValueError("Source schema has a foreign-key cycle")
                for table in ready:
                    allowed = {r[0] for r in dst.execute("SELECT column_name FROM information_schema.columns "
                               "WHERE table_schema=current_schema() AND table_name=? AND is_generated='NEVER'", (table,))}
                    cols = [r[1] for r in src.execute(f'PRAGMA table_info("{table}")') if r[1] in allowed]
                    if table == "ownership":
                        cols.insert(0, "rowid")
                    names = ",".join(f'"{c}"' for c in cols)
                    insert = f'INSERT INTO "{table}" ({names}) VALUES ({",".join("?" for _ in cols)})'
                    count = 0
                    for row in src.execute(f'SELECT {names} FROM "{table}"'):
                        dst.execute(insert, tuple(row))
                        count += 1
                    counts[table] = count
                    pending.remove(table)
            for table, column in (("events", "id"), ("ownership", "rowid"),
                                  ("decisions", "rowid"), ("decision_revisions", "rowid")):
                dst.execute(f"SELECT setval(pg_get_serial_sequence(?, ?), "
                            f'coalesce((SELECT max("{column}") FROM "{table}"), 1), '
                            f'EXISTS(SELECT 1 FROM "{table}"))', (table, column))
        # Apply legacy backfills to imported rows after the atomic copy.
        Store(target)
        return counts
    finally:
        src.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("--db", default=os.environ.get("DATABASE_URL", ""))
    args = parser.parse_args()
    print(json.dumps(import_database(args.source, args.db), indent=2))


if __name__ == "__main__":
    main()
