"""Diagnostic surfaces for a shutdown-interrupted occurrence and its replay.

An interruption that is only visible in the ledger's raw columns is not diagnosable. These pin
that ``hermes cron list/history/incidents/doctor`` each say enough to trace the original attempt
and its retry.
"""

from __future__ import annotations

import argparse

import cron.executions as executions
import cron.jobs as cron_jobs
import hermes_cli.cron as cron_cli


def _point_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")


def _interrupt_and_reconcile(job_id: str) -> str:
    import cron.interrupted_retry as retry

    record = executions.create_execution(job_id, source="builtin")
    executions.finish_execution(
        record["id"], success=False, error="Interrupted by shutdown before terminal completion.",
        interrupted=True,
    )
    retry.reconcile_interrupted_executions()
    return record["id"]


class TestHistoryExposesTheInterruptionAndItsRetry:
    def test_history_marks_the_interrupted_attempt_and_its_decision(
        self, monkeypatch, tmp_path, capsys
    ):
        _point_ledger(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="hist")
            execution_id = _interrupt_and_reconcile(job["id"])

        cron_cli.cron_runs(job_id=job["id"])
        out = capsys.readouterr().out

        assert execution_id in out
        assert "interrupted" in out
        assert "retry scheduled" in out

    def test_ordinary_failures_carry_no_interruption_marker(self, monkeypatch, tmp_path, capsys):
        _point_ledger(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="plain")
            record = executions.create_execution(job["id"], source="builtin")
            executions.finish_execution(record["id"], success=False, error="boom")

        cron_cli.cron_runs(job_id=job["id"])
        out = capsys.readouterr().out

        assert "boom" in out
        assert "interrupted" not in out


class TestListAndDoctorSurfaceAPendingReplay:
    def test_list_shows_the_pending_interrupted_retry(self, monkeypatch, tmp_path, capsys):
        _point_ledger(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="listed")
            execution_id = _interrupt_and_reconcile(job["id"])
            monkeypatch.setattr(cron_cli, "_warn_if_gateway_not_running", lambda: None)
            cron_cli.cron_list()

        out = capsys.readouterr().out
        assert "Retry" in out
        assert execution_id in out

    def test_doctor_reports_a_pending_replay_of_a_lost_occurrence(
        self, monkeypatch, tmp_path
    ):
        _point_ledger(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="doc")
            _interrupt_and_reconcile(job["id"])
            issues = cron_cli._cron_doctor_issues_for_job(cron_jobs.get_job(job["id"]))

        assert any("interrupted by a shutdown" in issue for issue in issues)

    def test_doctor_is_quiet_for_a_healthy_job(self, monkeypatch, tmp_path):
        _point_ledger(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="healthy")
            issues = cron_cli._cron_doctor_issues_for_job(cron_jobs.get_job(job["id"]))

        assert not any("interrupted" in issue for issue in issues)


class TestIncidentsListsInterruptions:
    def test_interruption_incident_is_listed_with_its_type(self, monkeypatch, tmp_path, capsys):
        import cron.incidents as incidents

        _point_ledger(monkeypatch, tmp_path)
        incidents.upsert_incident(
            "job-x", "Interrupted by shutdown before terminal completion.")

        cron_cli.cron_incidents(argparse.Namespace(incident_action="list", state=None))
        out = capsys.readouterr().out

        assert "interruption" in out
        assert "job-x" in out
