"""Kanban dispatcher concurrency caps and the pause/resume circuit.

Extracted from ``hermes_cli.kanban_db_dispatch`` behind one
``# >>> FORK ANCHOR: kanban-dispatch-concurrency <<<`` import site in that
file. Covers: resolving ``kanban.*`` concurrency settings for one
``dispatch_once`` call (:class:`DispatchCaps` / :func:`resolve_dispatch_caps` /
:func:`clamp_requested_max_spawn`), host-wide running-task counting
(:func:`count_running_tasks_by_assignee` and friends), and the durable
dispatch pause/rate-limit-cooldown circuit
(:func:`read_dispatch_pause` / :func:`pause_dispatch` / :func:`resume_dispatch`
and their helpers).

Pure logic over an injected ``sqlite3.Connection`` — no schema/migration
ownership, no dashboard payload shape. Origin-resident helpers this module
still needs (``_positive_int``, ``_nonnegative_int``, ``_any_int``,
``resolve_max_in_progress``, ``count_running_tasks``,
``count_running_tasks_other_boards``, ``_profile_exists_fn``) are reached
late-bound via ``_kb``/``_kbc``/``_kd`` (import-cycle breaking, mirroring how
``kanban_db_dispatch.py`` already reaches ``kanban_db.py``) so monkeypatching
``kanban_db.<name>`` / ``kanban_db_dispatch.<name>`` keeps working.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from datetime import timezone
import json
import os
import time
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Mapping
from typing import Optional


# Hard stop on the review<->changes_requested loop (kanban.max_review_rounds).
# 0 = unlimited (legacy, pre-cap behavior).
DEFAULT_MAX_REVIEW_ROUNDS = 3

# High-priority slot reservation (kanban.priority_reserved_slots /
# kanban.priority_reserved_threshold). 0 slots = the feature is OFF and dispatch is
# byte-identical to the pre-reservation behaviour, which is the shipped default:
# this changes scheduling on a live fleet, so the operator opts in.
DEFAULT_PRIORITY_RESERVED_SLOTS = 0
# 1 = High and above on the documented tier scale (critical=2, high=1, normal=0,
# low=-1). Negative thresholds are legitimate (reserve for everything above Low).
DEFAULT_PRIORITY_RESERVED_THRESHOLD = 1


@dataclass(frozen=True)
class DispatchCaps:
    """Resolved ``kanban.*`` concurrency settings for one ``dispatch_once`` call.

    ``max_in_progress`` is already routed through :func:`resolve_max_in_progress`,
    so it carries the memory-derived default when config leaves it unset.
    """

    max_in_progress: Optional[int]
    max_in_progress_per_profile: Optional[int]
    max_spawn: Optional[int]
    default_assignee: Optional[str]
    # kanban.default_reviewer rides on the same shared resolution as the caps:
    # every dispatch_once entry point must route review-lane cards identically,
    # and an entry point that resolves caps but not the reviewer would silently
    # leave review cards self-assigned to their implementer.
    default_reviewer: Optional[str] = None
    dispatch_start_budget: Optional[int] = None
    dispatch_start_window_seconds: int = 600
    review_rework_escalation_profile: Optional[str] = None
    # Hard stop on the review<->changes_requested loop (kanban.max_review_rounds). Always a
    # concrete int (0 = unlimited) — unlike the Optional caps above, "not configured" and
    # "explicitly disabled" both resolve to a number the dispatcher can compare directly.
    max_review_rounds: int = DEFAULT_MAX_REVIEW_ROUNDS
    # High-priority slot reservation. Like max_review_rounds these are always concrete
    # ints, because 0 slots is a real operator choice (the feature OFF) rather than
    # "unbounded": a None here would read as "no limit on the reservation", the opposite
    # of what an absent setting means.
    priority_reserved_slots: int = DEFAULT_PRIORITY_RESERVED_SLOTS
    priority_reserved_threshold: int = DEFAULT_PRIORITY_RESERVED_THRESHOLD


def resolve_dispatch_caps(kanban_cfg: Optional[dict] = None) -> DispatchCaps:
    """Resolve the concurrency caps every ``dispatch_once`` entry point must honour.

    The caps bound the HOST, so they cannot be a property of one entry point:
    the gateway's periodic tick, ``hermes kanban dispatch`` and the dashboard's
    ``POST /dispatch`` nudge all spawn real workers against the same CPU and
    memory. An entry point that skips this resolution does not merely dispatch
    "differently" — it dispatches *uncapped*, because ``dispatch_once`` treats
    ``None`` as unlimited. Keeping the parsing here means adding a fourth caller
    cannot reintroduce that gap by omission.

    Reads config itself when *kanban_cfg* is None. Fails open to all-``None``
    only on a config-read error, which is the pre-existing behaviour of every
    caller — a broken config must not wedge dispatch entirely.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config

            cfg = load_config()
            kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        except Exception:
            _kb._log.warning(
                "kanban dispatch: config unreadable; proceeding without configured caps"
            )
            kanban_cfg = {}
    if not isinstance(kanban_cfg, dict):
        kanban_cfg = {}

    return DispatchCaps(
        max_in_progress=_kd.resolve_max_in_progress(
            _kd._positive_int_or_none(kanban_cfg.get("max_in_progress"))
        ),
        max_in_progress_per_profile=_kd._positive_int_or_none(
            kanban_cfg.get("max_in_progress_per_profile")
        ),
        max_spawn=_kd._positive_int_or_none(kanban_cfg.get("max_spawn")),
        default_assignee=(kanban_cfg.get("default_assignee") or "").strip() or None,
        default_reviewer=(kanban_cfg.get("default_reviewer") or "").strip() or None,
        dispatch_start_budget=_kd._positive_int_or_none(
            kanban_cfg.get("dispatch_start_budget")
        ),
        dispatch_start_window_seconds=_kd._positive_int(
            kanban_cfg.get("dispatch_start_window_seconds"), 600,
        ),
        review_rework_escalation_profile=(
            kanban_cfg.get("review_rework_escalation_profile") or ""
        ).strip() or None,
        max_review_rounds=_kd._nonnegative_int(
            kanban_cfg.get("max_review_rounds"), DEFAULT_MAX_REVIEW_ROUNDS,
        ),
        priority_reserved_slots=_kd._nonnegative_int(
            kanban_cfg.get("priority_reserved_slots"), DEFAULT_PRIORITY_RESERVED_SLOTS,
        ),
        priority_reserved_threshold=_kd._any_int(
            kanban_cfg.get("priority_reserved_threshold"),
            DEFAULT_PRIORITY_RESERVED_THRESHOLD,
        ),
    )


def clamp_requested_max_spawn(
    requested: Optional[int], caps: "DispatchCaps"
) -> Optional[int]:
    """Narrow a caller-supplied request to the resolved HOST cap; never widen.

    Only for values that arrive from outside the operator's config — the
    dashboard nudge reads ``?max=`` straight off a query string, so an
    unclamped value lets a hand-crafted ``?max=99`` ask for more than the host
    allows. Clamps against ``max_in_progress`` alone: ``max_spawn`` is a
    separate per-board axis that ``dispatch_once`` enforces on its own, and
    folding it in here would silently tighten a cap the operator set
    deliberately.

    ``None`` on either side means that side imposes no bound.
    """
    bounds = [b for b in (requested, caps.max_in_progress) if b is not None]
    return min(bounds) if bounds else None


def count_running_tasks_by_assignee_other_boards(board: Optional[str] = None) -> dict[str, int]:
    """Return running-worker counts per assignee on every board except ``board``.

    Per-profile concurrency is host-wide just like ``max_in_progress``: a
    profile may be assigned work from any board, but its model/API quota is one
    shared resource. A path pin represents one DB, so every enumerated slug
    remains pinned for this internal sweep just as in
    :func:`kanban_db_dispatch.count_running_tasks_other_boards`.
    """
    pinned = bool(os.environ.get("HERMES_KANBAN_DB", "").strip())
    try:
        current_path = str(_kb.kanban_db_path(board=None if pinned else board).expanduser().resolve())
        boards = _kb.list_boards(include_archived=False)
    except Exception:
        return {}
    counts: dict[str, int] = {}
    for meta in boards:
        slug = meta.get("slug") or _kb.DEFAULT_BOARD
        try:
            path = _kb.kanban_db_path(board=None if pinned else slug).expanduser()
            if str(path.resolve()) == current_path or not path.exists():
                continue
            other = _kbc.connect(board=None if pinned else slug)
            try:
                rows = other.execute(
                    "SELECT assignee, COUNT(*) AS n FROM tasks "
                    "WHERE status = 'running' AND assignee IS NOT NULL GROUP BY assignee"
                )
                for row in rows:
                    assignee = row["assignee"]
                    counts[assignee] = counts.get(assignee, 0) + int(row["n"])
            finally:
                with contextlib.suppress(Exception):
                    other.close()
        except Exception:
            continue
    return counts


def count_running_tasks_by_assignee(conn: sqlite3.Connection, board: Optional[str] = None) -> dict[str, int]:
    """Host-wide running-worker counts per assignee: this board's rows plus every
    other board's (:func:`count_running_tasks_by_assignee_other_boards`).

    Single source of truth for "how many workers does profile X have in flight
    right now" — both the dispatcher's per-profile cap enforcement and
    diagnostics' concurrency-aware ``stranded_in_ready`` rule read this so they
    can never drift into two counters that disagree.
    """
    counts = count_running_tasks_by_assignee_other_boards(board)
    for prow in conn.execute(
        "SELECT assignee, COUNT(*) AS n FROM tasks "
        "WHERE status = 'running' AND assignee IS NOT NULL "
        "GROUP BY assignee"
    ):
        assignee = prow["assignee"]
        counts[assignee] = counts.get(assignee, 0) + int(prow["n"])
    return counts


def total_running_tasks(conn: sqlite3.Connection, board: Optional[str] = None) -> int:
    """Host-wide running-worker count: this board's rows (``count_running_tasks``)
    plus every other board's (``count_running_tasks_other_boards``).

    Shared so the dispatcher's ``max_in_progress`` enforcement and diagnostics'
    concurrency-aware rules agree on the same number.
    """
    return _kd.count_running_tasks(conn) + _kd.count_running_tasks_other_boards(board)


def concurrency_snapshot(conn: sqlite3.Connection, board: Optional[str] = None,
                          *, kanban_cfg: Optional[dict] = None) -> dict:
    """Host concurrency snapshot for concurrency-aware diagnostics.

    Resolves the same caps (:func:`resolve_dispatch_caps`) and running-task
    counts (:func:`total_running_tasks` / :func:`count_running_tasks_by_assignee`)
    the dispatcher itself uses to enforce ``kanban.max_in_progress`` /
    ``kanban.max_in_progress_per_profile``. Callers (dashboard/CLI diagnostics)
    pass the result into ``kanban_diagnostics.compute_task_diagnostics(...,
    concurrency=...)`` so ``stranded_in_ready`` can tell "queued behind a full
    pipe" from "actually stuck" without reimplementing a second counter that
    can drift from the enforcer.
    """
    caps = resolve_dispatch_caps(kanban_cfg)
    return {
        "max_in_progress": caps.max_in_progress,
        "max_in_progress_per_profile": caps.max_in_progress_per_profile,
        "total_running": total_running_tasks(conn, board),
        "running_by_assignee": count_running_tasks_by_assignee(conn, board),
    }


OPERATOR_PAUSE_REASON = "operator_paused"
"""Pause reason for a deliberate operator maintenance drain.

Distinct from the self-expiring ``start_budget_exceeded`` cooldown and from the
fault circuits (``restart_safe_scope_unavailable``, ``pause_persistence_failed``)
so "why is this paused" stays a stable, machine-readable record.
"""


def _dispatch_pause_path(board: Optional[str]) -> Path:
    """Sticky circuit state beside the resolved board database.

    Deriving this from :func:`kanban_db_path` preserves ``HERMES_KANBAN_DB``
    sandbox/path-pin isolation. A test or worker pinned to another database must
    never trip or resume the live board's circuit.
    """
    return _kb.kanban_db_path(board).with_suffix(".dispatch-pause.json")


def _valid_start_budget_pause_state(state: Mapping[str, Any]) -> bool:
    """Recognize current and legacy cooldown records without trusting bare reasons."""
    recent_starts = state.get("recent_starts")
    budget = state.get("budget")
    window_seconds = state.get("window_seconds")
    values = (recent_starts, budget, window_seconds)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        return False
    assert isinstance(recent_starts, int)
    assert isinstance(budget, int)
    assert isinstance(window_seconds, int)
    return recent_starts >= 0 and budget > 0 and window_seconds > 0


def read_dispatch_pause(board: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Return the board's current dispatch stop state, if any.

    The SQLite fallback exists only for a sticky systemic fault and therefore
    takes precedence over the JSON sentinel, which may contain a self-expiring
    start-budget cooldown. Unreadable state fails closed; silently treating a
    damaged safety record as absent widens dispatch.
    """
    from hermes_cli.kanban_db_dispatch_circuit import read_pause

    path = _dispatch_pause_path(board)
    try:
        fallback = read_pause(_kb.kanban_db_path(board))
        if fallback is not None:
            return fallback
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        raw = json.loads(text)
        if not isinstance(raw, dict) or not raw.get("reason"):
            raise ValueError("pause state must be an object with a reason")
        if raw["reason"] == "start_budget_exceeded" and not _valid_start_budget_pause_state(raw):
            raise ValueError("start-budget cooldown state is missing required fields")
        return raw
    except Exception as exc:
        return {
            "reason": "pause_state_unreadable",
            "detail": str(exc),
            "path": str(path),
            "recovery": "repair dispatch-pause storage, then run `hermes kanban dispatch --resume-circuit`",
        }


def _write_dispatch_pause(
    board: Optional[str], reason: str, *, replace: bool = False, **details: Any,
) -> dict[str, Any]:
    """Atomically persist a board pause or rate-limit cooldown state."""
    current = read_dispatch_pause(board)
    if current is not None:
        if not replace:
            return current
        if current.get("reason") == reason and all(
            current.get(key) == value for key, value in details.items()
        ):
            return current
    state: dict[str, Any] = {
        "reason": reason,
        "paused_at": int(time.time()),
        **details,
    }
    path = _dispatch_pause_path(board)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    _kb._log.warning(
        "kanban dispatch for board %s: %s",
        board or _kb.DEFAULT_BOARD,
        dispatch_pause_message(state, board=board),
    )
    return state


def _clear_expired_start_budget_pause(board: Optional[str]) -> None:
    """Remove the normal cooldown only while the dispatch tick lock is held."""
    with contextlib.suppress(FileNotFoundError):
        _dispatch_pause_path(board).unlink()


def _recent_dispatch_start_window(
    conn: sqlite3.Connection, *, window_seconds: int, budget: int = 1,
    now: Optional[int] = None,
) -> tuple[int, Optional[int]]:
    """Return starts in the inclusive window and the exact next eligible time.

    When a live reload changes the budget, more than one in-window start may
    need to age out before another start is legal. The required expiry is the
    ``count - budget``-indexed start, not always the oldest one: after it
    leaves the inclusive window, exactly ``budget - 1`` starts remain.
    """
    current = int(now if now is not None else time.time())
    cutoff = current - window_seconds
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM task_events "
        "WHERE kind = 'spawned' AND created_at >= ?",
        (cutoff,),
    ).fetchone()
    starts = int(row["count"])
    if starts < budget:
        return starts, None
    expiry_row = conn.execute(
        "SELECT created_at FROM task_events WHERE kind = 'spawned' AND created_at >= ? "
        "ORDER BY created_at, id LIMIT 1 OFFSET ?",
        (cutoff, starts - budget),
    ).fetchone()
    # The query includes the cutoff boundary, so capacity returns one second
    # after the final required start is no longer in the measured interval.
    return starts, int(expiry_row["created_at"]) + window_seconds + 1


def dispatch_pause_message(state: Mapping[str, Any], *, board: Optional[str] = None) -> str:
    """One status message for CLI, gateway-backed dashboard, and API callers."""
    if state.get("reason") == "start_budget_exceeded":
        next_eligible = state.get("next_eligible_at")
        if isinstance(next_eligible, int):
            when = datetime.fromtimestamp(next_eligible, tz=timezone.utc).isoformat()
            return f"rate limited until {when}; dispatch resumes automatically"
        return "rate limited; dispatch resumes automatically when capacity is available"
    command = "hermes kanban "
    if board:
        command += f"--board {board} "
    command += "dispatch --resume-circuit"
    if state.get("reason") == OPERATOR_PAUSE_REASON:
        # A deliberate maintenance drain is not a fault: rendering it with the
        # generic "manual intervention required" phrasing below would report a
        # healthy, intentionally-stopped board as broken.
        context = []
        if state.get("paused_by"):
            context.append(f"by={state['paused_by']}")
        if isinstance(state.get("paused_at"), int):
            context.append(
                f"at={datetime.fromtimestamp(state['paused_at'], tz=timezone.utc).isoformat()}"
            )
        if state.get("note"):
            context.append(f"note={state['note']}")
        suffix = f" ({'; '.join(context)})" if context else ""
        return (
            f"paused for maintenance{suffix}; already-running workers are unaffected; "
            f"resume with: {command}"
        )
    details = [f"reason={state.get('reason', 'unknown pause')}"]
    if state.get("fault_code"):
        details.append(f"fault_code={state['fault_code']}")
    paused_at = state.get("tripped_at") or state.get("paused_at")
    if paused_at:
        details.append(f"time={paused_at}")
    if state.get("recovery"):
        details.append(f"recovery={state['recovery']}")
    return (
        f"manual intervention required ({'; '.join(details)}); "
        f"resume explicitly with: {command}"
    )


def pause_dispatch(board: Optional[str] = None, *, note: Optional[str] = None) -> dict[str, Any]:
    """Deliberately stop this board claiming/spawning new workers.

    The operator counterpart to :func:`resume_dispatch`, for draining a board
    before a gateway/service restart: workers share the gateway's
    ``KillMode=mixed`` cgroup, so restarting while any are running SIGKILLs
    them and discards uncommitted worktree progress.

    This only fences NEW dispatch — ``_dispatch_once_locked`` returns early on
    a live pause and there is deliberately no kill/reclaim behaviour here, so
    already-running workers keep running and can still complete or block
    normally while the board drains.

    Idempotent, and never overrides an existing pause: re-pausing returns the
    current state untouched so the first (possibly fault-written) "why is this
    paused" record and its recovery guidance survive.
    """
    _kb._assert_not_delegated_child_mutation()
    db_path = _kb.kanban_db_path(board=board)
    # Same discipline as resume_dispatch: a tick in flight may be about to
    # write a fault pause of its own, and either side landing inside that
    # window would silently clobber the other. Refusing keeps the operator
    # action deliberate — retry once the tick finishes.
    with _kbc._dispatch_tick_lock(db_path) as held:
        if not held:
            return {"paused": False, "state": None, "reason": "dispatch_in_progress"}
        details: dict[str, Any] = {"paused_by": _kb._hook_profile_name()}
        if note:
            details["note"] = note
        state = _write_dispatch_pause(board, OPERATOR_PAUSE_REASON, **details)
    return {"paused": True, "state": state}


def resume_dispatch(board: Optional[str] = None) -> dict[str, Any]:
    """Explicitly clear a board safety pause or current rate-limit status."""
    from hermes_cli.kanban_db_dispatch_circuit import clear_pause
    _kb._assert_not_delegated_child_mutation()
    db_path = _kb.kanban_db_path(board=board)
    # The pause check and its removal must share the dispatch tick's board lock.
    # Otherwise a tick can pass its check, write a fresh pause, and then have
    # this operator action unlink that newer safety state. Refusing a contended
    # resume makes recovery deliberate: repair, then re-run the explicit probe.
    with _kbc._dispatch_tick_lock(db_path) as held:
        if not held:
            return {
                "was_paused": read_dispatch_pause(board) is not None,
                "resumed": False,
                "reason": "dispatch_in_progress",
            }
        path = _dispatch_pause_path(board)
        previous = read_dispatch_pause(board)
        # The operator changed their mind about the maintenance window, so any
        # action queued to fire when this board drained is no longer wanted.
        # Cancelled under the same board lock that clears the pause: resuming
        # and leaving a reboot armed would be the worst possible split outcome.
        # ``_cancel_locked`` is the lock-HELD variant — the public
        # ``cancel_post_drain_action`` would try to re-acquire the tick lock we
        # are already holding, see the non-blocking guard decline against our
        # own hold, and silently leave the action armed.
        try:
            from hermes_cli.kanban_db_dispatch_postdrain import _cancel_locked
            _cancel_locked(board, reason="dispatch resumed")
        except Exception:
            _kb._log.warning(
                "kanban dispatch for board %s: could not cancel the queued post-drain action",
                board or _kb.DEFAULT_BOARD, exc_info=True,
            )
        # Clear SQLite first. A JSON-only circuit must remain authoritative if
        # fallback cleanup fails; unlinking it first would silently re-arm the
        # next tick even though this explicit recovery returned an error.
        # Conversely, if the later unlink fails, the JSON sentinel still
        # fences dispatch. Both stores are cleared under the board lock.
        clear_pause(db_path)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    return {"was_paused": previous is not None, "previous": previous, "resumed": True}


def _recent_dispatch_starts(
    conn: sqlite3.Connection, *, window_seconds: int, now: Optional[int] = None,
) -> int:
    return _recent_dispatch_start_window(
        conn, window_seconds=window_seconds, now=now,
    )[0]


def _high_priority_demand(ready_rows: list[sqlite3.Row], threshold: int) -> int:
    """How many READY cards actually WANT a reserved slot on this board this tick.

    Counts rows at or above *threshold* that are plausibly spawnable — an
    assignee that names a real Hermes profile, using the same ``profile_exists``
    gate (and the same trust-the-operator fallback when ``profiles`` is
    unimportable) as ``kanban_db_dispatch._any_spawnable_review``. An unassigned
    card, or one on a control-plane lane that a terminal pulls via
    ``claim_task``, must never hold a worker slot hostage: nothing would ever
    spawn into it.

    READY ONLY, deliberately. The review lane already has its own reservation
    (``_any_spawnable_review`` holds one slot back out of ``spawn_budget``
    regardless of priority), which is sufficient for review work. Counting a
    high-priority review row here would reserve a SECOND slot for a card already
    protected, and — because review spawns draw on the full shared budget — the
    net effect was to strand one slot: a Critical review card plus a normal ready
    backlog spawned one worker total where the reviewed behaviour spawns two.

    This count is what makes "reserved slots no high-priority card is waiting for
    fall through to normal work in the same tick" implementable. The ready lane is
    sorted ``priority DESC``, so by the time the loop reaches a below-threshold
    row no high-priority row is left *later in the list* — testing the list would
    make the reservation vacuous at every setting. Demand, not list position, is
    the thing a normal card can be held back for.
    """
    profile_exists = _kd._profile_exists_fn()

    def _wants_slot(row: sqlite3.Row) -> bool:
        if (row["priority"] or 0) < threshold:
            return False
        assignee = row["assignee"]
        if not assignee:
            return False
        return profile_exists(assignee) if profile_exists is not None else True

    return sum(1 for row in ready_rows if _wants_slot(row))


# Late-bound origin namespaces (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db_dispatch`` imports from it,
# mirroring ``hermes_fork/kanban/dispatch_resilience.py``'s own tail import.
from hermes_cli import kanban_db as _kb  # noqa: E402
from hermes_cli import kanban_db_connect as _kbc  # noqa: E402
from hermes_cli import kanban_db_dispatch as _kd  # noqa: E402
