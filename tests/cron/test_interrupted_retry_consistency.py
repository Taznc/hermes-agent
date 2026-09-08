"""Crash consistency of the replay decision: no half-armed retry, in either direction.

Round-1 review found two split-brain windows and these pin both closed:

1. The ledger decision was committed BEFORE the retry was armed, so a crash in between left a
   permanently "scheduled" row that is never selected again and no retry anywhere — the lost
   occurrence silently lost a second time.
2. Arming itself was two ``jobs.json`` writes (``trigger_job`` then the loop-bounding stamp), so a
   crash between them left a job armed to run with no stamp — exactly the state that lets a
   restart storm re-arm the same job over and over.

The fix is an ordering plus an atomic job-store mutation, and these tests inject a failure at each
commit boundary to prove there is no interleaving that breaks the invariant:

  at most one outstanding retry per job, and a "scheduled" ledger row implies an armed job.
"""

from __future__ import annotations

import pytest

import cron.executions as executions
import cron.interrupted_retry as retry
import cron.jobs as cron_jobs


def _point_stores(monkeypatch, tmp_path):
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    import cron.incidents as incidents

    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", tmp_path / "cron" / "incidents.db")


def _interrupt(job_id: str) -> dict:
    record = executions.create_execution(job_id, source="builtin")
    executions.finish_execution(
        record["id"], success=False,
        error="Interrupted by shutdown before terminal completion.", interrupted=True)
    return executions.get_execution(record["id"])


class TestArmingIsAtomic:
    def test_the_armed_flag_and_the_loop_bound_land_together(self, monkeypatch, tmp_path):
        """``manual_run_at`` (run me) and ``interrupted_retry`` (you already got your one retry)
        must be written in ONE job-store mutation. Persisting the first without the second is the
        state that permits an unbounded restart loop."""
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="atomic")
            record = _interrupt(job["id"])

            seen: list[tuple[bool, bool]] = []
            real_save = cron_jobs.save_jobs

            def _observe(jobs, *args, **kwargs):
                for saved in jobs:
                    if saved.get("id") == job["id"]:
                        seen.append(
                            (bool(saved.get("manual_run_at")),
                             bool(saved.get("interrupted_retry"))))
                return real_save(jobs, *args, **kwargs)

            monkeypatch.setattr(cron_jobs, "save_jobs", _observe)

            assert retry.reconcile_interrupted_executions() == 1

            armed = [state for state in seen if state != (False, False)]
            assert armed, "the retry must actually be armed"
            assert all(state == (True, True) for state in armed), (
                f"no save may persist one half of the arm without the other: {seen}")

            refreshed = cron_jobs.get_job(job["id"])
            assert refreshed["manual_run_at"]
            assert refreshed["interrupted_retry"]["execution_id"] == record["id"]

    def test_a_failure_while_arming_leaves_no_half_armed_job(self, monkeypatch, tmp_path):
        """Injected right at the job-store write: the job must be left entirely un-armed, so the
        next sweep can decide the occurrence again rather than running it unbounded."""
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="crash-arm")
            _interrupt(job["id"])

            crashing = {"now": True}
            real_save = cron_jobs.save_jobs

            def _maybe_boom(jobs, *args, **kwargs):
                if crashing["now"]:
                    raise OSError("jobs.json write failed")
                return real_save(jobs, *args, **kwargs)

            monkeypatch.setattr(cron_jobs, "save_jobs", _maybe_boom)
            assert retry.reconcile_interrupted_executions() == 0

            crashing["now"] = False
            refreshed = cron_jobs.get_job(job["id"])
            assert refreshed.get("manual_run_at") is None
            assert refreshed.get("interrupted_retry") is None


class TestTheDecisionSurvivesACrashBeforeArming:
    def test_a_crash_between_decision_and_arming_does_not_strand_the_occurrence(
        self, monkeypatch, tmp_path
    ):
        """The round-1 blocker: with the decision committed first, a crash before arming left
        ``retry_state='scheduled'`` and NO retry, and the row was never selected again. The
        occurrence must instead remain reconcilable and be armed on the next sweep."""
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="crash-gap")
            record = _interrupt(job["id"])

            crashing = {"now": True}
            real_save = cron_jobs.save_jobs

            def _maybe_boom(jobs, *args, **kwargs):
                if crashing["now"]:
                    raise OSError("gateway died between deciding and arming")
                return real_save(jobs, *args, **kwargs)

            monkeypatch.setattr(cron_jobs, "save_jobs", _maybe_boom)
            assert retry.reconcile_interrupted_executions() == 0
            assert executions.get_execution(record["id"])["retry_state"] is None, (
                "a crash before arming must leave the occurrence reconcilable, not decided")

            # A restart: the next sweep must still see work to do for this occurrence.
            crashing["now"] = False
            assert retry.reconcile_interrupted_executions() == 1

            assert executions.get_execution(record["id"])["retry_state"] == "scheduled"
            assert cron_jobs.get_job(job["id"])["interrupted_retry"]["execution_id"] == record["id"]

    def test_a_crash_after_preparing_the_arm_is_recovered_on_the_next_sweep(
        self, monkeypatch, tmp_path
    ):
        """If the process dies after jobs.json is prepared but before the ledger decision commits,
        the same occurrence must resume finalization instead of being mistaken for a rival retry."""
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="prepared-gap")
            record = _interrupt(job["id"])
            real_finalize = executions.finalize_retry_decision
            calls = {"count": 0}

            def _crash_once(*args, **kwargs):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise OSError("gateway died after preparing jobs.json")
                return real_finalize(*args, **kwargs)

            monkeypatch.setattr(executions, "finalize_retry_decision", _crash_once)

            assert retry.reconcile_interrupted_executions() == 0
            prepared = cron_jobs.get_job(job["id"])["interrupted_retry"]
            assert prepared["execution_id"] == record["id"]
            assert prepared["state"] == "prepared"
            assert executions.get_execution(record["id"])["retry_state"] is None

            assert retry.reconcile_interrupted_executions() == 1
            queued = cron_jobs.get_job(job["id"])["interrupted_retry"]
            assert queued["execution_id"] == record["id"]
            assert queued["state"] == "queued"

        assert executions.get_execution(record["id"])["retry_state"] == "scheduled"

    def test_a_crash_after_queueing_but_before_ledger_commit_is_recovered(
        self, monkeypatch, tmp_path
    ):
        """jobs.json is the recoverable side of the cross-store handoff: if queueing lands but
        the SQLite decision does not, the next sweep must finish that same occurrence."""
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="queued-gap")
            record = _interrupt(job["id"])
            real_finalize = executions.finalize_retry_decision
            calls = {"count": 0}

            def _crash_after_queueing(execution_id, resolver):
                calls["count"] += 1
                if calls["count"] != 1:
                    return real_finalize(execution_id, resolver)
                with executions._transaction() as conn:
                    row = conn.execute(
                        "SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
                    assert row is not None

                    def _fail_commit(_decision):
                        raise OSError("ledger commit failed after queueing jobs.json")

                    return resolver(conn, dict(row), _fail_commit)

            monkeypatch.setattr(
                executions, "finalize_retry_decision", _crash_after_queueing)

            assert retry.reconcile_interrupted_executions() == 0
            queued = cron_jobs.get_job(job["id"])["interrupted_retry"]
            assert queued["execution_id"] == record["id"]
            assert queued["state"] == "queued"
            assert executions.get_execution(record["id"])["retry_state"] is None

            assert retry.reconcile_interrupted_executions() == 1

        assert executions.get_execution(record["id"])["retry_state"] == "scheduled"

    def test_a_replay_that_finishes_before_ledger_recovery_is_not_queued_twice(
        self, monkeypatch, tmp_path
    ):
        """A second process can fire the queued retry after the jobs write but before recovery.
        Its durable ``retry_of`` link must settle the old decision without creating another replay."""
        from cron.scheduler_provider import InProcessCronScheduler

        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="finished-gap")
            record = _interrupt(job["id"])
            real_finalize = executions.finalize_retry_decision
            first = {"pending": True}

            def _crash_after_queueing(execution_id, resolver):
                if not first["pending"]:
                    return real_finalize(execution_id, resolver)
                first["pending"] = False
                with executions._transaction() as conn:
                    row = conn.execute(
                        "SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
                    assert row is not None

                    def _fail_commit(_decision):
                        raise OSError("ledger commit failed after queueing jobs.json")

                    return resolver(conn, dict(row), _fail_commit)

            monkeypatch.setattr(
                executions, "finalize_retry_decision", _crash_after_queueing)
            assert retry.reconcile_interrupted_executions() == 0

            claimed = InProcessCronScheduler().claim_fire(job["id"])
            assert isinstance(claimed, dict)
            replay = executions.get_execution(claimed["execution_id"])
            assert replay["retry_of"] == record["id"]
            executions.finish_execution(replay["id"], success=True)
            cron_jobs.mark_job_run(job["id"], True)
            assert cron_jobs.get_job(job["id"]).get("interrupted_retry") is None

            assert retry.reconcile_interrupted_executions() == 0
            assert cron_jobs.get_job(job["id"]).get("interrupted_retry") is None

        assert executions.get_execution(record["id"])["retry_state"] == "scheduled"

    def test_a_scheduled_ledger_row_always_has_an_armed_job(self, monkeypatch, tmp_path):
        """The invariant stated as a contract between the two stores, checked after a sweep that
        had to retry past an injected failure."""
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="invariant")
            _interrupt(job["id"])

            failures = {"left": 1}
            real_save = cron_jobs.save_jobs

            def _flaky(jobs, *args, **kwargs):
                if failures["left"]:
                    failures["left"] -= 1
                    raise OSError("transient store failure")
                return real_save(jobs, *args, **kwargs)

            monkeypatch.setattr(cron_jobs, "save_jobs", _flaky)

            retry.reconcile_interrupted_executions()
            retry.reconcile_interrupted_executions()

            scheduled = [
                row for row in executions.list_executions(limit=50)
                if row["retry_state"] == "scheduled"
            ]
            refreshed = cron_jobs.get_job(job["id"])
            if scheduled:
                assert refreshed["interrupted_retry"], (
                    "a scheduled decision must never exist without an armed job")
                assert refreshed["manual_run_at"]
            assert len(scheduled) <= 1


class TestPauseRacesCannotResurrectAJob:
    def test_a_pause_after_the_arm_write_cancels_the_prepared_retry(
        self, monkeypatch, tmp_path
    ):
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="post-arm-pause")
            record = _interrupt(job["id"])
            real_arm = retry._arm_retry

            def _arm_then_pause(rec):
                outcome = real_arm(rec)
                cron_jobs.pause_job(job["id"])
                return outcome

            monkeypatch.setattr(retry, "_arm_retry", _arm_then_pause)

            assert retry.reconcile_interrupted_executions() == 0
            refreshed = cron_jobs.get_job(job["id"])
            assert refreshed["enabled"] is False
            assert refreshed["state"] == "paused"
            assert refreshed.get("manual_run_at") is None
            assert refreshed.get("interrupted_retry") is None

        assert executions.get_execution(record["id"])["retry_state"] == "declined:disabled"

    def test_a_live_execution_and_fire_claim_after_decide_block_the_retry(
        self, monkeypatch, tmp_path
    ):
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="post-decide-live")
            record = _interrupt(job["id"])
            live = {}
            real_decide = retry._decide

            def _decide_then_start(rec, max_age):
                outcome = real_decide(rec, max_age)
                live["execution"] = executions.create_execution(job["id"], source="builtin")
                executions.mark_execution_running(live["execution"]["id"])
                live["claim"] = cron_jobs.claim_job_for_fire(job["id"], return_job=True)
                return outcome

            monkeypatch.setattr(retry, "_decide", _decide_then_start)

            assert retry.reconcile_interrupted_executions() == 0
            refreshed = cron_jobs.get_job(job["id"])
            assert isinstance(live["claim"], dict)
            assert refreshed.get("manual_run_at") is None
            assert refreshed.get("interrupted_retry") is None

        assert executions.get_execution(record["id"])["retry_state"] == "declined:in_flight"

    def test_a_pause_landing_after_eligibility_wins(self, monkeypatch, tmp_path):
        """Round-1 blocker 3: eligibility was read, then ``trigger_job`` re-enabled the job as a
        side effect. If the operator pauses in that gap, the pause must win — a disabled job is
        never resurrected by a replay."""
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="raced")
            record = _interrupt(job["id"])

            real_decide = retry._decide

            def _decide_then_pause(rec, max_age):
                decision = real_decide(rec, max_age)
                cron_jobs.pause_job(job["id"])  # the operator, in the TOCTOU window
                return decision

            monkeypatch.setattr(retry, "_decide", _decide_then_pause)

            assert retry.reconcile_interrupted_executions() == 0

            refreshed = cron_jobs.get_job(job["id"])
            assert refreshed["enabled"] is False, "a paused job must not be re-enabled"
            assert refreshed["state"] == "paused"
            assert refreshed.get("manual_run_at") is None
            assert refreshed.get("interrupted_retry") is None
        assert executions.get_execution(record["id"])["retry_state"] == "declined:disabled"

    def test_a_removal_landing_after_eligibility_is_declined(self, monkeypatch, tmp_path):
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="removed")
            record = _interrupt(job["id"])

            real_decide = retry._decide

            def _decide_then_remove(rec, max_age):
                decision = real_decide(rec, max_age)
                cron_jobs.remove_job(job["id"])
                return decision

            monkeypatch.setattr(retry, "_decide", _decide_then_remove)

            assert retry.reconcile_interrupted_executions() == 0
            assert cron_jobs.get_job(job["id"]) is None
        assert executions.get_execution(record["id"])["retry_state"] == "declined:job_missing"

    def test_a_second_retry_stamped_in_the_window_is_declined(self, monkeypatch, tmp_path):
        """The at-most-once bound must also be enforced at the arming point, not only at the
        earlier read: another reconciler stamping the job first must make this one decline."""
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="double")
            record = _interrupt(job["id"])

            real_decide = retry._decide

            def _decide_then_stamp(rec, max_age):
                decision = real_decide(rec, max_age)
                cron_jobs.update_job(job["id"], {"interrupted_retry": {
                    "execution_id": "someone-elses-attempt"}})
                return decision

            monkeypatch.setattr(retry, "_decide", _decide_then_stamp)

            assert retry.reconcile_interrupted_executions() == 0

            stamp = cron_jobs.get_job(job["id"])["interrupted_retry"]
            assert stamp["execution_id"] == "someone-elses-attempt", "the winner keeps the stamp"
        assert executions.get_execution(record["id"])["retry_state"] == "declined:retry_outstanding"


@pytest.mark.parametrize("terminal_state", ["completed", "error"])
def test_a_job_that_went_terminal_in_the_window_is_never_re_armed(
    monkeypatch, tmp_path, terminal_state
):
    """``trigger_job`` raises on a terminal job. The reconciler must decline it as a recorded
    decision rather than letting the exception abort the occurrence undecided."""
    _point_stores(monkeypatch, tmp_path)
    with cron_jobs.use_cron_store(tmp_path):
        job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="terminal")
        record = _interrupt(job["id"])

        real_decide = retry._decide

        def _decide_then_retire(rec, max_age):
            decision = real_decide(rec, max_age)
            cron_jobs.update_job(job["id"], {"enabled": False, "state": terminal_state})
            return decision

        monkeypatch.setattr(retry, "_decide", _decide_then_retire)

        assert retry.reconcile_interrupted_executions() == 0
        assert cron_jobs.get_job(job["id"])["state"] == terminal_state
    assert executions.get_execution(record["id"])["retry_state"] == "declined:disabled"
