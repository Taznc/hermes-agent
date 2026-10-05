"""Read-only durable fallback using existing ownership and liveness authorities."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .activity import home_matches


def _async_blockers(conn, ids):
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='async_delegations'"
    ).fetchone():
        return []
    from tools.async_delegation import _owner_liveness

    alive = _owner_liveness()
    rows = conn.execute(
        "SELECT parent_session_id, origin_ui_session_id, origin_session, state, delivery_state, event_json, owner_pid, owner_started_at FROM async_delegations"
    ).fetchall()
    for row in rows:
        if (
            not {
                row["parent_session_id"],
                row["origin_ui_session_id"],
                row["origin_session"],
            }
            & ids
        ):
            continue
        if row["state"] in {"running", "finalizing"}:
            if alive is None or alive(row["owner_pid"], row["owner_started_at"]):
                return ["durable_async_delegation"]
        elif row["delivery_state"] in {"pending", "claimed"} and row["event_json"]:
            return ["pending_delegation_delivery"]
    return []


def _process_blockers(db, home, ids):
    path = home / "processes.json"
    if not path.is_file():
        return []
    from tools.process_registry import (
        _CHECKPOINT_FIELDS,
        ProcessRegistry,
        ProcessSession,
    )

    checkpoint = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(checkpoint, list) or any(
        not isinstance(row, dict) for row in checkpoint
    ):
        raise ValueError("Invalid process ownership checkpoint")
    probe = object.__new__(ProcessRegistry)
    for row in checkpoint:
        if not {row.get("parent_session_id"), row.get("session_key")} & ids:
            continue
        if not home_matches(row.get("profile_home") or home, db):
            continue
        values = {key: row[key] for key in _CHECKPOINT_FIELDS if key in row}
        values.setdefault("command", "")
        values.setdefault("profile_home", str(home))
        process = ProcessSession(id=row["session_id"], detached=True, **values)
        # Sandbox checkpoints cannot be host-probed: do not mistake an unrelated host PID
        # for the owned process or retire work whose sandbox liveness is unavailable.
        if (
            process.pid_scope != "host"
            or probe._detached_host_fate(process.pid, process.host_start_time)
            == "running"
        ):
            return ["checkpoint_background_process"]
    return []


def _mailbox_blockers(home, ids):
    from tools.bot_live_delivery import _root, _scan_read

    root = _root(home)
    if not root.is_dir():
        return []
    for path in root.glob("*.json"):
        record = _scan_read(path)
        if (
            record
            and record.get("status") in {"queued", "claimed"}
            and record.get("session_id") in ids
        ):
            return ["pending_mailbox_delivery"]
    return []


def blockers(db, ids, conn):
    home = Path(db.db_path).resolve().parent
    try:
        return (
            _async_blockers(conn, ids)
            + _process_blockers(db, home, ids)
            + _mailbox_blockers(home, ids)
        )
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        # A broken owned-work store is NOT evidence that no work exists. Refusal is
        # non-destructive and exposes a stable reason instead of silently failing open.
        return ["owned_work_status_unavailable"]
