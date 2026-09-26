"""Kanban worker-launcher and systemd-argv construction.

Extracted from ``hermes_cli.kanban_db_dispatch`` behind one
``# >>> FORK ANCHOR: kanban-worker-launcher <<<`` import site in that file.
Covers: the optional operator-configured ``kanban.worker_launcher`` prefix
(``_worker_launcher_prefix`` / ``_apply_worker_launcher`` and its systemd-run
user-scope helpers), the restart-safe supervised-child argv wrapper
(``_restart_safe_worker_argv``), and the standalone worker-log timestamp
filter argv/spawn (``_worker_log_stamper_argv`` / ``_start_worker_log_stamper``).

Pure argv/env construction plus a couple of narrow subprocess spawns — no
schema/migration or dashboard-payload ownership. Origin-resident helpers this
module still needs (``_log``, ``_IS_WINDOWS``) are reached late-bound via
``_kb`` (import-cycle breaking, mirroring how ``kanban_db_dispatch.py``
already reaches ``kanban_db.py``) so monkeypatching ``kanban_db.<name>``
keeps working. ``Task`` is imported only for type checking to avoid a runtime
cycle with ``kanban_db``.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from typing import Optional
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task


def _worker_log_stamper_argv(log_path: Path) -> list[str]:
    """argv for the standalone per-line timestamp filter.

    Invoked by absolute script path with this interpreter, so it needs neither
    the ``hermes_cli`` package on ``PYTHONPATH`` nor an external binary.
    """
    from hermes_cli import kanban_log_stamp

    return [sys.executable, os.path.abspath(kanban_log_stamp.__file__), str(log_path)]


def _start_worker_log_stamper(
    task: Task, log_path: Path
) -> "Optional[tuple[Any, int]]":
    """Start the timestamp filter and return ``(proc, write_fd)``, or None.

    The worker's stdout/stderr is wired to ``write_fd`` instead of straight to
    the log file; the filter on the other end stamps each line and appends it.

    The filter is spawned through the SAME restart-safe path as the worker
    (own session, and its own transient systemd scope when this dispatcher is
    supervised). That is load-bearing, not defensive: left inside a supervised
    dispatcher's cgroup, ``systemctl restart`` would kill the filter, close the
    read end of the pipe, and SIGPIPE a live worker that was supposed to
    survive the restart.

    Returns None — and the caller falls back to today's byte-for-byte raw
    ``stdout=log_f`` spawn — whenever the filter cannot be established. A
    logging refinement must never stall the board or endanger a worker.
    """
    if task.current_run_id is None:
        # Mirrors _restart_safe_worker_argv: never mint an untraceable scope.
        return None
    try:
        argv = _worker_log_stamper_argv(log_path)
    except Exception:
        return None
    stamper_env = dict(os.environ)
    try:
        from tools.process_registry import restart_safe_supervised_child_argv

        argv = restart_safe_supervised_child_argv(
            argv,
            unit_suffix=f"kanban-log-{task.id}-run-{task.current_run_id}",
            env=stamper_env,
        )
    except Exception as exc:
        _kb._log.warning(
            "kanban: could not place the worker-log timestamp filter for %s in a "
            "restart-safe scope (%s); logging this run without timestamps",
            task.id, exc,
        )
        return None
    try:
        read_fd, write_fd = os.pipe()
    except OSError:
        return None
    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv is a fixed list built above
            argv,
            stdin=read_fd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=stamper_env,
            start_new_session=True,
            creationflags=subprocess.CREATE_NO_WINDOW if _kb._IS_WINDOWS else 0,
        )
    except Exception as exc:
        with contextlib.suppress(OSError):
            os.close(read_fd)
        with contextlib.suppress(OSError):
            os.close(write_fd)
        _kb._log.warning(
            "kanban: worker-log timestamp filter failed to start for %s (%s); "
            "logging this run without timestamps", task.id, exc,
        )
        return None
    # Only the filter needs the read end; holding a copy here would keep the
    # pipe from ever reaching EOF.
    with contextlib.suppress(OSError):
        os.close(read_fd)
    return proc, write_fd


def _restart_safe_worker_argv(
    task: Task,
    command: list[str],
    env: dict[str, str] | None = None,
    working_directory: str | None = None,
    service_environment: dict[str, str] | None = None,
) -> list[str]:
    """Wrap a worker spawned by a supervised systemd unit in the shared restart-safe scope.

    ``env`` is the child's environment, mutated in place with the user-bus variables the
    wrapped ``systemd-run --user`` needs — the dispatcher snapshots ``os.environ`` before
    this call, so without it the spawn execs systemd-run with no bus and dies instantly.
    """
    from tools.process_registry import restart_safe_supervised_child_argv

    if task.current_run_id is None:
        # Outside managed systemd this is harmless, but a managed dispatch must
        # never mint an untraceable scope.  Check topology through the shared
        # helper first, using a placeholder suffix that cannot be launched.
        scoped = restart_safe_supervised_child_argv(
            command, unit_suffix=f"kanban-{task.id}-run-missing"
        )
        if scoped is not command:
            raise RuntimeError(
                "cannot create restart-safe systemd scope for Kanban worker: "
                "the claimed task has no current run id"
            )
        return command

    return restart_safe_supervised_child_argv(
        command,
        unit_suffix=f"kanban-{task.id}-run-{task.current_run_id}",
        env=env,
        working_directory=working_directory,
        service_environment=service_environment,
    )


def _worker_launcher_prefix() -> list[str]:
    """Resolve ``kanban.worker_launcher`` from config.

    Empty list (the default) means "no launcher" — every caller must treat
    that as a no-op and fall through to plain ``Popen``, keeping behaviour on
    Windows/macOS/non-systemd Linux byte-for-byte identical to today.

    Fails OPEN for a plain unresolvable binary (typo, uninstalled tool): log
    once and return ``[]`` so a misconfigured optional knob degrades to
    today's spawn behaviour instead of stalling the board. But for a
    ``systemd-run --user`` launcher specifically, this fails CLOSED against
    an unreachable user D-Bus: the gateway process's environment commonly
    lacks ``XDG_RUNTIME_DIR``/``DBUS_SESSION_BUS_ADDRESS`` even though the
    binary itself resolves fine via ``which()``, and letting that through
    means every spawn using this launcher fails at ``systemd-run`` time
    ("Failed to connect to bus: No medium found") instead of degrading —
    see :func:`_systemd_user_bus_reachable`. This is deliberately the
    opposite failure philosophy from ``restart_safe_gateway_child_argv``'s
    fail-closed ``RuntimeError``: that path guards an actual
    gateway-restart-survival contract for a narrow supervised case, while
    this one is plain opt-in operator config that must never stall the
    board either way — closed here means "don't use the launcher", not
    "refuse to spawn the worker".
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config().get("kanban") or {}
    except Exception:
        return []
    raw = cfg.get("worker_launcher") or []
    if not isinstance(raw, (list, tuple)):
        return []
    prefix = [str(p) for p in raw if p]
    if not prefix:
        return []
    import shutil

    if shutil.which(prefix[0]) is None:
        _kb._log.warning(
            "kanban.worker_launcher binary %r not found on PATH; "
            "falling back to plain Popen spawn for this worker", prefix[0],
        )
        return []
    if os.path.basename(prefix[0]) == "systemd-run" and "--user" in prefix:
        if not _systemd_user_bus_reachable():
            return []
    return prefix


def _resolve_systemd_user_bus_env() -> "tuple[str, str]":
    """Resolve ``XDG_RUNTIME_DIR``/``DBUS_SESSION_BUS_ADDRESS`` for this
    process's own uid, falling back to the standard ``/run/user/<uid>``
    convention when the environment lacks them.

    Same derivation as hermes-dev-infrastructure PR #22's ``getent``-based
    playbook fix (which resolves a *different* user's uid via ``getent
    passwd`` before building the same paths); here we already run as the
    target uid, so ``os.getuid()`` replaces the ``getent`` lookup and the
    path convention is identical. Reuses ``hermes_cli.gateway``'s
    ``_runtime_dir_is_ours`` guard so a leaked ``XDG_RUNTIME_DIR`` from
    another user (e.g. a root shell where the env still points at
    ``/run/user/0``, #86558) is never trusted as our own bus directory.
    """
    from hermes_cli.gateway import _runtime_dir_is_ours

    uid = os.getuid()  # windows-footgun: ok — POSIX systemd helper, never invoked on Windows
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    fallback = f"/run/user/{uid}"
    if not xdg or not _runtime_dir_is_ours(xdg):
        xdg = fallback if _runtime_dir_is_ours(fallback) else (xdg or fallback)
    bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS") or f"unix:path={xdg}/bus"
    return xdg, bus


def _is_systemd_user_scope_prefix(prefix: list[str]) -> bool:
    """True if *prefix* is a ``systemd-run --user`` (optionally ``--scope``) launcher entry."""
    return bool(prefix) and os.path.basename(prefix[0]) == "systemd-run" and "--user" in prefix


def _worker_launcher_env_overrides(prefix: list[str]) -> dict[str, str]:
    """Extra env vars a resolved ``kanban.worker_launcher`` prefix needs on the spawned child.

    ``_systemd_user_bus_reachable`` resolves ``XDG_RUNTIME_DIR``/
    ``DBUS_SESSION_BUS_ADDRESS`` to decide whether a ``systemd-run --user``
    launcher entry is usable, but ``systemd-run --user`` itself finds the
    bus via its OWN process environment at spawn time — resolving the
    values for the reachability check and then discarding them meant the
    check could pass (the uid fallback resolves a real, reachable socket)
    while the actual spawn still failed with "Failed to connect to bus:
    No medium found", because the child's environment (built by
    ``build_subprocess_env``, which may already have stripped these vars)
    never received them. Only fires for a ``systemd-run --user`` launcher
    entry; every other launcher configuration (or the default ``[]``) is
    untouched.
    """
    if not _is_systemd_user_scope_prefix(prefix):
        return {}
    xdg, bus = _resolve_systemd_user_bus_env()
    return {"XDG_RUNTIME_DIR": xdg, "DBUS_SESSION_BUS_ADDRESS": bus}


def _cmd_is_systemd_user_scope_wrapped(command: list[str]) -> bool:
    """True if *command* is a systemd-run user scope invocation.

    ``systemd-run --pipe`` implicitly creates a scope on current systemd, so
    recognize that form as well as an explicit ``--scope``.  The shared
    restart-safe helper deliberately uses ``--pipe`` to preserve the child's
    stdio, and its unit is still registered as ``.scope``.
    """
    if not command or os.path.basename(command[0]) != "systemd-run":
        return False
    return "--user" in command and ("--scope" in command or "--pipe" in command)


def _extract_unit_from_systemd_scope_argv(command: list[str]) -> Optional[str]:
    """Recover the registered unit id from a systemd-run user invocation.

    Explicit ``--scope`` registers ``.scope``; the restart-safe helper's
    ``--pipe`` form instead registers a transient ``.service``.  Persist the
    suffix systemd will actually create so later ``systemctl --user`` calls
    address the same live unit.
    """
    suffix = ".scope" if "--scope" in command else ".service"
    for i, part in enumerate(command):
        if part == "--unit" and i + 1 < len(command):
            value = command[i + 1]
            return value if value.endswith((".scope", ".service")) else f"{value}{suffix}"
        if part.startswith("--unit="):
            value = part[len("--unit="):]
            return value if value.endswith((".scope", ".service")) else f"{value}{suffix}"
    return None


_SYSTEMD_USER_BUS_WARNED = False


def _systemd_user_bus_reachable() -> bool:
    """True if the resolved user D-Bus socket actually exists on disk.

    Fails CLOSED: a ``kanban.worker_launcher`` entry that shells out to
    ``systemd-run --user`` must never be handed to ``Popen`` when the bus is
    unreachable — the prior guard checked only ``shutil.which()``, which
    passes even though the gateway's stripped environment commonly has no
    ``XDG_RUNTIME_DIR``/``DBUS_SESSION_BUS_ADDRESS``, so enabling the knob as
    documented let ``systemd-run --user`` fail at spawn time instead of
    degrading to a plain Popen spawn. Logs once per process, not once per
    spawn, so a persistently-unreachable bus doesn't spam the log every tick.
    """
    global _SYSTEMD_USER_BUS_WARNED
    xdg, bus = _resolve_systemd_user_bus_env()
    socket_path = bus[len("unix:path="):] if bus.startswith("unix:path=") else os.path.join(xdg, "bus")
    if socket_path and os.path.exists(socket_path):
        return True
    if not _SYSTEMD_USER_BUS_WARNED:
        _kb._log.warning(
            "kanban.worker_launcher configures a systemd-run --user spawn but no "
            "reachable user D-Bus socket was found (checked %s); falling back to "
            "plain Popen spawn for this worker until the bus becomes reachable",
            socket_path or "<unresolved>",
        )
        _SYSTEMD_USER_BUS_WARNED = True
    return False


def _worker_launcher_unit_name(task: Task) -> str:
    """``--unit=`` value for a launcher-wrapped worker; stable across the task's runs.

    Always carries the explicit ``.scope`` suffix: every later
    ``systemctl --user`` query/stop against this unit must use the exact
    same string ``systemd-run`` registered it under, or it silently
    resolves to a same-named ``.service`` unit that never existed
    (``rc=5``/"not loaded", worker left running).
    """
    run_part = task.current_run_id if task.current_run_id is not None else "missing"
    return f"kanban-{task.id}-run-{run_part}.scope"


def _apply_worker_launcher(task: Task, command: list[str]) -> "tuple[list[str], Optional[str]]":
    """Prepend ``kanban.worker_launcher`` (when configured and resolvable) to *command*.

    Returns ``(argv, worker_unit)``. ``worker_unit`` is the unit name minted
    via an appended ``--unit=<name>`` flag when the launcher fired, else
    ``None`` — the default/no-launcher path, where ``argv`` is ``command``
    unchanged. The operator supplies only the launcher binary + its own
    flags in config; the dispatcher appends ``--unit=<name>`` and the
    trailing ``-- <command>`` itself so every launcher invocation carries a
    traceable, task-scoped unit name without the operator hand-typing it.

    Double-scope guard: in the supervised-gateway topology,
    ``_restart_safe_worker_argv`` may have already wrapped *command* in its
    own ``systemd-run --user --scope``. If the resolved launcher prefix is
    ALSO a ``systemd-run --user --scope`` invocation, nesting a second one
    around the first is not a stronger wrap — ``--scope`` is a transparent
    exec, so the outer ``systemd-run`` execs straight into the inner one
    and only the INNER unit ever registers with systemd; the outer
    (persisted) unit name would be permanently unresolvable
    (``LoadState=not-found``), defeating every termination path that
    depends on ``tasks.worker_unit`` actually existing. In that case, skip
    the redundant outer wrap and track the ALREADY-REAL inner unit instead
    — the worker is still isolated in exactly one scope, and
    ``worker_unit`` still refers to a unit that genuinely exists.
    """
    prefix = _worker_launcher_prefix()
    if not prefix:
        return command, None
    if _is_systemd_user_scope_prefix(prefix) and _cmd_is_systemd_user_scope_wrapped(command):
        return command, _extract_unit_from_systemd_scope_argv(command)
    unit_name = _worker_launcher_unit_name(task)
    argv = [*prefix, f"--unit={unit_name}", "--", *command]
    return argv, unit_name


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db_dispatch`` imports from it,
# mirroring ``hermes_fork/kanban/dispatch_resilience.py``'s own tail import.
from hermes_cli import kanban_db as _kb  # noqa: E402
