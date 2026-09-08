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

GROUP_ARMING = "arming"
GROUP_ARMED = "armed"


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
    """Argv for a SERVICE-scoped command.

    ``service_restart_scope`` declares where the allowlisted UNIT lives, so it
    belongs only to commands about that unit. A host action (reboot) that read
    it would silently become ``systemctl --user reboot``, which addresses the
    user manager and cannot reboot anything.
    """
    argv = ["systemctl"]
    if cfg.service_restart_scope == "user":
        argv.append("--user")
    else:
        argv.append("--no-ask-password")
    argv.extend(args)
    return argv


def _host_systemctl_argv(*args: str) -> list[str]:
    """Argv for a command against the HOST, independent of any unit's scope."""
    return ["systemctl", "--no-ask-password", *args]


def _run_system_action(argv: list[str], *, allow_privileged_fallback: bool) -> None:
    """Run a fixed systemd action without ever opening an authentication prompt.

    The gateway normally runs as an unprivileged service user. A direct system
    manager call may still be authorized by local policy, so try it first with
    ``--no-ask-password``. System-scope actions then get exactly one declared
    fallback: the same fixed argv through ``sudo -n``. User-manager actions are
    already in the gateway user's authority domain and must never cross into
    root's user manager via sudo.
    """
    candidates = [argv]
    if allow_privileged_fallback:
        candidates.append(["sudo", "-n", *argv])
    errors: list[str] = []
    for candidate in candidates:
        try:
            result = subprocess.run(
                candidate,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"{' '.join(candidate)}: {exc}")
            continue
        if result.returncode == 0:
            return
        detail = (result.stderr or result.stdout or "").strip()[:400]
        errors.append(f"{' '.join(candidate)} exited {result.returncode}: {detail}")
    raise RuntimeError("; ".join(errors))


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
    _run_system_action(argv, allow_privileged_fallback=cfg.service_restart_scope == "system")


def _monotonic_value(raw: Any) -> Optional[int]:
    """A systemd monotonic timestamp as an int, or None when it is not evidence.

    Absent, empty, and unparseable all collapse to None on purpose: each means
    the same thing for the question being asked, which is whether the unit can
    be SHOWN to have restarted. ``0`` is a real value (a unit that had never
    started), so it must survive this.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _service_observe_after(record: Mapping[str, Any], cfg: PostDrainConfig) -> dict[str, Any]:
    """Success is a live unit whose start timestamp STRICTLY advanced.

    A ``systemctl restart`` that exits 0 while the unit immediately fails, one
    against a unit that was never actually replaced, and one whose baseline
    could not be read at all are all the same verdict: the restart cannot be
    shown to have happened, so it is not reported as a completed maintenance
    action. Only ``after > before`` is evidence — a merely DIFFERENT timestamp
    would also accept a stale or backwards reading.
    """
    before = record.get("observed_before") or {}
    props = _unit_properties(str(record.get("target") or ""), cfg)
    after = {
        "active_state": props.get("ActiveState"),
        "main_pid": props.get("MainPID"),
        "start_monotonic": props.get("ExecMainStartTimestampMonotonic"),
    }
    started_before = _monotonic_value(before.get("start_monotonic"))
    started_after = _monotonic_value(after.get("start_monotonic"))
    restarted = (
        started_before is not None
        and started_after is not None
        and started_after > started_before
    )
    if after.get("active_state") == "active" and restarted:
        return {"state": SUCCEEDED, "observed_after": after}
    return {
        "state": FAILED,
        "observed_after": after,
        "error": (
            f"unit did not come back active with a strictly newer start time "
            f"(active_state={after.get('active_state')!r}, "
            f"start_monotonic {before.get('start_monotonic')!r} -> "
            f"{after.get('start_monotonic')!r})"
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
    # Host action: deliberately NOT ``_systemctl_argv``. See its docstring.
    argv = _host_systemctl_argv("reboot")
    _run_system_action(argv, allow_privileged_fallback=True)


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
    record = _new_post_drain_record(
        action_kind=action_kind,
        target=target,
        requested_by=requested_by,
        expires_in_seconds=expires_in_seconds,
        group_id=group_id,
        now=now,
    )
    return _write_post_drain_action(board, record)


def _new_post_drain_record(
    *,
    action_kind: str,
    target: Optional[str] = None,
    requested_by: Optional[str] = None,
    expires_in_seconds: Optional[int] = None,
    group_id: Optional[str] = None,
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Validate one request and build its not-yet-persisted intent record."""
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
    return record


def _disarm_group_records(
    boards: list[str], group_id: str, *, reason: str, now: int,
) -> dict[str, dict[str, Any]]:
    """Cancel every readable waiting leg after aggregate arming fails.

    A leg whose write itself is broken remains ``arming`` (and therefore cannot
    fire), while every readable leg is made visibly terminal for the operator.
    Caller holds the host and all selected board locks.
    """
    disarmed: dict[str, dict[str, Any]] = {}
    for slug in boards:
        record = read_post_drain_action(slug)
        if record is None or record.get("group_id") != group_id:
            continue
        if record.get("state") != WAITING:
            continue
        try:
            disarmed[slug] = _write_post_drain_action(
                slug,
                {
                    **record,
                    "state": CANCELLED,
                    "resolution": reason,
                    "resolved_at": now,
                },
            )
        except Exception:
            # The persisted ``group_phase != armed`` remains a fail-closed fence.
            continue
    return disarmed


def queue_post_drain_group(
    boards: list[str],
    *,
    action_kind: str,
    group_id: str,
    target: Optional[str] = None,
    requested_by: Optional[str] = None,
    expires_in_seconds: Optional[int] = None,
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Persist and arm one aggregate intent without exposing a partial group.

    Every leg first records the COMPLETE expected member set in ``arming``.
    Only after all writes succeed is every leg promoted to ``armed``. Evaluation
    requires the complete matching manifest and all armed markers, so a crash,
    an exception, or a tick between any two writes cannot fire a partial group.
    """
    members = sorted(dict.fromkeys(str(board) for board in boards if str(board)))
    if not members:
        raise PostDrainActionRejected("an aggregate post-drain action needs at least one board")

    # Validate and resolve exactly once before taking locks or writing anything.
    prototype = _new_post_drain_record(
        action_kind=action_kind,
        target=target,
        requested_by=requested_by,
        expires_in_seconds=expires_in_seconds,
        now=now,
    )
    current = int(now if now is not None else time.time())
    records: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, str]] = []
    lock_members: list[tuple[Optional[str], dict[str, Any]]] = [
        (slug, {}) for slug in members
    ]
    with _group_locks(lock_members) as held:
        if not held:
            return {
                "queued": False,
                "records": records,
                "failures": [{"board": slug, "error": "dispatch_in_progress"} for slug in members],
            }
        for phase in (GROUP_ARMING, GROUP_ARMED):
            for slug in members:
                record = {
                    **prototype,
                    "group_id": group_id,
                    "group_members": members,
                    "group_phase": phase,
                }
                try:
                    records[slug] = _write_post_drain_action(slug, record)
                except Exception as exc:
                    failures.append({"board": slug, "error": str(exc)})
                    records.update(
                        _disarm_group_records(
                            members,
                            group_id,
                            reason="aggregate arming failed",
                            now=current,
                        )
                    )
                    return {"queued": False, "records": records, "failures": failures}
    return {"queued": True, "records": records, "failures": failures}


def _cancel_locked(
    board: Optional[str], *, reason: str, now: Optional[int] = None,
) -> dict[str, Any]:
    """Cancel a ``waiting`` action. Caller MUST already hold the board's tick lock."""
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


def cancel_post_drain_action(
    board: Optional[str] = None, *, reason: str = "cancelled", now: Optional[int] = None,
) -> dict[str, Any]:
    """Cancel a ``waiting`` action. A firing or already-terminal record is left alone.

    Serialized against the ``waiting -> firing`` claim on the board's dispatch
    tick lock. Without that, a cancel could read ``waiting``, an evaluation
    could claim and fire in the gap, and the cancel would then write its stale
    read back as ``cancelled`` — reporting a cancellation of an action that had
    already rebooted the host. Reporting ``cancelled: True`` has to mean nothing
    ran, or it is worse than not offering cancel at all.

    A contended cancel refuses rather than guessing, in the same shape
    :func:`pause_dispatch` and :func:`resume_dispatch` already use.
    """
    from hermes_cli import kanban_db_connect as _kbc

    db_path = _kb.kanban_db_path(board=board)
    with _kbc._dispatch_tick_lock(db_path) as held:
        if not held:
            return {
                "cancelled": False,
                "state": read_post_drain_action(board),
                "reason": "dispatch_in_progress",
            }
        return _cancel_locked(board, reason=reason, now=now)


# --- firing on observed drain ----------------------------------------------


def _group_members(
    board: Optional[str], record: Mapping[str, Any],
) -> list[tuple[Optional[str], dict[str, Any]]]:
    """Every board armed by the same aggregate request, this one included.

    Membership is by ``group_id`` in ANY state, not only the waiting ones.
    Filtering to waiting members would let a group "fire" on the survivors after
    a sibling was cancelled or expired — the same early-fire bug in a different
    dress, since a host reboot fired for two boards is not made safe by one of
    them having dropped out.
    """
    group_id = record.get("group_id")
    if not group_id:
        return [(board, dict(record))]

    expected = record.get("group_members")
    if (
        not isinstance(expected, list)
        or not expected
        or not all(isinstance(slug, str) and slug for slug in expected)
        or len(set(expected)) != len(expected)
    ):
        # A group whose complete membership is unknown is never safe to fire.
        return [(board, dict(record))]

    members: list[tuple[Optional[str], dict[str, Any]]] = []
    for slug in sorted(expected):
        sibling = read_post_drain_action(slug)
        if sibling is not None and sibling.get("group_id") == group_id:
            members.append((slug, sibling))
    return members


def _group_is_fully_armed(
    record: Mapping[str, Any], members: list[tuple[Optional[str], dict[str, Any]]],
) -> bool:
    """Every declared aggregate leg exists, agrees on membership, and is armed."""
    if not record.get("group_id"):
        return True
    expected = record.get("group_members")
    if not isinstance(expected, list) or not expected:
        return False
    expected_set = set(expected)
    if {slug for slug, _ in members} != expected_set:
        return False
    return all(
        member.get("group_id") == record.get("group_id")
        and member.get("group_members") == expected
        and member.get("group_phase") == GROUP_ARMED
        for _, member in members
    )


@contextlib.contextmanager
def _group_locks(members: list[tuple[Optional[str], dict[str, Any]]]):
    """Hold the host reservation plus EVERY member board's dispatch tick lock.

    Two separate guarantees, both required:

    * the host lock makes the ACTION exactly-once — a reboot is one machine
      event no matter how many boards armed it, so two boards' ticks racing the
      same group must not each invoke the handler;
    * each board lock makes that board's ``waiting -> firing`` transition
      atomic against its own dispatcher tick and against a concurrent cancel.

    Non-blocking throughout (the loser skips and retries next tick, exactly like
    :func:`dispatch_once`), and acquired in sorted-path order so two evaluations
    can never hold complementary halves of one group.
    """
    from hermes_cli import kanban_db_connect as _kbc

    with contextlib.ExitStack() as stack:
        if not stack.enter_context(_kbc._host_dispatch_cap_lock()):
            yield False
            return
        try:
            paths = sorted(
                (str(post_drain_path(slug)), slug) for slug, _ in members
            )
        except Exception:
            yield False
            return
        for _, slug in paths:
            db_path = _kb.kanban_db_path(board=slug)
            if not stack.enter_context(_kbc._dispatch_tick_lock(db_path)):
                yield False
                return
        yield True


def _settle_members(
    members: list[tuple[Optional[str], dict[str, Any]]],
    verdict: Mapping[str, Any],
    *,
    now: int,
    only_waiting: bool = False,
) -> dict[Optional[str], dict[str, Any]]:
    """Write one outcome across the group, so the record set never disagrees."""
    written: dict[Optional[str], dict[str, Any]] = {}
    for slug, member in members:
        if only_waiting and member.get("state") != WAITING:
            continue
        written[slug] = _write_post_drain_action(
            slug, {**member, **verdict, "resolved_at": now},
        )
    return written


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


def _claim_group_for_firing(
    board: Optional[str], record: Mapping[str, Any], now: int,
) -> dict[str, Any]:
    """Decide the whole group's next state atomically.

    Returns ``{"fire": members}`` when every member was moved to ``firing``,
    ``{"settled": record}`` when the group reached a terminal state instead, or
    ``{}`` when there was nothing to do yet.
    """
    with _group_locks(_group_members(board, record)) as held:
        if not held:
            return {}
        # Re-read under the locks: the pre-lock snapshot may be stale, and a
        # cancel or a sibling tick could have moved any member since.
        fresh = read_post_drain_action(board)
        if fresh is None or fresh.get("state") != WAITING:
            return {}
        members = _group_members(board, fresh)
        if not _group_is_fully_armed(fresh, members):
            return {}

        # Expiry first, and for ANY member: a group can only fire as a unit, so
        # one dead leg means the rest can never fire either. Leaving them
        # ``waiting`` would strand records the operator still reads as armed.
        expired = [
            slug for slug, member in members
            if isinstance(member.get("expires_at"), int) and now >= member["expires_at"]
        ]
        if expired:
            written = _settle_members(
                members,
                {
                    "state": EXPIRED,
                    "resolution": "expired before the board drained",
                },
                now=now, only_waiting=True,
            )
            return {"settled": written.get(board)}

        # A member that already left ``waiting`` (cancelled by a resume, failed,
        # or settled) disarms the rest. For a host-wide action that is the safe
        # direction: resuming ONE board of an armed group must not still reboot
        # the machine out from under it.
        settled_elsewhere = [
            slug for slug, member in members if member.get("state") != WAITING
        ]
        if settled_elsewhere:
            written = _settle_members(
                members,
                {
                    "state": CANCELLED,
                    "resolution": (
                        "another board in this group is no longer waiting: "
                        + ", ".join(str(slug) for slug in settled_elsewhere)
                    ),
                },
                now=now, only_waiting=True,
            )
            return {"settled": written.get(board)}

        # EVERY member must have drained. Firing while a sibling board still has
        # a live worker is the exact damage the drain wait exists to prevent.
        if not all(_board_is_operator_drained(slug) for slug, _ in members):
            return {}

        fired = [
            (slug, _write_post_drain_action(slug, {**member, "state": FIRING, "fired_at": now}))
            for slug, member in members
        ]
        return {"fire": fired}


def _reconcile_firing(
    board: Optional[str], record: Mapping[str, Any], now: int,
) -> Optional[dict[str, Any]]:
    """Settle a ``firing`` record from a later observation, without re-firing.

    The action can destroy the process that issued it: a reboot always does, and
    a ``service_restart`` of the dispatcher's own gateway does too. The record
    therefore routinely outlives every chance to observe it in-process, and
    without this it would sit in ``firing`` forever. This path only OBSERVES —
    the handler is never invoked again, so running it on every tick is safe.
    """
    handler = ACTION_HANDLERS.get(str(record.get("action_kind")))
    if handler is None:
        return None
    cfg = resolve_post_drain_config()
    try:
        verdict = handler.observe_after(record, cfg) or {}
    except Exception as exc:
        verdict = {"state": FAILED, "error": f"post-fire observation failed: {str(exc)[:400]}"}
    if not verdict.get("state"):
        # Still unobservable (same machine instantiation, unit not back yet).
        # Settling either way here would be a guess.
        return None
    with _group_locks(_group_members(board, record)) as held:
        if not held:
            return None
        fresh = read_post_drain_action(board)
        if fresh is None or fresh.get("state") != FIRING:
            return None
        members = [
            (slug, member) for slug, member in _group_members(board, fresh)
            if member.get("state") == FIRING
        ]
        written = _settle_members(members, verdict, now=now)
        return written.get(board)


def evaluate_post_drain_action(
    board: Optional[str] = None, *, now: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Advance this board's queued action: fire it, expire it, or settle it.

    Returns the record it transitioned, or None when there was nothing to do.
    Called from the dispatcher tick, so the trigger is entirely server-side: no
    browser, no dashboard poll, and no wall-clock scheduler is involved.
    """
    current = int(now if now is not None else time.time())
    record = read_post_drain_action(board)
    if record is None:
        return None
    state = record.get("state")
    if state == FIRING:
        return _reconcile_firing(board, record, current)
    if state != WAITING:
        return None

    outcome = _claim_group_for_firing(board, record, current)
    if "settled" in outcome:
        return outcome["settled"]
    members = outcome.get("fire")
    if not members:
        return None

    own = next((member for slug, member in members if slug == board), members[0][1])
    handler = ACTION_HANDLERS[own["action_kind"]]
    cfg = resolve_post_drain_config()

    # Observe-then-act: the pre-conditions are persisted BEFORE the side effect,
    # so a crash mid-action still leaves enough evidence to resolve the record.
    try:
        observed = handler.observe_before(own, cfg)
    except Exception as exc:
        written = _settle_members(
            members,
            {"state": FAILED, "error": f"pre-fire observation failed: {str(exc)[:400]}"},
            now=current,
        )
        return written.get(board)
    members = [
        (slug, _write_post_drain_action(slug, {**member, "observed_before": observed}))
        for slug, member in members
    ]
    own = next((member for slug, member in members if slug == board), members[0][1])

    # ONE invocation for the whole group: the action is host-wide, so a group of
    # three boards is still a single reboot.
    try:
        handler.fire(own, cfg)
    except Exception as exc:
        written = _settle_members(
            members, {"state": FAILED, "error": str(exc)[:400]}, now=current,
        )
        return written.get(board)

    try:
        verdict = handler.observe_after(own, cfg) or {}
    except Exception as exc:
        verdict = {"state": FAILED, "error": f"post-fire observation failed: {str(exc)[:400]}"}
    if not verdict.get("state"):
        # No observable verdict yet (a reboot cannot watch its own success).
        # The records stay ``firing``; a later tick reconciles them.
        return own
    written = _settle_members(members, verdict, now=current)
    return written.get(board, own)
