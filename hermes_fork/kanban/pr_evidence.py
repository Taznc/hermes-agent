"""Local-only worker PR evidence for the existing preclaim seam (dev d2dd6da7a3).

Unknown repositories (including scratch cards) retain worker-authored suppression.
No PR-state/network lookup is allowed inside dispatch's write lock.
"""
from __future__ import annotations

import contextlib
import re
import sqlite3
import subprocess
import time

from hermes_fork.kanban import pr_requeue


def repo_slug(url):
    match = re.fullmatch(
        r"(?:https?://github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/?", url.strip(), re.IGNORECASE,
    )
    return match[1].removesuffix(".git").lower() if match else None


def local_repo(path):
    if not path:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "config", "--local", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=1, check=False,
        )
        return repo_slug(result.stdout.strip()) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def task_repo(task):
    # Read-only: never initialize/migrate projects.db while holding dispatch's lock.
    if task["project_id"]:
        from hermes_cli import projects_db as pdb
        try:
            with contextlib.closing(sqlite3.connect(
                pdb.projects_db_path().resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1,
            )) as pconn:
                pconn.row_factory = sqlite3.Row
                project = pdb.get_project(pconn, task["project_id"])
            if project:
                primary = project.primary_path or next(
                    (f.path for f in project.folders if f.is_primary), None,
                )
                if slug := local_repo(primary):
                    return slug
        except (OSError, sqlite3.Error):
            pass
    return local_repo(task["workspace_path"]) if task["workspace_kind"] == "worktree" else None


def evidence(conn, task, now):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd
    newest = None
    urls = []
    repo = task_repo(task)
    for c in conn.execute(
        "SELECT author, body, created_at FROM task_comments WHERE task_id = ? "
        "AND created_at >= ? ORDER BY created_at DESC, id DESC",
        (task["id"], now - kbd._RESPAWN_GUARD_PR_WINDOW),
    ):
        author = kb._lossy_text(c["author"]) or ""
        if not task["assignee"] or author != task["assignee"]:
            continue
        body = kb._lossy_text(c["body"]) or ""
        if pr_requeue._is_receipt(author, body):
            continue
        found = [m.group(0) for m in kbd._RESPAWN_GUARD_PR_URL_RE.finditer(body)
                 if repo is None or "/".join(m.group(0).split("/")[3:5]).lower() == repo]
        if found:
            if newest is None:
                newest = int(c["created_at"])
            urls.extend(u for u in found if u not in urls)
    return newest, urls


def release(conn, task_id, guard_reason, *, dry_run):
    """Scope PR holds and retry ended workers after 300s (dev 367a03b3ba).

    Other guards, failure counters, claim identity and review admission are untouched.
    Upstream None (including handoff and rate-limit bypass) remains authoritative.
    """
    from hermes_cli import kanban_db_dispatch as kbd
    if guard_reason != "active_pr":
        return guard_reason
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None or task["status"] != "ready":
        return guard_reason
    run = conn.execute(
        "SELECT id, profile, outcome, ended_at FROM task_runs WHERE task_id = ? "
        "AND ended_at IS NOT NULL ORDER BY ended_at DESC, id DESC LIMIT 1", (task_id,),
    ).fetchone()
    now = int(time.time())
    newest, urls = evidence(conn, task, now)
    if newest is None:
        return None
    events = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? AND created_at > ? "
        "AND kind IN ('assigned', 'changes_requested', 'review_reopened')", (task_id, newest),
    )
    if any(kbd._is_handoff_event(e["kind"], e["payload"]) for e in events):
        return None
    recovery = pr_requeue.recovery_for_evidence(conn, task_id, newest, urls)
    if recovery is None and run is not None and (
        run["profile"] == task["assignee"]
        and run["outcome"] in {"crashed", "timed_out", "interrupted"}
        and newest <= int(run["ended_at"])
        and now >= int(run["ended_at"]) + 300
    ):
        recovery = {
            "recovery_reason": "failed_run", "prior_run_id": run["id"],
            "prior_outcome": run["outcome"], "eligible_at": int(run["ended_at"]) + 300,
            "pr_comment_at": newest, "pr_urls": urls, "recovery": pr_requeue.SAME_PR_RECOVERY,
        }
    if recovery is None:
        return "active_pr"
    pr_requeue.write_receipt(conn, task_id, recovery, dry_run=dry_run)
    return None
