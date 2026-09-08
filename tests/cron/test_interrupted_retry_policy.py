"""Bounded, at-most-once replay of a cron occurrence lost to a shutdown.

The policy under test: an interrupted occurrence is replayed once, only while it is still fresh,
only for a job that is still supposed to run, and never while any attempt for that job is still
in flight. Everything else is DECLINED with a recorded reason, so a lost occurrence is never
silently dropped and never replayed twice.
"""

from __future__ import annotations

from datetime import timedelta

import cron.executions as executions
import cron.interrupted_retry as retry
import cron.jobs as cron_jobs
from hermes_time import now as _hermes_now


def _interrupted_attempt(job_id: str, *, age_minutes: float = 0.0) -> dict:
    """A terminal, shutdown-interrupted ledger row for ``job_id``, aged into the past."""
    record = executions.create_execution(job_id, source="builtin")
    executions.finish_execution(
        record["id"], success=False, error="Interrupted by shutdown before terminal completion.",
        interrupted=True,
    )
    if age_minutes:
        claimed_at = (_hermes_now() - timedelta(minutes=age_minutes)).isoformat()
        with executions._transaction() as conn:
            conn.execute(
                "UPDATE executions SET claimed_at=? WHERE id=?", (claimed_at, record["id"])
            )
    return executions.get_execution(record["id"])


def _decisions() -> dict:
    return {row["id"]: row["retry_state"] for row in executions.list_executions(limit=100)}


class TestBoundedRetryPolicy:
    def test_weekly_no_agent_occurrence_is_replayed_once(self, monkeypatch, tmp_path):
        """The card's headline case: a weekly script job interrupted by a shutdown must get its
        lost occurrence back, not wait another week."""
        monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(
                prompt="", schedule="every monday 9am", name="weekly gc",
                script="echo hi", no_agent=True,
            )
            record = _interrupted_attempt(job["id"])

            assert retry.reconcile_interrupted_executions() == 1

            refreshed = cron_jobs.get_job(job["id"])
            assert refreshed["manual_run_at"], "the lost occurrence must be re-armed to fire now"
            assert refreshed["interrupted_retry"]["execution_id"] == record["id"]
        assert _decisions()[record["id"]] == "scheduled"

    def test_high_frequency_job_is_replayed_the_same_way(self, monkeypatch, tmp_path):
        monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="fleet health")
            record = _interrupted_attempt(job["id"])

            assert retry.reconcile_interrupted_executions() == 1
            assert cron_jobs.get_job(job["id"])["manual_run_at"]
        assert _decisions()[record["id"]] == "scheduled"

    def test_repeated_restarts_schedule_exactly_one_retry(self, monkeypatch, tmp_path):
        """A restart storm must not fan one job out into a queue of replays: while a retry is
        outstanding, later interruptions are recorded and declined."""
        monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="storm")
            for _ in range(4):
                _interrupted_attempt(job["id"])
                retry.reconcile_interrupted_executions()

        states = list(_decisions().values())
        assert states.count("scheduled") == 1
        assert states.count("declined:retry_outstanding") == 3

    def test_stale_occurrence_is_declined_not_replayed(self, monkeypatch, tmp_path):
        """Bounded freshness: a long-dead occurrence is not resurrected hours later."""
        monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="stale")
            record = _interrupted_attempt(job["id"], age_minutes=600)

            assert retry.reconcile_interrupted_executions() == 0
            assert cron_jobs.get_job(job["id"]).get("manual_run_at") is None
        assert _decisions()[record["id"]] == "declined:stale"

    def test_disabled_job_is_never_resurrected(self, monkeypatch, tmp_path):
        monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="paused")
            record = _interrupted_attempt(job["id"])
            cron_jobs.pause_job(job["id"])

            assert retry.reconcile_interrupted_executions() == 0

            refreshed = cron_jobs.get_job(job["id"])
            assert refreshed["enabled"] is False
            assert refreshed["state"] == "paused"
            assert refreshed.get("manual_run_at") is None
        assert _decisions()[record["id"]] == "declined:disabled"

    def test_removed_job_is_declined_without_raising(self, monkeypatch, tmp_path):
        monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="gone")
            record = _interrupted_attempt(job["id"])
            cron_jobs.remove_job(job["id"])

            assert retry.reconcile_interrupted_executions() == 0
        assert _decisions()[record["id"]] == "declined:job_missing"

    def test_in_flight_attempt_blocks_a_duplicate_run(self, monkeypatch, tmp_path):
        """Non-duplication beats replay: while any attempt for the job is still live, the lost
        occurrence is declined rather than run concurrently."""
        monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="busy")
            record = _interrupted_attempt(job["id"])
            live = executions.create_execution(job["id"], source="builtin")
            executions.mark_execution_running(live["id"])

            assert retry.reconcile_interrupted_executions() == 0
            assert cron_jobs.get_job(job["id"]).get("manual_run_at") is None
        assert _decisions()[record["id"]] == "declined:in_flight"

    def test_non_shutdown_failure_is_never_replayed(self, monkeypatch, tmp_path):
        """An ordinary job failure (and the 3-minute runaway interrupt, which surfaces as a
        timeout) is the job's own outcome — re-running it is not this policy's job."""
        monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="failing")
            for error in ("boom", "Cron job timed out (inactivity)"):
                record = executions.create_execution(job["id"], source="builtin")
                executions.finish_execution(record["id"], success=False, error=error)

            assert retry.reconcile_interrupted_executions() == 0
            assert cron_jobs.get_job(job["id"]).get("manual_run_at") is None
        assert set(_decisions().values()) == {None}

    def test_retry_is_disabled_by_a_zero_freshness_budget(self, monkeypatch, tmp_path):
        monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
        monkeypatch.setattr(retry, "_max_age_minutes", lambda: 0.0)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="off")
            record = _interrupted_attempt(job["id"])

            assert retry.reconcile_interrupted_executions() == 0
            assert cron_jobs.get_job(job["id"]).get("manual_run_at") is None
        assert _decisions()[record["id"]] == "declined:disabled_by_config"


class TestRestartReconciliation:
    """End-to-end: a killed gateway leaves a live-looking row; the next startup must terminalize
    it, raise an incident, and re-arm the lost occurrence — in one step."""

    def test_startup_recovery_terminalizes_and_replays_in_one_pass(
        self, monkeypatch, tmp_path, make_cron_provider
    ):
        monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(
                prompt="", schedule="every monday 9am", name="worktree gc",
                script="echo gc", no_agent=True,
            )
            # The dead gateway's attempt: claimed, running, never terminalized.
            record = executions.create_execution(job["id"], source="builtin")
            executions.mark_execution_running(record["id"])
            assert executions.get_execution(record["id"])["status"] == "running"

            # Restart: a new process whose predecessor is provably gone.
            monkeypatch.setattr(executions, "_PROCESS_ID", "replacement-gateway")
            monkeypatch.setattr(executions, "_owner_is_live", lambda _pid, _started: False)

            assert make_cron_provider().recover_interrupted() == 1

            reconciled = executions.get_execution(record["id"])
            assert reconciled["status"] == "unknown", "no permanently running ledger row"
            assert reconciled["interrupted"] == 1
            assert reconciled["retry_state"] == "scheduled"
            assert cron_jobs.get_job(job["id"])["interrupted_retry"]["execution_id"] == record["id"]

    def test_a_live_owner_is_left_alone_by_startup_recovery(
        self, monkeypatch, tmp_path, make_cron_provider
    ):
        """Recovery must not steal an attempt whose owner is still alive (a second gateway, or a
        restart-safe worker that outlived its dispatcher)."""
        monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="live")
            record = executions.create_execution(job["id"], source="builtin")
            executions.mark_execution_running(record["id"])

            monkeypatch.setattr(executions, "_PROCESS_ID", "replacement-gateway")
            monkeypatch.setattr(executions, "_owner_is_live", lambda _pid, _started: True)

            assert make_cron_provider().recover_interrupted() == 0

            assert executions.get_execution(record["id"])["status"] == "running"
            assert cron_jobs.get_job(job["id"]).get("interrupted_retry") is None


class TestRetryStampLifecycle:
    def test_the_freshness_budget_comes_from_config(self, monkeypatch, tmp_path):
        """The bound is real config, not a hardcoded constant: a config.yaml value must reach the
        policy through the ordinary loader."""
        import yaml

        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text(
            yaml.safe_dump({"cron": {"interrupted_retry_max_age_minutes": 5}}))
        monkeypatch.setenv("HERMES_HOME", str(home))

        assert retry._max_age_minutes() == 5.0

    def test_the_default_budget_is_the_shipped_config_default(self, monkeypatch, tmp_path):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))

        assert retry._max_age_minutes() == float(
            DEFAULT_CONFIG["cron"]["interrupted_retry_max_age_minutes"])
        assert retry._max_age_minutes() == retry.DEFAULT_MAX_AGE_MINUTES

    def test_a_successful_run_clears_the_outstanding_stamp(self, monkeypatch, tmp_path):
        """Only a SUCCESSFUL run retires the stamp, so the at-most-once bound cannot be reset by
        the retry failing the same way."""
        monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="stamped")
            _interrupted_attempt(job["id"])
            retry.reconcile_interrupted_executions()
            assert cron_jobs.get_job(job["id"])["interrupted_retry"]

            cron_jobs.mark_job_run(job["id"], False, "still broken")
            assert cron_jobs.get_job(job["id"])["interrupted_retry"], "a failed retry keeps it"

            cron_jobs.mark_job_run(job["id"], True)
            assert cron_jobs.get_job(job["id"]).get("interrupted_retry") is None
