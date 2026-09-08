"""Profile-local durable audit ledger for cron execution attempts.

The ledger records what is known about each attempt; it is not a retry queue. Interrupted attempts
become ``unknown`` only after their exact owner process is proved gone. Terminal states are
immutable.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from cron.ledger import ledger_transaction, open_ledger, prepare_ledger
from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now

logger = logging.getLogger(__name__)

# Optional test override. Production resolves the path at transaction time so dashboard operations
# that temporarily enter another profile cannot leak that profile's records into the import-time
# home.
EXECUTIONS_FILE: Optional[Path] = None
MAX_TERMINAL_EXECUTIONS = 1000
HANDOFF_ADOPTION_GRACE_SECONDS = 30.0
_TERMINAL_STATES = ("completed", "failed", "unknown")

# Terminal error text for an attempt whose owner process is proved gone. Kept as a module constant
# because the incident classifier and the retry sweep both key on this exact interruption.
RECOVERED_INTERRUPTION_ERROR = (
    "Scheduler restarted after this execution's owner exited before a durable terminal state; "
    "whether side effects ran is unknown."
)
_lock = threading.RLock()
_PROCESS_ID = uuid.uuid4().hex

# Interruption error text written before the ``interrupted`` column existed. Matching on text is
# wrong for NEW rows (that is the whole point of the column), but it is the only evidence the
# already-recorded rows carry, and adopting them once at migration time is what makes the failures
# that motivated this feature visible instead of stranding them as untyped ``unknown`` history.
_LEGACY_INTERRUPTION_ERRORS = (
    "Interrupted by shutdown before terminal completion.",
    "Interrupted by gateway shutdown before terminal completion.",
    RECOVERED_INTERRUPTION_ERROR,
)


def _adopt_legacy_interruptions(conn: sqlite3.Connection) -> int:
    """Backfill ``interrupted`` for pre-migration rows; returns how many were adopted.

    Runs exactly once, in the transaction that adds the column, so it can never re-classify a row
    the running code has since written. Only terminal failures are considered: a completed run is
    never an interruption whatever its error text says.
    """
    placeholders = ",".join("?" for _ in _LEGACY_INTERRUPTION_ERRORS)
    cur = conn.execute(
        f"""UPDATE executions SET interrupted=1
            WHERE status IN ('failed','unknown') AND error IN ({placeholders})""",
        _LEGACY_INTERRUPTION_ERRORS,
    )
    return cur.rowcount or 0


# --- executions ledger --------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    return open_ledger(EXECUTIONS_FILE or (get_hermes_home().resolve() / "cron" / "executions.db"))


def _initialize_schema(conn: sqlite3.Connection) -> None:
    prepare_ledger(conn, db_label="cron/executions.db")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS executions (
             id TEXT PRIMARY KEY,
             job_id TEXT NOT NULL,
             source TEXT NOT NULL,
             process_id TEXT NOT NULL,
             pid INTEGER NOT NULL,
             process_started_at INTEGER,
             status TEXT NOT NULL CHECK(status IN
               ('claimed','running','completed','failed','unknown')),
             handoff_pending INTEGER NOT NULL DEFAULT 0,
             handoff_started_at REAL,
             claimed_at TEXT NOT NULL,
             started_at TEXT,
             finished_at TEXT,
             error TEXT
           )"""
    )
    from hermes_cli.sqlite_util import add_column_if_missing

    add_column_if_missing(
        conn, "executions", "handoff_pending",
        "handoff_pending INTEGER NOT NULL DEFAULT 0",
    )
    add_column_if_missing(
        conn, "executions", "handoff_started_at", "handoff_started_at REAL"
    )
    # Durable interruption facts. ``interrupted`` says an attempt died to a shutdown/abandonment
    # rather than to its own failure — a persisted fact, not a substring match on ``error``.
    # ``retry_state`` is the at-most-once replay decision for that occurrence (NULL = undecided).
    if add_column_if_missing(
        conn, "executions", "interrupted",
        "interrupted INTEGER NOT NULL DEFAULT 0",
    ):
        _adopt_legacy_interruptions(conn)
    add_column_if_missing(conn, "executions", "retry_state", "retry_state TEXT")
    # Lineage of a replay: the interrupted attempt this row was created to recover. Durable on the
    # ledger because the job's ``interrupted_retry`` stamp is transient — a successful replay
    # clears it, and history would then be unable to tell the replay from an ordinary run.
    add_column_if_missing(conn, "executions", "retry_of", "retry_of TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_job_claimed "
        "ON executions(job_id, claimed_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_status_claimed "
        "ON executions(status, claimed_at DESC, id DESC)"
    )
    add_column_if_missing(conn, "executions", "delivery_outcome", "delivery_outcome TEXT")
    add_column_if_missing(conn, "executions", "scheduled_instant", "scheduled_instant TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_occurrence "
        "ON executions(job_id, scheduled_instant) WHERE status='completed'"
    )


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    with ledger_transaction(_lock, _connect, _initialize_schema) as conn:
        yield conn


def _fetch(conn: sqlite3.Connection, execution_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
    return dict(row) if row is not None else None


def _emit_execution_state(
    record: Optional[Dict[str, Any]], *, delivery_outcome: Optional[str] = None
) -> None:
    """Project durable state to monitoring without affecting ledger behavior."""
    try:
        from agent.monitoring.cron_health import emit_execution_state

        emit_execution_state(record, delivery_outcome=delivery_outcome)
    except Exception:
        pass


def _process_start_time(pid: int) -> Optional[int]:
    try:
        from gateway.status import get_process_start_time
        return get_process_start_time(pid)
    except Exception:
        return None


def _owner_is_live(pid: int, started_at: Optional[int]) -> bool:
    try:
        from gateway.status import _pid_exists
        if not _pid_exists(pid):
            return False
    except Exception:
        return True  # fail safe: inability to prove death must not rewrite state
    if started_at is None:
        return pid == os.getpid()
    current = _process_start_time(pid)
    return current is not None and current == started_at


def _prune_unlocked(conn: sqlite3.Connection) -> None:
    conn.execute(
        """DELETE FROM executions WHERE id IN (
             SELECT id FROM executions
             WHERE status IN ('completed','failed','unknown')
             ORDER BY finished_at DESC, claimed_at DESC, id DESC LIMIT -1 OFFSET ?
           )""",
        (max(0, int(MAX_TERMINAL_EXECUTIONS)),),
    )


def create_execution(
    job_id: str, *, source: str, scheduled_instant: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist a claimed attempt before executor/provider dispatch."""
    from cron.occurrences import scheduled_instant as canonical_instant

    now = _hermes_now().isoformat()
    execution_id = uuid.uuid4().hex
    pid = os.getpid()
    with _transaction() as conn:
        conn.execute(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, process_started_at,
                status, claimed_at, scheduled_instant, retry_of)
               VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?)""",
            (execution_id, str(job_id), str(source), _PROCESS_ID, pid,
             _process_start_time(pid), now, canonical_instant(scheduled_instant), None),
        )
        record = _fetch(conn, execution_id)
    _emit_execution_state(record)
    return record  # type: ignore[return-value]


def bind_interrupted_retry_lineage(
    execution_id: str, job_id: str, retry_of: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Bind replay lineage only after this durable attempt won the job's fire claim.

    Creating a ledger row is only admission to the ownership race. Binding before the jobs-store
    CAS lets a losing contender consume the replay, and mutating jobs.json before the INSERT lets
    an INSERT failure point the stamp at a nonexistent row. The winner calls this after both the
    INSERT and fire claim are durable, but before any user side effect starts.
    """
    if not retry_of:
        return get_execution(execution_id)
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions SET retry_of=?
               WHERE id=? AND job_id=? AND status='claimed'
                 AND (retry_of IS NULL OR retry_of=?)""",
            (str(retry_of), str(execution_id), str(job_id), str(retry_of)),
        )
        if cur.rowcount != 1:
            return None
        return _fetch(conn, execution_id)


def set_execution_occurrence(execution_id: str, instant: Optional[str]) -> None:
    """Bind the store-claimed snapshot before a provider hands it to a worker."""
    from cron.occurrences import scheduled_instant

    with _transaction() as conn:
        cur = conn.execute(
            "UPDATE executions SET scheduled_instant=? WHERE id=? AND status='claimed' "
            "AND handoff_pending=0 AND process_id=? AND pid=?",
            (scheduled_instant(instant), execution_id, _PROCESS_ID, os.getpid()),
        )
        if cur.rowcount != 1:
            raise RuntimeError("Cron occurrence could not be bound before dispatch")


def mark_execution_handoff_pending(execution_id: str) -> Optional[Dict[str, Any]]:
    """Fence restart recovery while an external worker is adopting a claim."""
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET handoff_pending=1, handoff_started_at=?
               WHERE id=? AND status='claimed'
                 AND process_id=? AND pid=?""",
            (time.time(), execution_id, _PROCESS_ID, os.getpid()),
        )
        if cur.rowcount != 1:
            return None
        record = _fetch(conn, execution_id)
    _emit_execution_state(record)
    return record


def adopt_claimed_execution(execution_id: str) -> Optional[Dict[str, Any]]:
    """Atomically transfer and start an attempt in its worker process.

    The dispatching gateway creates the row before spawning a restart-safe
    worker.  Adoption is the single ``claimed`` → ``running`` gate: only the
    winner may acknowledge ownership or run side effects.
    """
    pid = os.getpid()
    process_started_at = _process_start_time(pid)
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET process_id=?, pid=?, process_started_at=?,
                   status='running', started_at=?, handoff_pending=0,
                   handoff_started_at=NULL
               WHERE id=? AND status='claimed' AND handoff_pending=1""",
            (_PROCESS_ID, pid, process_started_at, now, execution_id),
        )
        if cur.rowcount != 1:
            return None
        record = _fetch(conn, execution_id)
    _emit_execution_state(record)
    return record


def mark_execution_running(execution_id: str) -> Optional[Dict[str, Any]]:
    """Transition one claimed attempt to running exactly once."""
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET status='running', started_at=?, handoff_pending=0,
                   handoff_started_at=NULL
               WHERE id=? AND status='claimed' AND handoff_pending=0
                 AND process_id=? AND pid=?""",
            (now, execution_id, _PROCESS_ID, os.getpid()),
        )
        if cur.rowcount != 1:
            return None
        record = _fetch(conn, execution_id)
    _emit_execution_state(record)
    return record


def finish_execution(
    execution_id: str, *, success: bool, error: Optional[str] = None,
    delivery_outcome: Optional[str] = None, interrupted: bool = False,
) -> Optional[Dict[str, Any]]:
    """Write a terminal result once; terminal attempts cannot be rewritten.

    ``interrupted`` records that the attempt died to a shutdown/ownership loss rather than to its
    own failure, so the reconciler can find it without parsing ``error``.
    """
    now = _hermes_now().isoformat()
    status = "completed" if success else "failed"
    detail = None if success else (str(error) if error else "unknown failure")
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET status=?, finished_at=?, error=?, handoff_pending=0,
                   handoff_started_at=NULL, delivery_outcome=?, interrupted=?
               WHERE id=? AND status IN ('claimed','running')
                 AND process_id=? AND pid=?""",
            (status, now, detail, delivery_outcome,
             1 if (interrupted and not success) else 0,
             execution_id, _PROCESS_ID, os.getpid()),
        )
        if cur.rowcount != 1:
            return None
        _prune_unlocked(conn)
        record = _fetch(conn, execution_id)
    _emit_execution_state(record, delivery_outcome=delivery_outcome)
    return record


def recover_interrupted_executions() -> int:
    """Mark provably abandoned attempts unknown without scheduling retries."""
    now = _hermes_now().isoformat()
    changed = 0
    recovered: List[Dict[str, Any]] = []
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT id, status, process_id, pid, process_started_at,
                      handoff_pending, handoff_started_at
               FROM executions
               WHERE status IN ('claimed','running')"""
        ).fetchall()
        for row in rows:
            if row["process_id"] == _PROCESS_ID:
                continue
            if _owner_is_live(int(row["pid"]), row["process_started_at"]):
                continue
            handoff_started_at = row["handoff_started_at"]
            if (
                row["handoff_pending"]
                and handoff_started_at is not None
                and time.time() - float(handoff_started_at)
                < HANDOFF_ADOPTION_GRACE_SECONDS
            ):
                continue
            cur = conn.execute(
                """UPDATE executions
                   SET status='unknown', finished_at=?, error=?, interrupted=1,
                       handoff_pending=0, handoff_started_at=NULL
                   WHERE id=? AND status=? AND process_id=? AND pid=?
                     AND handoff_pending=?
                     AND handoff_started_at IS ?""",
                (now, RECOVERED_INTERRUPTION_ERROR,
                 row["id"], row["status"], row["process_id"], row["pid"],
                 row["handoff_pending"], row["handoff_started_at"]),
            )
            changed += cur.rowcount
            if cur.rowcount:
                record = _fetch(conn, row["id"])
                if record is not None:
                    recovered.append(record)
        if changed:
            _prune_unlocked(conn)
    for record in recovered:
        _emit_execution_state(record)
    return changed


def list_undecided_interruptions(limit: int = 50) -> List[Dict[str, Any]]:
    """Interrupted attempts with no replay decision yet, oldest first.

    Oldest-first because the reconciler applies a freshness bound: the oldest undecided occurrence
    is the one that decides (retry or decline) first, so a backlog drains deterministically.
    """
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT * FROM executions
               WHERE interrupted=1 AND retry_state IS NULL
               ORDER BY claimed_at ASC, id ASC LIMIT ?""",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
    return [dict(row) for row in rows]


def has_replay_of(execution_id: str) -> bool:
    """Return whether a durable attempt already names this interrupted occurrence.

    This is the recovery witness when a replay finishes and clears its transient jobs.json stamp
    after queueing landed but before the original row's retry decision committed.
    """
    with _transaction() as conn:
        row = conn.execute(
            "SELECT 1 FROM executions WHERE retry_of=? LIMIT 1", (str(execution_id),)
        ).fetchone()
    return row is not None


def claim_retry_decision(execution_id: str, decision: str) -> bool:
    """Record the one-and-only replay decision for an interrupted attempt.

    The compare-and-swap on ``retry_state IS NULL`` inside the ledger transaction is what makes the
    replay at-most-once: two reconcilers racing on one occurrence cannot both win, so a restart
    storm can never fan one lost occurrence out into several runs.
    """
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions SET retry_state=?
               WHERE id=? AND interrupted=1 AND retry_state IS NULL""",
            (str(decision), str(execution_id)),
        )
        return cur.rowcount == 1


def finalize_retry_decision(
    execution_id: str,
    resolver: Callable[[sqlite3.Connection, Dict[str, Any], Callable[[str], None]], Any],
) -> Any:
    """Resolve one interruption while holding its ledger write transaction.

    ``resolver`` may take the job/fire locks, re-check cross-store eligibility, mutate jobs.json,
    then invoke ``commit(decision)`` before releasing those locks. A crash after either the
    prepared or queued jobs write but before this commit leaves the row undecided; the matching
    stamp (or a replay row's durable lineage if it already ran) makes the next sweep recoverable.
    """
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM executions WHERE id=? AND interrupted=1 AND retry_state IS NULL",
            (str(execution_id),),
        ).fetchone()
        if row is None:
            return None

        def commit(decision: str) -> None:
            cur = conn.execute(
                """UPDATE executions SET retry_state=?
                   WHERE id=? AND interrupted=1 AND retry_state IS NULL""",
                (str(decision), str(execution_id)),
            )
            if cur.rowcount != 1:
                raise RuntimeError("Interrupted cron retry decision lost its ledger ownership")
            # Commit while the resolver still holds the job/fire locks. A pause or fire therefore
            # orders wholly before or after the durable decision, never between store writes.
            conn.commit()

        return resolver(conn, dict(row), commit)


def transaction_has_live_attempt(
    conn: sqlite3.Connection, job_id: str, *, excluding: Optional[str] = None,
) -> bool:
    """Check claimed/running attempts using an already-held ledger transaction."""
    params: List[Any] = [str(job_id)]
    exclude_sql = ""
    if excluding is not None:
        exclude_sql = " AND id != ?"
        params.append(str(excluding))
    row = conn.execute(
        "SELECT 1 FROM executions WHERE job_id=? AND status IN ('claimed','running')"
        + exclude_sql + " LIMIT 1",
        params,
    ).fetchone()
    return row is not None


def list_executions(
    *, job_id: Optional[str] = None, limit: int = 50, before_claimed_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return indexed, newest-first execution history with cursor pagination."""
    clauses: List[str] = []
    params: List[Any] = []
    if job_id is not None:
        clauses.append("job_id=?")
        params.append(str(job_id))
    if before_claimed_at is not None:
        clauses.append("claimed_at < ?")
        params.append(str(before_claimed_at))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM executions" + where
            + " ORDER BY claimed_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_execution(execution_id: str) -> Optional[Dict[str, Any]]:
    """Return one exact execution attempt, or ``None`` when it is absent."""
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM executions WHERE id=?",
            (str(execution_id),),
        ).fetchone()
    return dict(row) if row is not None else None


def latest_execution(job_id: str) -> Optional[Dict[str, Any]]:
    rows = list_executions(job_id=job_id, limit=1)
    return rows[0] if rows else None


def latest_executions(job_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Load latest execution for many jobs in one indexed query."""
    clean = [str(job_id) for job_id in dict.fromkeys(job_ids) if job_id]
    if not clean:
        return {}
    placeholders = ",".join("?" for _ in clean)
    with _transaction() as conn:
        rows = conn.execute(
            f"""SELECT e.* FROM executions e
                WHERE e.job_id IN ({placeholders})
                  AND e.id=(SELECT e2.id FROM executions e2
                            WHERE e2.job_id=e.job_id
                            ORDER BY e2.claimed_at DESC, e2.id DESC LIMIT 1)""",
            clean,
        ).fetchall()
    return {row["job_id"]: dict(row) for row in rows}
