"""Usage-aware admission as the Kanban DISPATCHER actually applies it.

These tests drive real ``dispatch_once`` ticks against temporary SQLite boards
with a fake spawn function, and control capacity with fake authenticated
snapshots.  They pin the integration contracts the module-level suite cannot:
that a drain stops CLAIMS without touching in-flight work, that the tick's
effective concurrency cap is narrowed rather than the operator's config
rewritten, that a route change reaches the worker's own launch identity while
the card row keeps the operator's route, and that all of this is one global
decision across boards.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent import kanban_throttle as kt
from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def board_home(tmp_path, monkeypatch):
    """Two real boards under a sandboxed shared kanban home.

    Every ``HERMES_KANBAN_*`` override is cleared first (an inherited
    ``HERMES_KANBAN_DB`` beats ``HERMES_KANBAN_HOME`` and would redirect these
    writes at the live board), and the resolved paths are asserted to be inside
    the temp dir before anything writes.
    """
    for key in tuple(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # These tests exercise admission, not route admissibility: keep the model
    # policy gate out of the way so synthetic routes reach the throttle.
    monkeypatch.setattr(
        kb, "validate_model_effort_policy", lambda **_kw: kb.ModelPolicyDecision(False),
    )
    monkeypatch.setattr(
        kb, "validate_task_model_policy", lambda *_a, **_kw: kb.ModelPolicyDecision(False),
    )
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: (lambda _name: True))
    # Host memory is a separate admission axis; hold it steady.
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: {})
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda *_a, **_kw: "unknown")
    kt.reset_signal_cache()
    assert kt.throttle_state_db_path().resolve().is_relative_to(tmp_path.resolve())
    assert kb.kanban_db_path(board="default").resolve().is_relative_to(tmp_path.resolve())
    kb.init_db(board="default")
    kb.init_db(board="second")
    yield home
    kt.reset_signal_cache()


def _pressure(monkeypatch, percent, *, provider="anthropic", destination=None):
    """Serve one fake AUTHENTICATED snapshot per provider."""
    def _snapshot(pct, name):
        return AccountUsageSnapshot(
            provider=name, source="test", fetched_at=datetime.now(timezone.utc),
            windows=(AccountUsageWindow(label="Seven Day", used_percent=pct),),
        )

    mapping = {provider: _snapshot(percent, provider)}
    if destination is not None:
        mapping["openai-codex"] = _snapshot(destination, "openai-codex")

    monkeypatch.setattr(
        kt, "_fetch_snapshot", lambda name, *, timeout: mapping.get(name),  # noqa: ARG005
    )
    kt.reset_signal_cache()


def _throttle_cfg(**overrides):
    levers = {
        "downgrade_model": {"ladder": ["claude-opus-5", "claude-sonnet-5"]},
    }
    for key, value in (overrides.pop("levers", {}) or {}).items():
        levers.setdefault(key, {}).update(value)
    return {"usage_throttle": {"levers": levers, **overrides}}


def _use_config(monkeypatch, cfg):
    """Point the throttle's HOST config reader at *cfg* (no file I/O)."""
    monkeypatch.setattr(kt, "host_kanban_config", lambda: cfg)


def _ready_task(board="default", *, assignee="worker", model=None, provider=None):
    with kbc.connect(board=board) as conn:
        task_id = kb.create_task(
            conn, title=f"{board}-card", assignee=assignee,
            model_override=model, provider_override=provider,
        )
    return task_id


class _Spawner:
    """Records every spawn and the route the dispatcher prepared for it."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, task, workspace, *, board=None):  # noqa: ARG002
        analytics = dict(getattr(task, "_worker_run_analytics", {}) or {})
        self.calls.append({
            "task_id": task.id,
            "model_override": task.model_override,
            "provider_override": task.provider_override,
            "argv_model": _argv_model(task),
            "analytics_model": analytics.get("model"),
            "analytics_provider": analytics.get("provider"),
        })
        return 4_000_000 + len(self.calls)


def _argv_model(task):
    """The model the worker process would actually be launched with."""
    argv = kbd._worker_argv(task, task.assignee or "worker", None)
    return argv[argv.index("-m") + 1] if "-m" in argv else None


def _tick(board="default", **kwargs):
    with kbc.connect(board=board) as conn:
        return kbd.dispatch_once(conn, board=board, **kwargs)


# --- AC4: drain stops new claims; in-flight work continues ---------------


def test_drain_claims_nothing_new_while_in_flight_work_keeps_running(
    board_home, monkeypatch,
):
    _use_config(monkeypatch, _throttle_cfg())
    _pressure(monkeypatch, 20.0)
    running = _ready_task()
    spawner = _Spawner()
    first = _tick(spawn_fn=spawner)
    assert [tid for tid, _who, _ws in first.spawned] == [running]

    # Pressure crosses the drain threshold with a worker already in flight.
    queued = _ready_task()
    _pressure(monkeypatch, 95.0)
    drained = _tick(spawn_fn=spawner)

    assert drained.usage_throttle["state"] == kt.STATE_DRAIN
    assert drained.usage_throttle["drain"] is True
    assert drained.spawned == []
    assert queued in drained.throttle_drained
    assert len(spawner.calls) == 1

    with kbc.connect(board="default") as conn:
        # In-flight work is untouched: still running, still holding its claim
        # and its worker pid. Nothing was killed, reclaimed or released.
        row = conn.execute(
            "SELECT status, worker_pid, claim_lock FROM tasks WHERE id = ?", (running,),
        ).fetchone()
        assert row["status"] == "running"
        assert row["worker_pid"] is not None
        assert row["claim_lock"] is not None
        # The queued card is still simply ready — not blocked, not failed.
        queued_row = conn.execute(
            "SELECT status, consecutive_failures, last_failure_error FROM tasks WHERE id = ?",
            (queued,),
        ).fetchone()
        assert queued_row["status"] == "ready"
        assert queued_row["consecutive_failures"] == 0
        assert queued_row["last_failure_error"] is None


def test_drain_writes_no_dispatch_pause_so_no_operator_resume_is_needed(
    board_home, monkeypatch,
):
    _use_config(monkeypatch, _throttle_cfg())
    _pressure(monkeypatch, 97.0)
    _ready_task()
    result = _tick(spawn_fn=_Spawner())

    assert result.usage_throttle["drain"] is True
    # A drain is automatic and self-clearing; a pause sentinel is the sticky
    # operator/fault mechanism and must not be created here, or recovery would
    # need a human running `dispatch --resume-circuit`.
    assert result.dispatch_paused is None
    assert kbd.read_dispatch_pause("default") is None


def test_drain_still_runs_reclaim_and_promotion_bookkeeping(board_home, monkeypatch):
    _use_config(monkeypatch, _throttle_cfg())
    _pressure(monkeypatch, 99.0)

    # A card whose worker is long gone and whose claim has expired: reclaiming
    # it is owned by the tick's reclaim phase, which runs BEFORE admission.
    abandoned = _ready_task()
    with kbc.connect(board="default") as conn, kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'running', claim_lock = ?, claim_expires = ?, "
            "worker_pid = NULL WHERE id = ?",
            ("gone-host:999999", 1, abandoned),
        )
    queued = _ready_task()

    result = _tick(spawn_fn=_Spawner())

    assert result.usage_throttle["drain"] is True
    assert result.spawned == []
    # A drain stops NEW CLAIMS; it must not freeze the board's bookkeeping, or
    # a dead worker's card would stay "running" for the whole pressure window.
    assert result.reclaimed >= 1
    with kbc.connect(board="default") as conn:
        assert kb.get_task(conn, abandoned).status == "ready"
    # Both cards are reported as drained rather than silently absent.
    assert {abandoned, queued} <= set(result.throttle_drained)


# --- AC2/AC6: cap narrowing without mutating operator settings -------------


def test_throttle_narrows_this_tick_without_touching_configured_concurrency(
    board_home, monkeypatch,
):
    _use_config(monkeypatch, _throttle_cfg(
        levers={"reduce_concurrency": {"max_in_progress": 1}},
    ))
    _pressure(monkeypatch, 75.0)
    for _ in range(3):
        _ready_task()

    spawner = _Spawner()
    # The operator configured 3; the throttle allows 1 this tick.
    result = _tick(spawn_fn=spawner, max_in_progress=3)

    assert result.usage_throttle["state"] == kt.STATE_REDUCE
    assert result.usage_throttle["max_in_progress"] == 1
    assert len(result.spawned) == 1

    # Pressure clears. The operator's own 3 is live again with no restore step
    # and nothing to un-clobber, because it was never written over.
    _pressure(monkeypatch, 10.0)
    recovered = _tick(spawn_fn=spawner, max_in_progress=3)
    assert recovered.usage_throttle["state"] == kt.STATE_NORMAL
    assert recovered.usage_throttle["max_in_progress"] is None
    assert len(recovered.spawned) == 2  # 1 already running + 2 = the operator's 3


def test_operator_cap_tighter_than_the_automatic_ceiling_is_preserved(
    board_home, monkeypatch,
):
    _use_config(monkeypatch, _throttle_cfg(
        levers={"reduce_concurrency": {"max_in_progress": 5}},
    ))
    _pressure(monkeypatch, 75.0)
    for _ in range(3):
        _ready_task()

    result = _tick(spawn_fn=_Spawner(), max_in_progress=1)
    # The throttle only ever tightens: an operator running tighter than the
    # automatic ceiling keeps their own, stricter value.
    assert len(result.spawned) == 1


# --- Model downgrade reaches the worker; the card keeps its own route -------


def test_downgrade_changes_the_launched_route_but_not_the_stored_card(
    board_home, monkeypatch,
):
    _use_config(monkeypatch, _throttle_cfg())
    _pressure(monkeypatch, 85.0)
    task_id = _ready_task(model="claude-opus-5", provider="anthropic")

    spawner = _Spawner()
    result = _tick(spawn_fn=spawner)

    assert result.usage_throttle["state"] == kt.STATE_DOWNGRADE
    assert result.throttle_rerouted == [(task_id, "downgrade", "anthropic/claude-sonnet-5")]
    call = spawner.calls[0]
    # The worker is genuinely launched on the cheaper rung: argv AND the run
    # analytics persisted for it both describe the downgraded route.
    assert call["argv_model"] == "claude-sonnet-5"
    assert call["analytics_model"] == "claude-sonnet-5"
    assert call["analytics_provider"] == "anthropic"

    with kbc.connect(board="default") as conn:
        stored = kb.get_task(conn, task_id)
        # The operator's intent on the card is untouched, so when pressure
        # clears the next spawn uses it with nothing to restore.
        assert stored.model_override == "claude-opus-5"
        assert stored.provider_override == "anthropic"
        run = conn.execute(
            "SELECT model FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        # The run records what actually ran, not what the card asked for.
        assert run["model"] == "claude-sonnet-5"


def test_recovery_returns_the_next_spawn_to_the_operators_own_route(
    board_home, monkeypatch,
):
    _use_config(monkeypatch, _throttle_cfg())
    _pressure(monkeypatch, 85.0)
    first = _ready_task(model="claude-opus-5", provider="anthropic")
    spawner = _Spawner()
    _tick(spawn_fn=spawner)
    assert spawner.calls[0]["argv_model"] == "claude-sonnet-5"

    _pressure(monkeypatch, 5.0)
    second = _ready_task(model="claude-opus-5", provider="anthropic")
    result = _tick(spawn_fn=spawner)

    assert result.usage_throttle["state"] == kt.STATE_NORMAL
    assert result.throttle_rerouted == []
    assert spawner.calls[-1]["task_id"] == second
    assert spawner.calls[-1]["argv_model"] == "claude-opus-5"
    assert first != second


def test_an_operator_forced_route_is_never_downgraded(board_home, monkeypatch):
    _use_config(monkeypatch, _throttle_cfg())
    _pressure(monkeypatch, 88.0)
    task_id = _ready_task(model="claude-opus-5", provider="anthropic")
    with kbc.connect(board="default") as conn, kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET policy_forced_by = ?, policy_force_reason = ? WHERE id = ?",
            ("operator", "needed for this migration", task_id),
        )

    spawner = _Spawner()
    result = _tick(spawn_fn=spawner)

    # An explicit force carries a durable operator reason; automatic pressure
    # does not get to override it.
    assert result.throttle_rerouted == []
    assert spawner.calls[0]["argv_model"] == "claude-opus-5"


# --- AC5: failover through the dispatcher --------------------------------


def test_dispatcher_never_fails_over_without_opt_in_and_dual_capacity(
    board_home, monkeypatch,
):
    def _probe(assignee):
        """One real tick against a fresh card; returns (result, spawner)."""
        with kbc.connect(board="default") as conn, kb.write_txn(conn):
            conn.execute("DELETE FROM tasks")
        task_id = _ready_task(
            model="claude-opus-5", provider="anthropic", assignee=assignee,
        )
        spawner = _Spawner()
        return task_id, _tick(spawn_fn=spawner), spawner

    # 1. Shipped defaults: failover off, destination wide open. Pressure this
    #    high drains, and nothing is rerouted onto the other account.
    _use_config(monkeypatch, _throttle_cfg())
    _pressure(monkeypatch, 99.0, destination=1.0)
    task_id, result, spawner = _probe("worker")
    assert result.throttle_rerouted == []
    assert result.spawned == []
    assert result.throttle_drained == [task_id]

    opted_in = _throttle_cfg(levers={"cross_provider_failover": {
        "enabled": True, "threshold_pct": 95, "eligible_profiles": ["worker"],
        "destination": {"provider": "openai-codex", "model": "gpt-5.6-sol",
                        "max_pressure_pct": 50},
    }})
    _use_config(monkeypatch, opted_in)

    # 2. Opted in and allowlisted, but the destination has NO signal at all.
    #    Unknown capacity is not spare capacity: drain, do not reroute.
    _pressure(monkeypatch, 99.0, destination=None)
    task_id, result, spawner = _probe("worker")
    assert result.throttle_rerouted == []
    assert result.spawned == []

    # 3. Destination readable but itself under pressure.
    _pressure(monkeypatch, 99.0, destination=80.0)
    task_id, result, spawner = _probe("worker")
    assert result.throttle_rerouted == []
    assert result.spawned == []

    # 4. Everything proven: opted in, allowlisted, fresh source AND fresh
    #    destination capacity. Only now does work move, and only in memory.
    _pressure(monkeypatch, 99.0, destination=5.0)
    task_id, result, spawner = _probe("worker")
    assert result.throttle_rerouted == [
        (task_id, "failover", "openai-codex/gpt-5.6-sol")
    ]
    assert [tid for tid, _who, _ws in result.spawned] == [task_id]
    assert spawner.calls[0]["argv_model"] == "gpt-5.6-sol"
    assert spawner.calls[0]["analytics_provider"] == "openai-codex"
    with kbc.connect(board="default") as conn:
        stored = kb.get_task(conn, task_id)
        assert stored.model_override == "claude-opus-5"
        assert stored.provider_override == "anthropic"

    # 5. Same proven capacity, but a profile the operator did not name.
    #    Eligibility is an explicit allowlist, never inferred.
    task_id, result, spawner = _probe("other")
    assert result.throttle_rerouted == []
    assert result.spawned == []
    assert result.throttle_drained == [task_id]


# --- AC7: one global state across boards; degraded visibility -------------


def test_admission_state_is_global_across_boards(board_home, monkeypatch):
    _use_config(monkeypatch, _throttle_cfg())
    _pressure(monkeypatch, 96.0)
    _ready_task("default")
    _ready_task("second")

    first = _tick("default", spawn_fn=_Spawner())
    second = _tick("second", spawn_fn=_Spawner())

    # One subscription, one decision: the second board observes the state the
    # first established rather than re-deciding it independently.
    assert first.usage_throttle["state"] == kt.STATE_DRAIN
    assert second.usage_throttle["state"] == kt.STATE_DRAIN
    assert second.usage_throttle["changed"] is False
    assert first.spawned == second.spawned == []

    changes = [
        e for e in kt.recent_throttle_events(50)
        if e["kind"] == "state_change" and e["payload"]["to"] == kt.STATE_DRAIN
    ]
    assert len(changes) == 1


def test_missing_signal_produces_a_visible_degraded_tick_and_no_route_change(
    board_home, monkeypatch,
):
    _use_config(monkeypatch, _throttle_cfg())
    monkeypatch.setattr(kt, "_fetch_snapshot", lambda name, *, timeout: None)  # noqa: ARG005
    kt.reset_signal_cache()
    _ready_task(model="claude-opus-5", provider="anthropic")

    spawner = _Spawner()
    result = _tick(spawn_fn=spawner, max_in_progress=3)

    assert result.usage_throttle["degraded"] is True
    assert result.usage_throttle["degraded_reason"] == kt.DEGRADED_NO_SIGNAL
    assert result.usage_throttle["recovery"]
    # No speculative change: dispatch proceeds exactly as it would without the
    # feature, and the route is left alone.
    assert len(result.spawned) == 1
    assert result.throttle_rerouted == []
    assert spawner.calls[0]["argv_model"] == "claude-opus-5"


def test_unsupported_source_provider_is_visible_at_the_dispatch_surface(
    board_home, monkeypatch,
):
    """AC4 at the surface an operator actually reads.

    The throttle's own tests prove the classification; this proves the
    dispatcher reports it rather than flattening it into the generic
    no-signal record, and that the tick behaves exactly as it would without
    the feature. ``xai`` is named deliberately — its unsupported verdict comes
    from the real capability table, so a table that drifted to "supported"
    would fail here instead of silently throttling on a fabricated reading.
    """
    _use_config(
        monkeypatch, _throttle_cfg(source_providers=["xai"]),
    )
    kt.reset_signal_cache()
    _ready_task(model="claude-opus-5", provider="anthropic")

    spawner = _Spawner()
    result = _tick(spawn_fn=spawner, max_in_progress=3)

    state = result.usage_throttle
    assert state["degraded"] is True
    assert state["degraded_reason"] == kt.DEGRADED_UNSUPPORTED_SOURCE
    # The remedy differs from a transient miss: pointing at `/usage` would send
    # the operator chasing a reading this provider never serves.
    assert "source_providers" in state["recovery"]
    assert "/usage" not in state["recovery"]
    assert state["pressure_percent"] is None
    # No signal, no guess: the card dispatches on the operator's own route.
    assert len(result.spawned) == 1
    assert result.throttle_rerouted == []
    assert spawner.calls[0]["argv_model"] == "claude-opus-5"


def test_stale_signal_holds_the_state_instead_of_recovering(board_home, monkeypatch):
    _use_config(monkeypatch, _throttle_cfg())
    _pressure(monkeypatch, 97.0)
    _ready_task()
    assert _tick(spawn_fn=_Spawner()).usage_throttle["state"] == kt.STATE_DRAIN

    # The provider keeps answering, but with a reading far too old to act on.
    stale = AccountUsageSnapshot(
        provider="anthropic", source="test",
        fetched_at=datetime.now(timezone.utc) - timedelta(days=2),
        windows=(AccountUsageWindow(label="Seven Day", used_percent=1.0),),
    )
    monkeypatch.setattr(kt, "_fetch_snapshot", lambda name, *, timeout: stale)  # noqa: ARG005
    kt.reset_signal_cache()

    result = _tick(spawn_fn=_Spawner())
    # A stale "1% used" must NOT be read as recovery — that would resume full
    # dispatch against an account that may still be exhausted.
    assert result.usage_throttle["degraded"] is True
    assert result.usage_throttle["state"] == kt.STATE_DRAIN
    assert result.spawned == []


def test_throttle_failure_never_wedges_dispatch(board_home, monkeypatch):
    def _explode(**_kwargs):
        raise RuntimeError("throttle store is corrupt")

    monkeypatch.setattr(kt, "evaluate_throttle", _explode)
    _ready_task()

    result = _tick(spawn_fn=_Spawner(), max_in_progress=2)
    # Admission control failing closed on the whole board would be far worse
    # than not throttling: the tick proceeds as it did before the feature.
    assert len(result.spawned) == 1
    assert result.usage_throttle is None


def test_dry_run_reports_without_recording_a_transition(board_home, monkeypatch):
    _use_config(monkeypatch, _throttle_cfg())
    _pressure(monkeypatch, 93.0)
    _ready_task()

    preview = _tick(spawn_fn=_Spawner(), dry_run=True)
    assert preview.usage_throttle["state"] == kt.STATE_DRAIN
    # A report of what a tick WOULD do must not manufacture audit history.
    assert kt.recent_throttle_events(50) == []

    real = _tick(spawn_fn=_Spawner())
    assert real.usage_throttle["state"] == kt.STATE_DRAIN
    assert len([e for e in kt.recent_throttle_events(50) if e["kind"] == "state_change"]) == 1


def test_claim_path_is_race_safe_under_concurrent_throttled_ticks(
    board_home, monkeypatch,
):
    from concurrent.futures import ThreadPoolExecutor

    _use_config(monkeypatch, _throttle_cfg(
        levers={"reduce_concurrency": {"max_in_progress": 1}},
    ))
    _pressure(monkeypatch, 78.0)
    for _ in range(4):
        _ready_task()

    spawner = _Spawner()

    def _run():
        return _tick(spawn_fn=spawner, max_in_progress=4)

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = [future.result() for future in [pool.submit(_run) for _ in range(3)]]

    spawned = [tid for result in results for tid, _who, _ws in result.spawned]
    # The board lock plus the single-row claim means the narrowed ceiling is a
    # real cap, not a per-tick allowance each racing dispatcher gets its own of.
    assert len(spawned) == len(set(spawned))
    with kbc.connect(board="default") as conn:
        assert kbd.count_running_tasks(conn) <= 1
