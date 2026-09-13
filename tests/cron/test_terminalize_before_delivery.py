"""Durable terminalization of a cron run must not wait on Bot Chat delivery.

A ``no_agent`` cron job's script finishes in ~1s; its ``deliver: bot-chat`` notice is a full agent
turn in a subprocess (``cron.bot_chat_delivery_timeout_seconds``, default 600s). Before this
ordering fix, ``finish_execution`` was only reached AFTER that send returned, so a shutdown drain
or a fire-claim ownership loss landing in the delivery window found the ledger row still
``running`` and overwrote a successful run with
``"Interrupted by shutdown before terminal completion."`` — observed on job ``5c8bb0917cce``,
executions ``dcff785b…`` (307s) and ``03b68d…`` (161s), whose script had exited 0 in ~1s and whose
output was already saved.

The contract these tests pin:

1. the run's terminal ledger row is durable *before* the Bot Chat handler returns;
2. exactly one Bot Chat delivery happens per successful execution, and no recovery path replays it;
3. a delivery failure is recorded as its own outcome and never rewrites the completed run.

Every test drives the real ``run_one_job`` body against a real SQLite ledger in a temp
``HERMES_HOME``; only the transport boundary (``_deliver_to_bot_chat``'s subprocess) is faked.
"""

from __future__ import annotations

import contextlib
import threading

import pytest


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A real executions ledger in a temp home (no mocks on the durable layer)."""
    import cron.executions as executions

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "executions.db")
    yield executions
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", None)


@pytest.fixture
def scheduler_env(monkeypatch, ledger):
    """Patch the scheduler's non-ledger side effects; delivery stays under test control."""
    import cron.scheduler as scheduler

    @contextlib.contextmanager
    def owned_fence(*_args, **_kwargs):
        yield True

    monkeypatch.setattr(scheduler, "claim_dispatch", lambda *_a, **_kw: True)
    monkeypatch.setattr(scheduler, "heartbeat_fire_claim", lambda *_a, **_kw: True)
    monkeypatch.setattr(scheduler, "fire_claim_fence", owned_fence, raising=False)
    monkeypatch.setattr(scheduler, "save_job_output", lambda _jid, out: "/tmp/out.md")
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *_a, **_kw: True)
    return scheduler


def _script_job(**overrides):
    """A no_agent script job delivering to Bot Chat, mid-run on a claimed execution."""
    job = {
        "id": "botchat-job",
        "name": "morning-digest",
        "no_agent": True,
        "script": "digest.sh",
        "deliver": "bot-chat",
        "schedule": {"kind": "interval"},
    }
    job.update(overrides)
    return job


def _claimed_execution(scheduler, ledger, job):
    """Create the real ledger row the way the ticker does; run_one_job starts it."""
    record = ledger.create_execution(job["id"], source="schedule")
    job["execution_id"] = record["id"]
    return record["id"]


SCRIPT_RESULT = "queue depth 4; disk 62%"


def _returns_script_result(*_a, **_kw):
    return (True, SCRIPT_RESULT, SCRIPT_RESULT, None)


# ---------------------------------------------------------------------------
# AC1 — the run is durably terminal before Bot Chat handling returns
# ---------------------------------------------------------------------------


def test_execution_is_terminal_with_script_result_before_bot_chat_delivery_returns(
    scheduler_env, ledger, monkeypatch
):
    """The invariant: while the Bot Chat handler is still running, the ledger already
    carries this execution as completed with the script's result."""
    scheduler = scheduler_env
    job = _script_job()
    execution_id = _claimed_execution(scheduler, ledger, job)
    observed = {}

    def slow_bot_chat_delivery(delivering_job, _content, **_kw):
        # Stands in for _deliver_to_bot_chat's blocking subprocess.run. Read the DURABLE row
        # from inside the delivery, which is exactly the window the interruption lands in.
        observed["row"] = ledger.get_execution(delivering_job["execution_id"])
        return None

    monkeypatch.setattr(scheduler, "run_job", _returns_script_result)
    monkeypatch.setattr(scheduler, "_deliver_result", slow_bot_chat_delivery)

    assert scheduler.run_one_job(job) is True

    row = observed["row"]
    assert row is not None, "delivery ran before the execution row existed"
    assert row["status"] == "completed"
    assert row["interrupted"] == 0
    assert row["finished_at"] is not None
    assert row["error"] is None


def test_shutdown_during_bot_chat_delivery_cannot_rewrite_a_completed_run(
    scheduler_env, ledger, monkeypatch
):
    """The reported production failure: the drain flag is raised mid-delivery. The run had
    already succeeded, so it must stay completed rather than becoming
    'Interrupted by shutdown before terminal completion.'"""
    scheduler = scheduler_env
    job = _script_job()
    execution_id = _claimed_execution(scheduler, ledger, job)

    def bot_chat_delivery_interrupted_by_shutdown(delivering_job, _content, **_kw):
        # gateway shutdown drain fires while the bot's turn is still in flight
        scheduler.mark_running_jobs_interrupted("gateway shutting down")
        with scheduler._running_lock:
            scheduler._interrupted_job_ids.add(delivering_job["id"])
        return None

    monkeypatch.setattr(scheduler, "run_job", _returns_script_result)
    monkeypatch.setattr(
        scheduler, "_deliver_result", bot_chat_delivery_interrupted_by_shutdown)

    assert scheduler.run_one_job(job) is True

    row = ledger.get_execution(execution_id)
    assert row["status"] == "completed"
    assert row["interrupted"] == 0
    assert row["error"] is None


def test_fire_claim_ownership_lost_during_delivery_cannot_rewrite_a_completed_run(
    scheduler_env, ledger, monkeypatch
):
    """The sibling interruption path: the heartbeat declares ownership lost while delivery is
    in flight. #100401 shows this fires on healthy runs; either way, a finished run's durable
    result must survive it."""
    scheduler = scheduler_env
    job = _script_job(fire_claim={"at": "2026-09-08T06:00:00+00:00", "by": "owner-1"})
    execution_id = _claimed_execution(scheduler, ledger, job)
    lost = threading.Event()

    def bot_chat_delivery_loses_the_claim(*_a, **_kw):
        lost.set()  # heartbeat thread decides ownership is gone mid-delivery
        return None

    monkeypatch.setattr(scheduler, "run_job", _returns_script_result)
    monkeypatch.setattr(scheduler, "_deliver_result", bot_chat_delivery_loses_the_claim)

    assert scheduler.run_one_job(job, cancel_event=lost) is True

    row = ledger.get_execution(execution_id)
    assert row["status"] == "completed"
    assert row["interrupted"] == 0
    assert row["error"] is None


# ---------------------------------------------------------------------------
# AC2 — exactly one Bot Chat delivery, and no recovery path replays it
# ---------------------------------------------------------------------------


def test_one_successful_execution_performs_exactly_one_bot_chat_delivery(
    scheduler_env, ledger, monkeypatch
):
    """Reordering must not introduce a second send, and the completed row must not become a
    replay candidate for the interrupted-occurrence reconciler."""
    scheduler = scheduler_env
    job = _script_job()
    execution_id = _claimed_execution(scheduler, ledger, job)
    sends = []

    monkeypatch.setattr(scheduler, "run_job", _returns_script_result)
    monkeypatch.setattr(
        scheduler,
        "_deliver_result",
        lambda delivering_job, content, **_kw: sends.append(content) or None,
    )

    assert scheduler.run_one_job(job) is True
    assert sends == [SCRIPT_RESULT]

    # The reconciler is the ONLY path that could replay this occurrence, and it selects on
    # interrupted=1. A completed run must offer it nothing to replay.
    assert ledger.list_undecided_interruptions() == []
    row = ledger.get_execution(execution_id)
    assert row["status"] == "completed" and row["interrupted"] == 0


def test_shutdown_during_delivery_does_not_make_a_delivered_run_a_replay_candidate(
    scheduler_env, ledger, monkeypatch
):
    """The duplicate-delivery hazard, end to end: a run interrupted DURING delivery used to be
    flagged interrupted, which arms cron.interrupted_retry to run — and deliver — it again."""
    scheduler = scheduler_env
    job = _script_job()
    execution_id = _claimed_execution(scheduler, ledger, job)
    sends = []

    def deliver_then_shutdown(delivering_job, content, **_kw):
        sends.append(content)
        with scheduler._running_lock:
            scheduler._interrupted_job_ids.add(delivering_job["id"])
        return None

    monkeypatch.setattr(scheduler, "run_job", _returns_script_result)
    monkeypatch.setattr(scheduler, "_deliver_result", deliver_then_shutdown)

    assert scheduler.run_one_job(job) is True

    assert sends == [SCRIPT_RESULT]
    assert ledger.list_undecided_interruptions() == []
    assert ledger.get_execution(execution_id)["interrupted"] == 0


def test_run_interrupted_before_delivery_is_still_recorded_as_interrupted(
    scheduler_env, ledger, monkeypatch
):
    """Guard against over-correcting: when the RUN itself was killed (flag already set when the
    run returns), the attempt must still terminalize as an interruption so the reconciler can
    see it. Terminalizing early must not swallow real interruptions."""
    scheduler = scheduler_env
    job = _script_job()
    execution_id = _claimed_execution(scheduler, ledger, job)

    def run_job_killed_mid_flight(*_a, **_kw):
        with scheduler._running_lock:
            scheduler._interrupted_job_ids.add(job["id"])
        return (True, "truncated", "truncated", None)

    monkeypatch.setattr(scheduler, "run_job", run_job_killed_mid_flight)
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *_a, **_kw: None)

    assert scheduler.run_one_job(job) is True

    row = ledger.get_execution(execution_id)
    assert row["status"] == "failed"
    assert row["interrupted"] == 1
    assert len(ledger.list_undecided_interruptions()) == 1


# ---------------------------------------------------------------------------
# AC3 — delivery failure and run completion are independent durable outcomes
# ---------------------------------------------------------------------------


def test_bot_chat_delivery_failure_does_not_overwrite_the_completed_run(
    scheduler_env, ledger, monkeypatch
):
    """Both outcomes must stay visible: the script execution terminal-successful with its saved
    result, the delivery failure recorded separately on the ledger and the job record."""
    scheduler = scheduler_env
    job = _script_job()
    execution_id = _claimed_execution(scheduler, ledger, job)
    marked = []

    monkeypatch.setattr(scheduler, "run_job", _returns_script_result)
    monkeypatch.setattr(
        scheduler,
        "_deliver_result",
        lambda *_a, **_kw: "bot-chat delivery to profile '(own)' failed (exit 1)",
    )
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda *args, **kwargs: marked.append((args, kwargs)) or True,
    )

    assert scheduler.run_one_job(job) is True

    row = ledger.get_execution(execution_id)
    # The run: still terminal-successful, with no error text borrowed from the delivery.
    assert row["status"] == "completed"
    assert row["interrupted"] == 0
    assert row["error"] is None
    # The delivery: recorded on its own axis.
    assert row["delivery_outcome"] == "failed"
    # And on the job record, where `hermes cron list` surfaces it.
    (_job_id, success, error), kwargs = marked[0]
    assert success is True and error is None
    assert kwargs["delivery_error"] == "bot-chat delivery to profile '(own)' failed (exit 1)"


def test_delivery_failure_and_shutdown_together_keep_the_run_completed(
    scheduler_env, ledger, monkeypatch
):
    """The worst case seen in production: delivery fails AND the drain fires. Neither may
    convert the finished run into an interruption; the delivery failure is still reported."""
    scheduler = scheduler_env
    job = _script_job()
    execution_id = _claimed_execution(scheduler, ledger, job)
    updates = {}

    def failing_delivery_then_shutdown(delivering_job, _content, **_kw):
        with scheduler._running_lock:
            scheduler._interrupted_job_ids.add(delivering_job["id"])
        return "bot-chat delivery timed out after 600s"

    monkeypatch.setattr(scheduler, "run_job", _returns_script_result)
    monkeypatch.setattr(scheduler, "_deliver_result", failing_delivery_then_shutdown)
    monkeypatch.setattr(
        "cron.jobs.update_job",
        lambda job_id, fields: updates.update(fields) or {},
    )

    assert scheduler.run_one_job(job) is True

    row = ledger.get_execution(execution_id)
    assert row["status"] == "completed"
    assert row["interrupted"] == 0
    assert row["delivery_outcome"] == "failed"
    assert updates["last_delivery_error"] == "bot-chat delivery timed out after 600s"


def test_terminal_run_state_is_immutable_once_delivery_starts(ledger):
    """The ledger primitive this ordering relies on: a completed attempt accepts a delivery
    outcome but refuses any rewrite of status/error/interrupted."""
    record = ledger.create_execution("job-immutable", source="schedule")
    ledger.mark_execution_running(record["id"])
    ledger.finish_execution(record["id"], success=True)

    # The interruption writes that used to clobber a completed run are now no-ops.
    assert ledger.finish_execution(
        record["id"], success=False, error="Interrupted by shutdown before terminal completion.",
        interrupted=True) is None

    updated = ledger.record_delivery_outcome(record["id"], "failed")
    assert updated is not None
    assert updated["status"] == "completed"
    assert updated["interrupted"] == 0
    assert updated["error"] is None
    assert updated["delivery_outcome"] == "failed"

    # At-most-once: a second delivery outcome for one attempt is refused.
    assert ledger.record_delivery_outcome(record["id"], "delivered") is None


# ---------------------------------------------------------------------------
# Splitting the durable write must not split the monitoring projection
# ---------------------------------------------------------------------------


@pytest.fixture
def emitted_events(monkeypatch):
    """Capture what the real ledger projects to monitoring, via the real emitter seam."""
    from agent.monitoring import emitter

    events = []

    class RecordingEmitter:
        def emit(self, event):
            events.append(event)

        def flush(self, timeout=None):
            pass

    monkeypatch.setattr(emitter, "get_emitter", lambda: RecordingEmitter())
    return events


def _terminal(events):
    return [(e.status, e.delivery_outcome) for e in events if e.status in ("completed", "failed")]


def test_one_execution_emits_exactly_one_terminal_projection_carrying_its_delivery_outcome(
    scheduler_env, ledger, monkeypatch, emitted_events
):
    """``CronExecutionEvent`` carries a job key but no execution id, so a second terminal event
    for one attempt is indistinguishable from another attempt: exporters double-count completions
    and flush twice. Terminalizing before delivery splits the durable WRITE; it must not split
    the projection, and the one event that survives has to carry the final delivery outcome."""
    scheduler = scheduler_env
    job = _script_job()
    _claimed_execution(scheduler, ledger, job)

    monkeypatch.setattr(scheduler, "run_job", _returns_script_result)
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *_a, **_kw: None)

    assert scheduler.run_one_job(job) is True

    assert _terminal(emitted_events) == [("completed", "delivered")]


def test_terminal_projection_survives_a_lost_claim_that_never_records_a_delivery_outcome(
    scheduler_env, ledger, monkeypatch, emitted_events
):
    """The trap in deferring the emit to ``record_delivery_outcome``: the fire claim can be lost
    inside the DELIVERY fence, i.e. after the row terminalized, and that path never records a
    delivery outcome at all. Deferring must not trade a duplicated terminal event for a dropped
    one, so the run's own projection still has to leave exactly once."""
    scheduler = scheduler_env
    job = _script_job(fire_claim={"at": "2026-09-08T06:00:00+00:00", "by": "owner-1"})
    execution_id = _claimed_execution(scheduler, ledger, job)
    lost = threading.Event()

    def delivery_loses_the_claim(*_a, **_kw):
        lost.set()
        raise scheduler._FireClaimLostDuringSideEffect

    monkeypatch.setattr(scheduler, "run_job", _returns_script_result)
    monkeypatch.setattr(scheduler, "_deliver_result", delivery_loses_the_claim)

    assert scheduler.run_one_job(job, cancel_event=lost) is True

    assert ledger.get_execution(execution_id)["status"] == "completed"
    assert _terminal(emitted_events) == [("completed", None)]
