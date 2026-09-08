"""Bounded, at-most-once replay of a cron occurrence lost to a shutdown.

A gateway/desktop shutdown can kill an execution between its claim and its terminal write. The
ledger reconciles that row deterministically (``cron.executions``) and the incident store surfaces
it (``cron.incidents``); this module answers the remaining question — *may that lost occurrence be
run again?* — and answers it once per occurrence.

The policy is deliberately conservative, because a cron side effect may already have happened
before the interruption: correctness and non-duplication beat aggressive replay.

* **At most once per occurrence.** ``claim_retry_decision`` is a compare-and-swap on the ledger
  row, so concurrent reconcilers cannot both schedule one occurrence.
* **At most one outstanding retry per job.** While the job carries an ``interrupted_retry`` stamp,
  further interruptions are recorded and declined. The stamp is cleared only by a *successful*
  run, so repeated gateway restarts cannot build a retry loop.
* **Bounded freshness.** Only an occurrence claimed within ``cron.interrupted_retry_max_age_minutes``
  is replayed, so a weekly job's lost occurrence is recoverable across a restart while week-old
  work is never resurrected. ``0`` disables replay entirely.
* **Never resurrects.** Disabled, paused, completed and removed jobs are declined; a job with a
  live attempt is declined rather than run concurrently.

Every decision is recorded on the ledger row (``retry_state``), so a declined occurrence is
diagnosable rather than silently dropped.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from hermes_time import now as _hermes_now

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_MINUTES = 60.0
# The explicit incident type for a shutdown interruption. Passed to ``upsert_incident`` rather
# than left to text classification, so an interruption is never reported as the failure class its
# half-written error text happens to mention.
INTERRUPTION_FAILURE_TYPE = "interruption"
# Ledger ``retry_state`` values. "scheduled" is the only one that replays; the rest are recorded
# reasons a lost occurrence was deliberately not replayed.
RETRY_SCHEDULED = "scheduled"
DECLINE_DISABLED_BY_CONFIG = "declined:disabled_by_config"
DECLINE_STALE = "declined:stale"
DECLINE_JOB_MISSING = "declined:job_missing"
DECLINE_DISABLED = "declined:disabled"
DECLINE_IN_FLIGHT = "declined:in_flight"
DECLINE_RETRY_OUTSTANDING = "declined:retry_outstanding"


def _max_age_minutes() -> float:
    """Freshness budget for a replay, in minutes (``0`` disables replay)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        cron_cfg = (cfg.get("cron") or {}) if isinstance(cfg, dict) else {}
        return max(0.0, float(cron_cfg.get(
            "interrupted_retry_max_age_minutes", DEFAULT_MAX_AGE_MINUTES)))
    except Exception:
        return DEFAULT_MAX_AGE_MINUTES


def _parse_ts(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_fresh(record: Dict[str, Any], max_age_minutes: float) -> bool:
    claimed_at = _parse_ts(record.get("claimed_at"))
    if claimed_at is None:
        return False  # an unparseable timestamp cannot be proved fresh
    age_seconds = (_hermes_now() - claimed_at).total_seconds()
    return -60.0 <= age_seconds <= max_age_minutes * 60.0


def _job_has_live_attempt(job_id: str) -> bool:
    """True while any attempt for this job is still claimed/running."""
    from cron.executions import list_executions

    return any(
        row.get("status") in ("claimed", "running")
        for row in list_executions(job_id=job_id, limit=50)
    )


def _raise_interruption_incident(record: Dict[str, Any]) -> None:
    """Surface one interrupted occurrence in ``hermes cron incidents``.

    Raised here, from the ledger flag, rather than at each interruption write site: this is the
    one place every interrupted row passes through exactly once (the ``retry_state`` CAS gates
    it), so dead-owner recovery, the shutdown drain, fire-ownership loss and rows backfilled by
    the schema migration all reach the incident store on equal terms — and none of them raises
    twice when the reconciler runs again.

    ``failure_type`` is passed explicitly: an interruption is a fact about the process, and the
    error text of a run killed mid-flight may name any other failure class.
    """
    from cron.executions import RECOVERED_INTERRUPTION_ERROR
    from cron.incidents import upsert_incident
    from cron.jobs import get_job

    job_id = str(record.get("job_id") or "")
    job = get_job(job_id) or {}
    upsert_incident(
        job_id, str(record.get("error") or RECOVERED_INTERRUPTION_ERROR),
        job_name=job.get("name"), failure_type=INTERRUPTION_FAILURE_TYPE,
    )


def _decide(record: Dict[str, Any], max_age_minutes: float) -> tuple[str, Optional[Dict[str, Any]]]:
    """Classify one undecided interruption. Returns ``(decision, job)``; ``job`` is set only when
    the decision is to replay."""
    from cron.jobs import get_job, is_job_runnable

    if not _is_fresh(record, max_age_minutes):
        return DECLINE_STALE, None
    job_id = str(record.get("job_id") or "")
    job = get_job(job_id)
    if not job:
        return DECLINE_JOB_MISSING, None
    # Checked BEFORE any re-arm: trigger_job would re-enable a paused job as a side effect, so a
    # disabled/terminal job must be rejected here rather than resurrected.
    if not is_job_runnable(job) or job.get("state") in ("completed", "error"):
        return DECLINE_DISABLED, None
    if job.get("interrupted_retry"):
        return DECLINE_RETRY_OUTSTANDING, None
    if _job_has_live_attempt(job_id):
        return DECLINE_IN_FLIGHT, None
    return RETRY_SCHEDULED, job


def _arm_retry(record: Dict[str, Any]) -> str:
    """Re-arm the lost occurrence to fire on the next tick, stamped for diagnosis.

    Returns the ``arm_interrupted_retry`` outcome. Eligibility is re-checked inside the job-store
    lock there, so a pause/removal/rival stamp that lands after ``_decide`` read the job wins and
    comes back as a decline rather than being undone by this arm.
    """
    from cron.jobs import arm_interrupted_retry

    return arm_interrupted_retry(str(record.get("job_id") or ""), {
        "execution_id": record.get("id"),
        "interrupted_at": record.get("finished_at") or record.get("claimed_at"),
        "scheduled_at": _hermes_now().isoformat(),
    })


# How an arm outcome that is not a successful arm maps onto a recorded ledger decision.
_ARM_DECLINE = {
    "missing": DECLINE_JOB_MISSING,
    "disabled": DECLINE_DISABLED,
    "outstanding": DECLINE_RETRY_OUTSTANDING,
}


def reconcile_interrupted_executions(limit: int = 50) -> int:
    """Decide every undecided interruption; return how many occurrences were re-armed.

    Safe to call on every scheduler startup and periodically: each occurrence is decided exactly
    once, and a decision is durable across restarts.

    **Ordering matters.** The retry is armed BEFORE its decision is committed to the ledger, and
    arming is idempotent for the same occurrence. A crash between the two therefore leaves the
    occurrence still undecided — the next sweep sees it, gets ``already_armed`` back, and records
    the decision it owed. The reverse order (the round-1 implementation) could commit
    ``scheduled`` and then die, stranding the occurrence as decided-but-never-run, which is the
    silent loss this whole feature exists to prevent.
    """
    from cron.executions import claim_retry_decision, list_undecided_interruptions

    max_age_minutes = _max_age_minutes()
    scheduled = 0
    for record in list_undecided_interruptions(limit=limit):
        try:
            if max_age_minutes <= 0:
                decision, job = DECLINE_DISABLED_BY_CONFIG, None
            else:
                decision, job = _decide(record, max_age_minutes)
            armed = False
            if decision == RETRY_SCHEDULED and job is not None:
                outcome = _arm_retry(record)
                armed = outcome in ("armed", "already_armed")
                if not armed:
                    decision = _ARM_DECLINE.get(outcome, DECLINE_DISABLED)
            # The CAS is the at-most-once gate: losing it means another reconciler already owns
            # this occurrence's decision, so this pass records nothing further about it.
            if not claim_retry_decision(record["id"], decision):
                continue
            # Winning the CAS makes this pass the sole owner of the occurrence, so the incident is
            # raised here — once, whatever the replay decision turned out to be. A declined
            # occurrence is still a lost one and must be visible.
            _raise_interruption_incident(record)
            if not armed:
                logger.info(
                    "Cron occurrence %s for job %s was interrupted and not replayed (%s)",
                    record.get("id"), record.get("job_id"), decision)
                continue
            scheduled += 1
            logger.warning(
                "Replaying cron job '%s' occurrence lost to a shutdown (attempt %s)",
                (job or {}).get("name") or record.get("job_id"), record.get("id"))
        except Exception as exc:
            # One malformed record must not stop the rest from being reconciled.
            logger.debug("Interrupted-retry reconcile failed for %s: %s", record.get("id"), exc)
    return scheduled
