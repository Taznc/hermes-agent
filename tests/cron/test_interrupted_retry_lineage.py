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

import contextvars
import threading
from contextlib import contextmanager

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


def _claim_replay(job_id: str) -> dict:
    from cron.scheduler_provider import InProcessCronScheduler

    claimed = InProcessCronScheduler().claim_fire(job_id)
    assert isinstance(claimed, dict)
    replay = executions.get_execution(claimed["execution_id"])
    assert isinstance(replay, dict)
    return replay


class TestTheReplayRecordsWhatItRecovers:
    def test_only_the_provider_contender_that_wins_fire_ownership_gets_lineage(
        self, monkeypatch, tmp_path
    ):
        from cron.scheduler_provider import InProcessCronScheduler

        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="contenders")
            original = _lose_an_occurrence(job["id"])
            provider = InProcessCronScheduler()
            real_create = executions.create_execution
            first_created = threading.Event()
            release_first = threading.Event()
            result = {}

            def _gated_create(*args, **kwargs):
                created = real_create(*args, **kwargs)
                if threading.current_thread().name == "first-contender":
                    result["first_execution"] = created
                    first_created.set()
                    assert release_first.wait(5)
                return created

            monkeypatch.setattr(executions, "create_execution", _gated_create)

            def _claim_first():
                result["first_claim"] = provider.claim_fire(job["id"])

            ctx = contextvars.copy_context()
            thread = threading.Thread(
                target=lambda: ctx.run(_claim_first), name="first-contender")
            thread.start()
            assert first_created.wait(5)
            result["winner"] = provider.claim_fire(job["id"])
            release_first.set()
            thread.join(5)

            assert result["first_claim"] is None
            losing = executions.get_execution(result["first_execution"]["id"])
            winning = executions.get_execution(result["winner"]["execution_id"])

        assert losing["retry_of"] is None
        assert winning["retry_of"] == original

    def test_execution_insert_failure_cannot_consume_retry_lineage(
        self, monkeypatch, tmp_path
    ):
        from cron.scheduler_provider import InProcessCronScheduler

        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="insert-failure")
            original = _lose_an_occurrence(job["id"])
            real_transaction = executions._transaction

            @contextmanager
            def _broken_transaction():
                raise OSError("insert failed")
                yield

            monkeypatch.setattr(executions, "_transaction", _broken_transaction)
            try:
                InProcessCronScheduler().claim_fire(job["id"])
            except OSError:
                pass
            else:
                raise AssertionError("the injected ledger insert failure must propagate")
            monkeypatch.setattr(executions, "_transaction", real_transaction)

            stamp = cron_jobs.get_job(job["id"])["interrupted_retry"]
            assert stamp.get("replayed_by") is None

            claimed = InProcessCronScheduler().claim_fire(job["id"])
            actual = executions.get_execution(claimed["execution_id"])

        assert actual["retry_of"] == original

    def test_lineage_bind_failure_releases_the_replay_for_the_next_contender(
        self, monkeypatch, tmp_path
    ):
        """A failure after the job claim but before the ledger link must be compensatable: no
        nonexistent/lineage-free winner may consume the only bounded replay."""
        from cron.scheduler_provider import InProcessCronScheduler

        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="bind-failure")
            original = _lose_an_occurrence(job["id"])
            real_bind = executions.bind_interrupted_retry_lineage
            calls = {"count": 0}

            def _fail_once(*args, **kwargs):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise OSError("ledger update failed")
                return real_bind(*args, **kwargs)

            monkeypatch.setattr(executions, "bind_interrupted_retry_lineage", _fail_once)
            try:
                InProcessCronScheduler().claim_fire(job["id"])
            except OSError:
                pass
            else:
                raise AssertionError("the injected lineage update failure must propagate")

            stamp = cron_jobs.get_job(job["id"])["interrupted_retry"]
            assert stamp.get("replayed_by") is None
            assert cron_jobs.get_job(job["id"]).get("fire_claim") is None

            claimed = InProcessCronScheduler().claim_fire(job["id"])
            assert isinstance(claimed, dict)
            actual = executions.get_execution(claimed["execution_id"])

        assert actual["retry_of"] == original

    def test_the_retry_execution_names_the_attempt_it_replaces(self, monkeypatch, tmp_path):
        """The replay's own ledger row must carry the original attempt id, so the lineage
        survives the stamp being cleared."""
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="lineage")
            original = _lose_an_occurrence(job["id"])

            replay = _claim_replay(job["id"])

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
            replay = _claim_replay(job["id"])

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

            replay = _claim_replay(job["id"])
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
            replay = _claim_replay(job["id"])
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
            replay = _claim_replay(job["id"])
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
            replay = _claim_replay(job["id"])
            executions.finish_execution(replay["id"], success=True)

        cron_cli.cron_runs(job_id=job["id"])
        out = capsys.readouterr().out

        assert f"replay of {original}" in out
