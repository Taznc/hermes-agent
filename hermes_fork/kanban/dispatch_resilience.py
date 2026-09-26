"""Kanban dispatcher resilience: infra-failure classification, durable
timeout-kill-intent tracking, per-task interruption-streak accounting, and
provider-quota backoff with a max cap.

Extracted from ``hermes_cli.kanban_db`` (infra classification / timeout-kill
intents / interruption streaks / provider backoff) and ``hermes_cli.
kanban_db_dispatch`` (the infra-vs-legit crash classification path:
``_classify_dead_worker``, ``_reclaim_dead_workers``, ``_account_infra_deaths``,
``detect_crashed_workers``) behind one
``# >>> FORK ANCHOR: kanban-dispatch-resilience <<<`` import site in each file.
See ``hermes_fork/kanban/__init__.py`` for why this package exists despite
docs/fork-anchor-extraction.md's earlier "do not create hermes_fork/kanban/"
verdict.

Pure logic over an injected ``sqlite3.Connection`` — no schema/migration
ownership, no dashboard payload shape. Origin-resident helpers this module
still needs (``write_txn``, ``read_worker_log``, ``_append_event``,
``_classify_worker_exit``, ``_record_task_failure``, ``_account_crashes``,
...) are reached late-bound via ``_kb``/``_kd`` (import-cycle breaking,
mirroring how ``kanban_db_dispatch.py`` already reaches ``kanban_db.py``) so
monkeypatching ``kanban_db.<name>`` / ``kanban_db_dispatch.<name>`` keeps
working.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional


# --- Infra failure classification ---

def _count_infra_failures_enabled() -> bool:
    """kanban.count_infra_failures (default false) or HERMES_KANBAN_COUNT_INFRA_FAILURES.

    When true, restores pre-classification behaviour: every infra-eligible death
    (external allowed signal, startup-window dead pid, quota signature) is
    treated as an ordinary counted failure instead of a neutral interruption.
    """
    raw = os.environ.get("HERMES_KANBAN_COUNT_INFRA_FAILURES", "").strip().lower()
    if raw:
        return raw not in {"0", "false", "no", "off"}
    try:
        from hermes_cli.config import load_config_readonly
        return bool((load_config_readonly() or {}).get("kanban", {}).get("count_infra_failures", False))
    except Exception:
        return False


def _resolve_infra_startup_window_seconds() -> int:
    """kanban.infra_startup_window_seconds (default 120) or HERMES_KANBAN_INFRA_STARTUP_WINDOW_SECONDS."""
    raw = os.environ.get("HERMES_KANBAN_INFRA_STARTUP_WINDOW_SECONDS", "").strip()
    if raw:
        try:
            v = int(raw)
            if v >= 0:
                return v
        except ValueError:
            pass
    try:
        from hermes_cli.config import load_config_readonly
        cfg = (load_config_readonly() or {}).get("kanban", {})
        v = cfg.get("infra_startup_window_seconds")
        if isinstance(v, int) and v >= 0:
            return v
    except Exception:
        pass
    return 120


def _resolve_max_infra_interruptions() -> int:
    """kanban.max_infra_interruptions (default 3, min effective 1) or env override."""
    raw = os.environ.get("HERMES_KANBAN_MAX_INFRA_INTERRUPTIONS", "").strip()
    if raw:
        try:
            v = int(raw)
            if v >= 1:
                return v
        except ValueError:
            pass
    try:
        from hermes_cli.config import load_config_readonly
        cfg = (load_config_readonly() or {}).get("kanban", {})
        v = cfg.get("max_infra_interruptions")
        if isinstance(v, int) and v >= 1:
            return v
    except Exception:
        pass
    return 3


def _resolve_provider_backoff_max_seconds() -> int:
    """kanban.provider_backoff_max_seconds (default 86400) or env override."""
    raw = os.environ.get("HERMES_KANBAN_PROVIDER_BACKOFF_MAX_SECONDS", "").strip()
    if raw:
        try:
            v = int(raw)
            if v >= 1:
                return v
        except ValueError:
            pass
    try:
        from hermes_cli.config import load_config_readonly
        cfg = (load_config_readonly() or {}).get("kanban", {})
        v = cfg.get("provider_backoff_max_seconds")
        if isinstance(v, int) and v >= 1:
            return v
    except Exception:
        pass
    return 86400


def classify_infra_exit(
    *,
    exit_kind: str,
    signal_number: Optional[int] = None,
    dispatcher_killed: bool = False,
    within_startup_window: bool = False,
    quota_signal: Optional[bool] = None,
    quota_signal_dict: Optional[dict] = None,
) -> tuple[str, str]:
    """Pure classifier: is a reaped worker death "infra" or "legit"?

    Returns ``(category, reason)``. ``category`` is ``"infra"`` (does not count
    against the failure budget) or ``"legit"`` (counts exactly like today).

    Signal numbers treated as infra (external) when the dispatcher did NOT send
    them. Only SIGTERM and SIGKILL are on the allowlist. Every other signal
    (SIGABRT, SIGSEGV, SIGPIPE, etc.) is a legit crash regardless of source,
    because those indicate a crashed process, not a cleanly-terminated one.

    Quota/429 signature detection is run-scoped. It may classify a non-signal
    exit as infra, but cannot override a signaled exit's dispatcher ownership
    or explicit signal allowlist result.
    """
    # 1. Signaled: infra ONLY for SIGTERM/SIGKILL when the dispatcher did NOT
    #    send that signal. All other signals are legit. A dispatcher-owned kill
    #    (its own max-runtime timeout) is always legit.
    if exit_kind == "signaled":
        if signal_number is not None and not _is_infra_signal(signal_number):
            return ("legit", f"signal_{signal_number}")
        if dispatcher_killed:
            return ("legit", "dispatcher_kill")
        return ("infra", "external_signal")
    # 2. A quota log can explain a non-signal failure, but never overrides a
    # dispatcher-owned termination or a non-allowlisted crash signal.
    if quota_signal_dict or quota_signal:
        return ("infra", "quota")
    # 3. Unknown (no reap record - "pid N not alive"): infra only within the
    #    dispatcher's own startup window.
    if exit_kind == "unknown" and within_startup_window:
        return ("infra", "startup_window")
    # 4. Everything else is legit.
    return ("legit", exit_kind or "unknown")


# Signal numbers treated as "infra" (external) when the dispatcher did NOT send them.
# Only SIGTERM and SIGKILL are on the allowlist. Every other signal (SIGABRT, SIGSEGV,
# SIGPIPE, etc.) is a legit crash regardless of source, because those indicate a
# crashed process, not a cleanly-terminated one.
def _is_infra_signal(signum: int) -> bool:
    """Return True when signum is in the explicit infra signal allowlist (SIGTERM, SIGKILL)."""
    try:
        import signal as _signal
    except ImportError:
        return False
    return signum in frozenset({
        getattr(_signal, "SIGTERM", 15),
        getattr(_signal, "SIGKILL", 9),
    })


# --- Timeout kill intent persistence ( durable across restarts ) ---

# Track whether THIS dispatcher process sent a signal to a worker PID, so
# classify_infra_exit can distinguish the dispatcher's own max-runtime kill
# (legit, counted) from an external SIGTERM/SIGKILL (infra, not counted).
_DISPATCHER_STARTED_AT_ENV = "HERMES_KANBAN_DISPATCHER_STARTED_AT"
_DISPATCHER_KILL_INTENTS: dict[int, int] = {}  # pid -> signal_number


def mark_dispatcher_process_started() -> None:
    """Record that this process is a real dispatcher loop. Called once at startup."""
    os.environ[_DISPATCHER_STARTED_AT_ENV] = repr(time.time())


def _dispatcher_uptime_seconds() -> Optional[float]:
    """Seconds since mark_dispatcher_process_started() was called, or None."""
    raw = os.environ.get(_DISPATCHER_STARTED_AT_ENV, "").strip()
    if not raw:
        return None
    try:
        started = float(raw)
    except ValueError:
        return None
    return max(0.0, time.time() - started)


def _was_dispatcher_killed(pid: int) -> bool:
    """True when THIS dispatcher previously marked a kill-intent for this PID."""
    return pid in _DISPATCHER_KILL_INTENTS


def persist_timeout_kill_intent(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: Optional[int],
    worker_pid: int,
    signal: int,
) -> None:
    """Persist a dispatcher-owned timeout-kill intent before sending the signal.

    This survives a dispatcher restart between signal delivery and reap, so the path
    always remains an ordinary counted failure. Call IMMEDIATELY before os.kill /
    signal_fn in enforce_max_runtime and _terminate_reclaimed_worker.
    """
    with _kb.write_txn(conn, allow_nested=True):
        _clear_expired_timeout_kill_intents(conn)
        cur = conn.execute(
            "UPDATE kanban_timeout_kill_intents SET signal = ?, created_at = ? "
            "WHERE task_id = ? AND run_id IS ? AND worker_pid = ? AND consumed_at IS NULL",
            (int(signal), int(time.time()), task_id, run_id, int(worker_pid)),
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO kanban_timeout_kill_intents(task_id, run_id, worker_pid, signal, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, run_id, int(worker_pid), int(signal), int(time.time())),
            )


_TIMEOUT_KILL_INTENT_TTL_SECONDS = 24 * 60 * 60


def _clear_expired_timeout_kill_intents(conn: sqlite3.Connection) -> int:
    cutoff = int(time.time()) - _TIMEOUT_KILL_INTENT_TTL_SECONDS
    cur = conn.execute(
        "DELETE FROM kanban_timeout_kill_intents WHERE created_at < ?", (cutoff,),
    )
    return cur.rowcount


def consume_timeout_kill_intent(
    conn: sqlite3.Connection, *, task_id: str, run_id: Optional[int], worker_pid: int,
) -> bool:
    """Delete every pending timeout intent for one task/run/pid identity."""
    with _kb.write_txn(conn, allow_nested=True):
        _clear_expired_timeout_kill_intents(conn)
        cur = conn.execute(
            "DELETE FROM kanban_timeout_kill_intents "
            "WHERE task_id = ? AND run_id IS ? AND worker_pid = ? AND consumed_at IS NULL",
            (task_id, run_id, int(worker_pid)),
        )
        return cur.rowcount > 0


def has_pending_timeout_kill_intent(
    conn: sqlite3.Connection, *, task_id: str, run_id: Optional[int], worker_pid: int,
) -> bool:
    """True only for an unexpired intent belonging to this exact task/run/pid."""
    cutoff = int(time.time()) - _TIMEOUT_KILL_INTENT_TTL_SECONDS
    row = conn.execute(
        "SELECT 1 FROM kanban_timeout_kill_intents "
        "WHERE task_id = ? AND run_id IS ? AND worker_pid = ? "
        "AND consumed_at IS NULL AND created_at >= ? LIMIT 1",
        (task_id, run_id, int(worker_pid), cutoff),
    ).fetchone()
    return row is not None


def clear_consumed_timeout_kill_intents(conn: sqlite3.Connection) -> int:
    """Clear stale timeout intents during normal dispatcher operation."""
    with _kb.write_txn(conn, allow_nested=True):
        return _clear_expired_timeout_kill_intents(conn)


# --- Interruption streak persistence ---

def increment_interruption_streak(conn: sqlite3.Connection, *, task_id: str) -> int:
    """Increment the per-task interruption streak. Returns the new streak value.

    Creates the row if it does not exist (first interruption).
    """
    now = int(time.time())
    with _kb.write_txn(conn, allow_nested=True):
        conn.execute(
            "INSERT INTO kanban_interruption_streaks(task_id, streak, last_interrupted_at, created_at) "
            "VALUES (?, 1, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET "
            "streak = streak + 1, "
            "last_interrupted_at = ?",
            (task_id, now, now, now),
        )
        return conn.execute(
            "SELECT streak FROM kanban_interruption_streaks WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]


def read_interruption_streak(conn: sqlite3.Connection, *, task_id: str) -> int:
    """Return the current interruption streak for a task (0 if no row / no interruptions)."""
    row = conn.execute(
        "SELECT streak FROM kanban_interruption_streaks WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def reset_interruption_streak(conn: sqlite3.Connection, *, task_id: str) -> None:
    """Reset the interruption streak to 0. Called on non-interruption terminal outcomes or operator reset."""
    with _kb.write_txn(conn, allow_nested=True):
        conn.execute(
            "UPDATE kanban_interruption_streaks SET streak = 0, reset_at = ? WHERE task_id = ?",
            (int(time.time()), task_id),
        )


def delete_interruption_streak(conn: sqlite3.Connection, *, task_id: str) -> None:
    """Remove the interruption-streak row (e.g. on task deletion)."""
    with _kb.write_txn(conn, allow_nested=True):
        conn.execute("DELETE FROM kanban_interruption_streaks WHERE task_id = ?", (task_id,))


# --- Provider backoff with max cap ---

# Provider quota/429 signature as emitted by ``hermes_cli/auth.py`` (Codex
# OAuth refresh path) and any future provider adapter that raises the same
# shape of message. This catches quota deaths that slip past the existing
# EX_TEMPFAIL sentinel (``KANBAN_RATE_LIMIT_EXIT_CODE``) — e.g. an AuthError
# raised deep in a call stack that bubbles up as a crash or dead-pid instead
# of a clean ``sys.exit(75)``.
_QUOTA_EXIT_LOG_RE = re.compile(r"quota exhausted \(429\)", re.IGNORECASE)
_QUOTA_RETRY_AFTER_RE = re.compile(r"retry after ([0-9]+)s\b", re.IGNORECASE)
_HOST_QUOTA_PUBLISHED_LOG_RE = re.compile(r"host quota circuit published\.", re.IGNORECASE)


def worker_log_run_marker(run_id: int) -> str:
    return f"--- hermes-kanban-run:{int(run_id)} ---\n"


def _detect_quota_exit_signal(
    task_id: str, *, run_id: Optional[int], board: Optional[str] = None, tail_bytes: int = 8000,
) -> Optional[dict]:
    """Scan the worker's final log lines for a provider quota/429 signature.

    Returns ``{"retry_after_seconds": int | None}`` when the signature is
    found, else ``None``. Never raises — log I/O errors (missing file,
    already rotated, permission issue) are swallowed so a logging problem
    can never break crash reclaim.
    """
    try:
        log_text = _kb.read_worker_log(task_id, tail_bytes=tail_bytes, board=board)
    except Exception:
        return None
    if run_id is None or not log_text:
        return None
    marker = worker_log_run_marker(run_id)
    if marker not in log_text:
        return None
    log_text = log_text.rsplit(marker, 1)[1]
    if not _QUOTA_EXIT_LOG_RE.search(log_text):
        return None
    m = _QUOTA_RETRY_AFTER_RE.search(log_text)
    result = {"retry_after_seconds": _parse_retry_after(m.group(1)) if m else None}
    if _HOST_QUOTA_PUBLISHED_LOG_RE.search(log_text):
        result["host_circuit_published"] = True
    return result

def _parse_retry_after(text: Optional[str]) -> Optional[int]:
    """Parse a retry-after value: only positive base-10 integer. Returns None for malformed/missing/nonpositive."""
    if not text:
        return None
    s = str(text).strip()
    if not s:
        return None
    if not re.fullmatch(r"[0-9]+", s):
        return None
    v = int(s)
    if v <= 0:
        return None
    return v


def _clamp_retry_after(retry_after: Optional[int], max_seconds: int) -> tuple[Optional[int], Optional[str]]:
    """Clamp a parsed retry-after to the configured max. Returns (clamped_value, diagnostic_or_None).

    A malformed/missing/nonpositive retry_after returns (None, None) — it does NOT create a
    provider pause; it follows the bounded interruption policy instead.
    """
    if retry_after is None or retry_after <= 0:
        return None, None
    if retry_after > max_seconds:
        return max_seconds, f"retry-after {retry_after}s clamped to provider_backoff_max_seconds={max_seconds}s"
    return retry_after, None


def register_provider_backoff(
    conn: sqlite3.Connection,
    *,
    provider: str,
    retry_after: Optional[int],
    task_id: str,
    max_seconds: int,
) -> Optional[int]:
    """Register a provider backoff pause. Returns the effective until timestamp, or None if no pause.

    Only a valid, positive retry-after creates a provider pause. Malformed/missing/nonpositive
    retry-after returns None (the task follows the bounded interruption policy instead).
    The pause is durable across dispatcher restarts and clamped to max_seconds.
    """
    clamped, diagnostic = _clamp_retry_after(retry_after, max_seconds)
    if clamped is None:
        # No usable retry-after — no provider pause. The task will be handled by the
        # interruption streak policy instead.
        return None

    until = int(time.time()) + clamped
    with _kb.write_txn(conn, allow_nested=True):
        conn.execute(
            """INSERT INTO kanban_provider_backoff(provider, until, reason, task_id, created_at)
               VALUES (?, ?, 'quota', ?, ?)
               ON CONFLICT(provider) DO UPDATE SET
                 until = MAX(kanban_provider_backoff.until, excluded.until),
                 reason = excluded.reason,
                 task_id = excluded.task_id""",
            (provider, until, task_id, int(time.time())),
        )
        conn.execute(
            "INSERT OR IGNORE INTO kanban_provider_backoff_tasks(provider, task_id) VALUES (?, ?)",
            (provider, task_id),
        )
    return until


def active_provider_backoffs(conn: sqlite3.Connection) -> list[dict]:
    """Return active provider pauses in a stable, CLI-ready form."""
    now = int(time.time())
    return [dict(r) for r in conn.execute(
        "SELECT provider, until, reason, task_id FROM kanban_provider_backoff WHERE until > ? ORDER BY provider",
        (now,),
    ).fetchall()]


def release_expired_provider_backoffs(conn: sqlite3.Connection) -> list[str]:
    """Resume quota-parked tasks exactly once and clear expired pause rows."""
    now = int(time.time())
    resumed = []
    with _kb.write_txn(conn, allow_nested=True):
        expired = conn.execute(
            "SELECT provider, until FROM kanban_provider_backoff WHERE until <= ?",
            (now,),
        ).fetchall()
        for row in expired:
            task_rows = conn.execute(
                "SELECT task_id FROM kanban_provider_backoff_tasks WHERE provider = ?",
                (row["provider"],),
            ).fetchall()
            for task_row in task_rows:
                task_id = task_row["task_id"]
                cur = conn.execute(
                    "UPDATE tasks SET status='ready' WHERE id=? AND status='scheduled'",
                    (task_id,),
                )
                if cur.rowcount:
                    _kb._append_event(conn, task_id, "unblocked", {
                        "reason": "provider_backoff_elapsed",
                        "provider": row["provider"],
                        "resume_at": int(row["until"]),
                    })
                    resumed.append(task_id)
            conn.execute(
                "DELETE FROM kanban_provider_backoff_tasks WHERE provider = ?", (row["provider"],),
            )
        conn.execute("DELETE FROM kanban_provider_backoff WHERE until <= ?", (now,))
    return resumed


def provider_backoff_until(conn: sqlite3.Connection, *, provider: str) -> Optional[int]:
    """Return the active backoff-until timestamp for a provider, or None."""
    row = conn.execute(
        "SELECT until FROM kanban_provider_backoff WHERE provider = ? AND until > ?",
        (provider, int(time.time())),
    ).fetchone()
    return int(row["until"]) if row else None


def _provider_backoff_enabled() -> bool:
    """Whether provider-wide quota pauses are enabled (default true)."""
    raw = os.environ.get("HERMES_KANBAN_PROVIDER_BACKOFF", "").strip().lower()
    if raw:
        return raw not in {"0", "false", "no", "off", ""}
    try:
        from hermes_cli.config import load_config_readonly
        return bool((load_config_readonly().get("kanban") or {}).get("provider_backoff", True))
    except Exception:
        return True


def _task_provider(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Resolve a task's explicit provider, falling back to its profile config.

    Reading the assignee's own config gives profile-pinned providers separate pauses while
    deliberately leaving ``provider: auto`` unpaused: auto routing may choose a healthy
    provider and must not be guessed as exhausted.
    """
    row = conn.execute(
        "SELECT provider_override, assignee FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if not row:
        return None
    if row["provider_override"]:
        provider = str(row["provider_override"]).strip()
        return provider if provider and provider != "auto" else None
    assignee = row["assignee"]
    if not assignee:
        return None
    try:
        from hermes_constants import get_default_hermes_root
        from hermes_cli.config import read_user_config_raw
        from pathlib import Path
        root = Path(get_default_hermes_root())
        cfg_path = root / "config.yaml" if assignee == "default" else root / "profiles" / str(assignee) / "config.yaml"
        cfg = read_user_config_raw(cfg_path)
        provider = (cfg.get("agent") or {}).get("provider")
        p = str(provider).strip() if provider else None
        return p if p and p != "auto" else None
    except Exception:
        return None


# --- Crash reclaim: infra-vs-legit classification path ---


@dataclass
class _DeadWorker:
    """How ``detect_crashed_workers`` should book one dead worker."""

    kind: str
    code: Optional[int]
    error_text: str
    event_kind: str
    event_payload: dict
    protocol_violation: bool = False
    rate_limited: bool = False
    review_no_verdict: bool = False
    infra: bool = False

    @property
    def run_outcome(self) -> str:
        # A rate-limited requeue is recorded as ``rate_limited`` so board history doesn't show a
        # phantom crash for a quota wall; a reviewer no-verdict exit gets its own neutral outcome
        # for the same reason (neither a crash nor a failure); an infra death is recorded as
        # ``interrupted`` so it never shows up as a phantom ``crashed`` run outcome either.
        if self.rate_limited:
            return "rate_limited"
        if self.review_no_verdict:
            return "review_no_verdict"
        if self.infra:
            return "interrupted"
        return "crashed"


def _classify_dead_worker(
    conn: sqlite3.Connection, task_id: str, pid: int, claimer: Optional[str],
    retry_status: str = "ready", *, board: Optional[str] = None,
) -> _DeadWorker:
    """Map a dead worker's reaped exit status to its reclaim bookkeeping. ``retry_status`` is the
    run's source phase: a clean exit's handling differs by lane (see the review branch)."""
    kind, code = _kd._classify_worker_exit(pid)
    if kind == "clean_exit" and retry_status == "review":
        # A claimed reviewer run exited cleanly without approving, requesting changes, or
        # escalating. NOT a protocol violation (the implementer case below) and NOT a crash: an
        # idle/no-op reviewer pass is a legitimate outcome that says nothing about the work, so it
        # must never feed a failure counter or breaker. But it must not leave the card
        # auto-claimable next tick either (an idle reviewer would spin forever) — it parks in
        # ``blocked`` (``_has_sticky_block`` treats ``review_no_verdict`` as sticky) until an
        # explicit ``kanban_unblock`` reopens it.
        return _DeadWorker(
            kind, code,
            "reviewer exited cleanly without a verdict (no kanban_complete/kanban_request_changes/"
            "kanban_block call) — parked for an explicit review requeue via kanban_unblock; no "
            "failure counted.",
            "review_no_verdict",
            {"pid": pid, "claimer": claimer, "exit_code": code},
            review_no_verdict=True,
        )
    if kind == "clean_exit":
        # rc=0 while still ``running``: usually the work succeeded and only the
        # paperwork was skipped; the corrective sentence reaches the retry
        # worker via ``build_worker_context``.
        return _DeadWorker(
            kind, code, _kd._PROTOCOL_VIOLATION_ERROR, "protocol_violation",
            # ``protocol_violation`` is the durable marker for
            # _protocol_violation_streak: _end_run copies this payload into the
            # run metadata.
            {"pid": pid, "claimer": claimer, "exit_code": code, "protocol_violation": True},
            protocol_violation=True,
        )
    if kind == "rate_limited":
        # EX_TEMPFAIL is already a machine-readable quota outcome. When the
        # current run log also carries the reviewed quota signature/deadline,
        # preserve that parsed payload so both per-board and host circuits can
        # register on the first observation. Missing/malformed deadlines must
        # use the existing bounded interruption accounting; treating every
        # EX_TEMPFAIL as neutral would retry forever without advancing either
        # the interruption streak or the ordinary failure budget.
        run_id = _kb._current_run_id(conn, task_id)
        quota_signal = _detect_quota_exit_signal(
            task_id, run_id=run_id, board=board,
        )
        payload = {"pid": pid, "claimer": claimer, "exit_code": code}
        retry_after = quota_signal.get("retry_after_seconds") if quota_signal else None
        if retry_after is not None:
            payload["reason"] = "quota"
            payload["quota_retry_after_seconds"] = retry_after
            if quota_signal and quota_signal.get("host_circuit_published"):
                payload["host_circuit_published"] = True
            # Validated quota wall — NOT a task failure. Release to the source
            # phase and do not count a failure while its finite pause is active.
            return _DeadWorker(
                kind, code,
                f"pid {pid} exited rate-limited (quota wall) — requeued without counting a failure",
                "rate_limited",
                payload,
                rate_limited=True,
            )

        # The sentinel proves the result category, but not a finite recovery
        # window. Reuse the reviewed quota classifier and interruption streak
        # instead of entering the indefinitely neutral rate_limited path.
        _category, reason = classify_infra_exit(
            exit_kind="nonzero_exit", quota_signal=True,
        )
        payload["reason"] = reason
        payload["quota_retry_after_seconds"] = None
        return _DeadWorker(
            kind, code,
            f"pid {pid} exited rate-limited without a valid retry deadline "
            "(bounded infra interruption)",
            "interrupted",
            payload,
            infra=True,
        )
    # A pending durable timeout-kill intent means THIS dispatcher (or a
    # predecessor that died between signal and reap) sent this SIGTERM/SIGKILL
    # itself via enforce_max_runtime — that always remains a legit, counted
    # failure regardless of which process ends up reaping the worker.
    run_id = _kb._current_run_id(conn, task_id)
    dispatcher_killed = (
        kind == "signaled"
        and has_pending_timeout_kill_intent(
            conn, task_id=task_id, run_id=run_id, worker_pid=pid,
        )
    )
    if dispatcher_killed:
        consume_timeout_kill_intent(conn, task_id=task_id, run_id=run_id, worker_pid=pid)
    # Provider quota/429 signature in the worker's final log lines — checked
    # for every non-signaled/non-unknown death too (nonzero_exit is the
    # common case: an AuthError/RateLimitError bubbling up as a plain
    # nonzero exit code instead of the dedicated EX_TEMPFAIL sentinel).
    quota_signal_dict = _detect_quota_exit_signal(task_id, run_id=run_id, board=board)
    # Infra classification: for signaled / nonzero_exit / unknown, consult
    # classify_infra_exit. When infra, the death does NOT count against the
    # failure budget — it is tracked in the interruption streak instead.
    # ``kanban.count_infra_failures=true`` restores pre-classification
    # behaviour wholesale: every death that WOULD be infra-classified is
    # instead routed through the ordinary legit/counted path below.
    if kind in ("signaled", "nonzero_exit", "unknown") and not _kb._count_infra_failures_enabled():
        infra_category, infra_reason = classify_infra_exit(
            exit_kind=kind,
            signal_number=code if kind == "signaled" else None,
            dispatcher_killed=dispatcher_killed,
            within_startup_window=(
                kind == "unknown"
                and _kb._dispatcher_uptime_seconds() is not None
                and _kb._dispatcher_uptime_seconds() <= _kb._resolve_infra_startup_window_seconds()
            ),
            quota_signal_dict=quota_signal_dict,
        )
        if infra_category == "infra":
            payload = {"pid": pid, "claimer": claimer, "reason": infra_reason}
            if infra_reason == "quota" and quota_signal_dict:
                payload["quota_retry_after_seconds"] = quota_signal_dict.get("retry_after_seconds")
                if quota_signal_dict.get("host_circuit_published"):
                    payload["host_circuit_published"] = True
            error_text = (
                f"pid {pid} {infra_reason} (infra, not counted) "
                f"[exit_kind={kind}"
                + (f", signal={code}" if code is not None else "")
                + "]"
            )
            return _DeadWorker(
                kind, code, error_text, "interrupted", payload,
                infra=True,
            )

    # Legit paths: every code and signal not covered by the allowlist.
    if kind == "nonzero_exit":
        error_text = f"pid {pid} exited with code {code}"
    elif kind == "signaled":
        error_text = f"pid {pid} killed by signal {code}"
    else:
        error_text = f"pid {pid} not alive"
    event_payload = {"pid": pid, "claimer": claimer}
    if code is not None and kind != "unknown":
        event_payload["exit_kind"] = kind
        event_payload["exit_code"] = code
    return _DeadWorker(kind, code, error_text, "crashed", event_payload)


@dataclass
class _CrashSweep:
    """Everything ``detect_crashed_workers`` collects inside its reclaim txn."""

    crashed: list[str] = field(default_factory=list)
    rate_limited: list[str] = field(default_factory=list)
    # Parked with the neutral ``review_no_verdict`` outcome: never enters ``crash_details``.
    review_no_verdict: list[str] = field(default_factory=list)
    # ``(task_id, pid, claimer, protocol_violation, error_text)``: accounted
    # after the txn via ``_record_task_failure`` (needs its own write_txn).
    crash_details: list[tuple[str, int, str, bool, str]] = field(default_factory=list)
    # ``(task_id, pid, claimer, error_text)`` for infra-classified deaths: not
    # counted against the failure budget directly, instead bumps the per-task
    # interruption streak (see ``_account_infra_deaths``).
    infra_details: list[tuple[str, int, str, str]] = field(default_factory=list)
    # Task ids classified ``infra`` this tick — surfaced via
    # ``detect_crashed_workers._last_interrupted`` so callers (dispatch result,
    # tests) can distinguish an infra requeue from an actual counted crash.
    # Deliberately NOT included in ``crashed``: the public return value of
    # ``detect_crashed_workers`` must stay crashed-only, exactly like
    # ``rate_limited``/``review_no_verdict`` already do.
    interrupted: list[str] = field(default_factory=list)
    # Worker-exit observer payloads, fired only after every reclaim/accounting
    # txn has committed.
    exited_hook_payloads: list[dict] = field(default_factory=list)


def _reclaim_dead_workers(
    conn: sqlite3.Connection, *, board: Optional[str] = None,
) -> _CrashSweep:
    """Release every host-local ``running`` task whose worker PID is dead."""
    sweep = _CrashSweep()
    preserved_candidates: list[str] = []
    with _kb.write_txn(conn):
        rows = conn.execute(
            "SELECT id, worker_pid, worker_unit, claim_lock, started_at, assignee "
            "FROM tasks "
            "WHERE status = 'running' AND worker_pid IS NOT NULL"
        ).fetchall()
        host_prefix = _kb._host_prefix()
        for row in rows:
            lock = row["claim_lock"] or ""
            if not lock.startswith(host_prefix):
                continue
            # Launch-window grace so a freshly-spawned worker isn't reclaimed
            # before its PID is visible on /proc.
            started_at = _kb._row_get(row, "started_at")
            if started_at is not None and time.time() - started_at < _kb._resolve_crash_grace_seconds():
                continue
            if _kb._pid_alive(row["worker_pid"]):
                continue

            pid = int(row["worker_pid"])
            retry_status = _kb._retry_status_for_run(conn, row["id"])
            dead = _classify_dead_worker(
                conn, row["id"], pid, row["claim_lock"], retry_status, board=board,
            )
            dead.event_payload["retry_status"] = retry_status
            # A quota-signature infra death with a usable (parsed + clamped)
            # retry-after AND a resolvable non-``auto`` provider identity is
            # parked in ``scheduled`` instead of re-queued to ``ready`` —
            # every other same-provider task is protected from bouncing off
            # the same 429 wall (see register_provider_backoff / _task_provider).
            target_status = retry_status
            if dead.review_no_verdict:
                target_status = "blocked"
            elif (
                (getattr(dead, "infra", False) or dead.rate_limited)
                and dead.event_payload.get("reason") == "quota"
            ):
                retry_after = dead.event_payload.get("quota_retry_after_seconds")
                # Host-wide protection is account/budget scoped and therefore
                # only activates for an explicit non-secret route mapping. It
                # shares the reviewed quota classifier and deadline parser
                # above; no second classifier or provider-wide inference.
                from hermes_cli import kanban_quota_circuit as _kqc

                budget_group = _kqc.resolve_task_budget_group(conn, row["id"])
                if budget_group and not dead.event_payload.get("host_circuit_published"):
                    circuit = _kqc.register_quota_circuit(
                        budget_group,
                        retry_after=retry_after,
                        board=board or _kb.get_current_board(),
                        task_id=row["id"],
                        reason="quota",
                        max_seconds=_kb._resolve_provider_backoff_max_seconds(),
                    )
                    if circuit is not None:
                        dead.event_payload["budget_group"] = circuit["group"]
                        dead.event_payload["host_resume_at"] = circuit["next_eligible_at"]
                provider = _kb._task_provider(conn, row["id"]) if _kb._provider_backoff_enabled() else None
                if provider:
                    until = register_provider_backoff(
                        conn, provider=provider, retry_after=retry_after, task_id=row["id"],
                        max_seconds=_kb._resolve_provider_backoff_max_seconds(),
                    )
                    if until is not None:
                        target_status = "scheduled"
                        dead.event_payload["provider"] = provider
                        dead.event_payload["resume_at"] = until
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_unit = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND worker_pid = ? AND claim_lock IS ?",
                (target_status, row["id"], pid, row["claim_lock"]),
            )
            if cur.rowcount != 1:
                continue
            run_id = _kb._end_run(
                conn, row["id"],
                outcome=dead.run_outcome, status=dead.run_outcome,
                error=dead.error_text,
                metadata=dict(dead.event_payload),
            )
            _kb._append_event(conn, row["id"], dead.event_kind, dead.event_payload, run_id=run_id)
            sweep.exited_hook_payloads.append({
                "task_id": row["id"],
                "assignee": row["assignee"],
                "run_id": run_id,
                "worker_pid": pid,
                "exit_kind": dead.kind,
                "exit_code": dead.code,
                "outcome": dead.run_outcome,
                "retry_status": retry_status,
            })
            if (
                dead.infra
                and dead.kind == "rate_limited"
                and dead.event_payload.get("reason") == "quota"
                and dead.event_payload.get("quota_retry_after_seconds") is None
            ):
                # A rejected deadline supersedes any quota-wall text from the
                # prior run. Leaving that stale text makes blocker_auth stop the
                # bounded interruption sequence after its first increment.
                conn.execute(
                    "UPDATE tasks SET last_failure_error = NULL WHERE id = ?",
                    (row["id"],),
                )
            if dead.rate_limited or dead.protocol_violation:
                # Stamp last_failure_error WITHOUT touching ``consecutive_failures``:
                # a rate-limited requeue must show ``check_respawn_guard`` a quota
                # blocker; a below-budget protocol violation never reaches
                # ``_record_task_failure`` (which stamps this column), yet the
                # board UI and retry worker need the corrective message.
                conn.execute(
                    "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                    (dead.error_text[:500], row["id"]),
                )
            if dead.rate_limited:
                sweep.rate_limited.append(row["id"])
            elif dead.review_no_verdict:
                # Neutral: no ``last_failure_error`` stamp, never reaches ``_record_task_failure``.
                sweep.review_no_verdict.append(row["id"])
            elif getattr(dead, "infra", False):
                # Infra dead worker: does NOT enter crash_details or the failure
                # budget, and does NOT count toward the ``crashed`` return
                # value either — surfaced separately via ``interrupted`` /
                # ``_last_interrupted`` so callers can see which tasks were
                # infra-classified this tick without conflating them with
                # actual counted crashes.
                sweep.interrupted.append(row["id"])
                sweep.infra_details.append(
                    (row["id"], pid, row["claim_lock"], dead.error_text)
                )
            else:
                sweep.crashed.append(row["id"])
                sweep.crash_details.append(
                    (row["id"], pid, row["claim_lock"], dead.protocol_violation, dead.error_text)
                )
            preserved_candidates.append(row["id"])
    # Preservation runs AFTER the sweep transaction commits (it does slow git
    # work and records its own event, so it must not sit inside this write
    # txn), but still BEFORE any spawn in this tick — so a retry can never be
    # started onto a worktree whose previous run's output was not yet saved.
    # No expected_run_id: ``_end_run`` already cleared ``current_run_id``, and
    # the pid these tasks belonged to was verified dead above.
    for task_id in preserved_candidates:
        _kb._preserve_task_work(conn, task_id)
    return sweep


def _account_infra_deaths(
    conn: sqlite3.Connection, infra_details: list[tuple[str, int, str, str]],
) -> list[str]:
    """Bump the per-task interruption streak for each infra death and possibly
    promote to a legit counted crash when ``max_infra_interruptions`` is exceeded.

    Infra deaths (signaled-by-allowlist SIGTERM/SIGKILL, or unknown-within-
    startup-window) are NOT counted in ``consecutive_failures``. Instead each
    bump is recorded against a separate per-task streak. When the streak exceeds
    the configured cap the task is fed to ``_record_task_failure`` as a normal
    crashed failure so the bounded retry / circuit breaker still eventually
    applies — the infra window only suppresses the FIRST N infrastructure
    deaths, not forever.

    Streak is reset ONLY on a non-interruption terminal outcome or an explicit
    operator reset; a redispatch or protocol-violation retry never clears it.

    Returns the task ids promoted to a counted crash this call (streak
    exceeded the cap) — the caller removes these from the ``interrupted``
    side-channel list since they are no longer a neutral outcome.
    """
    promoted: list[str] = []
    if not infra_details:
        return promoted
    max_allowed = _kb._resolve_max_infra_interruptions()
    for tid, pid, claimer, error_text in infra_details:
        streak = increment_interruption_streak(conn, task_id=tid)
        if streak > max_allowed:
            # Promote to a legit counted crash: fed to the breaker exactly like
            # a today crash. ``force_trip`` because the decision was made against
            # the infra cap, not the normal failure counter. The streak is
            # PRESERVED (not reset) here — only a genuine non-interruption
            # terminal outcome or an explicit operator reset clears it, so a
            # task that keeps dying to interruptions cannot loop through the
            # cap forever by getting a few real successes in between.
            _kd._record_task_failure(
                conn, tid,
                error=(
                    f"{error_text} [infra interruption streak {streak} exceeded "
                    f"kanban.max_infra_interruptions={max_allowed}; routed through "
                    "normal counted-failure accounting]"
                ),
                outcome="crashed",
                failure_limit=max_allowed,
                force_trip=True,
                release_claim=False,
                end_run=False,
                event_payload_extra={
                    "pid": pid, "claimer": claimer, "infra_streak": streak,
                    "infra_streak_cap": max_allowed,
                },
            )
            promoted.append(tid)
    return promoted


def detect_crashed_workers(
    conn: sqlite3.Connection, *, board: Optional[str] = None,
) -> list[str]:
    """Reclaim ``running`` tasks whose worker PID is no longer alive.

    Restores the source phase immediately (no waiting for the claim TTL), for
    tasks claimed by *this host* only — other hosts' PIDs are meaningless.
    Clean exit while ``running`` is a protocol violation with a bounded
    violation-only retry budget; ``KANBAN_RATE_LIMIT_EXIT_CODE`` is a quota
    wall, released WITHOUT counting a failure and surfaced via the
    ``_last_rate_limited`` attribute (the return stays crashed-only).
    """
    sweep = _reclaim_dead_workers(conn, board=board)
    # Outside the main txn: account each crash and maybe trip the breaker.
    auto_blocked = _kd._account_crashes(conn, sweep.crash_details) if sweep.crash_details else []
    # Side-channel attributes keep the public ``list[str]`` return stable;
    # ``dispatch_once`` reads them to populate ``DispatchResult``. Rate-limited
    # requeues did NOT count a failure and are NOT crashes.
    detect_crashed_workers._last_auto_blocked = auto_blocked  # type: ignore[attr-defined]
    detect_crashed_workers._last_rate_limited = sweep.rate_limited  # type: ignore[attr-defined]
    # Reviewer no-verdict parks: not a failure, not a crash, and (unlike rate-limited) not
    # auto-recoverable — sticky in ``blocked`` until an explicit ``kanban_unblock``.
    detect_crashed_workers._last_review_no_verdict = sweep.review_no_verdict  # type: ignore[attr-defined]
    # Infra dead workers: bump the per-task interruption streak. When the
    # configured cap (``max_infra_interruptions``) is exceeded the task is
    # promoted to a legit counted crash (fed to _record_task_failure) so the
    # bounded retry / breaker still applies — the infra window only suppresses
    # the FIRST N infrastructure deaths, not forever.
    promoted = _account_infra_deaths(conn, sweep.infra_details) if sweep.infra_details else []
    detect_crashed_workers._last_interrupted = (  # type: ignore[attr-defined]
        [tid for tid in sweep.interrupted if tid not in promoted]
    )
    if promoted:
        # force_trip=True always trips inside _record_task_failure, so every
        # promoted id auto-blocked; fold into the public auto_blocked side-
        # channel so DispatchResult.auto_blocked reflects it too.
        auto_blocked.extend(promoted)
        detect_crashed_workers._last_auto_blocked = auto_blocked  # type: ignore[attr-defined]
        # A streak-exceeded infra death is, from the caller's perspective, now
        # an ordinary accounted crash — fold it into the public return value
        # too so callers that only look at the return list (not the
        # ``_last_interrupted``/``_last_auto_blocked`` side channels) still
        # see it.
        sweep.crashed.extend(promoted)

    # Fired only now, after the reclaim txn AND breaker accounting have
    # committed, so subscribers always observe fully durable board state.
    if sweep.exited_hook_payloads and _kb._kanban_observer_consumed("on_kanban_worker_exited"):
        _board = _kb.get_current_board()
        for hook_fields in sweep.exited_hook_payloads:
            hook_fields = dict(hook_fields)
            _kb._fire_kanban_lifecycle_hook(
                # Kanban worker-lifecycle, task-mutation, and dispatcher-tick observers (RFC #58548,
                # accepted as the design basis in the #64231 batch disposition; on_kanban_dispatch_tick is
                # the re-port of PR #56066). All five are observers only: return values are ignored, and
                # every fire site is fully best-effort, so a broken callback can never break dispatch or a
                # task mutation. Cost rule: every call site short-circuits on has_hook(), so when nothing
                # subscribes no payload is built and the hot paths (each dispatcher tick, each task write)
                # pay one dict probe. WHICH PROCESS: worker spawn/exit/stale-claim and the dispatch tick
                # fire in the DISPATCHER process (gateway-embedded dispatcher or ``hermes kanban
                # dispatch``); on_kanban_task_updated fires in whichever process committed the mutation
                # (CLI, worker, or the gateway-embedded dashboard API). Common kwargs (task-scoped hooks):
                # task_id: str, profile_name: str, board: str | None, assignee: str | None, run_id: int |
                # None. on_kanban_worker_spawned fires after ``spawn_fn`` returns AND the worker PID (when
                # one was reported) is durably persisted, per the RFC timing contract; like
                # kanban_task_claimed it runs inside the board's dispatch lock, so callbacks must stay fast.
                # Adds: worker_pid: int | None, workspace_path: str. Privacy: workspace_path is a filesystem
                # path and may reveal project layout or usernames.
                "on_kanban_worker_exited",
                hook_fields.pop("task_id"),
                board=_board,
                **hook_fields,
            )
    return sweep.crashed


# Late-bound origin namespaces (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db``/``kanban_db_dispatch`` import
# from it, mirroring ``kanban_db_dispatch.py``'s own tail import of
# ``kanban_db``.
from hermes_cli import kanban_db as _kb  # noqa: E402
from hermes_cli import kanban_db_dispatch as _kd  # noqa: E402
