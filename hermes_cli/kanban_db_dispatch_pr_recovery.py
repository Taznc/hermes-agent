"""Local, bounded recovery from PR-comment suppression after a failed run.

A URL is duplicate-work evidence, not a successful publication/CI handoff.
Keep the ordinary 24-hour guard, but let the existing failure-budgeted retry
path resume an ended worker after a short cooling-off period. A reviewer asking
for changes is explicit same-PR rework and needs no cooldown. No GitHub call
belongs under the dispatch lock, and recovery never grants completion authority.
"""
from __future__ import annotations

import sqlite3


PR_RECOVERY_COOLDOWN_SECONDS = 300


def pr_recovery_after_run(
    latest_run: sqlite3.Row | None, comment_created_at: int, assignee: str, now: int,
) -> dict | None:
    """Describe recovery only for evidence superseded by the latest ended run.

    Newer comments re-arm duplicate protection. A different assignee's failed
    run cannot authorize recovery for the current worker; a review verdict can.
    Retry counts and
    ownership remain enforced by the normal reaper, claim and dispatch gates.
    """
    if latest_run is None:
        return None
    outcome = latest_run["outcome"]
    rework = outcome == "changes_requested"
    if not rework and (
        latest_run["profile"] != assignee
        or outcome not in {"crashed", "timed_out", "interrupted"}
    ):
        return None
    ended_at = int(latest_run["ended_at"])
    if comment_created_at > ended_at:
        return None
    eligible_at = ended_at if rework else ended_at + PR_RECOVERY_COOLDOWN_SECONDS
    return {
        "prior_run_id": latest_run["id"],
        "prior_outcome": latest_run["outcome"],
        "recovery_reason": "changes_requested" if rework else "failed_run",
        "eligible_at": eligible_at,
        "eligible": now >= eligible_at,
        "recovery": (
            "Resume the existing PR and preserved workspace; verify prior work, "
            "do not create a duplicate PR. Finish the review/CI handoff or record "
            "an explicit blocker. Recovery does not waive PR acceptance."
        ),
    }
