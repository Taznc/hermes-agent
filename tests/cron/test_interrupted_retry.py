"""Shutdown-interruption reconciliation: durable classification, incident, bounded retry.

An execution abandoned by a gateway/desktop shutdown used to reach ``unknown`` (or a ``failed``
row whose text nobody classified) and the occurrence was silently lost. These tests pin the
contract: the ledger records interruption as a FACT (not a substring), the failure raises a
deduplicated ``interruption`` incident, and an eligible occurrence is replayed at most once.
"""

from __future__ import annotations

import cron.executions as executions
import cron.incidents as incidents


def _point_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return executions


class TestInterruptionIsADurableLedgerFact:
    def test_shutdown_interruption_is_recorded_as_a_flag_not_error_text(
        self, monkeypatch, tmp_path
    ):
        """The scheduler's interrupted terminal write stamps ``interrupted``; an ordinary
        failure with the same shape does not."""
        ledger = _point_ledger(monkeypatch, tmp_path)

        interrupted = ledger.create_execution("job-i", source="builtin")
        ledger.mark_execution_running(interrupted["id"])
        record = ledger.finish_execution(
            interrupted["id"], success=False, error="Interrupted by shutdown", interrupted=True
        )
        assert record["interrupted"] == 1
        assert record["retry_state"] is None

        ordinary = ledger.create_execution("job-o", source="builtin")
        ledger.mark_execution_running(ordinary["id"])
        plain = ledger.finish_execution(ordinary["id"], success=False, error="boom")
        assert plain["interrupted"] == 0

    def test_restart_recovery_marks_abandoned_rows_interrupted(self, monkeypatch, tmp_path):
        """A row whose owner process is provably gone is reconciled to a terminal ``unknown``
        that is also flagged as an interruption — no permanently running row survives."""
        ledger = _point_ledger(monkeypatch, tmp_path)
        record = ledger.create_execution("weekly-job", source="builtin")
        ledger.mark_execution_running(record["id"])

        monkeypatch.setattr(ledger, "_PROCESS_ID", "replacement-gateway")
        monkeypatch.setattr(ledger, "_owner_is_live", lambda _pid, _started: False)

        assert ledger.recover_interrupted_executions() == 1
        recovered = ledger.get_execution(record["id"])
        assert recovered["status"] == "unknown"
        assert recovered["interrupted"] == 1
        assert recovered["finished_at"]


class TestRetryDecisionIsClaimedExactlyOnce:
    def test_only_one_claimer_wins_the_retry_decision(self, monkeypatch, tmp_path):
        """Two concurrent reconcilers must not both schedule a replay of one occurrence."""
        ledger = _point_ledger(monkeypatch, tmp_path)
        record = ledger.create_execution("job-a", source="builtin")
        ledger.finish_execution(record["id"], success=False, error="x", interrupted=True)

        assert ledger.claim_retry_decision(record["id"], "scheduled") is True
        assert ledger.claim_retry_decision(record["id"], "scheduled") is False
        assert ledger.get_execution(record["id"])["retry_state"] == "scheduled"

    def test_a_non_interrupted_failure_is_never_claimable(self, monkeypatch, tmp_path):
        ledger = _point_ledger(monkeypatch, tmp_path)
        record = ledger.create_execution("job-a", source="builtin")
        ledger.finish_execution(record["id"], success=False, error="boom")

        assert ledger.claim_retry_decision(record["id"], "scheduled") is False

    def test_pending_lists_only_undecided_interruptions_oldest_first(
        self, monkeypatch, tmp_path
    ):
        ledger = _point_ledger(monkeypatch, tmp_path)
        first = ledger.create_execution("job-a", source="builtin")
        ledger.finish_execution(first["id"], success=False, error="x", interrupted=True)
        second = ledger.create_execution("job-b", source="builtin")
        ledger.finish_execution(second["id"], success=False, error="x", interrupted=True)
        decided = ledger.create_execution("job-c", source="builtin")
        ledger.finish_execution(decided["id"], success=False, error="x", interrupted=True)
        ledger.claim_retry_decision(decided["id"], "declined:stale")
        plain = ledger.create_execution("job-d", source="builtin")
        ledger.finish_execution(plain["id"], success=False, error="boom")

        pending = ledger.list_undecided_interruptions()

        assert [row["id"] for row in pending] == [first["id"], second["id"]]


class TestInterruptionFailureType:
    def test_shutdown_interruption_classifies_as_interruption(self, monkeypatch, tmp_path):
        """Shutdown text must not fall through to ``unknown`` (nor be eaten by 'timeout'/'agent')."""
        monkeypatch.setattr(incidents, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")

        for text in (
            "Interrupted by shutdown before terminal completion.",
            "Interrupted by gateway shutdown before terminal completion.",
            executions.RECOVERED_INTERRUPTION_ERROR,
        ):
            assert incidents._classify_failure_type(text) == "interruption"

    def test_ordinary_failures_keep_their_existing_classification(self, monkeypatch, tmp_path):
        monkeypatch.setattr(incidents, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")

        assert incidents._classify_failure_type("Request timed out") == "timeout"
        assert incidents._classify_failure_type("HTTP 429 rate limit") == "rate_limit"
        assert incidents._classify_failure_type("provider inference error") == "agent"


class TestSchedulerRaisesInterruptionIncidents:
    """The three interruption write paths must flag the ledger AND surface an incident.

    Before this, an interrupted occurrence wrote a terminal ledger row nobody classified and
    NOTHING in ``hermes cron incidents`` — the silent-loss the card is about.
    """

    def _point(self, monkeypatch, tmp_path):
        monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")

    def test_shutdown_drain_records_an_interruption_incident(self, monkeypatch, tmp_path):
        import cron.scheduler as sched

        self._point(monkeypatch, tmp_path)
        job = {"id": "drained-job", "name": "drained"}
        record = executions.create_execution(job["id"], source="builtin")

        sched._finish_interrupted_run(job, record["id"], None)

        stored = executions.get_execution(record["id"])
        assert stored["interrupted"] == 1
        raised = incidents.list_incidents()
        assert [inc["failure_type"] for inc in raised] == ["interruption"]
        assert raised[0]["job_id"] == "drained-job"
        assert raised[0]["state"] == "detected"

    def test_ownership_loss_under_a_held_claim_is_an_interruption(self, monkeypatch, tmp_path):
        import cron.scheduler as sched

        self._point(monkeypatch, tmp_path)
        record = executions.create_execution("owned-job", source="builtin")
        monkeypatch.setattr(sched, "heartbeat_fire_claim", lambda *_a, **_k: True)
        monkeypatch.setattr(sched, "mark_job_run", lambda *_a, **_k: True)

        sched._record_fire_ownership_lost("owned-job", "owner-1", record["id"])

        assert executions.get_execution(record["id"])["interrupted"] == 1
        assert [inc["failure_type"] for inc in incidents.list_incidents()] == ["interruption"]

    def test_intentional_cancellation_is_not_an_interruption(self, monkeypatch, tmp_path):
        """A discarded stale result (replacement owner / transport cancel) is deliberate: it must
        neither be flagged interrupted nor raise an incident, so it can never be replayed."""
        import cron.scheduler as sched

        self._point(monkeypatch, tmp_path)
        record = executions.create_execution("cancelled-job", source="builtin")
        monkeypatch.setattr(sched, "heartbeat_fire_claim", lambda *_a, **_k: False)

        sched._record_fire_ownership_lost("cancelled-job", "stale-owner", record["id"])

        assert executions.get_execution(record["id"])["interrupted"] == 0
        assert incidents.list_incidents() == []

    def test_interruption_incidents_dedupe_across_repeated_shutdowns(
        self, monkeypatch, tmp_path
    ):
        import cron.scheduler as sched

        self._point(monkeypatch, tmp_path)
        job = {"id": "weekly-job", "name": "weekly"}
        for _ in range(3):
            record = executions.create_execution(job["id"], source="builtin")
            sched._finish_interrupted_run(job, record["id"], None)

        raised = incidents.list_incidents()
        assert len(raised) == 1
        assert raised[0]["failure_type"] == "interruption"

