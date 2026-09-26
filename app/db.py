"""
SQLite access layer.

get_connection() opens a new connection per request (caller must close it).
init_schema()    creates tables and index if they don't exist.
"""

import os
import sqlite3

_DEFAULT_DB_PATH = "data/acme.db"


def get_connection() -> sqlite3.Connection:
    db_path = os.environ.get("ACME_DB_PATH", _DEFAULT_DB_PATH)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
          id         TEXT PRIMARY KEY,
          owner_id   TEXT NOT NULL,
          title      TEXT NOT NULL,
          body       TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chunks (
          id       TEXT PRIMARY KEY,
          doc_id   TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
          owner_id TEXT NOT NULL,
          seq      INTEGER NOT NULL,
          text     TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_chunks_doc_id ON chunks(doc_id);
    """)
    conn.commit()
