"""Durable lineage of a replay: which attempt it recovers, and what became of it.

Round-1 review found the replay undiagnosable after the fact. The only link between a lost
occurrence and the run replaying it lived in the job's transient ``interrupted_retry`` stamp, so a
successful replay erased the evidence and history could not tell the retry apart from an ordinary
run. Worse, after a FAILED replay the stamp is deliberately kept (that is what bounds the loop)
while ``cron list``/``doctor`` still described the retry as pending — telling the operator work was
queued that had already run and failed.

These pin the lineage onto the ledger, where it survives both outcomes, and pin the surfaces to
tell the truth before, during, and after each one.
"""

from __future__ import annotations

import cron.executions as executions
import cron.interrupted_retry as retry
import cron.jobs as cron_jobs
import hermes_cli.cron as cron_cli


def _point_stores(monkeypatch, tmp_path):
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    import cron.incidents as incidents

    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", tmp_path / "cron" / "incidents.db")


def _lose_an_occurrence(job_id: str) -> str:
    """Interrupt one attempt for ``job_id`` and reconcile it into an armed retry."""
    record = executions.create_execution(job_id, source="builtin")
    executions.finish_execution(
        record["id"], success=False,
        error="Interrupted by shutdown before terminal completion.", interrupted=True)
    retry.reconcile_interrupted_executions()
    return record["id"]


class TestTheReplayRecordsWhatItRecovers:
    def test_the_retry_execution_names_the_attempt_it_replaces(self, monkeypatch, tmp_path):
        """The replay's own ledger row must carry the original attempt id, so the lineage
        survives the stamp being cleared."""
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="lineage")
            original = _lose_an_occurrence(job["id"])

            replay = executions.create_execution(job["id"], source="builtin")

        assert executions.get_execution(replay["id"])["retry_of"] == original

    def test_an_ordinary_run_records_no_lineage(self, monkeypatch, tmp_path):
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="ordinary")
            plain = executions.create_execution(job["id"], source="builtin")

        assert executions.get_execution(plain["id"])["retry_of"] is None

    def test_lineage_survives_the_successful_run_that_clears_the_stamp(
        self, monkeypatch, tmp_path
    ):
        """The stamp is transient by design; the ledger link is what makes the replay auditable
        afterwards."""
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="survives")
            original = _lose_an_occurrence(job["id"])
            replay = executions.create_execution(job["id"], source="builtin")

            executions.finish_execution(replay["id"], success=True)
            cron_jobs.mark_job_run(job["id"], True)

            assert cron_jobs.get_job(job["id"]).get("interrupted_retry") is None

        assert executions.get_execution(replay["id"])["retry_of"] == original

    def test_only_the_first_run_after_arming_claims_the_lineage(self, monkeypatch, tmp_path):
        """One lost occurrence produces one replay. A later run of the same job is an ordinary
        occurrence and must not also claim to be recovering it."""
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="onlyfirst")
            original = _lose_an_occurrence(job["id"])

            replay = executions.create_execution(job["id"], source="builtin")
            executions.finish_execution(replay["id"], success=True)
            cron_jobs.mark_job_run(job["id"], True)
            later = executions.create_execution(job["id"], source="builtin")

        assert executions.get_execution(replay["id"])["retry_of"] == original
        assert executions.get_execution(later["id"])["retry_of"] is None


class TestSurfacesAreTruthfulAfterTheReplayRuns:
    def test_list_and_doctor_stop_claiming_a_replay_is_pending_once_it_has_failed(
        self, monkeypatch, tmp_path, capsys
    ):
        """The round-1 blocker: a failed replay keeps the stamp (bounding the loop) but is no
        longer queued. Saying 'queued for one retry' at that point is simply false."""
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="failedretry")
            _lose_an_occurrence(job["id"])
            replay = executions.create_execution(job["id"], source="builtin")
            executions.finish_execution(replay["id"], success=False, error="still broken")
            cron_jobs.mark_job_run(job["id"], False, "still broken")

            refreshed = cron_jobs.get_job(job["id"])
            assert refreshed["interrupted_retry"], "the loop bound must still be held"

            issues = cron_cli._cron_doctor_issues_for_job(refreshed)
            monkeypatch.setattr(cron_cli, "_warn_if_gateway_not_running", lambda: None)
            cron_cli.cron_list()

        out = capsys.readouterr().out
        assert not any("queued for one retry" in issue for issue in issues), (
            f"a spent replay must not be reported as queued: {issues}")
        assert any("replay of the interrupted occurrence has already run" in issue
                   for issue in issues)
        assert any("no further retry is queued" in issue for issue in issues)
        assert "replaying occurrence" not in out

    def test_doctor_still_reports_a_replay_that_has_not_run_yet(self, monkeypatch, tmp_path):
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="pending")
            _lose_an_occurrence(job["id"])
            issues = cron_cli._cron_doctor_issues_for_job(cron_jobs.get_job(job["id"]))

        assert any("queued for one retry" in issue for issue in issues)

    def test_a_successful_replay_leaves_no_stale_advisory(self, monkeypatch, tmp_path):
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="recovered")
            _lose_an_occurrence(job["id"])
            replay = executions.create_execution(job["id"], source="builtin")
            executions.finish_execution(replay["id"], success=True)
            cron_jobs.mark_job_run(job["id"], True)

            issues = cron_cli._cron_doctor_issues_for_job(cron_jobs.get_job(job["id"]))

        assert not any("interrupted" in issue for issue in issues)

    def test_history_marks_the_replay_row_with_what_it_recovers(
        self, monkeypatch, tmp_path, capsys
    ):
        """``cron history`` must let an operator walk from the replay back to the lost attempt."""
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="histlineage")
            original = _lose_an_occurrence(job["id"])
            replay = executions.create_execution(job["id"], source="builtin")
            executions.finish_execution(replay["id"], success=True)

        cron_cli.cron_runs(job_id=job["id"])
        out = capsys.readouterr().out

        assert f"replay of {original}" in out
