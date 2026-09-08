"""``approve_review_task`` — the reviewer verdict that preserves the card.

Approval is deliberately NOT completion. ``complete_task`` sets ``done`` and
runs ``_cleanup_workspace()``, which reaps a clean, fully-pushed task worktree
on the spot. That is correct for a card whose life ends at review, and fatal
for one that still has to be landed: the tree, the branch, and the card are the
evidence an attended ``hermes kanban land`` re-verifies before it merges
anything.

So an approval closes the reviewer's own run with ``outcome='approved'``,
records the exact commit that was reviewed, releases the claim, and leaves the
card sitting in ``review`` awaiting the attended landing. ``review`` is already
a locked column (a card cannot be dragged into it, only out), so an approved
card cannot drift into another lane; and reusing it keeps the ten-status
enumeration stable across the CLI, the dashboard, and the Desktop plugin
instead of adding an eleventh that every one of those surfaces must learn.

The card leaves ``review`` in exactly three ways afterwards, all explicit:
:func:`hermes_cli.kanban_land.land_task` closes it after a proven remote
read-back, ``request_changes`` sends it back, or a human moves it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import write_txn


def approve_review_task(
    conn: sqlite3.Connection, task_id: str, *, source_sha: str,
    summary: Optional[str] = None, metadata: Optional[dict] = None,
) -> tuple[bool, Optional[str]]:
    """Record an explicit reviewer approval bound to ``source_sha``.

    Returns ``(True, None)`` or ``(False, reason)``. The approval is only
    reachable from a live run the *review* column handed out, so an implementer
    cannot approve their own card, and it is bound to one commit: landing later
    refuses if the branch has moved since, because verification is not review.
    """
    sha = (source_sha or "").strip()
    if not sha:
        return False, "source_sha is required"
    summary = kb.redact_review_value(summary)
    metadata = kb.redact_review_value(metadata)

    with write_txn(conn):
        row = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return False, "task not found"
        run_id = row["current_run_id"]
        if row["status"] != "running" or run_id is None:
            return False, "task is not in an active review run"
        claimed = kb._latest_event(conn, task_id, "claimed", run_id)
        if kb._json_dict(kb._row_get(claimed, "payload")).get("source_status") != "review":
            return False, "active run was not claimed from review"

        run_metadata = {**(metadata if isinstance(metadata, dict) else {}), "approved_sha": sha}
        # Back to ``review``, not ``done``: the card is approved and waiting for
        # an attended landing, and nothing may reap its workspace before then.
        if conn.execute(
            "UPDATE tasks SET status = 'review', claim_lock = NULL, claim_expires = NULL, "
            "worker_pid = NULL, worker_unit = NULL "
            "WHERE id = ? AND status = 'running' AND current_run_id = ?",
            (task_id, int(run_id)),
        ).rowcount != 1:
            return False, "task changed during review handoff"
        ended_run_id = kb._end_run(
            conn, task_id, outcome="approved", status="review",
            summary=summary or _APPROVED_NOTE, metadata=run_metadata,
        )
        kb._append_event(
            conn, task_id, "approved",
            {"approved_sha": sha, "summary": summary}, run_id=ended_run_id,
        )
    return True, None


def resolve_reviewed_sha(conn: sqlite3.Connection, task_id: str) -> tuple[Optional[str], Optional[str]]:
    """``(sha, error)`` for the commit a reviewer is approving.

    Read from the task's own worktree HEAD rather than asked for, because a
    hand-typed sha is the one part of the binding a human can get wrong, and a
    wrong one either blocks a good landing or authorizes an unreviewed commit.
    """
    row = conn.execute(
        "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if row is None:
        return None, "task not found"
    path = str(row["workspace_path"] or "")
    if row["workspace_kind"] != "worktree" or not path:
        return None, (
            "this card has no git worktree, so the reviewed commit cannot be resolved "
            "automatically — pass --sha <commit>"
        )
    if not Path(path).is_dir():
        return None, f"the task worktree {path} is gone; pass --sha <commit>"
    try:
        return _git_head(path), None
    except OSError as exc:
        return None, f"cannot read HEAD from {path}: {exc}"


def _git_head(worktree: str) -> str:
    import subprocess

    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree, capture_output=True,
        text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise OSError((proc.stderr or "").strip())
    return (proc.stdout or "").strip()


def cmd_approve(args) -> int:
    """``hermes kanban approve <task-id> [--sha COMMIT] [reason...]``."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban_output import _err

    task_id = args.task_id
    reason = getattr(args, "reason", None)
    summary = str(reason).strip() if reason else None

    with kbc.connect_closing() as conn:
        sha = str(getattr(args, "sha", None) or "").strip()
        if not sha:
            sha, error = resolve_reviewed_sha(conn, task_id)
            if not sha:
                return _err(f"cannot approve {task_id}: {error}")
        ok, detail = approve_review_task(conn, task_id, source_sha=sha, summary=summary)
        if not ok:
            return _err(f"cannot approve {task_id}: {detail or 'invalid review state'}")
        print(
            f"Approved {task_id} at {sha[:12]} — the card stays in review, with its "
            f"worktree and branch intact, until `hermes kanban land {task_id}` proves "
            "the content reached the target."
        )
    return 0


_APPROVED_NOTE = "Review approved; awaiting attended landing."


@dataclass(frozen=True)
class Approval:
    """A live reviewer approval: the current verdict on the current review cycle."""

    run_id: int
    reviewer: Optional[str]
    summary: Optional[str]
    metadata: dict
    approved_sha: str
    approved_at: Optional[int]


def latest_approval(conn: sqlite3.Connection, task_id: str) -> Optional[Approval]:
    """The card's LIVE approval, or None when it has none.

    "Live" is the load-bearing word, and it is why this is not simply "the
    newest ``approved`` run". An approval is the verdict on ONE review cycle;
    anything that opens a newer cycle retires it:

    * a ``changes_requested`` run newer than the approval — the card was sent
      back, so the approval describes work that has since been rejected;
    * a ``review_requested`` event newer than the approval — the card was
      re-submitted, so a newer cycle is open and un-adjudicated. Without this,
      a stale approval would authorize whatever the branch grew afterwards.
    """
    row = conn.execute(
        "SELECT id, profile, summary, metadata, ended_at, outcome FROM task_runs "
        "WHERE task_id = ? AND outcome IN ('approved', 'changes_requested') "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if row is None or row["outcome"] != "approved":
        return None
    metadata = kb._json_dict(row["metadata"])
    sha = str(metadata.get("approved_sha") or "").strip()
    if not sha:
        return None
    resubmitted = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'review_requested' "
        "AND id > (SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind = 'approved') "
        "LIMIT 1", (task_id, task_id),
    ).fetchone()
    if resubmitted is not None:
        return None
    return Approval(
        run_id=int(row["id"]), reviewer=row["profile"], summary=row["summary"],
        metadata=metadata, approved_sha=sha, approved_at=row["ended_at"],
    )
