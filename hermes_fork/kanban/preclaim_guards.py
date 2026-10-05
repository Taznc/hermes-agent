"""Fork-owned pre-claim guard composition; preserve PR recovery side effects only on admission."""
from __future__ import annotations


def release(conn, task_id, guard_reason, *, dry_run):
    if guard_reason not in (None, "active_pr"):
        return guard_reason
    row = conn.execute("SELECT id, assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return guard_reason
    from hermes_fork.kanban.weekly_usage import admit
    reason = admit(conn, row, row["assignee"])
    if reason:
        return reason
    from hermes_fork.kanban.pr_evidence import release as release_pr
    return release_pr(conn, task_id, guard_reason, dry_run=dry_run)
