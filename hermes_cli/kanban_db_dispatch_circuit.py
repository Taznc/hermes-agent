"""SQLite fallback for dispatch pauses when the JSON sentinel cannot be written.

The singleton belongs to the board, not the triggering task: deleting a task or
its event history must not silently resume an infrastructure outage. The table
is created only on fallback, inside the dispatcher's existing write transaction;
old boards and status-only reads need no schema migration or write access.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from hermes_cli import kanban_db_connect as kbc


def _has_pause_table(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'dispatch_pause'"
    ).fetchone() is not None


def read_pause(db_path: Path) -> dict[str, Any] | None:
    """Read without initializing/mutating the board; unreadable state must raise."""
    if not db_path.exists():
        return None
    uri = db_path.resolve().as_uri() + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        if not _has_pause_table(conn):
            return None
        row = conn.execute("SELECT state FROM dispatch_pause WHERE id = 1").fetchone()
    if row is None:
        return None
    state = json.loads(row[0])
    if not isinstance(state, dict) or not state.get("reason"):
        raise ValueError("SQLite pause state must be an object with a reason")
    return state


def persist_pause(conn: sqlite3.Connection, state: dict[str, Any]) -> None:
    """Caller holds the board tick lock and the triggering run's write transaction."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dispatch_pause ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), state TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO dispatch_pause (id, state) VALUES (1, ?) "
        "ON CONFLICT(id) DO NOTHING",
        (json.dumps(state),),
    )


def clear_pause(db_path: Path) -> None:
    """Explicit resume only, under the same board tick lock as persistence."""
    if not db_path.exists():
        return
    with kbc.connect_closing(db_path=db_path) as conn:
        if _has_pause_table(conn):
            with kbc.write_txn(conn):
                conn.execute("DELETE FROM dispatch_pause WHERE id = 1")
