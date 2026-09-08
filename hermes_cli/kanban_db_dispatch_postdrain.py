"""Post-drain action queue: fire a declared maintenance action once a board drains.

Pausing dispatch (:func:`hermes_cli.kanban_db_dispatch.pause_dispatch`) fences new
claims but never kills a live worker, so an operator must sit and watch
``running_count`` fall to zero before restarting a service whose cgroup would
otherwise SIGKILL those workers. This module lets the intent be queued up front
and evaluated by the dispatcher tick itself, so the maintenance window does not
need a human — or a browser — present at the moment the board drains.

Three properties carry the safety of that automation:

* **Table-driven actions.** :data:`ACTION_HANDLERS` maps ``action_kind`` to a
  :class:`PostDrainAction`; anything absent from the table is rejected at queue
  time. Adding a kind is a registration, not a refactor of a branch ladder.
* **Intent before execution.** The record is persisted in ``waiting``, moved to
  ``firing`` under the board's dispatch tick lock, and only then does a handler
  run. A crash mid-action therefore leaves an auditable ``firing`` record rather
  than an invisible one, and the recorded pre-conditions are what later resolve
  it — success is an OBSERVED post-condition, never the mere absence of an
  exception.
* **Mandatory expiry.** Every record carries ``expires_at``. A pause that never
  drains must never fire a reboot hours later into a state nobody expects.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from hermes_cli import kanban_db as _kb

# --- states -----------------------------------------------------------------

WAITING = "waiting"
FIRING = "firing"
SUCCEEDED = "succeeded"
FAILED = "failed"
EXPIRED = "expired"
CANCELLED = "cancelled"

TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, EXPIRED, CANCELLED})


class PostDrainActionRejected(ValueError):
    """A queue request names an action or target the operator may not run."""


# --- configuration ----------------------------------------------------------

DEFAULT_EXPIRY_SECONDS = 3600
MAX_EXPIRY_SECONDS = 86400


@dataclass(frozen=True)
class PostDrainConfig:
    """Resolved ``kanban.post_drain`` policy.

    The restart allowlist is deliberately EMPTY by default: a queued action runs
    unattended with no operator watching, so which units may be restarted is an
    explicit local decision, never an inherited default.
    """

    service_restart_allowlist: tuple[str, ...] = ()
    service_restart_scope: str = "system"
    default_expiry_seconds: int = DEFAULT_EXPIRY_SECONDS
    max_expiry_seconds: int = MAX_EXPIRY_SECONDS


def _positive_int(value: Any, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return fallback
    return value


def resolve_post_drain_config() -> PostDrainConfig:
    """Read ``kanban.post_drain`` from config.yaml, failing closed on garbage.

    An unreadable or malformed section yields the default config, whose empty
    allowlist makes ``service_restart`` unqueueable — the safe direction.
    """
    try:
        from hermes_cli.config import load_config_readonly

        raw = (load_config_readonly() or {}).get("kanban", {})
        section = raw.get("post_drain", {}) if isinstance(raw, dict) else {}
    except Exception:
        section = {}
    if not isinstance(section, dict):
        section = {}
    allowlist = section.get("service_restart_allowlist")
    units: tuple[str, ...] = ()
    if isinstance(allowlist, (list, tuple)):
        units = tuple(
            unit.strip() for unit in allowlist if isinstance(unit, str) and unit.strip()
        )
    scope = section.get("service_restart_scope")
    max_expiry = _positive_int(section.get("max_expiry_seconds"), MAX_EXPIRY_SECONDS)
    return PostDrainConfig(
        service_restart_allowlist=units,
        service_restart_scope="user" if scope == "user" else "system",
        default_expiry_seconds=min(
            _positive_int(section.get("default_expiry_seconds"), DEFAULT_EXPIRY_SECONDS),
            max_expiry,
        ),
        max_expiry_seconds=max_expiry,
    )


# --- action registry --------------------------------------------------------


@dataclass(frozen=True)
class PostDrainAction:
    """One declared action kind.

    ``resolve_target`` validates and resolves the target from CONFIG (a request
    may only name which allowlisted entry to use, never supply a unit of its
    own). ``observe_before`` snapshots the pre-conditions that ``observe_after``
    later compares against, so "did it work" is answered by the system's own
    state rather than by the handler's return value.
    """

    kind: str
    takes_target: bool
    resolve_target: Callable[[Optional[str], PostDrainConfig], Optional[str]]
    observe_before: Callable[[Mapping[str, Any], PostDrainConfig], dict[str, Any]]
    fire: Callable[[Mapping[str, Any], PostDrainConfig], None]
    observe_after: Callable[[Mapping[str, Any], PostDrainConfig], dict[str, Any]]
    survives_execution: bool = True
    """False when the action destroys the process observing it (reboot): the
    record stays ``firing`` and is resolved by a later read from a new boot."""


def _systemctl_argv(cfg: PostDrainConfig, *args: str) -> list[str]:
    argv = ["systemctl"]
    if cfg.service_restart_scope == "user":
        argv.append("--user")
    argv.extend(args)
    return argv


def _unit_properties(unit: str, cfg: PostDrainConfig) -> dict[str, str]:
    """Read the unit's live state. Unreadable properties come back absent."""
    argv = _systemctl_argv(
        cfg, "show", unit,
        "-p", "ActiveState", "-p", "MainPID", "-p", "ExecMainStartTimestampMonotonic",
    )
    try:
        out = subprocess.run(
            argv, capture_output=True, text=True, timeout=30, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    props: dict[str, str] = {}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key:
            props[key.strip()] = value.strip()
    return props


def _resolve_service_target(target: Optional[str], cfg: PostDrainConfig) -> str:
    """Resolve the unit to restart from the config allowlist.

    A request body never supplies a unit name: it may only NAME one already in
    the allowlist, and a single-entry allowlist resolves with no request input at
    all. An empty allowlist means this kind is unqueueable on this host.
    """
    allowed = cfg.service_restart_allowlist
    if not allowed:
        raise PostDrainActionRejected(
            "service_restart is not configured on this host: set "
            "kanban.post_drain.service_restart_allowlist in config.yaml"
        )
    name = (target or "").strip()
    if not name:
        if len(allowed) == 1:
            return allowed[0]
        raise PostDrainActionRejected(
            "service_restart needs a target; allowed units: " + ", ".join(allowed)
        )
    if name not in allowed:
        raise PostDrainActionRejected(
            f"service_restart target {name!r} is not in "
            "kanban.post_drain.service_restart_allowlist"
        )
    return name


def _service_observe_before(record: Mapping[str, Any], cfg: PostDrainConfig) -> dict[str, Any]:
    props = _unit_properties(str(record.get("target") or ""), cfg)
    return {
        "active_state": props.get("ActiveState"),
        "main_pid": props.get("MainPID"),
        "start_monotonic": props.get("ExecMainStartTimestampMonotonic"),
    }


def _service_fire(record: Mapping[str, Any], cfg: PostDrainConfig) -> None:
    unit = str(record.get("target") or "")
    argv = _systemctl_argv(cfg, "restart", unit)
    result = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(argv)} exited {result.returncode}: "
            f"{(result.stderr or result.stdout or '').strip()[:400]}"
        )


def _service_observe_after(record: Mapping[str, Any], cfg: PostDrainConfig) -> dict[str, Any]:
    """Success is a live unit whose start timestamp actually advanced.

    A ``systemctl restart`` that exits 0 while the unit immediately fails, or one
    against a unit that was never actually replaced, must not be reported as a
    completed maintenance action.
    """
    before = record.get("observed_before") or {}
    props = _unit_properties(str(record.get("target") or ""), cfg)
    after = {
        "active_state": props.get("ActiveState"),
        "main_pid": props.get("MainPID"),
        "start_monotonic": props.get("ExecMainStartTimestampMonotonic"),
    }
    started_before = before.get("start_monotonic")
    started_after = after.get("start_monotonic")
    restarted = bool(started_after) and started_after != started_before
    if after.get("active_state") == "active" and restarted:
        return {"state": SUCCEEDED, "observed_after": after}
    return {
        "state": FAILED,
        "observed_after": after,
        "error": (
            f"unit did not come back active with a new start time "
            f"(active_state={after.get('active_state')!r}, "
            f"start_monotonic {started_before!r} -> {started_after!r})"
        ),
    }


def _reject_target(target: Optional[str], cfg: PostDrainConfig) -> None:
    if (target or "").strip():
        raise PostDrainActionRejected("reboot takes no target")
    return None


def _reboot_observe_before(record: Mapping[str, Any], cfg: PostDrainConfig) -> dict[str, Any]:
    """Stamp the machine instantiation this record was queued in.

    The reboot destroys the observer, so the post-condition can only be checked
    from the OTHER side of it: a later read whose epoch differs is the observation
    that the reboot actually happened.
    """
    from gateway.drain_control import current_instantiation_epoch

    return {"epoch": current_instantiation_epoch()}


def _reboot_fire(record: Mapping[str, Any], cfg: PostDrainConfig) -> None:
    argv = _systemctl_argv(cfg, "reboot")
    result = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(argv)} exited {result.returncode}: "
            f"{(result.stderr or result.stdout or '').strip()[:400]}"
        )


def _reboot_observe_after(record: Mapping[str, Any], cfg: PostDrainConfig) -> dict[str, Any]:
    from gateway.drain_control import current_instantiation_epoch

    before = (record.get("observed_before") or {}).get("epoch")
    now = current_instantiation_epoch()
    if before and now and now != before:
        return {"state": SUCCEEDED, "observed_after": {"epoch": now}}
    # Same machine instantiation: the reboot has not happened (yet). Leave the
    # record in ``firing`` — resolving it either way here would be a guess.
    return {}


ACTION_HANDLERS: dict[str, PostDrainAction] = {
    "service_restart": PostDrainAction(
        kind="service_restart",
        takes_target=True,
        resolve_target=_resolve_service_target,
        observe_before=_service_observe_before,
        fire=_service_fire,
        observe_after=_service_observe_after,
    ),
    "reboot": PostDrainAction(
        kind="reboot",
        takes_target=False,
        resolve_target=_reject_target,
        observe_before=_reboot_observe_before,
        fire=_reboot_fire,
        observe_after=_reboot_observe_after,
        survives_execution=False,
    ),
}


def action_kinds() -> list[str]:
    """Kinds this host will accept, in registration order."""
    return list(ACTION_HANDLERS)


# --- persistence ------------------------------------------------------------


def post_drain_path(board: Optional[str]) -> Path:
    """Queue state beside the resolved board database.

    Derived through :func:`kanban_db_path` exactly like the pause sentinel, so a
    ``HERMES_KANBAN_DB`` pin or a sandboxed test home can never queue an action
    against — or fire one on behalf of — the live board.
    """
    return _kb.kanban_db_path(board).with_suffix(".dispatch-post-drain.json")


def read_post_drain_action(board: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Return the board's queued action record, or None.

    Unreadable state reads as absent: unlike the pause circuit (where failing
    closed means "keep dispatch stopped"), failing closed here would mean firing
    a reboot off a record nobody can parse.
    """
    path = post_drain_path(board)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None
    if not isinstance(raw, dict) or raw.get("action_kind") not in ACTION_HANDLERS:
        return None
    if raw.get("state") not in {WAITING, FIRING} | TERMINAL_STATES:
        return None
    return raw


def _write_post_drain_action(board: Optional[str], record: Mapping[str, Any]) -> dict[str, Any]:
    path = post_drain_path(board)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(dict(record), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    return dict(record)


def queue_post_drain_action(
    board: Optional[str] = None,
    *,
    action_kind: str,
    target: Optional[str] = None,
    requested_by: Optional[str] = None,
    expires_in_seconds: Optional[int] = None,
    group_id: Optional[str] = None,
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Persist the intent to run *action_kind* once this board drains.

    Validation happens entirely against the handler table and config BEFORE
    anything is written, so a rejected request leaves no record behind.
    """
    handler = ACTION_HANDLERS.get(action_kind)
    if handler is None:
        raise PostDrainActionRejected(
            f"unknown post-drain action {action_kind!r}; "
            f"available: {', '.join(action_kinds())}"
        )
    cfg = resolve_post_drain_config()
    resolved_target = handler.resolve_target(target, cfg)

    if expires_in_seconds is None:
        ttl = cfg.default_expiry_seconds
    else:
        if (
            isinstance(expires_in_seconds, bool)
            or not isinstance(expires_in_seconds, int)
            or expires_in_seconds <= 0
            or expires_in_seconds > cfg.max_expiry_seconds
        ):
            raise PostDrainActionRejected(
                f"expires_in_seconds must be 1..{cfg.max_expiry_seconds}"
            )
        ttl = expires_in_seconds

    requested_at = int(now if now is not None else time.time())
    record = {
        "action_kind": action_kind,
        "target": resolved_target,
        "requested_by": requested_by or _kb._hook_profile_name(),
        "requested_at": requested_at,
        "expires_at": requested_at + ttl,
        "state": WAITING,
    }
    if group_id:
        record["group_id"] = group_id
    return _write_post_drain_action(board, record)


def cancel_post_drain_action(
    board: Optional[str] = None, *, reason: str = "cancelled", now: Optional[int] = None,
) -> dict[str, Any]:
    """Cancel a ``waiting`` action. A firing or already-terminal record is left alone."""
    record = read_post_drain_action(board)
    if record is None or record.get("state") != WAITING:
        return {"cancelled": False, "state": record}
    updated = {
        **record,
        "state": CANCELLED,
        "resolution": reason,
        "resolved_at": int(now if now is not None else time.time()),
    }
    return {"cancelled": True, "state": _write_post_drain_action(board, updated)}


# --- firing on observed drain ----------------------------------------------


def _board_is_operator_drained(board: Optional[str]) -> bool:
    """Is this board paused BY THE OPERATOR and down to zero running workers?

    Both halves matter. A fault circuit (``restart_safe_scope_unavailable``,
    ``pause_state_unreadable``, a rate-limit cooldown) also reads as "paused",
    but it is not a maintenance window an operator opened — it is precisely the
    unexpected state an unattended reboot must not be launched into.
    """
    from hermes_cli import kanban_db_connect as _kbc
    from hermes_cli import kanban_db_dispatch as _kbd

    pause = _kbd.read_dispatch_pause(board)
    if not pause or pause.get("reason") != _kbd.OPERATOR_PAUSE_REASON:
        return False
    conn = None
    try:
        conn = _kbc.connect(board=board)
        return _kbd.count_running_tasks(conn) == 0
    except Exception:
        # An unreadable board cannot be shown to have drained, so it has not.
        return False
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()


def _claim_for_firing(board: Optional[str], now: int) -> Optional[dict[str, Any]]:
    """Transition ``waiting -> firing`` atomically, or return None.

    The exactly-once guarantee lives here. The read, the drain check and the
    write all happen under the board's dispatch tick lock, which is the same
    non-blocking single-writer guard :func:`dispatch_once` already serializes
    ticks on — so a second tick racing this one either loses the lock outright
    or observes the record already in ``firing`` and declines.
    """
    from hermes_cli import kanban_db as _kbmod
    from hermes_cli import kanban_db_connect as _kbc

    db_path = _kbmod.kanban_db_path(board=board)
    with _kbc._dispatch_tick_lock(db_path) as held:
        if not held:
            return None
        record = read_post_drain_action(board)
        if record is None or record.get("state") != WAITING:
            return None
        expires_at = record.get("expires_at")
        if isinstance(expires_at, int) and now >= expires_at:
            return _write_post_drain_action(board, {
                **record, "state": EXPIRED, "resolved_at": now,
                "resolution": "expired before the board drained",
            })
        if not _board_is_operator_drained(board):
            return None
        return _write_post_drain_action(board, {
            **record, "state": FIRING, "fired_at": now,
        })


def evaluate_post_drain_action(
    board: Optional[str] = None, *, now: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Fire this board's queued action if the board has drained; else expire or wait.

    Returns the record it transitioned, or None when there was nothing to do.
    Called from the dispatcher tick, so the trigger is entirely server-side: no
    browser, no dashboard poll, and no wall-clock scheduler is involved.
    """
    current = int(now if now is not None else time.time())
    record = _claim_for_firing(board, current)
    if record is None or record.get("state") != FIRING:
        return record

    handler = ACTION_HANDLERS[record["action_kind"]]
    cfg = resolve_post_drain_config()

    # Observe-then-act: the pre-conditions are persisted BEFORE the side effect,
    # so a crash mid-action still leaves enough evidence to resolve the record.
    try:
        record = _write_post_drain_action(board, {
            **record, "observed_before": handler.observe_before(record, cfg),
        })
    except Exception as exc:
        return _write_post_drain_action(board, {
            **record, "state": FAILED, "resolved_at": current,
            "error": f"pre-fire observation failed: {str(exc)[:400]}",
        })

    try:
        handler.fire(record, cfg)
    except Exception as exc:
        return _write_post_drain_action(board, {
            **record, "state": FAILED, "resolved_at": current, "error": str(exc)[:400],
        })

    try:
        verdict = handler.observe_after(record, cfg) or {}
    except Exception as exc:
        verdict = {"state": FAILED, "error": f"post-fire observation failed: {str(exc)[:400]}"}
    if not verdict.get("state"):
        # No observable verdict yet (a reboot cannot watch its own success).
        # The record stays ``firing``: claiming success here would be a guess.
        return record
    return _write_post_drain_action(board, {
        **record, **verdict, "resolved_at": current,
    })
