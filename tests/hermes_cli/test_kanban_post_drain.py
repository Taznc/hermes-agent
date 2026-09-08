"""Post-drain action queue: registry, validation, persistence, firing.

Behaviour contracts on the server-side half of the queue — no HTTP client and
no renderer anywhere in this file, because the card's central claim is that the
trigger fires without a browser connected.
"""

from __future__ import annotations

import os
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
    with kbc.connect_closing(board=board) as conn:
        for index in range(count):
            task_id = kb.create_task(conn, title=f"worker-{index}", assignee="worker")
            conn.execute(
                "UPDATE tasks SET status='running', claim_lock=?, worker_pid=? WHERE id=?",
                (f"{kb._host_prefix()}1", 4242 + index, task_id),
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
    """The state machine, not thread timing, is what makes firing exactly-once."""
    kbd.pause_dispatch(None)
    record = pd.queue_post_drain_action(None, action_kind="reboot")
    pd._write_post_drain_action(None, {**record, "state": pd.FIRING})

    assert pd.evaluate_post_drain_action(None) is None
    assert recorder == []
    assert pd.read_post_drain_action(None)["state"] == pd.FIRING


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
