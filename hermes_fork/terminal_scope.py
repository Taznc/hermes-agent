"""Local terminal commands of a systemd-*service* backend run in their own transient user scope.

Why (t_5efd7328 / t_393a599c / t_9669953a): the web-desktop backend is a system service
(``hermes-webdesktop-backend.service``, ``KillMode=process``, ``MemoryHigh``/``MemorySwapMax``).
Every terminal command an agent turn runs there, foreground or background, parent or
``delegate_task`` child, used to land in the backend's own cgroup. One memory-heavy run (vitest,
a build) pushed that cgroup past ``memory.high`` with swap exhausted. The kernel then throttled
*every* task in it, the event-loop thread included (wchan ``mem_cgroup_handle_over_high``), and
health probes timed out until the reconciler restarted the unit. ``KillMode=process`` left the
commands behind as "left-over process" orphans that kept charging the next incarnation.

Upstream scopes background commands only for the supervised messaging gateway
(``tools/process_registry._scope_argv``). Upstream tried the same for dashboard/serve and
reverted it (8ffb3c65500): serve never recovers the process checkpoint, so scoped survivors of a
restart could not be listed, polled or killed. This module covers that case too.

* :func:`scope_foreground` wraps a foreground command. The ``terminal-scope`` anchor in
  ``LocalEnvironment._run_bash`` calls it. :func:`install` wraps ``ProcessRegistry._scope_argv``
  for background commands. It runs from the fork gateway seams at boot and again from the
  anchor; it is a method wrap, not an upstream edit, because ``tools/process_registry.py`` is
  already at its fork budget. Both wraps use ``systemd-run --user --scope`` with upstream's own
  argv builder, probe and per-scope ``MemoryMax`` (``TERMINAL_LOCAL_MEMORY_MAX_MB``, at most
  4 GiB). The scope's processes are charged to their own cgroup under the user manager, never
  to the backend's. ``systemd-run --scope`` execs the command, so the Popen pid, its process
  group and every existing kill path stay the same.
* Scope names carry the service and its ``INVOCATION_ID``. At serve start,
  :func:`reap_stale_scopes` stops this service's scopes from earlier invocations, so a restart
  no longer leaves orphans behind (upstream's intent: serve's processes die with it). Background
  commands started with ``persist_on_release`` get a ``keep`` scope, which is never reaped.

Active only where it applies: Linux, the process sits directly in the cgroup of a web-desktop
backend unit (:data:`HOST_UNITS`; a placement test, not an inherited env marker), it is not the
supervised gateway (upstream owns that), and ``HERMES_FORK_TERMINAL_SCOPE`` is not
``0``/``off``/``false``/``no``. It also needs a usable user scope (upstream's cached probe). When
any of these is missing, the argv comes back unchanged and behaviour is upstream's.

Why an allowlist and not any ``*.service`` (t_059acb3a): the managed workers of
``hermes-gateway.service`` (kanban worker shells) sit directly in that unit's cgroup without
being its supervised main pid. Under an "any service" rule their foreground commands got a
4 GiB ``MemoryMax`` (an OOM kill for a big tsc/vitest run), and a gateway restart would sweep
their in-flight scopes. Neither the gateway nor the dashboard runs the tightly swapped
``MemoryHigh`` unit this module exists for, so both keep upstream's behaviour.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

PREFIX = "hermes-term-"
DISABLE_ENV = "HERMES_FORK_TERMINAL_SCOPE"
MEMORY_MAX_ENV = "HERMES_FORK_TERMINAL_SCOPE_MEMORY_MAX"
_OFF_VALUES = frozenset({"0", "off", "false", "no"})
# kind: fg = foreground command, bg = background process (reaped on restart),
# keep = persist_on_release background process (never reaped).
_NAME_RE = re.compile(
    r"^hermes-term-(?P<slug>.+)-(?P<inv>[0-9a-f]{12}|noinvocation)-(?P<kind>fg|bg|keep)-(?P<rest>.+)\.scope$")
_CGROUP_FILE = Path("/proc/self/cgroup")
# Units whose terminal commands are scoped and whose stale scopes are swept: the `hermes serve`
# web-desktop backends (hermes-webdesktop-backend, hermes-webdesktop-next-backend).
HOST_UNITS = ("hermes-webdesktop*backend.service",)

_warned: set[str] = set()


def _degraded(site: str, message: str) -> None:
    """Log a fall-back to the unscoped path: WARNING once per site and process, DEBUG after.

    These paths rely on private ``tools.process_registry`` names. An upstream rename has to show
    in the log, not just quietly put commands back into the backend's cgroup."""
    key = f"{os.getpid()}:{site}"
    first = key not in _warned
    _warned.add(key)
    logger.log(logging.WARNING if first else logging.DEBUG, message, exc_info=True)


_placement_lock = threading.Lock()
_placement: Optional[tuple[int, Optional[str]]] = None  # (pid, service unit or None)


def _enabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip().lower() not in _OFF_VALUES


def _read_service_unit() -> Optional[str]:
    """The ``*.service`` whose cgroup this process sits in directly, else None."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        lines = _CGROUP_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("0::"):
            leaf = line.partition("::")[2].rstrip("/").rpartition("/")[2]
            # user@<uid>.service is the user manager's own unit, never a Hermes host.
            if leaf.endswith(".service") and not leaf.startswith("user@"):
                return leaf
            return None
    return None


def _service_unit() -> Optional[str]:
    """Cached per pid: placement only changes across fork() (or a scope move, which we never do)."""
    global _placement
    pid = os.getpid()
    cached = _placement
    if cached is None or cached[0] != pid:
        with _placement_lock:
            cached = _placement
            if cached is None or cached[0] != pid:
                cached = _placement = (pid, _read_service_unit())
    return cached[1]


def host_unit() -> Optional[str]:
    """The service whose terminal commands this module scopes, or None when inactive."""
    if not _enabled():
        return None
    unit = _service_unit()
    if not unit or not any(fnmatchcase(unit, pattern) for pattern in HOST_UNITS):
        return None
    try:
        from tools.process_registry import _is_supervised_gateway_process

        if _is_supervised_gateway_process():
            return None  # upstream's own policy covers the gateway
    except Exception:
        _degraded("gateway-probe", "terminal scope: gateway identity probe failed; commands run unscoped")
        return None
    return unit


def _slug(unit: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", unit[: -len(".service")])


def _invocation() -> str:
    inv = re.sub(r"[^0-9a-f]", "", os.environ.get("INVOCATION_ID", "").lower())
    return inv[:12] if len(inv) >= 12 else "noinvocation"


def unit_name(unit: str, kind: str, suffix: str) -> str:
    """``hermes-term-<service>-<invocation12>-<kind>-<suffix>`` (no ``.scope``; systemd-run adds it)."""
    safe_suffix = re.sub(r"[^A-Za-z0-9_-]", "_", suffix)[:64]
    return f"{PREFIX}{_slug(unit)}-{_invocation()}-{kind}-{safe_suffix}"


def _scope_argv(unit: str, kind: str, suffix: str, argv: list[str]) -> Optional[tuple[str, list[str]]]:
    """(scope name, wrapped argv) via upstream's argv builder, or None when no scope is usable."""
    from tools.process_registry import _systemd_run_user_scope_available, _systemd_scope_argv

    if not _systemd_run_user_scope_available():
        return None
    binary = shutil.which("systemd-run")
    if not binary:
        return None
    name = unit_name(unit, kind, suffix)
    wrapped = _systemd_scope_argv(binary, name, *argv)
    # Upstream caps each scope at min(host cgroup, RAM/2, 4 GiB). An interactive backend may run
    # bigger one-off builds, so allow an operator override (systemd syntax: 8G, infinity, ...).
    override = os.environ.get(MEMORY_MAX_ENV, "").strip()
    if override and re.fullmatch(r"(?:\d+[KMGT]?|infinity)", override):
        sep = wrapped.index("--")
        wrapped = [f"MemoryMax={override}" if i < sep and a.startswith("MemoryMax=") else a
                   for i, a in enumerate(wrapped)]
    return name, wrapped


def _runtime_dir_prefix() -> list[str]:
    """``systemd-run --user`` reaches the manager through ``$XDG_RUNTIME_DIR/systemd/private``.
    A service env may lack the variable: derive it the way upstream's probe does and set it for
    systemd-run only when it is missing. A command-level ``DBUS_SESSION_BUS_ADDRESS`` (Bot Desktop)
    is left alone: systemd-run does not need it, and the command keeps seeing it."""
    if os.environ.get("XDG_RUNTIME_DIR"):
        return []
    from tools.process_registry import systemd_user_bus_env

    runtime_dir = systemd_user_bus_env().get("XDG_RUNTIME_DIR")
    env_bin = shutil.which("env") or "/usr/bin/env"
    return [env_bin, f"XDG_RUNTIME_DIR={runtime_dir}"] if runtime_dir else []


def scope_foreground(args: list[str]) -> list[str]:
    """Wrap a foreground ``bash -c`` argv in a transient scope when this backend qualifies."""
    try:
        install()
    except Exception:
        # The registry wrap serves background spawns only; foreground scoping does not need it.
        _degraded("install", "terminal scope: install failed; background spawns run unscoped")
    try:
        unit = host_unit()
        if not unit:
            return args
        scoped = _scope_argv(unit, "fg", uuid.uuid4().hex[:12], list(args))
        if scoped is None:
            return args
        return _runtime_dir_prefix() + scoped[1]
    except Exception:
        _degraded("foreground", "terminal scope: foreground wrap failed; commands run unscoped")
        return args


def scope_background(session: Any, argv: list[str], unit_suffix: str) -> Optional[list[str]]:
    """Wrapped argv for a background process (and ``session.systemd_unit`` set), or None for upstream's path.

    Setting ``systemd_unit`` makes upstream's spawn add the user-bus env, and makes its kill stop
    the scope. That reaps descendants that ``setsid()`` past the process-group kill."""
    try:
        unit = host_unit()
        if not unit:
            return None
        kind = "keep" if getattr(session, "persist_on_release", False) else "bg"
        scoped = _scope_argv(unit, kind, unit_suffix, list(argv))
        if scoped is None:
            return None
        session.systemd_unit = f"{scoped[0]}.scope"
        return scoped[1]
    except Exception:
        _degraded("background", "terminal scope: background wrap failed; using upstream's path")
        return None


def stale_scope_names(listed: list[str], unit: str, invocation: str) -> list[str]:
    """Scopes in *listed* that this service minted in another invocation and that may be reaped."""
    slug = _slug(unit)
    stale = []
    for name in listed:
        match = _NAME_RE.match(name)
        if (match and match["slug"] == slug and match["inv"] != invocation
                and match["kind"] in ("fg", "bg")):
            stale.append(name)
    return stale


def reap_stale_scopes() -> list[str]:
    """Stop this service's ``fg``/``bg`` terminal scopes from earlier invocations. Returns the names.

    Only the unit's current main process reaps (``SYSTEMD_EXEC_PID`` equals our pid and
    ``INVOCATION_ID`` is set). An orphan from an older incarnation inherits a stale
    ``INVOCATION_ID``, so it must never take the current incarnation's scopes for stale ones."""
    unit = host_unit()
    invocation = _invocation()
    if (not unit or invocation == "noinvocation"
            or os.environ.get("SYSTEMD_EXEC_PID") != str(os.getpid())):
        return []
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return []
    from tools.process_registry import systemd_user_bus_env

    env = systemd_user_bus_env()
    listed = subprocess.run(
        [systemctl, "--user", "list-units", "--all", "--plain", "--no-legend", "--type=scope",
         f"{PREFIX}{_slug(unit)}-*"],
        capture_output=True, text=True, timeout=10, env=env, stdin=subprocess.DEVNULL, check=False)
    names = [line.split()[0] for line in listed.stdout.splitlines() if line.strip()]
    stale = stale_scope_names(names, unit, invocation)
    if stale:
        # --no-block: systemd sends SIGTERM, then SIGKILL after the scope's stop timeout.
        subprocess.run([systemctl, "--user", "stop", "--no-block", *stale], capture_output=True,
                       timeout=15, env=env, stdin=subprocess.DEVNULL, check=False)
        logger.warning("stopped %d terminal scope(s) left by a previous %s invocation: %s",
                       len(stale), unit, ", ".join(stale))
    return stale


def _reap_in_background() -> None:
    """Run :func:`reap_stale_scopes` off the caller's thread (it shells out to systemctl)."""
    def _run() -> None:
        try:
            reap_stale_scopes()
        except Exception:
            logger.warning("stale terminal scope sweep failed", exc_info=True)

    threading.Thread(target=_run, name="fork-terminal-scope-sweep", daemon=True).start()


_install_lock = threading.Lock()
_installed_pid: Optional[int] = None


def install() -> None:
    """Idempotent per process. It wraps ``ProcessRegistry._scope_argv`` so background spawns get a
    scope, and starts the one stale-scope sweep for this process.

    Called from ``hermes_fork.gateway.registry`` at gateway or serve boot, so the sweep runs even
    when no command ever does. Also called from the ``terminal-scope`` anchor, which covers hosts
    that never import the TUI gateway. A local background spawn always comes after its
    ``LocalEnvironment`` exists, and that environment's ``init_session`` runs through the anchor.
    So the wrapper is in place before the first background spawn either way.

    The pid is recorded only after the wrap is in place. A concurrent first caller waits on the
    lock instead of spawning unwrapped, and a failed attempt (an import error) is retried."""
    global _installed_pid
    pid = os.getpid()
    if _installed_pid == pid:
        return
    with _install_lock:
        if _installed_pid == pid:
            return
        _wrap_registry()
        sweep = host_unit() is not None
        _installed_pid = pid
    if sweep:
        _reap_in_background()


def _wrap_registry() -> None:
    """Wrap ``ProcessRegistry._scope_argv`` once (the class attribute survives fork)."""
    from tools.process_registry import ProcessRegistry

    upstream = ProcessRegistry._scope_argv
    if getattr(upstream, "_fork_terminal_scope", False):
        return

    def _scope_argv(self, session, safe_command, unit_suffix, label):
        argv = upstream(self, session, safe_command, unit_suffix, label)
        if getattr(session, "systemd_unit", ""):
            return argv  # upstream already scoped it (supervised gateway)
        return scope_background(session, argv, unit_suffix) or argv

    _scope_argv._fork_terminal_scope = True  # type: ignore[attr-defined]
    _scope_argv.__wrapped__ = upstream  # type: ignore[attr-defined]
    ProcessRegistry._scope_argv = _scope_argv
