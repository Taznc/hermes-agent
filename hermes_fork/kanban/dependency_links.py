"""Kanban dependency links / roadmap-lane validation.

Extracted from ``hermes_cli.kanban_db`` (project-link resolution for
``create_task``, the parent-dependency-satisfaction predicate and its
deferred-skip-link bookkeeping, and the roadmap-lane state machine
``on_hold``/``idea``/``roadmap`` transitions) behind one
``# >>> FORK ANCHOR: kanban-dependency-links <<<`` import site. See
``hermes_fork/kanban/__init__.py`` for why ``hermes_fork/kanban/`` exists
despite docs/fork-anchor-extraction.md's earlier "do not create
hermes_fork/kanban/" verdict — this module is pure logic over an injected
``sqlite3.Connection``, with no schema/migration or dashboard-payload
ownership, the same shape as ``dispatch_resilience.py``.

Origin-resident helpers this module still needs (``get_task``, ``write_txn``,
``_append_event``, ``_link``, ``_missing_task_ids``, ``_json_dict``,
``_row_get``, ``_landing_status_after_parents``, ``_resume_status_from_events``,
``_reclaim_dangling_run``, ``_end_or_synthesize_run``, and
``ROADMAP_LANE_STATUSES``) are reached late-bound via ``_kb`` (import-cycle
breaking, mirroring ``dispatch_resilience.py``'s own ``_kb``/``_kd`` pattern)
so monkeypatching ``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


def _resolve_project_link(
    conn: sqlite3.Connection, project_id: Optional[str], project_source_task_id: Optional[str],
    workspace_kind: str, workspace_path: Optional[str],
) -> tuple[Optional[str], Any, Optional[str], str]:
    """``(project_id, project_obj, project_repo, workspace_kind)`` for ``create_task``.

    A project-linked task is anchored to the project's primary repo as a
    worktree with a deterministic branch (slug + task id). Projects live in the
    creator's per-profile projects.db, but the stored repo path is absolute so
    the cross-profile dispatcher needs no projects.db access. ``project_repo``
    is set when the worktree path must still be derived from the new task id.
    """
    project_id = (str(project_id).strip() or None) if project_id is not None else None
    if not project_id:
        return None, None, None, workspace_kind
    from hermes_cli import projects_db as _pdb

    project_repo: Optional[str] = None
    try:
        with _pdb.connect_closing() as _pconn:
            project_obj = _pdb.get_project(_pconn, project_id)
    except Exception:
        project_obj = None
    if project_obj is None and project_source_task_id:
        project_obj, project_repo = _project_from_source_task(
            conn, _pdb, project_id, str(project_source_task_id),
        )
        if project_obj is not None and workspace_kind == "scratch":
            workspace_kind = "worktree"
    if project_obj is None:
        # Unresolvable id/slug: drop the link (never a dangling reference,
        # never a crash) and create an ordinary scratch task.
        return None, None, None, workspace_kind
    # Canonicalise (a slug may have been passed) and anchor the worktree
    # under the project's primary repo.
    if workspace_kind == "scratch" and project_obj.primary_path:
        workspace_kind = "worktree"
    if workspace_kind == "worktree" and workspace_path is None and project_obj.primary_path:
        # Concrete path is deferred to the insert loop: a fresh
        # ``<repo>/.worktrees/<task-id>`` keyed on the new task id.
        project_repo = str(project_obj.primary_path)
    return project_obj.id, project_obj, project_repo, workspace_kind


def _project_from_source_task(
    conn: sqlite3.Connection, _pdb: Any, project_id: str, source_task_id: str,
) -> tuple[Any, Optional[str]]:
    """Recover a Project (and its repo) from a canonical project-linked
    worktree task on this board. Worker profiles have their own projects.db
    while the Kanban DB is shared, so this carries the repo + branch
    convention forward without opening the creator's store and without
    reusing the source task's literal worktree path. ``(None, None)`` when
    the source task is not a ``<repo>/.worktrees/<id>`` project worktree."""
    source_task = _kb.get_task(conn, source_task_id)
    if not (
        source_task is not None
        and source_task.project_id == project_id
        and source_task.workspace_kind == "worktree"
        and source_task.workspace_path
    ):
        return None, None
    source_path = Path(source_task.workspace_path)
    if not (
        source_path.is_absolute()
        and source_path.name == source_task.id
        and source_path.parent.name == ".worktrees"
    ):
        return None, None
    project_slug = None
    if source_task.branch_name:
        prefix, separator, leaf = source_task.branch_name.partition("/")
        if separator and (leaf == source_task.id or leaf.startswith(f"{source_task.id}-")):
            with contextlib.suppress(ValueError):
                project_slug = _pdb.normalize_slug(prefix)
    if project_slug is None:
        with contextlib.suppress(ValueError):
            project_slug = _pdb.normalize_slug(project_id)
    if not project_slug:
        return None, None
    project_repo = str(source_path.parent.parent)
    project_obj = _pdb.Project(
        id=project_id, slug=project_slug, name=project_slug, created_at=0, primary_path=project_repo,
    )
    return project_obj, project_repo


def _validate_lane(
    lane: Optional[str], *, triage: bool = False, initial_status: str = "running",
) -> Optional[str]:
    """Normalize a roadmap lane for ``create_task``: ``None`` or one of
    ``ROADMAP_LANE_STATUSES``. Rejects combining a lane with ``triage`` or an explicit
    ``initial_status`` — those are three different answers to "where does this card land",
    and silently picking one would put live work in the wishlist (or vice versa)."""
    if lane is None:
        return None
    lane = str(lane).strip()
    if lane not in _kb.ROADMAP_LANE_STATUSES:
        raise ValueError(f"lane must be one of {sorted(_kb.ROADMAP_LANE_STATUSES)}, got {lane!r}")
    if triage:
        raise ValueError("lane and triage are mutually exclusive")
    if initial_status != "running":
        raise ValueError(f"lane and initial_status={initial_status!r} are mutually exclusive")
    return lane


def _parent_dependency_satisfied(parent: Mapping[str, Any]) -> bool:
    """Whether a parent has satisfied its dependency edge.

    ``completed_at`` is written only by the completion lifecycle. It preserves
    that evidence when completed work is later archived, without treating a
    manually archived incomplete task as successful.
    """
    status = parent["status"]
    return status == "done" or (status == "archived" and parent["completed_at"] is not None)


def _born_satisfied_parents(
    conn: sqlite3.Connection, parent_ids: Iterable[str],
) -> set[str]:
    """Of ``parent_ids``, those a NEW edge could never gate for: archived AND completed.

    The mirror of :func:`_clear_satisfied_outgoing_links`, which deletes such an
    edge when the link is made first and the parent archives second. This covers
    the reverse ordering — the parent is already archived-after-completion when
    the edge is minted — so both orderings converge on the same board state
    instead of one of them leaving a permanently-satisfied row behind for every
    future surface to remember to filter.

    Deliberately NARROWER than :func:`_parent_dependency_satisfied`, which is
    also true of a plain ``done`` parent. A ``done`` parent is only
    *provisionally* satisfied: reopening it clears ``completed_at`` and
    ``invalidate_descendants_for_parent_reopen`` walks ``task_links`` to retract
    the descendants that relied on it. Skipping those edges would strand every
    child linked after its parent finished. Archival is what makes the evidence
    permanent, because leaving ``archived`` is what clears it.
    """
    ids = tuple(dict.fromkeys(pid for pid in parent_ids if pid))
    if not ids:
        return set()
    rows = conn.execute(
        "SELECT id FROM tasks WHERE id IN (" + ",".join("?" * len(ids)) + ") "
        "AND status = 'archived' AND completed_at IS NOT NULL",
        ids,
    ).fetchall()
    return {r["id"] for r in rows}


def _pending_skipped_links(
    conn: sqlite3.Connection, *, parent_id: Optional[str] = None,
) -> list[tuple[str, str]]:
    """The (parent, child) relations that exist only as a deferred skip record.

    :func:`_born_satisfied_parents` refuses to write a row for an edge that
    would be born permanently satisfied, so between that refusal and the
    parent's reopening the relation lives in the audit trail rather than in
    ``task_links``. This is the ONE definition of that set; every consumer reads
    it here instead of re-deriving it, because two consumers derived it two
    slightly different ways is exactly how the first round got both the cycle
    walk and the restore wrong.

    A pair is pending iff its most recent ``link_skipped`` is not settled by a
    LATER event on the child, ranked by the event log's own monotonic id: a
    ``linked`` (the edge was materialized, including by a restore) or an
    ``unlinked`` (an operator cut the relation). Order matters in BOTH
    directions — an older unlink must not suppress a newer explicit relink, and
    an older skip must not resurrect an edge that was later materialized and
    then deleted by :func:`_clear_satisfied_outgoing_links`, whose ``linked``
    predecessor is what settles it. A pair whose row is live, or either of whose
    tasks is gone, is not pending either.

    ``linked`` is a complete record of post-skip materialization: the two
    ``_link`` call sites that emit no such event (``create_task`` and the
    decompose root edge) only ever link a task created in that same statement,
    which cannot already carry a ``link_skipped``.
    """
    sql = "SELECT id, task_id, payload FROM task_events WHERE kind = 'link_skipped'"
    params: tuple[str, ...] = ()
    if parent_id is not None:
        # payload matching is exact below; LIKE only narrows the scan.
        sql += " AND payload LIKE ?"
        params = (f"%{parent_id}%",)
    latest: dict[tuple[str, str], int] = {}
    for row in conn.execute(sql + " ORDER BY id", params).fetchall():
        payload = _kb._json_dict(_kb._row_get(row, "payload"))
        pid, cid = payload.get("parent"), payload.get("child") or row["task_id"]
        if not pid or not cid or (parent_id is not None and pid != parent_id):
            continue
        latest[(pid, cid)] = row["id"]  # ordered by id, so the last write wins
    pending: list[tuple[str, str]] = []
    for (pid, cid), skip_id in latest.items():
        if conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?", (pid, cid),
        ).fetchone() is not None:
            continue
        if _kb._missing_task_ids(conn, [pid, cid]):
            continue
        if not _skip_superseded(conn, pid, cid, skip_id):
            pending.append((pid, cid))
    return sorted(pending)


def _skip_superseded(
    conn: sqlite3.Connection, parent_id: str, child_id: str, skip_id: int,
) -> bool:
    """True iff an event after ``skip_id`` settled the ``parent -> child`` relation."""
    rows = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND id > ? AND kind IN ('linked', 'unlinked')",
        (child_id, int(skip_id)),
    ).fetchall()
    return any(
        _kb._json_dict(_kb._row_get(row, "payload")).get("parent") == parent_id for row in rows
    )


def _clear_satisfied_outgoing_links(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """Drop the child edges an archived task can never gate again.

    A completed task's dependency edge is satisfied *permanently* —
    :func:`_parent_dependency_satisfied` keys off ``completed_at``, which only
    the completion lifecycle writes, so no future state re-gates the child.
    Keeping the row past that point is pure debt: an archived parent is absent
    from the board payload, so every surface that resolves links against the
    active view (the Desktop drawer's ``resolveLinks``) can only report it as an
    unresolvable — and therefore conservatively still-gating — blocker.

    An archived task WITHOUT ``completed_at`` was withdrawn, not finished; its
    edges stay so the child keeps showing a real block. Edges where ``task_id``
    is the CHILD are dependency history belonging to the surviving parent and
    are never touched here. Returns the child ids whose edges were removed.

    Handles only the "link first, archive second" ordering; the reverse is
    :func:`_born_satisfied_parents`, which refuses to mint the row at all.
    """
    row = conn.execute("SELECT status, completed_at FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or not _parent_dependency_satisfied(row):
        return []
    children = [
        r["child_id"]
        for r in conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ? ORDER BY child_id", (task_id,),
        ).fetchall()
    ]
    if children:
        conn.execute("DELETE FROM task_links WHERE parent_id = ?", (task_id,))
    return children


def _restore_skipped_child_links(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """Re-mint the edges that were skipped while ``task_id`` was satisfied.

    The inverse of :func:`_born_satisfied_parents`. A skip is only correct while
    the parent's completion evidence is permanent; reopening it withdraws that
    evidence, and a child released on it has to start waiting again. Without
    this, ``invalidate_descendants_for_parent_reopen`` would find nothing to
    retract — the card would still say "waiting on this parent" to the user while
    the board quietly let it run.

    What to restore is :func:`_pending_skipped_links`, which resolves the LATEST
    intent per pair, so an operator's unlink, a later explicit relink, and an
    archive that cleared the materialized edge each win over older history in
    whatever order they happened. Returns the child ids re-linked.

    No cycle guard is needed here: :func:`_would_cycle` spans these deferred
    relations at mint time, so no edge that would close a cycle with one of them
    can exist. Dropping such an edge silently is exactly the failure this pairs
    against — the caller would be left believing in a link the board forgot.
    """
    restored: list[str] = []
    for _parent, child_id in _pending_skipped_links(conn, parent_id=task_id):
        _kb._link(conn, task_id, child_id)
        _kb._append_event(
            conn, child_id, "linked",
            {"parent": task_id, "child": child_id, "restored_from": "link_skipped"},
        )
        restored.append(child_id)
    return restored


def hold_task(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Shelve a task in ``on_hold`` — a deliberate human pause, distinct from ``blocked`` (worker
    needs input) and ``scheduled`` (waiting on time). Not dispatchable and ignored by
    ``recompute_ready``'s auto-recovery: a shelved task never resumes on its own — only an explicit
    :func:`unhold_task` (or the dashboard's drag-out-of-the-column action). Mirrors
    ``schedule_task``'s shape (closes any active run, clears the claim)."""
    with _kb.write_txn(conn):
        params: list[Any] = [task_id]
        sql = """
            UPDATE tasks
               SET status       = 'on_hold',
                   claim_lock   = NULL,
                   claim_expires= NULL,
                   worker_pid   = NULL,
                   worker_unit  = NULL
             WHERE id = ?
               AND status IN ('todo', 'triage', 'ready', 'running', 'blocked', 'scheduled')
        """
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params.append(int(expected_run_id))
        if conn.execute(sql, params).rowcount != 1:
            return False
        run_id = _kb._end_or_synthesize_run(
            conn, task_id, outcome="on_hold", status="on_hold", summary=reason, synthesize=bool(reason),
        )
        _kb._append_event(conn, task_id, "held", {"reason": reason}, run_id=run_id)
        return True


# --- Roadmap lanes (idea / roadmap) ---
#
# The lanes are inert: nothing selects them, so the ONLY way a card leaves one is an explicit
# operator verb here (or ``archive_task``). Every transition is a status-guarded CAS inside
# ``write_txn`` mirroring ``hold_task``'s shape, and a refused transition raises ``ValueError``
# naming the attempted from->to so a caller never has to guess why nothing moved.

# Allowed lane transitions, ``from -> {to, ...}``. Deliberately one-directional out of the
# wishlist: no live status may move INTO a lane (a card enters only at creation), so the existing
# ``unblock``/``hold`` habits can never park real work here and ``on_hold`` keeps its meaning.
ROADMAP_LANE_TRANSITIONS: dict[str, frozenset[str]] = {
    "idea": frozenset({"roadmap", "archived"}),
    "roadmap": frozenset({"triage", "ready", "idea", "archived"}),
}

# Where ``spawn_roadmap_task`` may land a card. ``triage`` is the default: the operator's standing
# decision is to always let auto_decompose re-specify/split a roadmap item before it executes.
ROADMAP_SPAWN_TARGETS = frozenset({"triage", "ready"})


def _lane_transition(
    conn: sqlite3.Connection, task_id: str, *, to: str, event: str,
    payload: Optional[dict] = None, parent_gated: bool = False,
) -> bool:
    """Move ``task_id`` out of a roadmap lane into ``to``, appending ``event``.

    Raises ``ValueError`` naming ``from -> to`` when the task's current status may not make this
    move (including every live status, which can never enter a lane). Returns ``False`` only when
    the row vanished mid-transaction.

    ``parent_gated`` routes a ``ready`` landing through ``_landing_status_after_parents``, the
    same re-gate ``unblock_task``/``promote_task``/``unarchive_task`` use, so a lane card linked
    under an unfinished parent lands in ``todo`` instead of rendering as bogus ``ready`` until
    ``claim_task`` refuses it. The gate downgrades the LANDING only — ``to`` is still validated
    against ``ROADMAP_LANE_TRANSITIONS`` first, so it can never widen what an operator may ask
    for. Only ``ready`` is parent-gated; ``triage`` is not a gated status.
    """
    with _kb.write_txn(conn):
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown task {task_id}")
        current = row["status"]
        if to not in ROADMAP_LANE_TRANSITIONS.get(current, frozenset()):
            raise ValueError(
                f"invalid roadmap lane transition for {task_id}: {current!r} -> {to!r} "
                f"(allowed from {current!r}: "
                f"{sorted(ROADMAP_LANE_TRANSITIONS.get(current, frozenset())) or 'nothing'})"
            )
        landing = to
        if parent_gated and to == "ready":
            landing = _kb._landing_status_after_parents(conn, task_id)
        if landing != to:
            payload = {**(payload or {}), "status": landing, "requested_status": to}
        cur = conn.execute(
            "UPDATE tasks SET status = ? WHERE id = ? AND status = ?", (landing, task_id, current),
        )
        if cur.rowcount != 1:
            return False
        _kb._append_event(conn, task_id, event, payload)
        return True


def refine_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """``idea -> roadmap``: the item has been hashed out with the operator. Still inert —
    refining authorizes nothing, it only says the shape is agreed."""
    return _lane_transition(conn, task_id, to="roadmap", event="refined")


def demote_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """``roadmap -> idea``: send a hashed-out item back to rough capture."""
    return _lane_transition(conn, task_id, to="idea", event="demoted")


def spawn_roadmap_task(conn: sqlite3.Connection, task_id: str, *, to: str = "triage") -> bool:
    """``roadmap -> triage`` (default) or ``ready``: authorize the item to execute.

    ``triage`` is the default so auto_decompose gets to re-specify/split it first; ``to="ready"``
    is the explicit opt-out for an item that is already a single well-formed task. This is the
    only path from the wishlist into live work — ``idea`` must be refined first.

    ``ready`` is parent-gated exactly as every other entry into ``ready`` is: a card whose
    parents are unfinished lands in ``todo`` and auto-promotes later, and the
    ``spawned_from_roadmap`` payload records both the real landing and the ``requested_status``.
    """
    if to not in ROADMAP_SPAWN_TARGETS:
        raise ValueError(f"spawn target must be one of {sorted(ROADMAP_SPAWN_TARGETS)}, got {to!r}")
    return _lane_transition(
        conn, task_id, to=to, event="spawned_from_roadmap", payload={"status": to},
        parent_gated=True,
    )


def unhold_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Resume an ``on_hold`` task to its safe resumable phase. Mirrors :func:`unblock_task`:
    re-gates on parent completion (``todo`` if any parent isn't done, else ``ready``/``review``),
    closes any dangling ``current_run_id``, never auto-fires."""
    now = int(time.time())
    with _kb.write_txn(conn):
        current = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not current or current["status"] != "on_hold":
            return False
        resume_status = _kb._resume_status_from_events(conn, task_id)
        _kb._reclaim_dangling_run(conn, task_id, statuses=("on_hold",), now=now, note="invariant recovery on unhold")
        landing_status = _kb._landing_status_after_parents(conn, task_id)
        new_status = "review" if landing_status == "ready" and resume_status == "review" else landing_status
        cur = conn.execute(
            "UPDATE tasks SET status = ?, current_run_id = NULL, "
            "consecutive_failures = 0, last_failure_error = NULL "
            "WHERE id = ? AND status = 'on_hold'", (new_status, task_id),
        )
        if cur.rowcount != 1:
            return False
        _kb._append_event(
            conn, task_id, "unheld",
            {"status": new_status, "resume_status": resume_status}
            if new_status != "ready" or resume_status != "ready" else None,
        )
        return True


from hermes_cli import kanban_db as _kb  # noqa: E402
