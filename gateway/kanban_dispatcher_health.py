"""Bounded, payload-free dispatcher telemetry for the existing gateway heartbeat.

Updated on the gateway loop, never from worker threads. A completed idle tick
is liveness; spawning a worker is progress. Neither implies the other.
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterable

from gateway.kanban_watchers_common import logger


class DispatcherHealth:
    """One current tick, no task ids, board names, paths, prompts or error text."""

    def __init__(self, interval: float = 60.0) -> None:
        self.interval = interval
        self._phase = "starting"
        self._phase_started = time.monotonic()
        self._tick_started: float | None = None
        self._durations: dict[str, float] = {}
        self._last_report: float | None = None
        self._last_stall_report: float | None = None
        self._state: dict[str, Any] = {
            "attempted_ticks": 0, "completed_ticks": 0,
            "last_attempt_at": None, "last_finished_at": None,
            "last_success_at": None, "last_outcome": None,
            "last_error_type": None, "last_tick_duration_seconds": None,
            "spawned": 0, "guard_deferred_boards": 0,
            "no_capacity_boards": 0, "error_boards": 0, "ready_pending": False,
        }

    def phase(self, name: str) -> None:
        now = time.monotonic()
        if self._tick_started is not None:
            self._durations[self._phase] = round(now - self._phase_started, 3)
        self._phase, self._phase_started = name, now

    def begin_tick(self) -> None:
        self._tick_started = time.monotonic()
        self._durations = {}
        self._phase, self._phase_started = "reap", self._tick_started
        self._state.update(
            attempted_ticks=self._state["attempted_ticks"] + 1,
            last_attempt_at=time.time(), last_error_type=None,
        )

    def finish_tick(
        self, results: Iterable[tuple[str, Any]] = (), *, ready_pending: bool = False,
        error_type: str | None = None, paused: bool = False,
    ) -> None:
        results = list(results or ())
        spawned = sum(len(getattr(res, "spawned", ()) or ()) for _, res in results)
        guarded = sum(bool(
            getattr(res, "dispatch_paused", None) or getattr(res, "skipped_locked", False)
            or getattr(res, "respawn_guarded", ()) or getattr(res, "serialized_coedit", ())
        ) for _, res in results)
        capped = sum(bool(
            getattr(res, "skipped_per_profile_capped", ())
            or getattr(res, "memory_pressure", None) == "critical"
        ) for _, res in results)
        errors = sum(res is None for _, res in results)
        # No-spawn without a known reason stays unknown, NOT inferred to be a
        # hang or a full global cap. Older dispatch results expose neither.
        outcomes = (
            (error_type or errors, "error"), (spawned, "spawned"),
            (paused or guarded, "guard_deferred"), (capped, "no_capacity"),
            (ready_pending, "no_spawn"),
        )
        outcome = next((name for condition, name in outcomes if condition), "idle")
        now = time.monotonic()
        self.phase("sleeping")
        duration = now - self._tick_started if self._tick_started is not None else 0.0
        self._tick_started = None
        self._state.update(
            completed_ticks=self._state["completed_ticks"] + 1,
            last_finished_at=time.time(), last_outcome=outcome,
            last_error_type=error_type, last_tick_duration_seconds=round(duration, 3),
            spawned=spawned, guard_deferred_boards=guarded,
            no_capacity_boards=capped, error_boards=errors, ready_pending=bool(ready_pending),
        )
        if outcome != "error":
            self._state["last_success_at"] = self._state["last_finished_at"]
        payload = json.dumps(self.snapshot(), separators=(",", ":"))
        if self._last_report is None or now - self._last_report >= 300:
            logger.info("kanban dispatcher health: %s", payload)
            self._last_report = now
        else:
            logger.debug("kanban dispatcher health: %s", payload)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        elapsed = max(0.0, now - self._phase_started)
        stalled = self._phase not in {
            "sleeping", "standby", "disabled", "stopped", "retained",
        } and elapsed > max(30.0, self.interval * 3)
        snapshot = {
            **self._state, "phase": self._phase,
            "phase_elapsed_seconds": round(elapsed, 3),
            "phase_durations_seconds": dict(self._durations),
            "interval_seconds": self.interval, "stalled": stalled,
        }
        if stalled and (self._last_stall_report is None or now - self._last_stall_report >= 300):
            logger.warning("kanban dispatcher phase stalled: phase=%s elapsed_seconds=%.1f "
                           "completed_ticks=%d; thread still in flight, not restarted",
                           self._phase, elapsed, self._state["completed_ticks"])
            self._last_stall_report = now
        return snapshot
