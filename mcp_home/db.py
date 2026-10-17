"""SQLite storage. Every row belongs to one household; queries always filter by it."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS grocery (
    id INTEGER PRIMARY KEY, household TEXT NOT NULL, item TEXT NOT NULL, quantity TEXT NOT NULL DEFAULT '',
    aisle TEXT NOT NULL, added_by TEXT NOT NULL DEFAULT '', done INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS bills (
    id INTEGER PRIMARY KEY, household TEXT NOT NULL, provider TEXT NOT NULL, amount REAL NOT NULL,
    due_date TEXT NOT NULL, paid INTEGER NOT NULL DEFAULT 0, recurrence TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS chores (
    id INTEGER PRIMARY KEY, household TEXT NOT NULL, chore TEXT NOT NULL, person TEXT NOT NULL,
    done_on TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    household TEXT NOT NULL, person TEXT NOT NULL, PRIMARY KEY (household, person)
);
CREATE TABLE IF NOT EXISTS pantry (
    id INTEGER PRIMARY KEY, household TEXT NOT NULL, item TEXT NOT NULL, expires_on TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
"""


def db_path() -> str:
    return os.getenv("MCP_HOME_DB", "home.db")


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release to databases created before them."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bills)")}
    if "recurrence" not in cols:
        conn.execute("ALTER TABLE bills ADD COLUMN recurrence TEXT NOT NULL DEFAULT ''")


@contextmanager
def connect():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()
