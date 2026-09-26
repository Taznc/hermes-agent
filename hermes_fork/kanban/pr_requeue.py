"""Release the ``active_pr`` respawn guard on a deliberate requeue (tier T1).

Upstream ``check_respawn_guard`` holds a ready card for 24 h whenever a recent
comment carries a GitHub PR URL, and only an ``assigned`` /
``changes_requested`` / ``review_reopened`` handoff lifts it. A card whose
worker posted its PR, then blocked on a dependency and was promoted when the
parent landed, therefore sits in ``ready`` as ``respawn_guarded=active_pr``
for the whole window and wedges everything downstream of it (dev e51fe4a1cf:
316 ticks, 0 spawns, a dead board overnight).

Same escape the ``recent_success`` guard already honours: a ``status`` /
``promoted`` / ``unblocked`` / ``reclaimed`` event recorded strictly AFTER the
newest PR comment is a deliberate re-run, so the guard is released. Duplicate
protection moves into the worker's context instead: the release writes an
``active_pr_recovery`` event and a ``dispatcher`` comment naming the existing
PR(s) with the same-PR instruction, so the resumed worker continues that PR
rather than opening a second one.

Still guarded: no requeue after the PR comment (a crash or plain retry), any
PR comment newer than the requeue (fresh evidence re-arms), and a same-second
tie (fail closed, as upstream's handoff rule). The receipt comment itself is
not PR evidence, or a spawn that fails after the release would re-wedge the
card on its own receipt.

Called from exactly one site: the ``pr-requeue-recovery`` FORK ANCHOR right
after ``check_respawn_guard`` in ``hermes_cli.kanban_db_dispatch.
_dispatch_lane_task``, before the claim. Ported from dev
``hermes_cli/kanban_db_dispatch_pr_recovery.py`` (``pr_recovery_after_requeue``).
"""

from __future__ import annotations

import sqlite3
from typing import Optional

REQUEUE_EVENT_KINDS = ("status", "promoted", "unblocked", "reclaimed")
"""Deliberate re-queues; the same set upstream's ``recent_success`` escape uses."""

RECEIPT_AUTHOR = "dispatcher"
RECEIPT_PREFIX = "PR recovery:"

SAME_PR_RECOVERY = (
    "Resume the existing PR and preserved workspace; verify prior work, "
    "do not create a duplicate PR. Finish the review/CI handoff or record "
    "an explicit blocker. Recovery does not waive PR acceptance."
)


def _is_receipt(author: str, body: str) -> bool:
    return author == RECEIPT_AUTHOR and body.startswith(RECEIPT_PREFIX)


def pr_requeue_recovery(conn: sqlite3.Connection, task_id: str, now: int) -> Optional[dict]:
    """The recovery receipt when a requeue follows the newest PR comment, else None.

    None means "leave the guard as upstream decided": no PR comment in the
    window, or no requeue strictly after the newest one.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    newest_at: Optional[int] = None
    urls: list[str] = []
    for c in conn.execute(
        "SELECT author, body, created_at FROM task_comments "
        "WHERE task_id = ? AND created_at >= ? ORDER BY created_at DESC, id DESC",
        (task_id, now - kbd._RESPAWN_GUARD_PR_WINDOW),
    ).fetchall():
        body = kb._lossy_text(c["body"]) or ""
        found = [m.group(0) for m in kbd._RESPAWN_GUARD_PR_URL_RE.finditer(body)]
        if not found or _is_receipt(kb._lossy_text(c["author"]) or "", body):
            continue
        if newest_at is None:
            newest_at = int(c["created_at"] or 0)
        urls.extend(u for u in found if u not in urls)
    if newest_at is None:
        return None
    requeue = conn.execute(
        "SELECT kind, created_at FROM task_events WHERE task_id = ? AND created_at > ? "
        f"AND kind IN ({','.join('?' * len(REQUEUE_EVENT_KINDS))}) "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id, newest_at, *REQUEUE_EVENT_KINDS),
    ).fetchone()
    if requeue is None:
        return None
    return {
        "recovery_reason": "requeued",
        "requeue_event": requeue["kind"],
        "requeued_at": int(requeue["created_at"]),
        "pr_comment_at": newest_at,
        "pr_urls": urls,
        "recovery": SAME_PR_RECOVERY,
    }


def release(
    conn: sqlite3.Connection, task_id: str, guard_reason: Optional[str], *, dry_run: bool,
) -> Optional[str]:
    """Anchor entry point: pass every guard reason through except a releasable
    ``active_pr``, which becomes None after the recovery receipt is written
    (``dry_run`` previews the release and writes nothing)."""
    if guard_reason != "active_pr":
        return guard_reason
    import time

    from hermes_cli import kanban_db as kb

    recovery = pr_requeue_recovery(conn, task_id, int(time.time()))
    if recovery is None:
        return guard_reason
    if not dry_run:
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "active_pr_recovery", recovery)
            # The worker packet shows comments, not event payloads: this is
            # what carries the same-PR constraint into the resumed worker.
            kb.add_comment(
                conn, task_id, RECEIPT_AUTHOR,
                f"{RECEIPT_PREFIX} {', '.join(recovery['pr_urls'])}\n{recovery['recovery']}",
            )
        kb._log.info(
            "kanban active_pr released by requeue task=%s event=%s prs=%s",
            task_id, recovery["requeue_event"], recovery["pr_urls"],
        )
    return None
