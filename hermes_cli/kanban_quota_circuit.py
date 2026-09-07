"""Host-wide, account-scoped quota circuits for Kanban dispatch.

The per-board provider backoff in :mod:`hermes_cli.kanban_db` remains the
local recovery mechanism.  This module adds one authoritative SQLite store
under the shared Kanban home so separate board dispatchers coordinate before
starting workers.  Account identity is never inferred from a provider name:
operators explicitly assign opaque, non-secret budget-group labels to
provider/profile routes.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping, Optional

_log = logging.getLogger(__name__)
_DEFAULT_RESUME_SPREAD_SECONDS = 30
_SCHEMA = """
CREATE TABLE IF NOT EXISTS quota_circuits (
    budget_group       TEXT PRIMARY KEY,
    next_eligible_at   INTEGER NOT NULL,
    reason             TEXT NOT NULL,
    first_observed_at  INTEGER NOT NULL,
    last_observed_at   INTEGER NOT NULL,
    observations       INTEGER NOT NULL DEFAULT 1,
    resume_probe_at    INTEGER
);
CREATE TABLE IF NOT EXISTS quota_circuit_sources (
    budget_group TEXT NOT NULL,
    board        TEXT NOT NULL,
    task_id      TEXT NOT NULL,
    observed_at  INTEGER NOT NULL,
    PRIMARY KEY (budget_group, board, task_id)
);
CREATE TABLE IF NOT EXISTS quota_circuit_deferrals (
    budget_group TEXT NOT NULL,
    board        TEXT NOT NULL,
    task_id      TEXT NOT NULL,
    first_deferred_at INTEGER NOT NULL,
    last_deferred_at  INTEGER NOT NULL,
    deferred_count    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (budget_group, board, task_id)
);
"""


def quota_circuit_db_path() -> Path:
    """Return the host coordination DB path under the shared Kanban home."""
    from hermes_cli.kanban_db import kanban_home

    return kanban_home() / "kanban" / "quota-circuits.db"


def _connect() -> sqlite3.Connection:
    path = quota_circuit_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


@contextlib.contextmanager
def _write_conn():
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sanitized_group_label(group: str) -> str:
    """Return a stable opaque handle suitable for diagnostics and clear APIs."""
    digest = hashlib.sha256(str(group).encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"budget-{digest}"


def configured_budget_groups() -> Mapping[str, Any]:
    """Read explicit ``kanban.quota_budget_groups`` configuration.

    No inferred default is intentional: a provider can serve many independent
    wallets, and provider-wide grouping would suppress healthy accounts.
    """
    try:
        from hermes_cli.config import load_config_readonly

        value = ((load_config_readonly() or {}).get("kanban") or {}).get(
            "quota_budget_groups", {}
        )
    except Exception:
        return {}
    return value if isinstance(value, Mapping) else {}


def _string_set(value: Any) -> set[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _task_route(conn: sqlite3.Connection, task_id: str) -> tuple[Optional[str], Optional[str]]:
    row = conn.execute(
        "SELECT provider_override, assignee FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return None, None
    assignee = str(row["assignee"] or "").strip() or None
    raw_provider = str(row["provider_override"] or "").strip()
    if raw_provider:
        return raw_provider, assignee
    # Reuse the reviewed effective-provider resolution for profile-pinned tasks.
    from hermes_cli.kanban_db import _task_provider

    return _task_provider(conn, task_id) or "auto", assignee


def _groups_for_route(
    provider: str,
    profile: str,
    groups: Mapping[str, Any],
    *,
    auto_candidates: bool,
) -> list[str]:
    matched: list[str] = []
    for raw_group, selectors in groups.items():
        group = str(raw_group).strip()
        if not group or not isinstance(selectors, Mapping):
            continue
        providers = _string_set(selectors.get("providers"))
        profiles = _string_set(selectors.get("profiles"))
        if (
            (auto_candidates or provider in providers or "*" in providers)
            and (profile in profiles or "*" in profiles)
        ):
            matched.append(group)
    return sorted(matched)


def resolve_task_budget_groups(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    config: Optional[Mapping[str, Any]] = None,
) -> list[str]:
    """Resolve explicitly configured groups for the task's effective route.

    A pinned route matches only when both its provider and profile are listed
    (``*`` is accepted explicitly). For ``provider=auto``, every explicitly
    mapped group for the profile is a candidate: dispatch proceeds only while
    at least one is unpaused, and the worker publishes the provider it actually
    selected. A pinned provider with overlapping groups is ambiguous and fails
    closed to no group.
    """
    provider, profile = _task_route(conn, task_id)
    if not provider or not profile:
        return []
    groups = config if config is not None else configured_budget_groups()
    if not isinstance(groups, Mapping):
        return []
    matched = _groups_for_route(
        provider,
        profile,
        groups,
        auto_candidates=provider == "auto",
    )
    if provider != "auto" and len(matched) > 1:
        _log.warning(
            "kanban quota circuit: ambiguous budget groups for provider/profile route; "
            "host circuit disabled for task %s",
            task_id,
        )
        return []
    return matched


def resolve_task_budget_group(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    config: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Return one unambiguous group, used when registering a quota failure."""
    groups = resolve_task_budget_groups(conn, task_id, config=config)
    return groups[0] if len(groups) == 1 else None


def _clamped_delay(retry_after: Optional[int], max_seconds: int) -> Optional[int]:
    if isinstance(retry_after, bool) or not isinstance(retry_after, int) or retry_after <= 0:
        return None
    return min(retry_after, max(1, int(max_seconds)))


def register_quota_circuit(
    budget_group: str,
    *,
    retry_after: Optional[int],
    board: str,
    task_id: str,
    reason: str,
    max_seconds: int,
    now: Optional[int] = None,
) -> Optional[dict]:
    """Atomically register/extend one host circuit and return its public state."""
    group = str(budget_group or "").strip()
    delay = _clamped_delay(retry_after, max_seconds)
    if not group or delay is None:
        return None
    observed_at = int(time.time()) if now is None else int(now)
    until = observed_at + delay
    with _write_conn() as conn:
        conn.execute(
            """INSERT INTO quota_circuits(
                   budget_group, next_eligible_at, reason, first_observed_at,
                   last_observed_at, observations, resume_probe_at
               ) VALUES (?, ?, ?, ?, ?, 1, NULL)
               ON CONFLICT(budget_group) DO UPDATE SET
                   next_eligible_at = MAX(quota_circuits.next_eligible_at, excluded.next_eligible_at),
                   reason = excluded.reason,
                   last_observed_at = excluded.last_observed_at,
                   observations = quota_circuits.observations + 1,
                   resume_probe_at = NULL""",
            (group, until, str(reason or "quota"), observed_at, observed_at),
        )
        conn.execute(
            """INSERT INTO quota_circuit_sources(budget_group, board, task_id, observed_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(budget_group, board, task_id) DO UPDATE SET
                   observed_at = excluded.observed_at""",
            (group, str(board or "default"), task_id, observed_at),
        )
        row = conn.execute(
            "SELECT * FROM quota_circuits WHERE budget_group = ?", (group,)
        ).fetchone()
    return _public_state(dict(row), boards_deferred=0, cards_deferred=0)


def publish_worker_quota_result(
    result: Any,
    *,
    task_id: Optional[str] = None,
    board: Optional[str] = None,
    provider: Optional[str] = None,
    now: Optional[int] = None,
    max_seconds: Optional[int] = None,
) -> Optional[dict]:
    """Publish a structured worker quota result before the EX_TEMPFAIL exit.

    Only verified rate-limit/billing terminal results with a finite reset or
    retry deadline qualify. Missing/malformed deadlines deliberately return
    ``None`` so the reviewed bounded interruption policy remains authoritative.
    """
    if not isinstance(result, Mapping) or not result.get("failed"):
        return None
    reason = str(result.get("failure_reason") or "")
    if reason not in {"rate_limit", "billing"}:
        return None
    task_id = str(task_id or "").strip()
    if not task_id:
        return None
    observed_at = int(time.time()) if now is None else int(now)
    retry_after: Optional[int] = None
    reset_at = result.get("reset_at")
    if isinstance(reset_at, (int, float)) and not isinstance(reset_at, bool):
        delta = float(reset_at) - observed_at
        if delta > 0:
            # Ceiling without importing math: a fractional reset must never be
            # shortened to an already-expired integer deadline.
            retry_after = int(delta)
            if retry_after < delta:
                retry_after += 1
    if retry_after is None:
        raw_retry = result.get("retry_after_seconds")
        if isinstance(raw_retry, int) and not isinstance(raw_retry, bool) and raw_retry > 0:
            retry_after = raw_retry
    if retry_after is None:
        return None

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect_closing(board=board) as conn:
        if provider:
            row = conn.execute(
                "SELECT assignee FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            profile = str(row["assignee"] or "").strip() if row else ""
            configured = configured_budget_groups()
            matched = _groups_for_route(
                str(provider).strip().lower(),
                profile,
                configured,
                auto_candidates=False,
            ) if profile else []
            group = matched[0] if len(matched) == 1 else None
        else:
            group = resolve_task_budget_group(conn, task_id)
    if not group:
        return None
    cap = max_seconds if max_seconds is not None else kb._resolve_provider_backoff_max_seconds()
    return register_quota_circuit(
        group,
        retry_after=retry_after,
        board=board or kb.get_current_board(),
        task_id=task_id,
        reason=reason,
        max_seconds=cap,
        now=observed_at,
    )


def _resume_spread_seconds() -> int:
    try:
        from hermes_cli.config import load_config_readonly

        raw = ((load_config_readonly() or {}).get("kanban") or {}).get(
            "quota_resume_spread_seconds", _DEFAULT_RESUME_SPREAD_SECONDS
        )
        value = int(raw)
        return max(1, value)
    except Exception:
        return _DEFAULT_RESUME_SPREAD_SECONDS


def _record_deferral(
    conn: sqlite3.Connection, group: str, board: str, task_id: str, now: int
) -> None:
    conn.execute(
        """INSERT INTO quota_circuit_deferrals(
               budget_group, board, task_id, first_deferred_at, last_deferred_at, deferred_count
           ) VALUES (?, ?, ?, ?, ?, 1)
           ON CONFLICT(budget_group, board, task_id) DO UPDATE SET
               last_deferred_at = excluded.last_deferred_at,
               deferred_count = quota_circuit_deferrals.deferred_count + 1""",
        (group, str(board or "default"), task_id, now, now),
    )


def task_quota_guard(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    board: Optional[str] = None,
    consume_probe: bool = False,
) -> Optional[str]:
    """Return a host circuit guard reason for a task, or ``None``.

    At expiry, one dispatcher atomically receives a probe lease. Other matching
    starts remain spread for a short grace period. If no renewed quota failure
    extends the circuit, the row auto-clears after that period.
    """
    groups = resolve_task_budget_groups(conn, task_id)
    if not groups:
        return None
    now = int(time.time())
    spread = _resume_spread_seconds()
    blocked_reasons: list[str] = []
    with _write_conn() as host:
        for group in groups:
            row = host.execute(
                "SELECT * FROM quota_circuits WHERE budget_group = ?", (group,)
            ).fetchone()
            if row is None:
                # An auto route may have several explicit candidates. Any
                # healthy candidate means the worker can route elsewhere.
                return None
            until = int(row["next_eligible_at"])
            probe_at = row["resume_probe_at"]
            if until > now:
                blocked_reasons.append("host_quota_circuit")
                continue
            if probe_at is None:
                if consume_probe:
                    updated = host.execute(
                        "UPDATE quota_circuits SET resume_probe_at = ? "
                        "WHERE budget_group = ? AND resume_probe_at IS NULL",
                        (now, group),
                    )
                    if updated.rowcount:
                        return None
                    probe_at = host.execute(
                        "SELECT resume_probe_at FROM quota_circuits WHERE budget_group = ?",
                        (group,),
                    ).fetchone()[0]
                else:
                    return None
            if int(probe_at) + spread > now:
                blocked_reasons.append("host_quota_resume_spread")
                continue
            host.execute("DELETE FROM quota_circuits WHERE budget_group = ?", (group,))
            host.execute("DELETE FROM quota_circuit_sources WHERE budget_group = ?", (group,))
            host.execute("DELETE FROM quota_circuit_deferrals WHERE budget_group = ?", (group,))
            return None

        if blocked_reasons:
            # Record one opaque aggregate; raw group names never enter board
            # events or API payloads.
            for group in groups:
                if host.execute(
                    "SELECT 1 FROM quota_circuits WHERE budget_group = ?", (group,)
                ).fetchone():
                    _record_deferral(host, group, str(board or "default"), task_id, now)
            return (
                "host_quota_resume_spread"
                if all(r == "host_quota_resume_spread" for r in blocked_reasons)
                else "host_quota_circuit"
            )
    return None


def _public_state(row: Mapping[str, Any], *, boards_deferred: int, cards_deferred: int) -> dict:
    return {
        "group": sanitized_group_label(str(row["budget_group"])),
        "reason": str(row["reason"]),
        "first_observed_at": int(row["first_observed_at"]),
        "last_observed_at": int(row["last_observed_at"]),
        "next_eligible_at": int(row["next_eligible_at"]),
        "observations": int(row["observations"]),
        "resume_probe_at": (
            int(row["resume_probe_at"]) if row.get("resume_probe_at") is not None else None
        ),
        "boards_deferred": int(boards_deferred),
        "cards_deferred": int(cards_deferred),
    }


def list_quota_circuits(*, now: Optional[int] = None) -> list[dict]:
    """Return sanitized active/recovery diagnostics and purge expired rows."""
    current = int(time.time()) if now is None else int(now)
    spread = _resume_spread_seconds()
    with _write_conn() as conn:
        expired = conn.execute(
            "SELECT budget_group FROM quota_circuits "
            "WHERE resume_probe_at IS NOT NULL AND resume_probe_at + ? <= ?",
            (spread, current),
        ).fetchall()
        for expired_row in expired:
            group = expired_row["budget_group"]
            conn.execute("DELETE FROM quota_circuits WHERE budget_group = ?", (group,))
            conn.execute("DELETE FROM quota_circuit_sources WHERE budget_group = ?", (group,))
            conn.execute("DELETE FROM quota_circuit_deferrals WHERE budget_group = ?", (group,))
        rows = conn.execute("SELECT * FROM quota_circuits ORDER BY first_observed_at").fetchall()
        result: list[dict] = []
        for row in rows:
            counts = conn.execute(
                "SELECT COUNT(DISTINCT board), COUNT(*) FROM quota_circuit_deferrals "
                "WHERE budget_group = ?",
                (row["budget_group"],),
            ).fetchone()
            result.append(
                _public_state(
                    dict(row), boards_deferred=int(counts[0]), cards_deferred=int(counts[1])
                )
            )
        return result


def clear_quota_circuit(group_handle: str) -> bool:
    """Clear a circuit by its opaque diagnostic handle."""
    handle = str(group_handle or "").strip()
    if not handle:
        return False
    with _write_conn() as conn:
        rows = conn.execute("SELECT budget_group FROM quota_circuits").fetchall()
        group = next(
            (str(row[0]) for row in rows if sanitized_group_label(str(row[0])) == handle), None
        )
        if group is None:
            return False
        conn.execute("DELETE FROM quota_circuits WHERE budget_group = ?", (group,))
        conn.execute("DELETE FROM quota_circuit_sources WHERE budget_group = ?", (group,))
        conn.execute("DELETE FROM quota_circuit_deferrals WHERE budget_group = ?", (group,))
        return True
