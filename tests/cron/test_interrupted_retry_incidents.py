"""Every interruption raises an explicit ``interruption`` incident, from every write path.

Round-1 review found the hole these pin: dead-owner startup recovery terminalized the row but
raised no incident, so the exact shutdown failures this work exists for stayed invisible to
``hermes cron incidents``. The contract is now that the *ledger flag* is what raises the incident,
so every path that produces an interrupted row — scheduler drain, fire-ownership loss, dead-owner
recovery, and rows already on disk before the flag existed — reaches the incident store exactly
once, with the type passed explicitly rather than guessed from the error text.
"""

from __future__ import annotations

import cron.executions as executions
import cron.incidents as incidents
import cron.interrupted_retry as retry
import cron.jobs as cron_jobs
import cron.scheduler as scheduler


def _point_stores(monkeypatch, tmp_path):
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", tmp_path / "cron" / "incidents.db")


class TestRecoveryRaisesTheIncident:
    def test_startup_recovery_delivers_and_alerts_the_incident(
        self, monkeypatch, tmp_path, make_cron_provider
    ):
        _point_stores(monkeypatch, tmp_path)
        delivered = []

        def _deliver(job, content, *, adapters, loop, for_failure):
            delivered.append((job["id"], content, adapters, loop, for_failure))
            return None

        monkeypatch.setattr(scheduler, "_deliver_result", _deliver)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(
                prompt="check", schedule="every monday 9am", name="startup-alert",
                deliver="bot-chat",
            )
            record = executions.create_execution(job["id"], source="builtin")
            executions.mark_execution_running(record["id"])
            monkeypatch.setattr(executions, "_PROCESS_ID", "replacement-gateway")
            monkeypatch.setattr(executions, "_owner_is_live", lambda *_: False)

            assert make_cron_provider().recover_interrupted(adapters="adapter", loop="loop") == 1

        raised = incidents.list_incidents()
        assert len(delivered) == 1
        assert delivered[0][2:] == ("adapter", "loop", True)
        assert raised[0]["state"] == "alerted"
        assert raised[0]["failure_type"] == "interruption"

    def test_unresolved_origin_does_not_claim_the_operator_was_alerted(
        self, monkeypatch, tmp_path
    ):
        """Normal incident lifecycle means ``alerted`` only after a notice reaches a target."""
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(
                prompt="check", schedule="every monday 9am", name="no-origin",
                deliver="origin",
            )
            record = executions.create_execution(job["id"], source="builtin")
            executions.finish_execution(
                record["id"], success=False,
                error="Interrupted by shutdown before terminal completion.", interrupted=True,
            )
            assert retry.reconcile_interrupted_executions() == 1

        raised = incidents.list_incidents()
        assert len(raised) == 1
        assert raised[0]["state"] == "detected"

    def test_incident_store_failure_is_retried_before_the_decision_is_final(
        self, monkeypatch, tmp_path
    ):
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="store-retry")
            record = executions.create_execution(job["id"], source="builtin")
            executions.finish_execution(
                record["id"], success=False,
                error="Interrupted by shutdown before terminal completion.", interrupted=True,
            )
            real_upsert = incidents.upsert_incident
            monkeypatch.setattr(
                incidents, "upsert_incident",
                lambda *_a, **_k: (_ for _ in ()).throw(OSError("store failed")),
            )

            assert retry.reconcile_interrupted_executions() == 0
            assert executions.get_execution(record["id"])["retry_state"] is None

            monkeypatch.setattr(incidents, "upsert_incident", real_upsert)
            assert retry.reconcile_interrupted_executions() == 1

        assert executions.get_execution(record["id"])["retry_state"] == "scheduled"
        assert incidents.list_incidents()[0]["failure_type"] == "interruption"

    def test_startup_recovery_of_a_dead_owner_raises_an_interruption_incident(
        self, monkeypatch, tmp_path, make_cron_provider
    ):
        """The gateway was killed mid-run. The next startup must not merely terminalize the row:
        the lost occurrence has to become a visible, typed incident."""
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(
                prompt="", schedule="every monday 9am", name="worktree gc",
                script="echo gc", no_agent=True,
            )
            record = executions.create_execution(job["id"], source="builtin")
            executions.mark_execution_running(record["id"])

            monkeypatch.setattr(executions, "_PROCESS_ID", "replacement-gateway")
            monkeypatch.setattr(executions, "_owner_is_live", lambda _pid, _started: False)

            assert make_cron_provider().recover_interrupted() == 1

        raised = incidents.list_incidents()
        assert len(raised) == 1, "a recovered interruption must be visible in `cron incidents`"
        assert raised[0]["failure_type"] == "interruption"
        assert raised[0]["job_id"] == job["id"]
        assert raised[0]["state"] == "detected"

    def test_the_type_is_passed_explicitly_not_classified_from_the_error_text(
        self, monkeypatch, tmp_path
    ):
        """Classification is a fallback for rows nobody typed. An interruption knows what it is,
        so the type must survive an error string that reads like some other failure class."""
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="misleading")
            record = executions.create_execution(job["id"], source="builtin")
            executions.finish_execution(
                record["id"], success=False, error="timed out waiting for 429", interrupted=True)

            retry.reconcile_interrupted_executions()

        assert incidents.list_incidents()[0]["failure_type"] == "interruption"

    def test_an_ordinary_failure_with_the_same_shape_is_not_an_interruption(
        self, monkeypatch, tmp_path
    ):
        """The flag, not the text, decides. A plain failure keeps its own classification."""
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="plain")
            record = executions.create_execution(job["id"], source="builtin")
            executions.finish_execution(
                record["id"], success=False, error="timed out waiting for 429")

            assert retry.reconcile_interrupted_executions() == 0

        assert incidents.list_incidents() == []

    def test_repeated_interruptions_dedupe_onto_one_incident(self, monkeypatch, tmp_path):
        """Normal lifecycle: a restart storm is one problem, not five."""
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="storm")
            for _ in range(4):
                record = executions.create_execution(job["id"], source="builtin")
                executions.finish_execution(
                    record["id"], success=False,
                    error="Interrupted by shutdown before terminal completion.", interrupted=True)
                retry.reconcile_interrupted_executions()

        raised = incidents.list_incidents()
        assert len(raised) == 1
        assert raised[0]["failure_type"] == "interruption"
        assert raised[0]["last_seen_at"] >= raised[0]["first_seen_at"]

    def test_an_acknowledged_incident_stays_closed(self, monkeypatch, tmp_path):
        """Ack suppression is the ordinary incident contract and interruptions do not bypass it."""
        _point_stores(monkeypatch, tmp_path)
        delivered = []
        monkeypatch.setattr(
            scheduler, "_deliver_result",
            lambda *_a, **_k: delivered.append("sent"),
        )
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(
                prompt="check", schedule="every 15m", name="acked", deliver="bot-chat")

            def _interrupt():
                record = executions.create_execution(job["id"], source="builtin")
                executions.finish_execution(
                    record["id"], success=False,
                    error="Interrupted by shutdown before terminal completion.", interrupted=True)
                retry.reconcile_interrupted_executions()

            _interrupt()
            incident_id = incidents.list_incidents()[0]["id"]
            assert incidents.get_incident(incident_id)["state"] == "alerted"
            assert delivered == ["sent"]
            assert incidents.ack_incident(incident_id) is True

            _interrupt()

        assert delivered == ["sent"], "a closed incident must suppress the repeated notice"
        assert incidents.get_incident(incident_id)["state"] == "closed"

    def test_the_incident_is_raised_once_per_occurrence_not_once_per_sweep(
        self, monkeypatch, tmp_path
    ):
        """Reconciliation runs on every reap cycle; a decided occurrence must not keep re-touching
        its incident forever (which would keep resurfacing a resolved problem as fresh)."""
        _point_stores(monkeypatch, tmp_path)
        with cron_jobs.use_cron_store(tmp_path):
            job = cron_jobs.create_job(prompt="check", schedule="every 15m", name="once")
            record = executions.create_execution(job["id"], source="builtin")
            executions.finish_execution(
                record["id"], success=False,
                error="Interrupted by shutdown before terminal completion.", interrupted=True)

            retry.reconcile_interrupted_executions()
            after_first = incidents.list_incidents()[0]["last_seen_at"]

            for _ in range(3):
                retry.reconcile_interrupted_executions()

        assert incidents.list_incidents()[0]["last_seen_at"] == after_first


class TestPreExistingShutdownRowsAreMigrated:
    def test_shutdown_failures_recorded_before_the_flag_existed_are_backfilled(
        self, monkeypatch, tmp_path
    ):
        """The rows that motivated this card were written by the old code, which had no
        ``interrupted`` column. Adding the column must adopt them, or the reported failures stay
        invisible to both the incident store and the reconciler forever."""
        _point_stores(monkeypatch, tmp_path)
        db = tmp_path / "cron" / "executions.db"
        db.parent.mkdir(parents=True, exist_ok=True)

        import sqlite3

        legacy = sqlite3.connect(db)
        legacy.execute(
            """CREATE TABLE executions (
                   id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
                   process_id TEXT NOT NULL, pid INTEGER NOT NULL, process_started_at INTEGER,
                   status TEXT NOT NULL, claimed_at TEXT NOT NULL, started_at TEXT,
                   finished_at TEXT, error TEXT)"""
        )
        rows = [
            ("old-shutdown", "job-gc", "failed",
             "Interrupted by shutdown before terminal completion."),
            ("old-drain", "job-watch", "failed",
             "Interrupted by gateway shutdown before terminal completion."),
            ("old-abandoned", "job-health", "unknown",
             executions.RECOVERED_INTERRUPTION_ERROR),
            ("old-ordinary", "job-other", "failed", "the script exited 1"),
            ("old-success", "job-other", "completed", None),
        ]
        for exec_id, job_id, status, error in rows:
            legacy.execute(
                "INSERT INTO executions (id, job_id, source, process_id, pid, status, "
                "claimed_at, finished_at, error) VALUES (?,?,'builtin','old',1,?,?,?,?)",
                (exec_id, job_id, status, "2026-09-07T20:11:59+00:00",
                 "2026-09-07T20:12:30+00:00", error),
            )
        legacy.commit()
        legacy.close()

        adopted = {
            row["id"]: row["interrupted"] for row in executions.list_executions(limit=50)
        }
        assert adopted["old-shutdown"] == 1
        assert adopted["old-drain"] == 1
        assert adopted["old-abandoned"] == 1
        assert adopted["old-ordinary"] == 0, "an ordinary failure is not an interruption"
        assert adopted["old-success"] == 0

    def test_backfilled_rows_reach_the_reconciler_and_the_incident_store(
        self, monkeypatch, tmp_path
    ):
        """Adoption is only useful if it feeds the two surfaces the card asks for. These rows are
        long stale, so the correct outcome is a recorded decline plus a visible incident — never a
        replay of week-old work."""
        _point_stores(monkeypatch, tmp_path)
        db = tmp_path / "cron" / "executions.db"
        db.parent.mkdir(parents=True, exist_ok=True)

        import sqlite3

        legacy = sqlite3.connect(db)
        legacy.execute(
            """CREATE TABLE executions (
                   id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
                   process_id TEXT NOT NULL, pid INTEGER NOT NULL, process_started_at INTEGER,
                   status TEXT NOT NULL, claimed_at TEXT NOT NULL, started_at TEXT,
                   finished_at TEXT, error TEXT)"""
        )
        legacy.execute(
            "INSERT INTO executions (id, job_id, source, process_id, pid, status, "
            "claimed_at, finished_at, error) VALUES "
            "('old-shutdown','job-gc','builtin','old',1,'failed',"
            "'2026-09-07T20:11:59+00:00','2026-09-07T20:12:30+00:00',"
            "'Interrupted by shutdown before terminal completion.')"
        )
        legacy.commit()
        legacy.close()

        with cron_jobs.use_cron_store(tmp_path):
            assert retry.reconcile_interrupted_executions() == 0

        assert executions.get_execution("old-shutdown")["retry_state"] == "declined:stale"
        raised = incidents.list_incidents()
        assert len(raised) == 1
        assert raised[0]["failure_type"] == "interruption"
