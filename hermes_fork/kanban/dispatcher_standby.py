"""Safe-standby embedded Kanban dispatcher with liveness/progress telemetry.

Reached from ONE upstream line: the ``dispatcher-standby`` FORK ANCHOR at the
top of ``gateway.kanban_watchers.GatewayKanbanWatchersMixin._kanban_dispatcher_watcher``.
The upstream body after that anchor (and ``_kanban_dispatcher_boot``) is
unreachable while the anchor is present; it is kept as the reference this module
ports. Upstream-sync owner: when a ``next`` <- ``upstream/main`` merge touches
``gateway/kanban_watchers.py`` or ``gateway/kanban_watchers_dispatcher.py``,
diff that dispatcher body and port behaviour changes here.
``test_anchor_routes_watcher_to_fork_runtime`` proves the anchor is live.

What this changes relative to upstream ``next``:

* Standby instead of permanent exit. Upstream returns forever when another
  gateway holds the machine-global ``<kanban_home>/kanban/.dispatcher.lock``, so
  a second gateway never takes over after the leader exits. Here a contended
  gateway stays in ``standby`` and retries at the dispatch interval,
  re-checking ``HERMES_KANBAN_DISPATCH_IN_GATEWAY`` / ``kanban.dispatch_in_gateway``
  before every attempt. Exactly one process can hold the OS lock, so there is
  never more than one leader; a standby never steals a live owner's lock.
* Fail closed. Upstream dispatches on "config control alone" when the lock
  cannot be established. Here that is standby too (``lock_state=unavailable``),
  because an unprotected second dispatcher is the failure the lock exists for.
* Safe shutdown. ``to_thread`` work cannot be cancelled. Cancelling the watcher
  while a board write is in flight keeps the lock until that thread returns
  (``phase=draining``); loop teardown that cancels even the shielded service
  keeps it until process exit (``phase=retained``) instead of letting a standby
  race the still-running writer.
* Liveness vs progress. :class:`DispatcherHealth` separates a completed tick
  (``last_success_at``: the dispatcher is alive, including healthy idle ticks)
  from worker progress (``last_progress_at``/``spawned_total``: a worker was
  started), and exposes where an in-flight tick is waiting (``phase``,
  ``phase_elapsed_seconds``, ``stalled``). Snapshots are payload-free: no task
  ids, board names, paths or error text.
* Capacity wait vs starvation. A tick that spawns nothing because every
  spawnable row is held by an occupied concurrency limit (``max_in_progress``,
  ``max_spawn``, ``max_in_progress_per_profile``) is ``last_outcome=capacity_wait``
  (``capacity_wait_boards``, ``capacity_wait_streak``) and does not feed the
  "dispatcher stuck" streak; free capacity with no spawn still does. Rules:
  :mod:`hermes_fork.kanban.capacity_wait`.

Observable per watcher at ``runner._fork_kanban_dispatcher_health.snapshot()``
(every role, including standby and disabled), and machine-wide in the leader
view ``<kanban_home>/kanban/.dispatcher_health.json`` next to the dispatcher
lock. That file has exactly one writer: the watcher currently holding the lock;
a standby never writes it. It is rewritten by a loop timer every
``_PUBLISH_PERIOD_S`` (so a hung dispatch thread still shows as a growing
``phase_elapsed_seconds``) and at every role change / completed tick, and the
leader's final ``stopped`` record is written BEFORE the lock is released, so a
promoted standby's record is never overwritten by its predecessor. ``watcher``
names the writing watcher run. A reader must check ``pid`` is alive and
``written_at`` is fresh: a crashed leader leaves its last record until a
standby promotes and overwrites it.

Ported from frozen ``dev`` f6cfcb0bb8 (standby) and 7175e94c81 (telemetry),
re-implemented at T1 instead of inline. Not ported: dev's per-tick live settings
reload (``_reload_dispatcher_settings``) and the gateway.heartbeat integration;
both need further upstream anchors.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from gateway.kanban_watchers_common import (
    _acquire_singleton_lock,
    _kanban_dispatch_allowed,
    _resolve_auto_decompose_settings,
    _to_thread_process_service,
    logger,
)
from gateway.kanban_watchers_dispatcher import (
    _KanbanDispatcher,
    _log_spawn_results,
    _resolve_dispatcher_settings,
)
from hermes_fork.kanban.capacity_wait import tick_capacity_wait_boards

HEALTH_ATTR = "_fork_kanban_dispatcher_health"
_ACTIVE_ATTR = "_fork_kanban_dispatcher_active"
SNAPSHOT_FILENAME = ".dispatcher_health.json"

_HEALTH_WINDOW = 6  # consecutive ready-but-no-spawn ticks before the "stuck" warning
_LEADER_START_DELAY_S = 5.0  # let adapters wire up before the first spawn (upstream parity)
_PUBLISH_PERIOD_S = 10.0
_STALL_FLOOR_S = 30.0
_REPORT_EVERY_S = 300.0
_IDLE_PHASES = frozenset({"sleeping", "standby", "disabled", "stopped", "retained", "starting"})


class DispatcherHealth:
    """Current dispatcher role and tick state. Mutated on the gateway loop only."""

    def __init__(self, interval: float = 60.0, *, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None:
        self.interval = interval
        self._clock, self._wall = clock, wall
        self._phase, self._phase_started = "starting", clock()
        self._tick_started: Optional[float] = None
        self._durations: dict[str, float] = {}
        self._last_report: Optional[float] = None
        self._last_stall_report: Optional[float] = None
        self.watcher_id = uuid.uuid4().hex[:12]
        self._state: dict[str, Any] = {
            "watcher": self.watcher_id, "role": "candidate", "lock_state": None, "standby_attempts": 0,
            "standby_since": None, "leader_since": None,
            "attempted_ticks": 0, "completed_ticks": 0,
            "last_attempt_at": None, "last_finished_at": None,
            "last_success_at": None, "last_outcome": None,
            "last_error_type": None, "last_tick_duration_seconds": None,
            "spawned": 0, "spawned_total": 0, "last_progress_at": None,
            "guard_deferred_boards": 0, "no_capacity_boards": 0, "error_boards": 0,
            "ready_pending": False, "no_spawn_streak": 0,
            "capacity_wait_boards": 0, "capacity_wait_streak": 0,
        }

    # -- role -------------------------------------------------------------
    def standby(self, lock_state: str) -> None:
        if self._state["role"] != "standby":
            self._state["standby_since"] = self._wall()
        self._state.update(role="standby", lock_state=lock_state,
                           standby_attempts=self._state["standby_attempts"] + 1)
        self.phase("standby")

    def lead(self) -> None:
        self._state.update(role="leader", lock_state="held", leader_since=self._wall())
        self.phase("starting")

    def end(self, role: str) -> None:
        """Terminal role: ``disabled`` / ``stopped`` / ``retained`` (still holds the lock)."""
        self._state["role"] = role
        if role != "retained":
            self._state["lock_state"] = None
        self.phase(role)

    # -- ticks ------------------------------------------------------------
    def phase(self, name: str) -> None:
        now = self._clock()
        if self._tick_started is not None:
            self._durations[self._phase] = round(now - self._phase_started, 3)
        self._phase, self._phase_started = name, now

    def begin_tick(self) -> None:
        self._tick_started = self._clock()
        self._durations = {}
        self._phase, self._phase_started = "reap", self._tick_started
        self._state.update(attempted_ticks=self._state["attempted_ticks"] + 1,
                           last_attempt_at=self._wall(), last_error_type=None)

    def finish_tick(self, results: Optional[Iterable[tuple[str, Any]]] = (), *,
                    ready_pending: bool = False, error_type: Optional[str] = None,
                    paused: bool = False, no_spawn_streak: int = 0,
                    capacity_wait_boards: int = 0, capacity_wait_streak: int = 0) -> None:
        results = list(results or ())
        spawned = sum(len(getattr(res, "spawned", ()) or ()) for _, res in results)
        guarded = sum(bool(
            getattr(res, "dispatch_paused", None) or getattr(res, "skipped_locked", False)
            or getattr(res, "respawn_guarded", ())
        ) for _, res in results)
        capped = sum(bool(
            getattr(res, "skipped_per_profile_capped", ())
            or getattr(res, "memory_pressure", None) == "critical"
        ) for _, res in results)
        errors = sum(res is None for _, res in results)
        # Precedence: an error outranks everything; "no_spawn" means ready work
        # exists but nothing started for no reported reason (not an inferred hang);
        # "capacity_wait" means every pending row is held by an occupied limit.
        outcome = next((name for cond, name in (
            (error_type or errors, "error"), (spawned, "spawned"),
            (paused or guarded, "guard_deferred"), (capacity_wait_boards, "capacity_wait"),
            (capped, "no_capacity"),
            (ready_pending, "no_spawn"),
        ) if cond), "idle")
        now = self._clock()
        duration = now - self._tick_started if self._tick_started is not None else 0.0
        self.phase("sleeping")
        self._tick_started = None
        finished = self._wall()
        self._state.update(
            completed_ticks=self._state["completed_ticks"] + 1, last_finished_at=finished,
            last_outcome=outcome, last_error_type=error_type,
            last_tick_duration_seconds=round(duration, 3), spawned=spawned,
            spawned_total=self._state["spawned_total"] + spawned,
            guard_deferred_boards=guarded, no_capacity_boards=capped, error_boards=errors,
            ready_pending=bool(ready_pending), no_spawn_streak=no_spawn_streak,
            capacity_wait_boards=capacity_wait_boards, capacity_wait_streak=capacity_wait_streak,
        )
        if outcome != "error":
            self._state["last_success_at"] = finished  # liveness, even when idle
        if spawned:
            self._state["last_progress_at"] = finished  # progress, only when a worker started
        payload = json.dumps(self.snapshot(), separators=(",", ":"))
        if self._last_report is None or now - self._last_report >= _REPORT_EVERY_S:
            logger.info("kanban dispatcher health: %s", payload)
            self._last_report = now
        else:
            logger.debug("kanban dispatcher health: %s", payload)

    def stall_threshold(self) -> float:
        return max(_STALL_FLOOR_S, self.interval * 3)

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        elapsed = max(0.0, now - self._phase_started)
        stalled = self._phase not in _IDLE_PHASES and elapsed > self.stall_threshold()
        snap = {
            **self._state, "phase": self._phase,
            "phase_elapsed_seconds": round(elapsed, 3),
            "phase_durations_seconds": dict(self._durations),
            "interval_seconds": self.interval, "stalled": stalled,
        }
        if stalled and (self._last_stall_report is None
                        or now - self._last_stall_report >= _REPORT_EVERY_S):
            logger.warning("kanban dispatcher phase stalled: phase=%s elapsed_seconds=%.1f "
                           "completed_ticks=%d; thread still in flight, not restarted",
                           self._phase, elapsed, self._state["completed_ticks"])
            self._last_stall_report = now
        return snap


def snapshot_path() -> Path:
    """Leader view, in the same kanban home as the lock that elects its writer."""
    from hermes_cli import kanban_db as kb
    return kb.kanban_home() / "kanban" / SNAPSHOT_FILENAME


class _Publisher:
    """Rewrites the leader view from a loop timer, independent of tick progress.

    Writes only while ``owns_lock()``, so the OS lock elects the single writer.
    Check-then-write cannot race: ownership is dropped only by this watcher's own
    release, which also runs on the loop thread.
    """

    def __init__(self, health: DispatcherHealth, path: Path, owns_lock: Callable[[], bool]) -> None:
        self.health, self.path, self.owns_lock = health, path, owns_lock
        self._handle: Optional[asyncio.TimerHandle] = None
        self._failed = False

    def start(self) -> None:
        self.publish()
        self._schedule()

    def _schedule(self) -> None:
        loop = asyncio.get_running_loop()
        self._handle = loop.call_later(_PUBLISH_PERIOD_S, self._on_timer)

    def _on_timer(self) -> None:
        self.publish()
        if self._handle is not None:
            self._schedule()

    def stop(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        self.publish()

    def publish(self) -> None:
        if not self.owns_lock():
            return
        # Tiny file on the loop thread by design: a saturated default executor
        # (the very stall this reports) must not stop the report.
        data = {**self.health.snapshot(), "pid": os.getpid(), "written_at": time.time()}
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
            self._failed = False
        except OSError as exc:
            if not self._failed:
                logger.warning("kanban dispatcher: cannot write health snapshot %s: %s", self.path, exc)
                self._failed = True


class _Service:
    """Runs blocking dispatcher work off-loop, shielded so cancellation cannot orphan it."""

    def __init__(self, health: DispatcherHealth) -> None:
        self.health = health
        self.pending: Optional[asyncio.Future] = None

    async def __call__(self, phase: str, func: Callable[..., Any], *args: Any) -> Any:
        self.health.phase(phase)
        self.pending = asyncio.ensure_future(_to_thread_process_service(func, *args))
        return await asyncio.shield(self.pending)

    def in_flight(self) -> bool:
        return self.pending is not None and (not self.pending.done() or self.pending.cancelled())


def _dispatch_disabled(load_config: Callable[[], Any]) -> tuple[Optional[str], dict]:
    """``(reason, cfg)`` when dispatch must not run in this gateway, else ``(None, kanban_cfg)``."""
    if os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower() in {"0", "false", "no", "off"}:
        return "HERMES_KANBAN_DISPATCH_IN_GATEWAY env", {}
    try:
        cfg = load_config()
    except Exception as exc:
        return f"cannot load config ({exc})", {}
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    if not isinstance(kanban_cfg, dict):
        kanban_cfg = {}
    if not kanban_cfg.get("dispatch_in_gateway", True):
        return "config kanban.dispatch_in_gateway=false", kanban_cfg
    return None, kanban_cfg


def _interval(kanban_cfg: dict) -> float:
    """Quiet re-read of the dispatch cadence (the leader logs full settings once)."""
    try:
        value = float(kanban_cfg.get("dispatch_interval_seconds", 60) or 60)
    except (TypeError, ValueError):
        return 60.0
    return max(value, 1.0) if math.isfinite(value) else 60.0


async def _await_leadership(runner: Any, health: DispatcherHealth) -> Optional[tuple]:
    """Block in standby until this gateway holds the dispatcher lock; None = do not dispatch."""
    try:
        from hermes_cli.config import load_config
        from hermes_cli import kanban_db as kb
    except Exception:
        logger.warning("kanban dispatcher: config loader or kanban_db unavailable; disabled")
        return None
    last_state: Optional[str] = None
    while runner._running:
        reason, kanban_cfg = _dispatch_disabled(load_config)
        if reason is not None:
            logger.info("kanban dispatcher: disabled via %s", reason)
            return None
        health.interval = interval = _interval(kanban_cfg)
        lock_path = kb.kanban_home() / "kanban" / ".dispatcher.lock"
        handle, state = _acquire_singleton_lock(lock_path)
        if state == "held":
            runner._kanban_dispatcher_lock_handle = handle  # held for the watcher's lifetime
            health.lead()
            if last_state is None:
                logger.info("kanban dispatcher: holding singleton dispatcher lock (%s)", lock_path)
            else:
                logger.warning("kanban dispatcher: assumed leadership after standby (%s)", lock_path)
            return load_config, kb, kanban_cfg
        if state != last_state:
            if state == "contended":
                logger.info("kanban dispatcher: another gateway holds the dispatcher lock (%s); "
                            "standing by, retrying every %.1fs", lock_path, interval)
            else:
                logger.error("kanban dispatcher: advisory lock unavailable at %s; NOT dispatching "
                             "until exclusion can be established (retrying every %.1fs)",
                             lock_path, interval)
        last_state = state
        health.standby(state)
        await runner._sleep_between_ticks(interval)
    return None


async def _lead(runner: Any, health: DispatcherHealth, service: _Service, publish: Callable[[], None]) -> None:
    boot = await _await_leadership(runner, health)
    publish()
    if boot is None:
        health.end("disabled" if runner._running else "stopped")
        return
    load_config, kb, kanban_cfg = boot
    settings = _resolve_dispatcher_settings(kanban_cfg, kb)
    health.interval = interval = settings.interval
    dispatcher = _KanbanDispatcher(kb, settings)
    await asyncio.sleep(_LEADER_START_DELAY_S)

    from hermes_cli import kanban_db_dispatch as kbd

    bad_ticks, cap_ticks, last_warn_at = 0, 0, 0.0
    logger.info("kanban dispatcher: embedded in gateway (interval=%.1fs)", interval)
    while runner._running:
        health.begin_tick()
        results: Optional[list] = None
        ready_pending, paused, error_type, cap_boards = False, False, None, 0
        try:
            # Reap before per-board work so a board DB failure cannot block cleanup.
            pids = await service("reap", kbd.reap_worker_zombies)
            if pids:
                logger.info("kanban dispatcher: reaped %d zombie worker(s), pids=%s", len(pids), pids)
        except Exception as exc:
            error_type = type(exc).__name__
            logger.exception("kanban dispatcher: zombie reaper failed")
        try:
            if not _kanban_dispatch_allowed():  # `hermes pause`
                bad_ticks, cap_ticks, paused = 0, 0, True
            else:
                health.phase("configure")
                ad_enabled, ad_per_tick = _resolve_auto_decompose_settings(load_config)  # live, #49638
                if ad_enabled:
                    await service("auto_decompose", dispatcher.auto_decompose_tick, ad_per_tick)
                results = await service("dispatch", dispatcher.tick_once)
                any_spawned = _log_spawn_results(results)
                ready_pending = await service("ready_probe", dispatcher.ready_nonempty)
                if ready_pending and not any_spawned:
                    cap_boards = await service("capacity_probe", tick_capacity_wait_boards, results)
                bad_ticks = bad_ticks + 1 if ready_pending and not any_spawned and not cap_boards else 0
                cap_ticks = cap_ticks + 1 if cap_boards else 0
            now = time.time()
            if bad_ticks >= _HEALTH_WINDOW and now - last_warn_at >= _REPORT_EVERY_S:
                held = kbd.describe_suppression(res for _slug, res in (results or []))
                logger.warning(
                    "kanban dispatcher stuck: ready queue non-empty for %d consecutive ticks but "
                    "0 workers spawned.%s Dispatcher itself is live (ticks completing). Check "
                    "profile health (venv, PATH, credentials) and `hermes kanban list --status ready`.",
                    bad_ticks, f" Last tick held back: {held}." if held else "",
                )
                last_warn_at = now
        except asyncio.CancelledError:
            logger.debug("kanban dispatcher: cancelled")
            raise
        except Exception as exc:
            error_type = type(exc).__name__
            logger.exception("kanban dispatcher: unexpected watcher error")
        health.finish_tick(results, ready_pending=ready_pending, error_type=error_type,
                           paused=paused, no_spawn_streak=bad_ticks,
                           capacity_wait_boards=cap_boards, capacity_wait_streak=cap_ticks)
        publish()
        await runner._sleep_between_ticks(interval)


async def run_watcher(runner: Any) -> None:
    """Anchor target: the whole embedded-dispatcher lifecycle for one gateway runner."""
    if getattr(runner, _ACTIVE_ATTR, False):
        logger.warning("kanban dispatcher: watcher already active; ignoring duplicate start")
        return
    if runner._owns_kanban_dispatcher_lock():
        logger.error("kanban dispatcher: leadership still retained by an earlier watcher; refusing duplicate start")
        return
    setattr(runner, _ACTIVE_ATTR, True)
    health = DispatcherHealth()
    setattr(runner, HEALTH_ATTR, health)
    publisher = _Publisher(health, snapshot_path(), runner._owns_kanban_dispatcher_lock)
    publisher.start()
    service = _Service(health)

    def finish(fut: Optional[asyncio.Future]) -> None:
        if fut is not None and fut.cancelled():
            # Loop teardown cancelled the shielded service; its thread may still be
            # writing. Keep the lock (process exit releases it) rather than let a
            # standby race that writer.
            logger.error("kanban dispatcher: service cancelled at loop teardown; leadership "
                         "retained until process exit to protect in-flight writes")
            health.end("retained")
            publisher.stop()
            return
        if fut is not None:
            fut.exception()  # consume; the watcher already logged or was cancelled
        if health.snapshot()["role"] != "disabled":
            health.end("stopped")
        publisher.stop()  # final leader record while the lock is still held
        runner._release_kanban_dispatcher_lock()
        setattr(runner, _ACTIVE_ATTR, False)

    try:
        await _lead(runner, health, service, publisher.publish)
    finally:
        if service.in_flight():
            health.phase("draining")
            publisher.publish()
            logger.info("kanban dispatcher: stopping; lock held until in-flight work returns")
            service.pending.add_done_callback(finish)  # type: ignore[union-attr]
        else:
            finish(None)
