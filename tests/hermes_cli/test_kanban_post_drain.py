"""Post-drain action queue: registry, validation, persistence, firing.

Behaviour contracts on the server-side half of the queue — no HTTP client and
no renderer anywhere in this file, because the card's central claim is that the
trigger fires without a browser connected.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_dispatch_postdrain as pd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert kb.kanban_db_path().is_relative_to(tmp_path), "NOT ISOLATED"
    kb.init_db()
    return home


@pytest.fixture
def allowlisted(monkeypatch):
    """Config where exactly one unit may be restarted."""
    monkeypatch.setattr(
        pd, "resolve_post_drain_config",
        lambda: pd.PostDrainConfig(
            service_restart_allowlist=("hermes-gateway.service",),
            service_restart_scope="system",
            default_expiry_seconds=3600,
            max_expiry_seconds=86400,
        ),
    )


def test_queue_persists_the_intent_beside_the_board_database(kanban_home):
    record = pd.queue_post_drain_action(None, action_kind="reboot", requested_by="operator")

    assert record["state"] == "waiting"
    assert record["action_kind"] == "reboot"
    assert record["requested_by"] == "operator"
    assert record["requested_at"] <= record["expires_at"]
    # Same per-board, path-pinned derivation as the pause sentinel.
    path = kb.kanban_db_path(None).with_suffix(".dispatch-post-drain.json")
    assert path.exists()
    assert pd.read_post_drain_action(None) == record


def test_unknown_action_kind_is_rejected(kanban_home):
    for kind in ("", "shell", "poweroff", "service-restart"):
        with pytest.raises(pd.PostDrainActionRejected):
            pd.queue_post_drain_action(None, action_kind=kind)
    assert pd.read_post_drain_action(None) is None


def test_service_restart_target_must_come_from_the_config_allowlist(kanban_home, allowlisted):
    with pytest.raises(pd.PostDrainActionRejected):
        pd.queue_post_drain_action(None, action_kind="service_restart", target="sshd.service")
    assert pd.read_post_drain_action(None) is None

    record = pd.queue_post_drain_action(None, action_kind="service_restart", target="hermes-gateway.service")
    assert record["target"] == "hermes-gateway.service"


def test_service_restart_is_unqueueable_when_the_allowlist_is_empty(kanban_home, monkeypatch):
    monkeypatch.setattr(
        pd, "resolve_post_drain_config",
        lambda: pd.PostDrainConfig(service_restart_allowlist=()),
    )
    with pytest.raises(pd.PostDrainActionRejected):
        pd.queue_post_drain_action(None, action_kind="service_restart", target="hermes-gateway.service")
    with pytest.raises(pd.PostDrainActionRejected):
        pd.queue_post_drain_action(None, action_kind="service_restart")


def test_a_single_entry_allowlist_resolves_the_target_without_a_request_body(kanban_home, allowlisted):
    record = pd.queue_post_drain_action(None, action_kind="service_restart")

    assert record["target"] == "hermes-gateway.service"


def test_reboot_rejects_a_target(kanban_home):
    with pytest.raises(pd.PostDrainActionRejected):
        pd.queue_post_drain_action(None, action_kind="reboot", target="hermes-gateway.service")


def test_expiry_is_bounded_by_config(kanban_home, allowlisted):
    with pytest.raises(pd.PostDrainActionRejected):
        pd.queue_post_drain_action(None, action_kind="reboot", expires_in_seconds=0)
    with pytest.raises(pd.PostDrainActionRejected):
        pd.queue_post_drain_action(None, action_kind="reboot", expires_in_seconds=86401)

    record = pd.queue_post_drain_action(None, action_kind="reboot", expires_in_seconds=600)
    assert 590 <= record["expires_at"] - record["requested_at"] <= 610


def test_cancel_releases_a_waiting_action(kanban_home):
    pd.queue_post_drain_action(None, action_kind="reboot")

    cancelled = pd.cancel_post_drain_action(None)

    assert cancelled["cancelled"] is True
    assert cancelled["state"]["state"] == "cancelled"
    assert pd.read_post_drain_action(None)["state"] == "cancelled"


def test_cancel_is_a_no_op_when_nothing_is_queued(kanban_home):
    assert pd.cancel_post_drain_action(None) == {"cancelled": False, "state": None}


# --- firing on observed drain ----------------------------------------------


@pytest.fixture
def recorder(monkeypatch):
    """Replace both shipped handlers' side effects with a call recorder.

    Only ``fire``/``observe_*`` are stubbed — validation, persistence and the
    state machine under test all run for real.
    """

    class Recorder(list):
        def install(self, kind, *, succeeds=True):
            original = pd.ACTION_HANDLERS[kind]
            calls = self

            def fire(record, cfg):
                calls.append(f"{kind}:{record.get('target')}")

            monkeypatch.setitem(
                pd.ACTION_HANDLERS, kind,
                type(original)(
                    kind=original.kind,
                    takes_target=original.takes_target,
                    resolve_target=original.resolve_target,
                    observe_before=lambda record, cfg: {"probe": "before"},
                    fire=fire,
                    observe_after=lambda record, cfg: (
                        {"state": pd.SUCCEEDED, "observed_after": {"probe": "after"}}
                        if succeeds
                        else {"state": pd.FAILED, "error": "did not come back"}
                    ),
                ),
            )

    calls = Recorder()
    calls.install("reboot")
    calls.install("service_restart")
    return calls


def _running(board, count):
    """Put ``count`` tasks into ``running`` with a live claim on ``board``.

    The pid must be a LIVE process: the dispatcher's reclaim phase reconciles a
    ``running`` row whose worker is gone, so a fake pid would legitimately drain
    the board mid-tick and the "never fires while running" contract would be
    tested against a board that is not actually running anything.
    """
    with kbc.connect_closing(board=board) as conn:
        for index in range(count):
            task_id = kb.create_task(conn, title=f"worker-{index}", assignee="worker")
            conn.execute(
                "UPDATE tasks SET status='running', claim_lock=?, worker_pid=? WHERE id=?",
                (f"{kb._host_prefix()}1", os.getpid(), task_id),
            )
        conn.commit()


def test_a_queued_action_never_fires_while_workers_are_running(kanban_home, recorder):
    _running(None, 2)
    kbd.pause_dispatch(None)
    pd.queue_post_drain_action(None, action_kind="reboot")

    outcome = pd.evaluate_post_drain_action(None)

    assert outcome is None
    assert recorder == []
    assert pd.read_post_drain_action(None)["state"] == pd.WAITING


def test_a_queued_action_fires_when_the_paused_board_reaches_zero_running(kanban_home, recorder):
    kbd.pause_dispatch(None)
    pd.queue_post_drain_action(None, action_kind="reboot")

    outcome = pd.evaluate_post_drain_action(None)

    assert recorder == ["reboot:None"]
    assert outcome["state"] == pd.SUCCEEDED
    assert pd.read_post_drain_action(None)["state"] == pd.SUCCEEDED


def test_a_queued_action_does_not_fire_on_an_unpaused_board(kanban_home, recorder):
    pd.queue_post_drain_action(None, action_kind="reboot")

    assert pd.evaluate_post_drain_action(None) is None
    assert recorder == []


def test_a_fault_pause_is_not_a_maintenance_window(kanban_home, recorder):
    """Only the operator's own drain fires an action.

    A board stopped by a fault circuit is in an unexpected state — exactly the
    state an unattended reboot must not be launched into.
    """
    kbd._write_dispatch_pause(None, "restart_safe_scope_unavailable", fault_code="x")
    pd.queue_post_drain_action(None, action_kind="reboot")

    assert pd.evaluate_post_drain_action(None) is None
    assert recorder == []


def test_an_expired_action_becomes_expired_and_never_fires(kanban_home, recorder):
    kbd.pause_dispatch(None)
    pd.queue_post_drain_action(None, action_kind="reboot", expires_in_seconds=600)

    outcome = pd.evaluate_post_drain_action(None, now=int(time.time()) + 601)

    assert recorder == []
    assert outcome["state"] == pd.EXPIRED
    assert pd.read_post_drain_action(None)["state"] == pd.EXPIRED


def test_expiry_wins_over_a_drained_board(kanban_home, recorder):
    """Expiry is checked before the drain condition, not after it."""
    kbd.pause_dispatch(None)
    pd.queue_post_drain_action(None, action_kind="reboot", expires_in_seconds=60)

    pd.evaluate_post_drain_action(None, now=int(time.time()) + 61)

    assert recorder == []


def test_a_fired_action_is_not_fired_again_by_later_evaluations(kanban_home, recorder):
    kbd.pause_dispatch(None)
    pd.queue_post_drain_action(None, action_kind="reboot")

    pd.evaluate_post_drain_action(None)
    pd.evaluate_post_drain_action(None)
    pd.evaluate_post_drain_action(None)

    assert recorder == ["reboot:None"]


def test_concurrent_evaluations_fire_a_queued_action_exactly_once(kanban_home, monkeypatch):
    """Two dispatch ticks racing one record must not double-fire it.

    Deterministic on purpose: the first handler to fire BLOCKS until every other
    thread has finished its own evaluation, so "only one fired" cannot be an
    artifact of the winner completing before the losers ever looked at the
    record. Whatever prevents the second fire has to do so while the first is
    still in flight.
    """
    import threading

    fired: list[str] = []
    everyone_tried = threading.Event()
    first_fire_entered = threading.Event()

    original = pd.ACTION_HANDLERS["reboot"]

    def slow_fire(record, cfg):
        fired.append("reboot")
        first_fire_entered.set()
        everyone_tried.wait(timeout=20)

    monkeypatch.setitem(
        pd.ACTION_HANDLERS, "reboot",
        type(original)(
            kind="reboot", takes_target=False, resolve_target=original.resolve_target,
            observe_before=lambda record, cfg: {}, fire=slow_fire,
            observe_after=lambda record, cfg: {"state": pd.SUCCEEDED},
        ),
    )

    kbd.pause_dispatch(None)
    pd.queue_post_drain_action(None, action_kind="reboot")

    start = threading.Barrier(8)
    errors: list[BaseException] = []
    done = threading.Semaphore(0)

    def tick():
        try:
            start.wait(timeout=10)
            pd.evaluate_post_drain_action(None)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)
        finally:
            done.release()

    threads = [threading.Thread(target=tick) for _ in range(8)]
    for thread in threads:
        thread.start()

    # Release the blocked handler only once 7 of the 8 have already returned;
    # every loser therefore made its decision while the winner held the record.
    assert first_fire_entered.wait(timeout=20), "no thread ever reached the handler"
    for _ in range(7):
        assert done.acquire(timeout=20), "a losing tick never completed"
    everyone_tried.set()

    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert fired == ["reboot"]


def test_a_record_already_firing_is_never_re_claimed(kanban_home, recorder):
    """The state machine, not thread timing, is what makes FIRING exactly-once.

    A ``firing`` record is reconciled by a later tick (that is finding #2), but
    reconciliation is a pure observation: the handler must never run twice.
    """
    kbd.pause_dispatch(None)
    record = pd.queue_post_drain_action(None, action_kind="reboot")
    pd._write_post_drain_action(None, {**record, "state": pd.FIRING})

    pd.evaluate_post_drain_action(None)
    pd.evaluate_post_drain_action(None)

    assert recorder == []


@pytest.mark.parametrize("state", [pd.SUCCEEDED, pd.FAILED, pd.EXPIRED, pd.CANCELLED])
def test_a_terminal_record_is_never_re_claimed(kanban_home, recorder, state):
    kbd.pause_dispatch(None)
    record = pd.queue_post_drain_action(None, action_kind="reboot")
    pd._write_post_drain_action(None, {**record, "state": state})

    assert pd.evaluate_post_drain_action(None) is None
    assert recorder == []
    assert pd.read_post_drain_action(None)["state"] == state


def test_a_failed_action_records_the_observed_failure(kanban_home, recorder):
    recorder.install("reboot", succeeds=False)
    kbd.pause_dispatch(None)
    pd.queue_post_drain_action(None, action_kind="reboot")

    outcome = pd.evaluate_post_drain_action(None)

    assert outcome["state"] == pd.FAILED
    record = pd.read_post_drain_action(None)
    assert record["state"] == pd.FAILED
    assert "did not come back" in record["error"]


def test_a_handler_that_raises_leaves_a_failed_record_not_a_stuck_one(kanban_home, monkeypatch):
    original = pd.ACTION_HANDLERS["reboot"]

    def boom(record, cfg):
        raise RuntimeError("systemctl exited 1")

    monkeypatch.setitem(
        pd.ACTION_HANDLERS, "reboot",
        type(original)(
            kind="reboot", takes_target=False, resolve_target=original.resolve_target,
            observe_before=lambda record, cfg: {}, fire=boom,
            observe_after=lambda record, cfg: {},
        ),
    )
    kbd.pause_dispatch(None)
    pd.queue_post_drain_action(None, action_kind="reboot")

    outcome = pd.evaluate_post_drain_action(None)

    assert outcome["state"] == pd.FAILED
    assert "systemctl exited 1" in pd.read_post_drain_action(None)["error"]


def test_an_action_whose_process_does_not_survive_stays_firing(kanban_home, monkeypatch):
    """A reboot cannot observe its own success; the record must not claim it.

    The real reboot handler's ``observe_after`` returns no verdict while the
    machine epoch is unchanged, so the record stays ``firing`` — an honest
    "intent issued, outcome not yet observed" — rather than a fabricated success.
    """
    fired: list[str] = []
    monkeypatch.setattr(pd, "_reboot_fire", lambda record, cfg: fired.append("reboot"))
    monkeypatch.setitem(
        pd.ACTION_HANDLERS, "reboot",
        type(pd.ACTION_HANDLERS["reboot"])(
            kind="reboot", takes_target=False,
            resolve_target=pd.ACTION_HANDLERS["reboot"].resolve_target,
            observe_before=pd._reboot_observe_before,
            fire=lambda record, cfg: fired.append("reboot"),
            observe_after=pd._reboot_observe_after,
            survives_execution=False,
        ),
    )
    kbd.pause_dispatch(None)
    pd.queue_post_drain_action(None, action_kind="reboot")

    outcome = pd.evaluate_post_drain_action(None)

    assert fired == ["reboot"]
    assert outcome["state"] == pd.FIRING
    assert pd.read_post_drain_action(None)["state"] == pd.FIRING


def test_resuming_dispatch_cancels_a_waiting_action(kanban_home, recorder):
    kbd.pause_dispatch(None)
    pd.queue_post_drain_action(None, action_kind="reboot")

    kbd.resume_dispatch(None)

    assert pd.read_post_drain_action(None)["state"] == pd.CANCELLED
    assert pd.evaluate_post_drain_action(None) is None
    assert recorder == []


# --- the dispatcher tick is the trigger (no browser involved) ---------------


def test_the_dispatcher_tick_fires_a_queued_action_on_a_drained_board(kanban_home, recorder):
    """The card's central claim, proven server-side.

    Nothing here opens an HTTP client or renders a component: ``dispatch_once``
    is the same call the in-gateway dispatcher makes every tick, and it is what
    fires the action.
    """
    kbd.pause_dispatch(None)
    pd.queue_post_drain_action(None, action_kind="reboot")

    with kbc.connect_closing(board=None) as conn:
        kbd.dispatch_once(conn, board=None)

    assert recorder == ["reboot:None"]
    assert pd.read_post_drain_action(None)["state"] == pd.SUCCEEDED


def test_the_dispatcher_tick_does_not_fire_while_workers_are_running(kanban_home, recorder):
    _running(None, 1)
    kbd.pause_dispatch(None)
    pd.queue_post_drain_action(None, action_kind="reboot")

    with kbc.connect_closing(board=None) as conn:
        kbd.dispatch_once(conn, board=None)

    assert recorder == []
    assert pd.read_post_drain_action(None)["state"] == pd.WAITING


def test_the_dispatcher_tick_fires_the_action_exactly_once_across_ticks(kanban_home, recorder):
    kbd.pause_dispatch(None)
    pd.queue_post_drain_action(None, action_kind="reboot")

    for _ in range(4):
        with kbc.connect_closing(board=None) as conn:
            kbd.dispatch_once(conn, board=None)

    assert recorder == ["reboot:None"]


def test_a_dry_run_tick_never_fires_a_queued_action(kanban_home, recorder):
    """A dry run reports what a tick WOULD do; rebooting the host is not that."""
    kbd.pause_dispatch(None)
    pd.queue_post_drain_action(None, action_kind="reboot")

    with kbc.connect_closing(board=None) as conn:
        kbd.dispatch_once(conn, board=None, dry_run=True)

    assert recorder == []
    assert pd.read_post_drain_action(None)["state"] == pd.WAITING


def test_a_tick_with_no_queued_action_is_unaffected(kanban_home, recorder):
    """Existing pause/resume/dispatch behaviour is unchanged when nothing is queued."""
    kbd.pause_dispatch(None)

    with kbc.connect_closing(board=None) as conn:
        result = kbd.dispatch_once(conn, board=None)

    assert recorder == []
    assert result.dispatch_paused is not None
    assert pd.read_post_drain_action(None) is None


# --- aggregate groups fire once, and only when every member has drained ------


@pytest.fixture
def two_boards(kanban_home):
    """A second board beside ``default``, both operator-paused."""
    kb.create_board("other")
    kbd.pause_dispatch(None)
    kbd.pause_dispatch("other")
    return ("default", "other")


def _arm_group(boards, group_id="group-1", **kwargs):
    return [
        pd.queue_post_drain_action(board, action_kind="reboot", group_id=group_id, **kwargs)
        for board in boards
    ]


def test_an_aggregate_group_never_fires_while_any_member_board_still_runs(two_boards, recorder):
    """The host action is one action: one busy board holds back the whole group.

    Evaluating the DRAINED board must not fire, because the reboot it would
    trigger would SIGKILL the worker still running on the sibling board — the
    exact damage the drain wait exists to prevent.
    """
    _running("other", 1)
    _arm_group(two_boards)

    assert pd.evaluate_post_drain_action("default") is None

    assert recorder == []
    assert [pd.read_post_drain_action(board)["state"] for board in two_boards] == [
        pd.WAITING, pd.WAITING,
    ]


def test_an_aggregate_group_fires_exactly_one_host_action_across_its_boards(two_boards, recorder):
    """Every member drained: one invocation, and every member settles from it."""
    _arm_group(two_boards)

    pd.evaluate_post_drain_action("default")
    pd.evaluate_post_drain_action("other")

    assert recorder == ["reboot:None"]
    assert [pd.read_post_drain_action(board)["state"] for board in two_boards] == [
        pd.SUCCEEDED, pd.SUCCEEDED,
    ]


def test_an_aggregate_group_fires_once_when_the_last_board_drains(two_boards, recorder):
    """The tick that observes the final board draining is the one that fires."""
    _running("other", 1)
    _arm_group(two_boards)

    pd.evaluate_post_drain_action("default")
    assert recorder == []

    with kbc.connect_closing(board="other") as conn:
        conn.execute("UPDATE tasks SET status='done', claim_lock=NULL, worker_pid=NULL")
        conn.commit()

    pd.evaluate_post_drain_action("other")
    pd.evaluate_post_drain_action("default")

    assert recorder == ["reboot:None"]


def test_concurrent_board_ticks_fire_an_aggregate_group_exactly_once(two_boards, monkeypatch):
    """Two boards' ticks racing one group must not each run the host action.

    Deterministic like the single-board race: the winning handler blocks until
    the losing board's tick has already returned, so "one call" cannot be an
    artifact of the winner finishing first.
    """
    fired: list[str] = []
    loser_done = threading.Event()
    fire_entered = threading.Event()

    original = pd.ACTION_HANDLERS["reboot"]

    def slow_fire(record, cfg):
        fired.append("reboot")
        fire_entered.set()
        loser_done.wait(timeout=20)

    monkeypatch.setitem(
        pd.ACTION_HANDLERS, "reboot",
        type(original)(
            kind="reboot", takes_target=False, resolve_target=original.resolve_target,
            observe_before=lambda record, cfg: {}, fire=slow_fire,
            observe_after=lambda record, cfg: {"state": pd.SUCCEEDED},
        ),
    )
    _arm_group(two_boards)

    start = threading.Barrier(2)
    errors: list[BaseException] = []
    finished = threading.Semaphore(0)

    def tick(board):
        try:
            start.wait(timeout=10)
            pd.evaluate_post_drain_action(board)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)
        finally:
            finished.release()

    threads = [threading.Thread(target=tick, args=(board,)) for board in two_boards]
    for thread in threads:
        thread.start()

    assert fire_entered.wait(timeout=20), "no board tick ever reached the handler"
    assert finished.acquire(timeout=20), "the losing board tick never completed"
    loser_done.set()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert fired == ["reboot"]


def test_one_expired_member_expires_the_whole_aggregate_group(two_boards, recorder):
    """A group can only fire as a unit, so one dead leg ends all of them.

    Leaving the siblings ``waiting`` would strand records that can never fire
    again, and the operator would keep reading "armed" off a dead group.
    """
    _arm_group(two_boards, expires_in_seconds=60)

    pd.evaluate_post_drain_action("default", now=int(time.time()) + 61)

    assert recorder == []
    assert [pd.read_post_drain_action(board)["state"] for board in two_boards] == [
        pd.EXPIRED, pd.EXPIRED,
    ]


def test_resuming_one_board_disarms_the_whole_aggregate_group(two_boards, recorder):
    """Resume cancels — and for a host-wide action that means the group, not a leg."""
    _arm_group(two_boards)

    kbd.resume_dispatch("other")
    pd.evaluate_post_drain_action("default")

    assert recorder == []
    assert pd.read_post_drain_action("other")["state"] == pd.CANCELLED
    assert pd.read_post_drain_action("default")["state"] == pd.CANCELLED


# --- a firing record is reconciled later, without re-firing ------------------


@pytest.fixture
def real_reboot_observation(monkeypatch):
    """The REAL reboot observers, with only the destructive ``fire`` stubbed.

    The generic ``recorder`` fixture replaces ``observe_after`` with an
    unconditional success, which would make every reconciliation assertion below
    vacuous — the epoch comparison is exactly the logic under test.
    """
    fired: list[str] = []
    original = pd.ACTION_HANDLERS["reboot"]
    monkeypatch.setitem(
        pd.ACTION_HANDLERS, "reboot",
        type(original)(
            kind="reboot", takes_target=False, resolve_target=original.resolve_target,
            observe_before=pd._reboot_observe_before,
            fire=lambda record, cfg: fired.append("reboot"),
            observe_after=pd._reboot_observe_after,
            survives_execution=False,
        ),
    )
    return fired


def _firing_record_from_epoch(epoch):
    kbd.pause_dispatch(None)
    record = pd.queue_post_drain_action(None, action_kind="reboot")
    return pd._write_post_drain_action(None, {
        **record, "state": pd.FIRING, "observed_before": {"epoch": epoch},
    })


def test_a_firing_record_is_settled_by_a_later_tick_once_the_outcome_is_observable(
    kanban_home, monkeypatch, real_reboot_observation,
):
    """The observer can be destroyed by its own action; a later tick settles it.

    A reboot (or a restart of the dispatcher's own gateway) kills the process
    that issued it, so the ``firing`` record outlives every chance to observe it
    in-process. Reconciliation is a pure observation: it must settle the record
    and must never invoke the handler again.
    """
    import gateway.drain_control as drain_control

    monkeypatch.setattr(drain_control, "current_instantiation_epoch", lambda: "epoch-2")
    _firing_record_from_epoch("epoch-1")

    outcome = pd.evaluate_post_drain_action(None)

    assert outcome["state"] == pd.SUCCEEDED
    assert pd.read_post_drain_action(None)["state"] == pd.SUCCEEDED
    assert real_reboot_observation == [], "reconciliation must observe, never re-fire"


def test_a_firing_record_stays_firing_while_the_outcome_is_still_unobservable(
    kanban_home, monkeypatch, real_reboot_observation,
):
    """No observable verdict yet: settling either way would be a guess."""
    import gateway.drain_control as drain_control

    monkeypatch.setattr(drain_control, "current_instantiation_epoch", lambda: "epoch-1")
    _firing_record_from_epoch("epoch-1")

    pd.evaluate_post_drain_action(None)

    assert pd.read_post_drain_action(None)["state"] == pd.FIRING
    assert real_reboot_observation == []


def test_the_dispatcher_tick_reconciles_a_firing_record(
    kanban_home, monkeypatch, real_reboot_observation,
):
    """Reconciliation reaches the record through the ordinary server-side tick."""
    import gateway.drain_control as drain_control

    monkeypatch.setattr(drain_control, "current_instantiation_epoch", lambda: "epoch-2")
    _firing_record_from_epoch("epoch-1")

    with kbc.connect_closing(board=None) as conn:
        kbd.dispatch_once(conn, board=None)

    assert pd.read_post_drain_action(None)["state"] == pd.SUCCEEDED
    assert real_reboot_observation == []


# --- cancel is serialized against the waiting -> firing claim ----------------


def _gate_cancel_read(monkeypatch, read_started, release):
    """Make the cancel path block between its read and its write."""
    real_read = pd.read_post_drain_action

    def gated(board=None):
        value = real_read(board)
        if threading.current_thread().name == "cancel-thread" and not read_started.is_set():
            read_started.set()
            release.wait(timeout=20)
        return value

    monkeypatch.setattr(pd, "read_post_drain_action", gated)
    return real_read


def test_a_cancel_that_reports_success_means_the_action_never_fired(
    kanban_home, monkeypatch, recorder,
):
    """A successful safety cancel is a promise: nothing ran.

    The dangerous interleaving is cancel reading ``waiting``, an evaluation
    claiming and firing, and the cancel then writing its stale read back as
    ``cancelled`` — reporting a cancellation of an action that has already
    rebooted the host.
    """
    kbd.pause_dispatch(None)
    pd.queue_post_drain_action(None, action_kind="reboot")

    read_started = threading.Event()
    release = threading.Event()
    real_read = _gate_cancel_read(monkeypatch, read_started, release)
    result: dict = {}

    thread = threading.Thread(
        target=lambda: result.update(pd.cancel_post_drain_action(None)), name="cancel-thread",
    )
    thread.start()
    assert read_started.wait(timeout=20)

    outcome = pd.evaluate_post_drain_action(None)

    release.set()
    thread.join(timeout=20)
    monkeypatch.setattr(pd, "read_post_drain_action", real_read)

    assert outcome is None, "the claim must not proceed while a cancel holds the board"
    assert recorder == []
    assert result["cancelled"] is True
    assert pd.read_post_drain_action(None)["state"] == pd.CANCELLED


def test_a_cancel_arriving_after_the_claim_reports_failure(kanban_home, monkeypatch):
    """The other side of the race: the action fired, so the cancel says so."""
    fired: list[str] = []
    fire_entered = threading.Event()
    release_fire = threading.Event()
    original = pd.ACTION_HANDLERS["reboot"]

    def slow_fire(record, cfg):
        fired.append("reboot")
        fire_entered.set()
        release_fire.wait(timeout=20)

    monkeypatch.setitem(
        pd.ACTION_HANDLERS, "reboot",
        type(original)(
            kind="reboot", takes_target=False, resolve_target=original.resolve_target,
            observe_before=lambda record, cfg: {}, fire=slow_fire,
            observe_after=lambda record, cfg: {"state": pd.SUCCEEDED},
        ),
    )
    kbd.pause_dispatch(None)
    pd.queue_post_drain_action(None, action_kind="reboot")

    thread = threading.Thread(target=lambda: pd.evaluate_post_drain_action(None))
    thread.start()
    assert fire_entered.wait(timeout=20)

    cancelled = pd.cancel_post_drain_action(None)

    release_fire.set()
    thread.join(timeout=20)

    assert fired == ["reboot"]
    assert cancelled["cancelled"] is False
    assert pd.read_post_drain_action(None)["state"] != pd.CANCELLED


# --- service restart success is a strictly newer start, or it is a failure ---


@pytest.fixture
def unit_props(monkeypatch):
    """Drive ``_unit_properties`` from the test instead of from systemd."""

    def install(**props):
        monkeypatch.setattr(pd, "_unit_properties", lambda unit, cfg: dict(props))

    return install


@pytest.mark.parametrize(
    "before, after",
    [
        pytest.param({}, "100", id="no_baseline_at_all"),
        pytest.param({"start_monotonic": None}, "100", id="unreadable_baseline"),
        pytest.param({"start_monotonic": ""}, "100", id="empty_baseline"),
        pytest.param({"start_monotonic": "200"}, "100", id="start_time_went_backwards"),
        pytest.param({"start_monotonic": "100"}, "100", id="unit_never_restarted"),
        pytest.param({"start_monotonic": "100"}, "not-a-number", id="unparseable_after"),
        pytest.param({"start_monotonic": "100"}, "", id="missing_after"),
    ],
)
def test_a_service_restart_without_a_strictly_newer_start_is_not_a_success(
    kanban_home, unit_props, before, after,
):
    """"It came back active" is not evidence the unit was actually replaced.

    A pre-observation that failed to read (so the baseline is missing) or a
    start timestamp that did not strictly advance both mean the same thing: the
    restart cannot be shown to have happened, so it must not be reported done.
    """
    unit_props(ActiveState="active", MainPID="222", ExecMainStartTimestampMonotonic=after)

    verdict = pd._service_observe_after(
        {"target": "svc", "observed_before": before}, pd.PostDrainConfig(),
    )

    assert verdict["state"] == pd.FAILED


def test_a_service_restart_with_a_strictly_newer_start_succeeds(kanban_home, unit_props):
    unit_props(ActiveState="active", MainPID="222", ExecMainStartTimestampMonotonic="300")

    verdict = pd._service_observe_after(
        {"target": "svc", "observed_before": {"start_monotonic": "100"}}, pd.PostDrainConfig(),
    )

    assert verdict["state"] == pd.SUCCEEDED


def test_a_unit_that_was_inactive_before_the_restart_can_still_succeed(kanban_home, unit_props):
    """systemd reports ``0`` for a unit that has never started; that is a real baseline."""
    unit_props(ActiveState="active", MainPID="222", ExecMainStartTimestampMonotonic="500")

    verdict = pd._service_observe_after(
        {"target": "svc", "observed_before": {"active_state": "inactive", "start_monotonic": "0"}},
        pd.PostDrainConfig(),
    )

    assert verdict["state"] == pd.SUCCEEDED


def test_a_unit_that_is_not_active_after_the_restart_is_a_failure(kanban_home, unit_props):
    unit_props(ActiveState="failed", MainPID="0", ExecMainStartTimestampMonotonic="300")

    verdict = pd._service_observe_after(
        {"target": "svc", "observed_before": {"start_monotonic": "100"}}, pd.PostDrainConfig(),
    )

    assert verdict["state"] == pd.FAILED


# --- reboot is a host action; the service scope must not leak into it --------


class _OkResult:
    returncode = 0
    stdout = ""
    stderr = ""


def _record_argv(monkeypatch):
    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        return _OkResult()

    monkeypatch.setattr(pd.subprocess, "run", fake_run)
    return seen


def test_reboot_never_inherits_the_user_service_restart_scope(kanban_home, monkeypatch):
    """``systemctl --user reboot`` is not a host reboot.

    ``service_restart_scope`` declares where the ALLOWLISTED UNIT lives. Reading
    it in the reboot handler would silently turn a queued host reboot into a
    command against the user manager, which cannot reboot anything.
    """
    argv_seen = _record_argv(monkeypatch)

    pd._reboot_fire({}, pd.PostDrainConfig(service_restart_scope="user"))

    assert argv_seen == [["systemctl", "reboot"]]


def test_a_user_scoped_service_restart_still_uses_the_user_manager(kanban_home, monkeypatch):
    """The scope setting keeps working for the kind that actually declares it."""
    argv_seen = _record_argv(monkeypatch)

    pd._service_fire(
        {"target": "hermes-gateway.service"}, pd.PostDrainConfig(service_restart_scope="user"),
    )

    assert argv_seen == [["systemctl", "--user", "restart", "hermes-gateway.service"]]
