"""Host-wide, account-scoped quota circuits for Kanban dispatch.

The per-board provider backoff in :mod:`hermes_cli.kanban_db` remains the
local recovery mechanism.  This module adds one authoritative SQLite store
under the shared Kanban home so separate board dispatchers coordinate before
starting workers.  Account identity is never inferred from a provider name:
operators explicitly assign opaque, non-secret budget-group labels to
provider/profile routes.

Lifecycle of one circuit row:

* ``paused`` — ``next_eligible_at`` is in the future; every matching start on
  every board is deferred.
* ``recovering`` — the deadline passed.  Exactly one dispatcher wins the
  recovery probe; every later matching start takes a serialized slot
  (``next_slot_at``, advanced by the resume spread per admission) so recovery
  is metered host-wide instead of bursting.  The row auto-clears once no
  further start has been admitted for a full recovery window.  A renewed
  quota failure re-arms it as ``paused``.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import math
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from hermes_constants import (
    get_default_hermes_root,
    reset_hermes_home_override,
    set_hermes_home_override,
)

_log = logging.getLogger(__name__)
_DEFAULT_RESUME_SPREAD_SECONDS = 30
# A recovering circuit disarms once no matching start has been admitted for
# this many spreads: contenders that never got observed during the pause
# (another board's dispatcher had not ticked yet) are still metered, while a
# drained backlog does not keep the group throttled forever.
_RECOVERY_IDLE_SPREADS = 4
_SCHEMA = """
CREATE TABLE IF NOT EXISTS quota_circuits (
    budget_group       TEXT PRIMARY KEY,
    next_eligible_at   INTEGER NOT NULL,
    reason             TEXT NOT NULL,
    first_observed_at  INTEGER NOT NULL,
    last_observed_at   INTEGER NOT NULL,
    observations       INTEGER NOT NULL DEFAULT 1,
    resume_probe_at    INTEGER,
    next_slot_at       INTEGER
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
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(quota_circuits)")}
    if "next_slot_at" not in columns:
        conn.execute("ALTER TABLE quota_circuits ADD COLUMN next_slot_at INTEGER")
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


def _kanban_config() -> Mapping[str, Any]:
    """Read host-level Kanban policy from the shared/default Hermes home.

    Workers run with ``HERMES_HOME`` scoped to their assignee profile, but
    quota budget groups coordinate accounts across profiles and therefore
    have one authoritative host-level configuration.  Use a context-local
    override so concurrent profile work in the same process is unaffected.
    """
    token = None
    try:
        from hermes_cli.config import load_config_readonly

        token = set_hermes_home_override(get_default_hermes_root())
        value = (load_config_readonly() or {}).get("kanban") or {}
    except Exception:
        return {}
    finally:
        if token is not None:
            reset_hermes_home_override(token)
    return value if isinstance(value, Mapping) else {}


def configured_budget_groups() -> Mapping[str, Any]:
    """Read explicit ``kanban.quota_budget_groups`` configuration.

    No inferred default is intentional: a provider can serve many independent
    wallets, and provider-wide grouping would suppress healthy accounts.
    """
    value = _kanban_config().get("quota_budget_groups", {})
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
    mapped group for the profile is a candidate; :func:`task_quota_guard`
    then admits the task only when runtime resolution provably lands on an
    unpaused candidate. A pinned provider with overlapping groups is
    ambiguous and fails closed to no group.
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


def _profile_home(profile: str) -> Path:
    from hermes_constants import get_default_hermes_root

    root = Path(get_default_hermes_root())
    return root if profile == "default" else root / "profiles" / profile


def predict_auto_provider(profile: str) -> Optional[str]:
    """Predict which provider ``provider=auto`` resolves to for ``profile``.

    Mirrors the worker's own startup ladder — ``resolve_requested_provider``
    (config ``model.provider`` / env) and then :func:`hermes_cli.auth.resolve_provider`
    for a genuine ``auto`` — under the profile's Hermes home, so the answer
    reflects that profile's config, ``.env`` and OAuth state rather than the
    dispatcher's. The worker is spawned without ``--provider`` and inherits
    the dispatcher environment, so it resolves from the same inputs moments
    later. Returns ``None`` when resolution fails or is unavailable; callers
    treat that as unprovable and fail closed.
    """
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(_profile_home(profile))
    try:
        from hermes_cli.auth import resolve_provider
        from hermes_cli.runtime_provider import resolve_requested_provider

        provider = resolve_requested_provider(None)
        if provider == "auto":
            provider = resolve_provider("auto")
    except Exception as exc:
        _log.debug("kanban quota circuit: auto resolution for %s failed: %s", profile, exc)
        return None
    finally:
        reset_hermes_home_override(token)
    provider = str(provider or "").strip().lower()
    return provider or None


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
                   last_observed_at, observations, resume_probe_at, next_slot_at
               ) VALUES (?, ?, ?, ?, ?, 1, NULL, NULL)
               ON CONFLICT(budget_group) DO UPDATE SET
                   next_eligible_at = MAX(quota_circuits.next_eligible_at, excluded.next_eligible_at),
                   reason = excluded.reason,
                   last_observed_at = excluded.last_observed_at,
                   observations = quota_circuits.observations + 1,
                   resume_probe_at = NULL,
                   next_slot_at = NULL""",
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
    return _public_state(dict(row), boards_deferred=0, cards_deferred=0, now=observed_at)


def quota_result_retry_after_seconds(
    result: Any, *, now: Optional[int] = None,
) -> Optional[int]:
    """Return the validated delay carried by a structured quota result.

    This is the single parser used by both host-circuit publication and the
    worker-log marker consumed by the existing run-scoped reaper classifier.
    """
    if not isinstance(result, Mapping) or not result.get("failed"):
        return None
    if str(result.get("failure_reason") or "") not in {"rate_limit", "billing"}:
        return None
    observed_at = int(time.time()) if now is None else int(now)
    reset_at = result.get("reset_at")
    if isinstance(reset_at, (int, float)) and not isinstance(reset_at, bool):
        numeric_reset = float(reset_at)
        if math.isfinite(numeric_reset):
            delta = numeric_reset - observed_at
            if delta > 0:
                # Ceiling: a fractional reset must never be shortened to an
                # already-expired integer deadline.
                retry_after = int(delta)
                return retry_after + (retry_after < delta)
    raw_retry = result.get("retry_after_seconds")
    if isinstance(raw_retry, int) and not isinstance(raw_retry, bool) and raw_retry > 0:
        return raw_retry
    return None


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
    retry_after = quota_result_retry_after_seconds(result, now=now)
    if retry_after is None:
        return None
    reason = str(result.get("failure_reason") or "")
    task_id = str(task_id or "").strip()
    if not task_id:
        return None
    observed_at = int(time.time()) if now is None else int(now)

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
        value = int(_kanban_config().get("quota_resume_spread_seconds", _DEFAULT_RESUME_SPREAD_SECONDS))
        return max(1, value)
    except Exception:
        return _DEFAULT_RESUME_SPREAD_SECONDS


def _recovery_window_seconds() -> int:
    """Idle time after the last granted slot before a recovering circuit clears."""
    return _resume_spread_seconds() * _RECOVERY_IDLE_SPREADS


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


def _drop_deferral(conn: sqlite3.Connection, group: str, task_id: str) -> None:
    """An admitted card no longer counts as deferred demand."""
    conn.execute(
        "DELETE FROM quota_circuit_deferrals WHERE budget_group = ? AND task_id = ?",
        (group, task_id),
    )


def _delete_group(conn: sqlite3.Connection, group: str) -> None:
    conn.execute("DELETE FROM quota_circuits WHERE budget_group = ?", (group,))
    conn.execute("DELETE FROM quota_circuit_sources WHERE budget_group = ?", (group,))
    conn.execute("DELETE FROM quota_circuit_deferrals WHERE budget_group = ?", (group,))


def _sweep_finished(conn: sqlite3.Connection, now: int) -> None:
    """Drop recovering rows whose serialized recovery has gone idle.

    A row is finished once the deadline passed, the probe went out and the
    last granted slot expired a full recovery window ago with no further
    admission. Rows in the ``paused`` state are never swept here — only
    expiry, a manual clear or a completed recovery ends a pause.
    """
    rows = conn.execute(
        """SELECT budget_group FROM quota_circuits
           WHERE next_eligible_at <= ? AND resume_probe_at IS NOT NULL
             AND COALESCE(next_slot_at, resume_probe_at + ?) + ? <= ?""",
        (now, _resume_spread_seconds(), _recovery_window_seconds(), now),
    ).fetchall()
    for row in rows:
        _delete_group(conn, str(row["budget_group"]))


def _guard_group(
    host: sqlite3.Connection,
    group: str,
    task_id: str,
    now: int,
    *,
    consume: bool,
) -> Optional[str]:
    """Return the guard reason for one group, granting a probe/slot when ``consume``."""
    row = host.execute(
        "SELECT * FROM quota_circuits WHERE budget_group = ?", (group,)
    ).fetchone()
    if row is None:
        return None
    if int(row["next_eligible_at"]) > now:
        return "host_quota_circuit"
    spread = _resume_spread_seconds()
    probe_at = row["resume_probe_at"]
    if probe_at is None:
        if not consume:
            return None
        updated = host.execute(
            "UPDATE quota_circuits SET resume_probe_at = ?, next_slot_at = ? "
            "WHERE budget_group = ? AND resume_probe_at IS NULL",
            (now, now + spread, group),
        )
        if updated.rowcount:
            _drop_deferral(host, group, task_id)
            return None
        row = host.execute(
            "SELECT * FROM quota_circuits WHERE budget_group = ?", (group,)
        ).fetchone()
        probe_at = row["resume_probe_at"]
    slot_at = row["next_slot_at"]
    slot_at = int(probe_at) + spread if slot_at is None else int(slot_at)
    if slot_at > now:
        return "host_quota_resume_spread"
    if not consume:
        return None
    # Serialized recovery: this admission owns the current slot; the next
    # matching start anywhere on the host waits one more spread. The row is
    # swept only after the last granted slot has been idle for a full
    # recovery window, so contenders on a board whose dispatcher has not
    # ticked yet are still metered rather than admitted in a burst.
    host.execute(
        "UPDATE quota_circuits SET next_slot_at = ? WHERE budget_group = ?",
        (now + spread, group),
    )
    _drop_deferral(host, group, task_id)
    return None


def _auto_guard(
    host: sqlite3.Connection,
    candidates: list[str],
    provider_groups: Mapping[str, list[str]],
    profile: str,
    task_id: str,
    now: int,
    *,
    consume: bool,
) -> tuple[Optional[str], list[str]]:
    """Guard an ``auto`` route against its candidate groups.

    Auto may start only when the worker's own resolution ladder provably
    lands on a provider whose mapped group is unpaused. Unmapped or failed
    resolution while any candidate is paused fails closed — a proven-empty
    wallet must never be hammered by an unpinned route. Returns the guard
    reason and the groups that deferred the task.
    """
    active = [
        group for group in candidates
        if host.execute(
            "SELECT 1 FROM quota_circuits WHERE budget_group = ?", (group,)
        ).fetchone()
    ]
    if not active:
        return None, []
    provider = predict_auto_provider(profile)
    resolved = sorted(
        set(provider_groups.get(provider or "", []))
        | set(provider_groups.get("*", []))
    )
    if len(resolved) != 1:
        # Unresolvable, unmapped, or ambiguous: cannot prove an unpaused route.
        return "host_quota_circuit", active
    group = resolved[0]
    reason = _guard_group(host, group, task_id, now, consume=consume)
    return reason, ([group] if reason else [])


def task_quota_guard(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    board: Optional[str] = None,
    consume_probe: bool = False,
) -> Optional[str]:
    """Return a host circuit guard reason for a task, or ``None``.

    While paused, every matching start defers. At expiry one dispatcher
    atomically receives the recovery probe; afterwards admissions are
    serialized host-wide, one per resume spread, until the deferred demand
    drains. ``consume_probe=False`` (dry runs) only peeks.
    """
    provider, profile = _task_route(conn, task_id)
    groups = resolve_task_budget_groups(conn, task_id)
    if not groups or not provider or not profile:
        return None
    now = int(time.time())
    with _write_conn() as host:
        _sweep_finished(host, now)
        if provider == "auto":
            configured = configured_budget_groups()
            provider_groups: dict[str, list[str]] = {}
            candidate_groups = set(groups)
            for raw_group, selectors in configured.items():
                group = str(raw_group).strip()
                if group not in candidate_groups or not isinstance(selectors, Mapping):
                    continue
                for candidate in _string_set(selectors.get("providers")):
                    provider_groups.setdefault(candidate, []).append(group)
            reason, deferred_by = _auto_guard(
                host, groups, provider_groups, profile, task_id, now, consume=consume_probe,
            )
        else:
            reason = _guard_group(host, groups[0], task_id, now, consume=consume_probe)
            deferred_by = [groups[0]] if reason else []
        if reason is not None:
            # Raw group names never enter board events or API payloads; the
            # deferral rows feed the sanitized diagnostics and recovery metering.
            # Recorded for peeks too — a dry run observing a deferred card is a
            # real observation, and stale demand ages out on its own.
            for group in deferred_by:
                _record_deferral(host, group, str(board or "default"), task_id, now)
        return reason


def _public_state(
    row: Mapping[str, Any], *, boards_deferred: int, cards_deferred: int, now: int,
) -> dict:
    until = int(row["next_eligible_at"])
    probe_at = row.get("resume_probe_at")
    slot_at = row.get("next_slot_at")
    if until > now or probe_at is None:
        state = "paused"
        next_eligible = until
    else:
        state = "recovering"
        next_eligible = max(now, int(slot_at)) if slot_at is not None else until
    return {
        "group": sanitized_group_label(str(row["budget_group"])),
        "state": state,
        "reason": str(row["reason"]),
        "first_observed_at": int(row["first_observed_at"]),
        "last_observed_at": int(row["last_observed_at"]),
        "next_eligible_at": next_eligible,
        "observations": int(row["observations"]),
        "resume_probe_at": int(probe_at) if probe_at is not None else None,
        "boards_deferred": int(boards_deferred),
        "cards_deferred": int(cards_deferred),
    }


def list_quota_circuits(*, now: Optional[int] = None) -> list[dict]:
    """Return sanitized active/recovery diagnostics and purge finished rows."""
    current = int(time.time()) if now is None else int(now)
    with _write_conn() as conn:
        _sweep_finished(conn, current)
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
                    dict(row),
                    boards_deferred=int(counts[0]),
                    cards_deferred=int(counts[1]),
                    now=current,
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
        _delete_group(conn, group)
        return True
