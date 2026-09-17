"""Kanban dashboard plugin — backend API routes, mounted at /api/plugins/kanban/.

Every handler is a thin wrapper around ``hermes_cli.kanban_db`` (the same code paths the CLI
and gateway ``/kanban`` command use, so the surfaces cannot drift). The ``/events`` WebSocket
tails the append-only ``task_events`` table on a short poll (WAL reads run alongside the
dispatcher's write txns); it carries its credential in the query string (browsers can't set
``Authorization`` on an upgrade) and is gated by the dashboard's canonical WS auth check.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import re
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect, status as http_status
from pydantic import BaseModel, Field

from hermes_cli import kanban_db
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_diagnostics as kd

from plugins.kanban.dashboard import attachments_router as _attachments_router
from plugins.kanban.dashboard import boards_router as _boards_router
from plugins.kanban.dashboard import dispatch_pause_router as _dispatch_pause_router
from plugins.kanban.dashboard import recovery_router as _recovery_router
from plugins.kanban.dashboard import worker_visibility_router as _worker_visibility_router
from plugins.kanban.dashboard._common import (
    BOARD_COLUMNS,
    _attachment_dict,
    _board_conn,
    _conflict,
    _conn,
    _errors_to_500,
    _map_errors,
    _projects_by_id,
    _require_ok,
    _require_task,
    _resolve_board,
    _run_aux,
    _value_error_400,
    _with_board_pinned,
)

log = logging.getLogger(__name__)

router = APIRouter()

# Each sibling module owns one topical slice of the dashboard's routes; this facade
# just mounts them alongside its own remaining handlers (board/task CRUD, comments,
# diagnostics, profiles, decompose, orchestration settings, the /events WebSocket).
# No route path, method, or response shape changes across the split — see the
# corresponding test files under tests/plugins/test_kanban_*.py.
router.include_router(_attachments_router.router)
router.include_router(_worker_visibility_router.router)
router.include_router(_recovery_router.router)
router.include_router(_dispatch_pause_router.router)
router.include_router(_boards_router.router)

_BOARD_Q = Query(None, description="Kanban board slug (omit for current)")


# --- Connection / board helpers ---------------------------------------------

def _ws_upgrade_authorized(ws: "WebSocket") -> bool:
    """Authorize a WS upgrade via the dashboard's canonical gate (``web_server_chat._ws_auth_ok``:
    ``?token=`` / ``?ticket=`` / ``?internal=``) so this endpoint can never drift from core
    auth; accepts when the dashboard isn't importable (bare-FastAPI test harness)."""
    try:
        from hermes_cli import web_server_chat as _ws
    except Exception:
        return True
    return bool(_ws._ws_auth_ok(ws))


# --- Serialization helpers --------------------------------------------------

_CARD_SUMMARY_PREVIEW_CHARS = 200


def _task_dict(task: kanban_db.Task, *, latest_summary: Optional[str] = None) -> dict[str, Any]:
    d = asdict(task)
    # Derived age metrics so the UI can colour stale cards without client deltas.
    try:
        d["age"] = kanban_db.task_age(task)
    except Exception:
        d["age"] = {"created_age_seconds": None, "started_age_seconds": None, "time_to_complete_seconds": None}
    # Latest non-null run summary (workers hand off via ``task_runs.summary``, not ``tasks.result``).
    d["latest_summary"] = latest_summary
    return d


def _placeholders(ids: list) -> str:
    return ",".join(["?"] * len(ids))


def _compute_task_diagnostics(
    conn: sqlite3.Connection, task_ids: Optional[list[str]] = None, *, board: Optional[str] = None,
) -> dict[str, list[dict]]:
    """``{task_id: [diagnostic_dict, ...]}`` (tasks with none omitted) via three aggregate
    queries (tasks, events, runs) — slurps the board; paginate if profiling shows a hotspot.

    ``board`` must be the SAME resolved slug ``conn`` was opened against (the caller's
    ``_board_conn``/``_conn`` resolution) so the concurrency snapshot's "other boards"
    total excludes the right board rather than falling back to the process's active-board
    default, which can differ under ``GET /board/all`` or an explicit ``?board=`` query.
    """
    from hermes_cli.config import load_config

    if task_ids is not None and not task_ids:
        return {}
    raw_config = load_config()
    diag_config = kd.config_from_runtime_config(raw_config)
    kanban_cfg = raw_config.get("kanban") if isinstance(raw_config, dict) else None
    # Same caps/counts the dispatcher enforces (kanban_db_dispatch.concurrency_snapshot)
    # so `stranded_in_ready` can suppress itself when the board is correctly at capacity
    # instead of drifting from the real cap check with a second counter.
    try:
        concurrency = kbd.concurrency_snapshot(
            conn, board=board, kanban_cfg=kanban_cfg if isinstance(kanban_cfg, dict) else None)
    except Exception:
        concurrency = None
    if task_ids is not None:
        rows = conn.execute(f"SELECT * FROM tasks WHERE id IN ({_placeholders(task_ids)})", tuple(task_ids)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM tasks WHERE status != 'archived'").fetchall()
    if not rows:
        return {}
    row_ids = [r["id"] for r in rows]

    def _rows_by_task(table: str) -> dict[str, list]:
        by_task: dict[str, list] = {tid: [] for tid in row_ids}
        for row in conn.execute(
            f"SELECT * FROM {table} WHERE task_id IN ({_placeholders(row_ids)}) ORDER BY id", tuple(row_ids)):
            by_task.setdefault(row["task_id"], []).append(row)
        return by_task

    events_by_task = _rows_by_task("task_events")
    runs_by_task = _rows_by_task("task_runs")
    graph_by_task = kanban_db.task_graph_contexts(conn, row_ids)
    out: dict[str, list[dict]] = {}
    for r in rows:
        tid = r["id"]
        diags = kd.compute_task_diagnostics(
            r, events_by_task[tid], runs_by_task[tid], config=diag_config,
            graph=graph_by_task.get(tid), concurrency=concurrency)
        if diags:
            out[tid] = [d.to_dict() for d in diags]
    return out


def _warnings_summary_from_diagnostics(diagnostics: list[dict]) -> Optional[dict]:
    """Compact card badge summary ``{count, kinds, latest_at, highest_severity}``; None when empty."""
    if not diagnostics:
        return None
    kinds: dict[str, int] = {}
    count = latest = 0
    highest_idx, highest_sev = -1, None
    for d in diagnostics:
        n = d.get("count", 1)
        kinds[d["kind"]] = kinds.get(d["kind"], 0) + n
        count += n
        latest = max(latest, d.get("last_seen_at") or 0)
        sev = d.get("severity")
        if sev in kd.SEVERITY_ORDER and kd.SEVERITY_ORDER.index(sev) > highest_idx:
            highest_idx, highest_sev = kd.SEVERITY_ORDER.index(sev), sev
    return {"count": count, "kinds": kinds, "latest_at": latest, "highest_severity": highest_sev}


def _attach_diagnostics(task_d: dict, diags: Optional[list[dict]], *, include_full: bool = True) -> None:
    """Card badge / attention-strip summary only from ``warning``+ diagnostics -- an ``info``
    diagnostic (e.g. respawn_guarded) must never badge a card or join "needs attention".
    ``include_full`` controls whether the raw ``diagnostics`` list (all severities, consumed by
    the desktop drawer and the dashboard's collectDiagTasks) is included: True for the
    task-detail payload, False for the board payload, since a bare non-empty list there would
    re-trigger the attention strip regardless of ``warnings``."""
    if not diags:
        return
    if include_full:
        task_d["diagnostics"] = diags
    warning_plus = [d for d in diags if kd.severity_at_or_above(d.get("severity"), "warning")]
    task_d["warnings"] = _warnings_summary_from_diagnostics(warning_plus)


def _links_for(conn: sqlite3.Connection, task_id: str) -> dict[str, list[str]]:
    """Return {'parents': [...], 'children': [...]} for a task.

    A parent that is archived AND has satisfied its dependency edge is omitted.
    It can never gate this task again (``_parent_dependency_satisfied`` keys off
    ``completed_at``, which only the completion lifecycle writes) and it is
    absent from every default board view, so the drawer's ``resolveLinks`` can
    only report it as unresolvable -- which ``partitionBlockers`` counts as
    still-gating by design. Keeping it paints a permanent "waiting on blocker"
    banner naming a task the user cannot see or act on.

    Unlike the board payload this filter is not view-scoped: ``GET /tasks/:id``
    takes no ``include_archived``, so it drops what NO default view can resolve
    rather than what one particular view happens to omit. A parent id with no
    task row at all (a genuinely deleted task) is preserved and keeps gating --
    a dangling link is exactly what the user needs to see so they can cut it.
    """
    def _ids(col: str, other: str) -> list[str]:
        return [r[col] for r in conn.execute(f"SELECT {col} FROM task_links WHERE {other} = ? ORDER BY {col}", (task_id,))]
    parents = _ids("parent_id", "child_id")
    if parents:
        rows = conn.execute(
            "SELECT id, status, completed_at FROM tasks WHERE id IN (" + ",".join("?" * len(parents)) + ")",
            parents).fetchall()
        cleared = {
            r["id"] for r in rows
            if r["status"] == "archived" and kanban_db._parent_dependency_satisfied(r)}
        parents = [p for p in parents if p not in cleared]
    return {"parents": parents, "children": _ids("child_id", "parent_id")}


def _unresolvable_satisfied_parents(conn: sqlite3.Connection, visible_ids: set[str]) -> set[str]:
    """Parent ids a board payload lists edges for but cannot render a card for,
    and which provably no longer gate anyone.

    The desktop resolves every edge endpoint against the payload's OWN task
    index (``indexBoard``/``resolveLinks`` in ``apps/desktop/src/plugins/kanban/
    deps.ts``) and treats an id it cannot find as still-gating -- deliberately,
    since the backend link exists and may still be enforced. That default is
    right for a deleted parent and wrong for a completed-then-archived one,
    which the default ``include_archived=False`` fetch omits while its edge
    survives. The result is a card stuck "waiting on a blocker" with no
    resolvable reason until someone unlinks it by hand.

    Only edges satisfying BOTH halves are dropped, so the safety default holds
    everywhere it should: a parent that is merely archived without ever
    completing (withdrawn, not finished) still gates, a parent whose row is gone
    entirely still gates, and a satisfied parent that IS in this payload keeps
    its edge -- that is the "blockers clear" state the desktop renders in green.
    """
    rows = conn.execute(
        "SELECT DISTINCT t.id AS id, t.status AS status, t.completed_at AS completed_at "
        "FROM tasks t JOIN task_links l ON l.parent_id = t.id").fetchall()
    return {
        r["id"] for r in rows
        if r["id"] not in visible_ids and kanban_db._parent_dependency_satisfied(r)}


# --- GET /board -------------------------------------------------------------

def _board_payload(
    conn: sqlite3.Connection, *, tenant: Optional[str], include_archived: bool,
    workflow_template_id: Optional[str], current_step_key: Optional[str],
    board: Optional[str] = None,
) -> dict[str, Any]:
    """Build one board's grouped-by-status payload: link/comment/progress rollups,
    diagnostics, latest summaries, tenant/assignee facets, latest_event_id. This IS
    ``GET /board``'s response shape (byte-for-byte — existing dashboard/desktop clients
    depend on it); ``get_board`` is a thin wrapper and ``GET /board/all`` calls this once
    per board and re-attributes/merges the results, never duplicating the rollup logic.
    """
    tasks = kanban_db.list_tasks(
        conn, tenant=tenant, include_archived=include_archived,
        workflow_template_id=workflow_template_id, current_step_key=current_step_key)
    # Link / comment / progress rollups are each one aggregate query rather than N per-task lookups.
    link_counts: dict[str, dict[str, int]] = {}
    # The same rows are kept as an explicit edge list so the UI can highlight a card's whole
    # dependency chain without N per-task round-trips.
    link_edges: list[list[str]] = []
    # An edge whose parent this payload cannot render, but which no longer gates anyone, is
    # dropped from BOTH rollups: the desktop reads `link_edges` when it has them and falls back
    # to the `link_counts` numbers when it doesn't, so filtering only one of the two would still
    # leave a phantom "blocked by 1" chip on the card. See _unresolvable_satisfied_parents.
    cleared_parents = _unresolvable_satisfied_parents(conn, {t.id for t in tasks})
    for row in conn.execute("SELECT parent_id, child_id FROM task_links").fetchall():
        if row["parent_id"] in cleared_parents:
            continue
        link_counts.setdefault(row["parent_id"], {"parents": 0, "children": 0})["children"] += 1
        link_counts.setdefault(row["child_id"], {"parents": 0, "children": 0})["parents"] += 1
        link_edges.append([row["parent_id"], row["child_id"]])
    # First image attachment per task for the card thumbnail indicator (one aggregate query; the
    # drawer fetches the full attachments list via GET /tasks/:id).
    first_image_attachment: dict[str, int] = {
        r["task_id"]: r["min_id"] for r in conn.execute(
            "SELECT task_id, MIN(id) AS min_id FROM task_attachments "
            "WHERE content_type LIKE 'image/%' GROUP BY task_id")}
    comment_counts: dict[str, int] = {
        r["task_id"]: r["n"] for r in conn.execute("SELECT task_id, COUNT(*) AS n FROM task_comments GROUP BY task_id")}
    progress: dict[str, dict[str, int]] = {}  # per parent: children done / total, rendered as "N/M"
    for row in conn.execute(
        "SELECT l.parent_id AS pid, t.status AS cstatus FROM task_links l JOIN tasks t ON t.id = l.child_id").fetchall():
        p = progress.setdefault(row["pid"], {"done": 0, "total": 0})
        p["total"] += 1
        p["done"] += row["cstatus"] == "done"
    diagnostics_per_task = _compute_task_diagnostics(conn, task_ids=None, board=board)
    latest_event_id = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM task_events").fetchone()["m"]
    columns: dict[str, list[dict]] = {c: [] for c in BOARD_COLUMNS}
    if include_archived:
        columns["archived"] = []
    # One window-function query for latest summaries (avoids N+1); cards get a
    # truncated preview, the full text comes from /tasks/:id.
    summary_map = kanban_db.latest_summaries(conn, [t.id for t in tasks])
    for t in tasks:
        full = summary_map.get(t.id)
        d = _task_dict(t, latest_summary=(full[:_CARD_SUMMARY_PREVIEW_CHARS] if full else None))
        d["link_counts"] = link_counts.get(t.id, {"parents": 0, "children": 0})
        d["comment_count"] = comment_counts.get(t.id, 0)
        d["image_attachment_id"] = first_image_attachment.get(t.id)
        d["progress"] = progress.get(t.id)  # None when the task has no children
        _attach_diagnostics(d, diagnostics_per_task.get(t.id), include_full=False)
        columns[t.status if t.status in columns else "todo"].append(d)

    # Per-column ordering (priority DESC, created_at ASC) comes from list_tasks.
    tenants = [r["tenant"] for r in conn.execute("SELECT DISTINCT tenant FROM tasks WHERE tenant IS NOT NULL ORDER BY tenant")]
    assignees = [r["assignee"] for r in conn.execute(
        "SELECT DISTINCT assignee FROM tasks WHERE assignee IS NOT NULL AND status != 'archived' ORDER BY assignee")]
    return {
        "columns": [{"name": name, "tasks": columns[name]} for name in columns], "tenants": tenants,
        "assignees": assignees, "link_edges": link_edges, "latest_event_id": int(latest_event_id),
        "now": int(time.time())}


@router.get("/board")
def get_board(
    tenant: Optional[str] = Query(None, description="Filter to a single tenant"),
    include_archived: bool = Query(False),
    board: Optional[str] = _BOARD_Q,
    workflow_template_id: Optional[str] = Query(None, description="Restrict to tasks using this workflow template id"),
    current_step_key: Optional[str] = Query(None, description="Restrict to tasks at this workflow step key")):
    """Full board grouped by status column; omitting ``board`` uses the active board
    (``HERMES_KANBAN_BOARD`` env → on-disk ``current`` pointer → ``default``)."""
    with _board_conn(board) as (board, conn):
        return _board_payload(
            conn, tenant=tenant, include_archived=include_archived,
            workflow_template_id=workflow_template_id, current_step_key=current_step_key, board=board)


# --- GET /board/all — consolidated multi-board view --------------------------

def _fetch_board_payload(
    slug: str, *, tenant: Optional[str], include_archived: bool,
    workflow_template_id: Optional[str], current_step_key: Optional[str],
) -> dict[str, Any]:
    """Open *slug* with the board pinned context-locally (``_with_board_pinned`` /
    ``scoped_current_board``), never the process-global ``HERMES_KANBAN_BOARD`` env var —
    concurrent ``/board/all`` requests iterating different boards would cross-write it."""
    def _run() -> dict[str, Any]:
        with closing(_conn(board=slug)) as conn:
            return _board_payload(
                conn, tenant=tenant, include_archived=include_archived,
                workflow_template_id=workflow_template_id, current_step_key=current_step_key, board=slug)
    return _with_board_pinned(slug, _run)


@router.get("/board/all")
def get_all_boards(
    tenant: Optional[str] = Query(None, description="Filter to a single tenant"),
    include_archived: bool = Query(False),
    boards: Optional[str] = Query(None, description="Comma-separated board slugs to restrict to (default: every board)"),
    workflow_template_id: Optional[str] = Query(None, description="Restrict to tasks using this workflow template id"),
    current_step_key: Optional[str] = Query(None, description="Restrict to tasks at this workflow step key")):
    """Cards from every board merged into the standard status columns, each task tagged
    ``board``/``board_name`` (task ids are only unique per board — clients key on the pair).

    A single corrupt/locked board DB must not 500 the whole view: each board is fetched in
    its own try/except, a failure omits that board's tasks and records it in ``errors``
    instead. Ordering within a column keeps each board's own ``priority DESC, created_at
    ASC`` and merges across boards on that same key — board is a tiebreaker, never a
    primary grouping.
    """
    all_meta = kanban_db.list_boards(include_archived=False)
    wanted: Optional[set[str]] = {s.strip() for s in boards.split(",") if s.strip()} if boards else None
    proj_map = _projects_by_id()

    merged_columns: dict[str, list[dict]] = {c: [] for c in BOARD_COLUMNS}
    if include_archived:
        merged_columns["archived"] = []
    board_infos: list[dict[str, Any]] = []
    all_tenants: set[str] = set()
    all_assignees: set[str] = set()
    link_edges: list[dict[str, str]] = []
    cursors: dict[str, int] = {}
    errors: list[dict[str, str]] = []

    for meta in all_meta:
        slug = meta["slug"]
        if wanted is not None and slug not in wanted:
            continue
        display_name = meta.get("name") or slug
        proj = proj_map.get(meta.get("project_id")) if meta.get("project_id") else None
        info: dict[str, Any] = {
            "slug": slug, "name": display_name, "color": meta.get("color") or "",
            "icon": meta.get("icon") or "", "project_name": (proj.name if proj else None), "task_count": 0}
        try:
            payload = _fetch_board_payload(
                slug, tenant=tenant, include_archived=include_archived,
                workflow_template_id=workflow_template_id, current_step_key=current_step_key)
        except Exception as exc:
            log.warning("kanban board/all: board %r failed: %s", slug, exc)
            errors.append({"board": slug, "detail": str(exc)})
            board_infos.append(info)
            continue
        task_count = 0
        for col in payload["columns"]:
            bucket = merged_columns.setdefault(col["name"], [])
            for t in col["tasks"]:
                t["board"] = slug
                t["board_name"] = display_name
                bucket.append(t)
                task_count += 1
        info["task_count"] = task_count
        board_infos.append(info)
        all_tenants.update(payload["tenants"])
        all_assignees.update(payload["assignees"])
        for parent_id, child_id in payload["link_edges"]:
            link_edges.append({"board": slug, "parent": parent_id, "child": child_id})
        cursors[slug] = payload["latest_event_id"]

    # Merge stably on each board's own ordering key; board only breaks a tie because sort()
    # is stable and boards are iterated in list_boards() order, so we never group by board.
    for tasks in merged_columns.values():
        tasks.sort(key=lambda d: (-(d.get("priority") or 0), d.get("created_at") or 0))

    return {
        "columns": [{"name": name, "tasks": merged_columns[name]} for name in merged_columns],
        "boards": board_infos, "tenants": sorted(all_tenants), "assignees": sorted(all_assignees),
        "link_edges": link_edges, "cursors": cursors, "errors": errors, "now": int(time.time())}


# --- Completed-card archive -------------------------------------------------

def _archive_done_scope(board: Optional[str], boards: Optional[str]) -> tuple[dict[str, Any], list[str]]:
    """Resolve the dashboard's existing board scope forms for archive-done.

    A concrete ``board`` keeps the operation on exactly one board. The Desktop
    aggregate uses ``/board/all`` and its sibling fan-out representation
    ``boards=*``; accepting that exact form here avoids inventing another
    aggregate sentinel while making the cross-board effect explicit.
    """
    if board is not None and boards is not None:
        raise HTTPException(status_code=400, detail="pass either board or boards, not both")
    if boards is not None:
        if boards.strip() != "*":
            raise HTTPException(status_code=400, detail="archive-done aggregate scope requires boards=*")
        slugs = [meta["slug"] for meta in kanban_db.list_boards(include_archived=False)]
        return {"kind": "all_boards", "label": "All Boards"}, slugs

    slug = _resolve_board(board) or kanban_db.get_current_board()
    meta = next((item for item in kanban_db.list_boards(include_archived=False) if item["slug"] == slug), None)
    return {"kind": "board", "board": slug, "label": (meta or {}).get("name") or slug}, [slug]


def _done_task_count(slugs: list[str]) -> int:
    total = 0
    for slug in slugs:
        with closing(_conn(board=slug)) as conn:
            total += int(conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status = 'done'").fetchone()["n"])
    return total


@router.get("/tasks/archive-done/preflight")
def archive_done_preflight(board: Optional[str] = _BOARD_Q, boards: Optional[str] = Query(None)):
    """Count completed cards for the selected board or explicit ``boards=*`` aggregate."""
    scope, slugs = _archive_done_scope(board, boards)
    return {"scope": scope, "done_count": _done_task_count(slugs)}


@router.post("/tasks/archive-done")
def archive_done_tasks(board: Optional[str] = _BOARD_Q, boards: Optional[str] = Query(None)):
    """Archive cards that are still ``done`` when each per-card write executes.

    Each card delegates to :func:`kanban_db.archive_task` so the established
    archive event, run cleanup, descendant recomputation, and workspace cleanup
    semantics remain intact. Failures are isolated to their card and returned
    for a partial-result toast rather than rolling back successful archives.
    """
    scope, slugs = _archive_done_scope(board, boards)
    archived_count = skipped_count = 0
    failures: list[dict[str, str]] = []
    candidate_count = 0
    for slug in slugs:
        with closing(_conn(board=slug)) as conn:
            task_ids = [row["id"] for row in conn.execute("SELECT id FROM tasks WHERE status = 'done'").fetchall()]
            candidate_count += len(task_ids)
            for task_id in task_ids:
                try:
                    if kanban_db.archive_task(conn, task_id, expected_status="done"):
                        archived_count += 1
                    else:
                        skipped_count += 1
                except Exception as exc:
                    failures.append({"board": slug, "task_id": task_id, "error": str(exc)})
    return {
        "scope": scope,
        "boards": slugs,
        "candidate_count": candidate_count,
        "archived_count": archived_count,
        "skipped_count": skipped_count,
        "failures": failures,
    }


# --- GET /tasks/:id ---------------------------------------------------------

@router.get("/tasks/{task_id}")
def get_task(
    task_id: str,
    board: Optional[str] = Query(None),
    run_state_type: Optional[str] = Query(None, description="With run_state_name: filter runs by column 'status' or 'outcome'"),
    run_state_name: Optional[str] = Query(None, description="With run_state_type: exact value for that run column")):
    with _board_conn(board) as (board, conn):
        if (run_state_type is None) ^ (run_state_name is None):
            raise HTTPException(status_code=400, detail="run_state_type and run_state_name must be passed together or omitted")
        if run_state_type not in (None, "status", "outcome"):
            raise HTTPException(status_code=400, detail="run_state_type must be 'status' or 'outcome'")
        task = _require_task(conn, task_id)
        # Drawer returns the FULL summary (cards on /board carry a 200-char preview).
        task_d = _task_dict(task, latest_summary=kanban_db.latest_summary(conn, task_id))
        links = _links_for(conn, task_id)
        child_summaries = kanban_db.latest_summaries(conn, links["children"])
        children = filter(None, (kanban_db.get_task(conn, cid) for cid in links["children"]))
        _attach_diagnostics(task_d, _compute_task_diagnostics(conn, task_ids=[task_id], board=board).get(task_id) or [])
        return {
            "task": task_d,
            "comments": [asdict(c) for c in kanban_db.list_comments(conn, task_id)],
            "events": [asdict(e) for e in kanban_db.list_events(conn, task_id)],
            "attachments": [_attachment_dict(a) for a in kanban_db.list_attachments(conn, task_id)],
            "links": links,
            "child_results": [
                {"id": c.id, "title": c.title, "status": c.status, "latest_summary": child_summaries.get(c.id), "result": c.result}
                for c in children],
            "runs": [asdict(r) for r in kanban_db.list_runs(conn, task_id, state_type=run_state_type, state_name=run_state_name)]}


# --- POST /tasks ------------------------------------------------------------

class CreateTaskBody(BaseModel):
    title: str
    body: Optional[str] = None
    assignee: Optional[str] = None
    tenant: Optional[str] = None
    priority: int = 0
    workspace_kind: str = "scratch"
    workspace_path: Optional[str] = None
    parents: list[str] = Field(default_factory=list)
    triage: bool = False
    idempotency_key: Optional[str] = None
    max_runtime_seconds: Optional[int] = None
    skills: Optional[list[str]] = None
    goal_mode: bool = False
    goal_max_turns: Optional[int] = None
    model_override: Optional[str] = None
    provider_override: Optional[str] = None
    reasoning_effort: Optional[str] = None  # none|minimal|…|ultra; None inherits the profile's level
    policy_force: bool = False
    policy_force_reason: Optional[str] = None
    project_id: Optional[str] = None  # None inherits the board's scoped project (if any)
    # Tokens from POST /attachments/staged (pasted images uploaded before this task existed, e.g.
    # the "new task" dialog); promoted into real task_attachments rows after creation. Defaults to
    # [] so older dashboard builds that never send this field are unaffected.
    pending_attachment_tokens: list[str] = Field(default_factory=list)


@router.post("/tasks")
def create_task(payload: CreateTaskBody, board: Optional[str] = Query(None)):
    with _board_conn(board) as (board, conn), _value_error_400():
        # Keep established explicit-override validation ahead of the idempotent
        # fast path, which otherwise skips the DB create validation entirely.
        kanban_db._validate_model_override(payload.model_override, payload.provider_override)
        kanban_db.normalize_reasoning_effort(payload.reasoning_effort)
        # An idempotent replay must return its existing route without consuming
        # another classifier invocation.
        existing = kanban_db.get_task_by_idempotency_key(conn, payload.idempotency_key)
        if existing is not None:
            return {
                "task": _task_dict(existing),
                "attachments": [],
                "attachment_warnings": [],
            }
        # CreateTaskBody field names match create_task's keyword parameters.
        from hermes_cli.kanban_model_routing import resolve_kanban_model_route

        routing = resolve_kanban_model_route(
            title=payload.title, body=payload.body,
            explicit_model=payload.model_override, explicit_provider=payload.provider_override,
            explicit_reasoning_effort=payload.reasoning_effort,
        )
        create_kwargs = payload.model_dump(exclude={"pending_attachment_tokens"})
        create_kwargs.update(
            model_override=routing.model_override,
            provider_override=routing.provider_override,
            reasoning_effort=routing.reasoning_effort,
            route_source=routing.route_source,
            route_name=routing.route_name,
            policy_forced_by=(kanban_db._hook_profile_name() if payload.policy_force else None),
        )
        task_id = kanban_db.create_task(
            conn, created_by="dashboard", board=board, **create_kwargs)
        task = kanban_db.get_task(conn, task_id)
        body: dict[str, Any] = {"task": _task_dict(task) if task else None}
        # Promote pasted-image attachments staged before the task existed; a stale/unknown token
        # is a warning, never a task-creation failure.
        attachments: list[dict[str, Any]] = []
        attachment_warnings: list[str] = []
        if payload.pending_attachment_tokens:
            promoted, attachment_warnings = kanban_db.promote_staged_attachments(
                conn, task_id, payload.pending_attachment_tokens, uploaded_by="dashboard", board=board)
            attachments = [_attachment_dict(a) for a in promoted]
        body["attachments"] = attachments
        body["attachment_warnings"] = attachment_warnings
        # Dispatcher-presence warning so the UI can banner a ready+assigned task that would
        # otherwise sit idle (no gateway / dispatch_in_gateway=false); triage/todo are expected
        # to wait, unassigned tasks can't dispatch anyway. Probe the request's active home: the
        # dashboard backend may run under a different HERMES_HOME than the board's profile.
        if task and task.status == "ready" and task.assignee:
            try:
                from hermes_cli.kanban import _check_dispatcher_presence
                from hermes_constants import get_hermes_home
                running, message = _check_dispatcher_presence(hermes_home=get_hermes_home())
                if not running and message:
                    body["warning"] = message
            except Exception:
                pass  # probe failure must never block the create itself
        return body


# --- PATCH /tasks/:id  and  POST /tasks/bulk ---------------------------------

class UpdateTaskBody(BaseModel):
    status: Optional[str] = None
    assignee: Optional[str] = None
    priority: Optional[int] = None
    title: Optional[str] = None
    body: Optional[str] = None
    result: Optional[str] = None
    block_reason: Optional[str] = None
    # Handoff fields forwarded to complete_task on -> 'done' (parity with ``hermes kanban complete``).
    summary: Optional[str] = None
    metadata: Optional[dict] = None
    # In a PATCH ``None`` means "field not sent", so ``clear_*=True`` is the explicit clear signal.
    # ``reasoning_effort="none"`` is a VALUE (thinking off); it is cleared separately so
    # dropping a model override doesn't silently reset the depth.
    model_override: Optional[str] = None
    provider_override: Optional[str] = None
    clear_model_override: bool = False
    reasoning_effort: Optional[str] = None
    clear_reasoning_effort: bool = False
    policy_force: bool = False
    policy_force_reason: Optional[str] = None
    # Explicit second gesture for a card the unblock-loop breaker parked in triage on an
    # unanswered ``needs_input`` question. Absent (False) is what an ordinary drag sends, so
    # the guard is on by default and only a deliberate confirmation clears it.
    acknowledge_block_loop: bool = False


class BulkTaskBody(BaseModel):
    ids: list[str]
    status: Optional[str] = None
    assignee: Optional[str] = None  # "" or None = unassign
    priority: Optional[int] = None
    archive: bool = False
    result: Optional[str] = None
    summary: Optional[str] = None
    metadata: Optional[dict] = None
    reclaim_first: bool = False
    # Same semantics as UpdateTaskBody.
    model_override: Optional[str] = None
    provider_override: Optional[str] = None
    clear_model_override: bool = False
    reasoning_effort: Optional[str] = None
    clear_reasoning_effort: bool = False
    policy_force: bool = False
    policy_force_reason: Optional[str] = None
    acknowledge_block_loop: bool = False


class _StatusRejected(Exception):
    """A status the dashboard may not set via this path; the message is user-facing."""


class _BlockLoopAckRequired(Exception):
    """A loop-broken triage card needs an explicit acknowledgment, not an ordinary drag.

    Surfaced as 409 (not 400): the request is well-formed, the card's *state* is what
    refuses it, and re-sending with ``acknowledge_block_loop`` resolves it.
    """


_RUNNING_DIRECT_MSG = "Cannot set status to 'running' directly; use the dispatcher/claim path"

# Statuses that put a card back into the dispatcher's reach. ``todo`` is here with ``ready``
# on purpose: ``recompute_ready()`` promotes any parent-satisfied ``todo`` card to ``ready``
# on the next tick, so guarding only ``ready`` would be bypassed by dropping the card one
# lane to the left.
_WORK_QUEUE_STATUSES = frozenset({"ready", "todo"})

_BLOCK_LOOP_ACK_MSG = (
    "This card was parked in triage by the unblock-loop breaker after re-blocking on the same "
    "unresolved question. Moving it back into the work queue without answering that question "
    "restarts the loop. Answer it in a comment first, then confirm the move (the dashboard asks "
    "for confirmation; API clients re-send with acknowledge_block_loop=true)."
)


def _is_block_loop_parked(
    status: Optional[str], block_kind: Optional[str], block_recurrences: Optional[int],
) -> bool:
    """Is this card sitting in ``triage`` *because the loop breaker put it there* for an
    unresolved human decision?

    Scoped to ``needs_input`` exactly as ``kanban_specify``/``kanban_decompose``'s sweep
    exclusion is: a ``capability``/``transient`` loop is a real scope problem that
    re-specifying may genuinely fix, so those stay ordinary triage cards. Takes primitives
    so the same predicate serves both a ``Task`` dataclass (board payload) and a raw
    ``sqlite3.Row`` (the write path) — one definition of "what counts".
    """
    return (
        status == "triage"
        and block_kind == "needs_input"
        and int(block_recurrences or 0) >= kanban_db.BLOCK_RECURRENCE_LIMIT
    )


def _drag_to(conn, task_id: str, s: str, *, acknowledge_block_loop: bool = False) -> bool:
    """Drag-drop into ready/todo/triage: archived cards use the explicit,
    evented unarchive verb; blocked/scheduled -> ready re-opens via
    ``unblock_task``; leaving ``review`` goes through ``reopen_review_task``
    (stale-run recovery, parent re-gate, ``review_reopened`` event) instead of
    a raw write; a ``roadmap`` card being dragged into the work queue is an
    authorization, so it goes through ``spawn_roadmap_task`` for the
    ``spawned_from_roadmap`` event (and ``idea`` is refused there — an idea must be
    refined first, which the DB layer states in its ValueError)."""
    current = kanban_db.get_task(conn, task_id)
    if current is not None and current.status == "archived":
        return kanban_db.unarchive_task(conn, task_id, status=s)
    if current is not None and current.status in kanban_db.ROADMAP_LANE_STATUSES:
        return kanban_db.spawn_roadmap_task(conn, task_id, to=s)
    if s == "ready" and current and current.status in ("blocked", "scheduled"):
        return kanban_db.unblock_task(conn, task_id)
    if s == "ready" and current and current.status == "on_hold":
        return kanban_db.unhold_task(conn, task_id)
    if current is not None and current.status == "review":
        return kanban_db.reopen_review_task(conn, task_id)
    return _set_status_direct(conn, task_id, s, acknowledge_block_loop=acknowledge_block_loop)


def _drag_to_lane(conn, task_id: str, lane: str) -> bool:
    """Drag-drop INTO a wishlist lane. Only the two intra-lane moves exist (``idea -> roadmap``
    refine, ``roadmap -> idea`` demote); dragging live work into the wishlist raises ValueError
    from the DB layer and surfaces as a 400 naming the attempted from->to."""
    return (kanban_db.refine_task(conn, task_id) if lane == "roadmap"
            else kanban_db.demote_task(conn, task_id))


# Status verb dispatch shared by PATCH /tasks/{id} and POST /tasks/bulk: (conn, task_id,
# payload) -> ok. ``review`` uses request_review (never a block, so it can't trip unblock-loop
# detection) with ``force=True``: a dashboard action is a human override of a live worker claim.
_STATUS_HANDLERS: dict[str, Any] = {
    "done": lambda conn, tid, p: kanban_db.complete_task(conn, tid, result=p.result, summary=p.summary, metadata=p.metadata),
    "blocked": lambda conn, tid, p: kanban_db.block_task(conn, tid, reason=getattr(p, "block_reason", None)),
    "scheduled": lambda conn, tid, p: kanban_db.schedule_task(conn, tid, reason=getattr(p, "block_reason", None)),
    "on_hold": lambda conn, tid, p: kanban_db.hold_task(conn, tid, reason=getattr(p, "block_reason", None)),
    "review": lambda conn, tid, p: kanban_db.request_review(
        conn, tid, summary=p.summary, metadata=p.metadata, reviewer=(p.assignee or None), force=True),
    "ready": lambda conn, tid, p: _drag_to(
        conn, tid, "ready", acknowledge_block_loop=getattr(p, "acknowledge_block_loop", False)),
    "todo": lambda conn, tid, p: _drag_to(
        conn, tid, "todo", acknowledge_block_loop=getattr(p, "acknowledge_block_loop", False)),
    "triage": lambda conn, tid, p: _drag_to(conn, tid, "triage"),
    "idea": lambda conn, tid, p: _drag_to_lane(conn, tid, "idea"),
    "roadmap": lambda conn, tid, p: _drag_to_lane(conn, tid, "roadmap")}


def _apply_status(conn, task_id: str, s: str, p, unknown_detail: str) -> bool:
    """Dispatch a status verb; raises ``_StatusRejected`` (user-facing message)
    for ``running`` or an unknown status (``unknown_detail``)."""
    if s == "running":
        raise _StatusRejected(_RUNNING_DIRECT_MSG)
    handler = _STATUS_HANDLERS.get(s)
    if handler is None:
        raise _StatusRejected(unknown_detail)
    return handler(conn, task_id, p)


def _set_priority(conn, task_id: str, priority: int, board: Optional[str]) -> None:
    with kanban_db.write_txn(conn):
        conn.execute("UPDATE tasks SET priority = ? WHERE id = ?", (int(priority), task_id))
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'reprioritized', ?, ?)",
            (task_id, json.dumps({"priority": int(priority)}), int(time.time())))
    # Mutation-boundary observer (post-commit): this direct-SQL write bypasses every kanban_db mutator.
    kanban_db.notify_task_updated(conn, task_id, ("priority",), board=board)


def _apply_model_override(conn, task_id: str, p, board: Optional[str] = None) -> bool:
    """Raises ValueError/RuntimeError from kanban_db for the caller to map."""
    new_model = None if p.clear_model_override else (p.model_override or "").strip() or None
    return kanban_db.set_model_override(
        conn, task_id, new_model, provider=p.provider_override,
        policy_force=p.policy_force, policy_force_reason=p.policy_force_reason,
        policy_forced_by=(kanban_db._hook_profile_name() if p.policy_force else None), board=board,
    )


def _apply_reasoning_effort(conn, task_id: str, p, board: Optional[str] = None) -> bool:
    return kanban_db.set_reasoning_effort(
        conn, task_id, None if p.clear_reasoning_effort else p.reasoning_effort,
        policy_force=p.policy_force, policy_force_reason=p.policy_force_reason,
        policy_forced_by=(kanban_db._hook_profile_name() if p.policy_force else None), board=board,
    )


def _apply_combined_route(conn, task_id: str, p, board: Optional[str] = None) -> bool:
    model = None if p.clear_model_override else (p.model_override or "").strip() or None
    effort = None if p.clear_reasoning_effort else p.reasoning_effort
    return kanban_db.set_route_overrides(
        conn, task_id, model=model, provider=p.provider_override,
        reasoning_effort=effort, policy_force=p.policy_force,
        policy_force_reason=p.policy_force_reason, policy_forced_by=(kanban_db._hook_profile_name() if p.policy_force else None),
        board=board,
    )


# Override knobs shared by PATCH and bulk: (payload wants it?, apply, bulk refusal message).
_OVERRIDE_OPS = (
    (lambda p: p.clear_model_override or p.model_override is not None, _apply_model_override, "model override refused"),
    (lambda p: p.clear_reasoning_effort or p.reasoning_effort is not None, _apply_reasoning_effort, "reasoning override refused"),
)


def _patch_status(conn, task_id: str, payload: UpdateTaskBody, review_assignee_deferred: bool) -> None:
    """PATCH status phase: 400 on a rejected verb, 409 when the transition is refused
    (naming the blocking parent(s) for ``ready`` so the UI renders an actionable toast)."""
    s = payload.status
    if s == "archived":
        ok = kanban_db.archive_task(conn, task_id)
    else:
        # ValueError is the roadmap-lane layer refusing a transition; its message names the
        # attempted from->to, which is exactly what the UI toast should say, so surface it as a
        # 400 rather than letting it fall through to the generic 409.
        # _BlockLoopAckRequired is a 409 instead: the payload is valid and the card's state is
        # what refuses, so re-sending WITH the acknowledgment is the resolution.
        with _map_errors(409, _BlockLoopAckRequired), _map_errors(400, _StatusRejected, ValueError):
            ok = _apply_status(conn, task_id, s, payload, f"unknown status: {s}")
        if s == "review" and ok and review_assignee_deferred and not payload.assignee:
            ok = kanban_db.assign_task(conn, task_id, None)
    if ok:
        return
    blockers = _parents_blocking_ready(conn, task_id) if s == "ready" else []
    if blockers:
        names = ", ".join(f"{p['title']!r} ({p['id']}, status={p['status']})" for p in blockers)
        raise _conflict(f"Cannot move to 'ready': blocked by parent(s) not done — {names}")
    raise _conflict(f"status transition to {s!r} not valid from current state")


def _patch_title_body(conn, task_id: str, payload: UpdateTaskBody, board: Optional[str]) -> None:
    """PATCH title/body phase: one UPDATE + ``edited`` event, then the post-commit observer
    (field names only — values never leave the DB via this payload)."""
    with kanban_db.write_txn(conn):
        sets, vals = [], []
        if payload.title is not None:
            if not payload.title.strip():
                raise HTTPException(status_code=400, detail="title cannot be empty")
            sets.append("title = ?")
            vals.append(payload.title.strip())
        if payload.body is not None:
            sets.append("body = ?")
            vals.append(payload.body)
        vals.append(task_id)
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals)
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'edited', NULL, ?)",
            (task_id, int(time.time())))
    kanban_db.notify_task_updated(
        conn, task_id, [f for f in ("title", "body") if getattr(payload, f) is not None], board=board)


@router.patch("/tasks/{task_id}")
def update_task(task_id: str, payload: UpdateTaskBody, board: Optional[str] = Query(None)):
    route_update_requested = any(wanted(payload) for wanted, _apply, _msg in _OVERRIDE_OPS)
    if payload.assignee is not None and route_update_requested:
        raise HTTPException(
            status_code=400,
            detail="assignee and model-route changes must be submitted as separate policy-checked updates",
        )
    with _board_conn(board) as (board, conn):
        _require_task(conn, task_id)
        # For a combined assignee+review patch, request_review must capture the
        # current implementer before the task is routed to the reviewer.
        review_assignee_deferred = payload.status == "review" and payload.assignee is not None
        if payload.assignee is not None and not review_assignee_deferred:
            # ValueError -> 400: the assignee-profile skill preflight refuses a
            # reassignment that would guarantee a worker init crash.
            with _map_errors(409, RuntimeError), _map_errors(400, ValueError):
                _require_ok(kanban_db.assign_task(conn, task_id, payload.assignee or None))
        wanted_model, wanted_effort = (_OVERRIDE_OPS[0][0](payload), _OVERRIDE_OPS[1][0](payload))
        if wanted_model and wanted_effort:
            with _map_errors(400, ValueError, RuntimeError):
                _require_ok(_apply_combined_route(conn, task_id, payload, board))
        else:
            for wanted, apply, _refused in _OVERRIDE_OPS:
                if wanted(payload):
                    with _map_errors(400, ValueError, RuntimeError):
                        ok = apply(conn, task_id, payload, board)
                    _require_ok(ok)
        if payload.status is not None:
            _patch_status(conn, task_id, payload, review_assignee_deferred)
        if payload.priority is not None:
            _set_priority(conn, task_id, payload.priority, board)
        if payload.title is not None or payload.body is not None:
            _patch_title_body(conn, task_id, payload, board)
        updated = kanban_db.get_task(conn, task_id)
        return {"task": _task_dict(updated) if updated else None}


@router.delete("/tasks/{task_id}")
def delete_task(task_id: str, board: Optional[str] = Query(None)):
    with _board_conn(board) as (board, conn):
        with _map_errors(409, RuntimeError):
            deleted = kanban_db.delete_task(conn, task_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        return {"deleted": True, "task_id": task_id}


def _parents_blocking_ready(conn: sqlite3.Connection, task_id: str) -> list:
    """Unsatisfied parent rows that block promotion to ``ready``.

    Used to enrich the 409 response from :func:`update_task` so the dashboard can show an actionable toast
    (#26744) instead of a silent no-op. Returns ``[]`` when nothing blocks the transition (e.g. no parents,
    or all parents have satisfied their dependency edges).
    """
    rows = conn.execute(
        "SELECT t.id, t.title, t.status, t.completed_at FROM tasks t "
        "JOIN task_links l ON l.parent_id = t.id "
        "WHERE l.child_id = ?",
        (task_id,)).fetchall()
    return [
        {"id": r["id"], "title": r["title"], "status": r["status"]}
        for r in rows if not kanban_db._parent_dependency_satisfied(r)
    ]


def _set_status_direct(
    conn: sqlite3.Connection, task_id: str, new_status: str, *, acknowledge_block_loop: bool = False,
) -> bool:
    """Direct status write for drag-drop moves without a structured verb (todo<->ready,
    running<->ready) + a ``status`` event. Leaving ``running`` closes the run as 'reclaimed'
    so attempt history isn't orphaned; the worker is killed only AFTER the txn commits.

    One state refuses this path outright: a card the unblock-loop breaker parked in
    ``triage`` for an unanswered ``needs_input`` question. Every other exit from a
    loop-broken state is a dedicated verb that a human chose deliberately, while this one
    is reachable by an ordinary drag gesture — which is how a live board re-armed the same
    loop three times in ~70 minutes. ``acknowledge_block_loop`` is the deliberate override
    and is recorded as its own event; the guard lives here rather than only in
    :func:`_drag_to` so any future caller of this raw write inherits it.
    """
    terminations: list[tuple[Optional[int], Optional[str], Optional[str]]] = []
    effective_status = new_status
    ack_recorded = False
    with kanban_db.write_txn(conn):
        prev = conn.execute(
            "SELECT status, current_run_id, worker_pid, claim_lock, worker_unit, "
            "block_kind, block_recurrences FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if prev is None:
            return False
        # Archived is a one-way door from this path: leaving it goes through the explicit
        # kanban_db.archive_task()/unarchive verb only, never a bare drag-drop status write.
        if prev["status"] == "archived":
            return False
        if new_status in _WORK_QUEUE_STATUSES and _is_block_loop_parked(
            prev["status"], prev["block_kind"], prev["block_recurrences"],
        ):
            if not acknowledge_block_loop:
                raise _BlockLoopAckRequired(_BLOCK_LOOP_ACK_MSG)
            ack_recorded = True
        if prev["status"] == "running" and new_status == "ready":
            resume_status = kanban_db._retry_status_for_run(conn, task_id, prev["current_run_id"])
            if resume_status == "review":
                effective_status = "review" if kanban_db._parents_satisfied(conn, task_id) else "todo"
        # Never promote to 'ready' unless all parents are done/archived — otherwise the
        # dispatcher spawns a child whose upstream work hasn't completed.
        if effective_status == "ready" and not kanban_db._parents_satisfied(conn, task_id):
            return False
        was_running = prev["status"] == "running"
        reopening_satisfied_parent = prev["status"] in {"done", "archived"} and effective_status not in {"done", "archived"}
        cur = conn.execute(
            "UPDATE tasks SET status = ?, "
            "  completed_at = CASE WHEN ? IN ('done', 'archived') THEN completed_at ELSE NULL END, "
            "  claim_lock = CASE WHEN ? = 'running' THEN claim_lock ELSE NULL END, "
            "  claim_expires = CASE WHEN ? = 'running' THEN claim_expires ELSE NULL END, "
            "  worker_pid = CASE WHEN ? = 'running' THEN worker_pid ELSE NULL END "
            # Defense-in-depth: the archived precondition above already returns before this
            # point, but the WHERE clause independently blocks the CAS if that check is ever
            # bypassed or refactored around.
            "WHERE id = ? AND status != 'archived'",
            (effective_status,) * 5 + (task_id,))
        if cur.rowcount != 1:
            return False
        run_id = None
        if was_running and effective_status != "running" and prev["current_run_id"]:
            run_id = kanban_db._end_run(
                conn, task_id, outcome="reclaimed", status="reclaimed",
                summary=f"status changed to {effective_status} (dashboard/direct)")
            terminations.append((prev["worker_pid"], prev["claim_lock"], prev["worker_unit"]))
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, 'status', ?, ?)",
            (task_id, run_id, json.dumps({"status": effective_status, "requested_status": new_status}), int(time.time())))
        if ack_recorded:
            # Audit trail for the override, written in the SAME txn as the move it
            # authorizes so the two can never disagree. ``block_kind`` /
            # ``block_recurrences`` are deliberately NOT reset (mirroring
            # ``unblock_task``): an acknowledgment resumes the card, it does not forgive
            # its loop history, so a re-block still trips the breaker at the same count.
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, ?, 'block_loop_ack', ?, ?)",
                (task_id, run_id,
                 json.dumps({"status": effective_status, "requested_status": new_status,
                             "block_kind": prev["block_kind"],
                             "recurrences": int(prev["block_recurrences"] or 0)}),
                 int(time.time())))
        if reopening_satisfied_parent:
            # Domain-layer invalidation composes via a savepoint inside our txn and hands
            # back worker terminations to perform post-commit.
            result = kanban_db.invalidate_descendants_for_parent_reopen(conn, task_id, author="dashboard")
            terminations.extend(result["terminations"])
    for pid, claim_lock, worker_unit in terminations:
        kanban_db._terminate_reclaimed_worker(pid, claim_lock, worker_unit=worker_unit)
    # Re-opening something may have made children stale.
    if effective_status in {"done", "ready", "review"}:
        kanban_db.recompute_ready(conn)
    return True


# --- Comments / links -------------------------------------------------------

class ChoiceResponse(BaseModel):
    """Structured multiple-choice answer submitted alongside a comment
    (docs/design/blocked-callout-multiple-choice-spec.md). ``question_event_id`` must reference an
    existing ``task_events`` row on the same task — enforced in ``kanban_db.add_comment``."""

    key: str
    label: str
    question_event_id: int


class CommentBody(BaseModel):
    body: str
    author: Optional[str] = "dashboard"
    choice: Optional[ChoiceResponse] = None


@router.post("/tasks/{task_id}/comments")
def add_comment(task_id: str, payload: CommentBody, board: Optional[str] = Query(None)):
    if not payload.body.strip():
        raise HTTPException(status_code=400, detail="body is required")
    with _board_conn(board) as (board, conn):
        _require_task(conn, task_id)
        with _map_errors(422, ValueError):
            kanban_db.add_comment(
                conn, task_id, author=payload.author or "dashboard", body=payload.body,
                choice=(payload.choice.model_dump() if payload.choice is not None else None))
        return {"ok": True}


class LinkBody(BaseModel):
    parent_id: str
    child_id: str


@router.post("/links")
def add_link(payload: LinkBody, board: Optional[str] = Query(None)):
    with _board_conn(board) as (board, conn), _value_error_400():
        kanban_db.link_tasks(conn, payload.parent_id, payload.child_id)
        return {"ok": True}


@router.delete("/links")
def delete_link(parent_id: str = Query(...), child_id: str = Query(...), board: Optional[str] = Query(None)):
    with _board_conn(board) as (board, conn):
        return {"ok": bool(kanban_db.unlink_tasks(conn, parent_id, child_id))}


def _bulk_apply_one(conn, tid: str, payload: BulkTaskBody, board: Optional[str], entry: dict) -> None:
    """Apply the bulk patch to one task, recording refusals in ``entry`` without aborting the
    remaining ops — except a rejected status verb (``_StatusRejected`` propagates)."""
    wanted_model, wanted_effort = (_OVERRIDE_OPS[0][0](payload), _OVERRIDE_OPS[1][0](payload))
    if payload.assignee is not None and (wanted_model or wanted_effort):
        entry.update(
            ok=False,
            error="assignee and model-route changes require separate policy-checked updates",
        )
        return
    if wanted_model and wanted_effort:
        try:
            if not _apply_combined_route(conn, tid, payload, board):
                entry.update(ok=False, error="route override refused")
        except (RuntimeError, ValueError) as e:
            entry.update(ok=False, error=str(e))
            return
    else:
        for wanted, apply, refused in _OVERRIDE_OPS:
            if wanted(payload):
                try:
                    if not apply(conn, tid, payload, board):
                        entry.update(ok=False, error=refused)
                        return
                except (RuntimeError, ValueError) as e:
                    entry.update(ok=False, error=str(e))
                    return
    if payload.archive and not kanban_db.archive_task(conn, tid):
        entry.update(ok=False, error="archive refused")
    if payload.status is not None and not payload.archive:
        s = payload.status
        try:
            if not _apply_status(conn, tid, s, payload, f"unknown status {s!r}"):
                entry.update(ok=False, error=f"transition to {s!r} refused")
        except (ValueError, _BlockLoopAckRequired) as exc:
            # Roadmap-lane refusal or a loop-broken card needing acknowledgment: record the
            # message per task, matching how every other per-task refusal in this bulk loop
            # is reported instead of aborting the batch.
            entry.update(ok=False, error=str(exc))
    if payload.assignee is not None:
        try:
            ok = (kanban_db.reassign_task(conn, tid, payload.assignee or None, reclaim_first=True) if payload.reclaim_first
                  else kanban_db.assign_task(conn, tid, payload.assignee or None))
            if not ok:
                entry.update(ok=False, error="assign refused")
        except (RuntimeError, ValueError) as e:
            entry.update(ok=False, error=str(e))
    if payload.priority is not None:
        _set_priority(conn, tid, payload.priority, board)



@router.post("/tasks/bulk")
def bulk_update(payload: BulkTaskBody, board: Optional[str] = Query(None)):
    """Apply the same patch to every id. Independent iteration — per-task
    failures don't abort siblings; returns per-id outcome for partials."""
    ids = [i for i in (payload.ids or []) if i]
    if not ids:
        raise HTTPException(status_code=400, detail="ids is required")
    results: list[dict] = []
    with _board_conn(board) as (board, conn):
        for tid in ids:
            entry: dict[str, Any] = {"id": tid, "ok": True}
            try:
                if kanban_db.get_task(conn, tid) is None:
                    entry.update(ok=False, error="not found")
                else:
                    _bulk_apply_one(conn, tid, payload, board, entry)
            except Exception as e:  # one bad id shouldn't kill the batch (incl. _StatusRejected)
                entry.update(ok=False, error=str(e))
            results.append(entry)
        return {"results": results}


# --- Diagnostics — fleet-wide distress signals (see kanban_diagnostics) ------

@router.get("/diagnostics")
def list_diagnostics(
    board: Optional[str] = _BOARD_Q,
    severity: Optional[str] = Query(None, description="Filter by severity: info|warning|error|critical")):
    """Tasks with an active diagnostic, highest severity first then most recent; also
    consumed by ``hermes kanban diagnostics`` when the dashboard runs."""
    with _board_conn(board) as (board, conn):
        diags_by_task = _compute_task_diagnostics(conn, task_ids=None, board=board)
        if severity and diags_by_task:
            diags_by_task = {
                tid: keep
                for tid, dl in diags_by_task.items()
                if (keep := [d for d in dl if kd.severity_at_or_above(d.get("severity"), severity)])}
        if not diags_by_task:
            return {"diagnostics": [], "count": 0}
        ids = list(diags_by_task.keys())
        rows = {r["id"]: r for r in conn.execute(
            f"SELECT id, title, status, assignee FROM tasks WHERE id IN ({_placeholders(ids)})", tuple(ids)).fetchall()}
        out = []
        for tid, dl in diags_by_task.items():
            r = rows.get(tid) or {"title": None, "status": None, "assignee": None}
            out.append({
                "task_id": tid, "task_title": r["title"], "task_status": r["status"], "task_assignee": r["assignee"],
                "diagnostics": dl})
        sev_idx = {s: i for i, s in enumerate(kd.SEVERITY_ORDER)}
        out.sort(key=lambda row: (
            -sev_idx.get(row["diagnostics"][0].get("severity"), -1), -(row["diagnostics"][0].get("last_seen_at") or 0)))
        return {"diagnostics": out, "count": sum(len(d["diagnostics"]) for d in out)}



# --- POST /roadmap/idea — capture a free-typed roadmap idea as an ``idea`` card on the resolved
# board. Previously this appended to the roadmap-sync plugin's markdown "## Ideas" inbox; the
# board is now the system of record for the wishlist (the inert ``idea`` lane), and the roadmap
# document is rendered FROM those cards. The response shape is unchanged so the shipped Desktop
# callers (api.ts ``addRoadmapIdea``, IdeaCaptureDialog, the per-card "send to roadmap ideas"
# action) keep working without a client change.

# Bound on captured text. Kept at the markdown inbox's old cap so an oversized paste still gets a
# clean 400 instead of landing a wall of text as a card title.
_ROADMAP_IDEA_MAX_LEN = 300


class RoadmapIdeaBody(BaseModel):
    text: str
    # Optional provenance when captured from an existing card. Validated against the CANONICAL
    # kanban task-id shape (``"t_" + 8 lowercase hex``) so provenance on a value that didn't come
    # from the board is rejected with a 422.
    source_id: Optional[str] = Field(default=None, pattern=r"^t_[0-9a-f]{8}$")


@router.post("/roadmap/idea")
def append_roadmap_idea(payload: RoadmapIdeaBody, board: Optional[str] = Query(None)):
    """Capture one idea as an ``idea`` card on the active board. Never a 5xx (fail-open):
    ``{"ok": true}`` or ``{"ok": false, "reason"}``. ``roadmap_unavailable`` now means only
    "no board could be resolved". Empty text is rejected here (the card title cannot be blank);
    the length pre-check stays a real 400 so a megabyte paste never reaches the DB."""
    text = (payload.text or "").strip()
    if len(payload.text or "") > _ROADMAP_IDEA_MAX_LEN:
        raise HTTPException(status_code=400, detail=f"idea text exceeds {_ROADMAP_IDEA_MAX_LEN} characters")
    if not text:
        return {"ok": False, "reason": "empty_idea"}
    slug = _resolve_board(board) or kanban_db.get_current_board()
    if not slug:
        return {"ok": False, "reason": "roadmap_unavailable"}
    # Provenance lives in the body, not the title: the title is what renders in the roadmap.
    body = f"Captured from the dashboard idea inbox.\n\nSource card: {payload.source_id}" if payload.source_id else None
    try:
        with _board_conn(slug) as (_slug, conn):
            kanban_db.create_task(
                conn, title=text, body=body, created_by="dashboard", lane="idea", board=_slug)
    except Exception:
        # Fail-open at the endpoint boundary: a capture failure must never 500 the dialog.
        return {"ok": False, "reason": "roadmap_unavailable"}
    return {"ok": True, "reason": None}


# --- Plugin config ----------------------------------------------------------

def _load_config_or_empty() -> dict:
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception:
        return {}


@router.get("/config")
def get_config():
    """Kanban dashboard preferences from the ``dashboard.kanban`` config section."""
    k_cfg = (_load_config_or_empty().get("dashboard") or {}).get("kanban") or {}
    return {
        "default_tenant": k_cfg.get("default_tenant") or "",
        "lane_by_profile": bool(k_cfg.get("lane_by_profile", True)),
        "include_archived_by_default": bool(k_cfg.get("include_archived_by_default", False)),
        "render_markdown": bool(k_cfg.get("render_markdown", True))}


# --- Home-channel subscriptions (per-task, per-platform toggles) -------------
# Each gateway platform has at most one "home" (chat_id, thread_id, name); a toggle-on writes
# exactly the notify_subs row ``/kanban create`` would, so the gateway notifier needs no plumbing.

def _configured_home_channels() -> list[dict]:
    """Every platform with a home_channel, from the live GatewayConfig (so env overlays
    like ``TELEGRAM_HOME_CHANNEL`` are honored), sorted by platform."""
    try:
        from gateway.config import load_gateway_config
        gw_cfg = load_gateway_config()
    except Exception:
        return []
    result = [
        {"platform": platform.value, "chat_id": pcfg.home_channel.chat_id,
         "thread_id": pcfg.home_channel.thread_id or "", "name": pcfg.home_channel.name or "Home"}
        for platform, pcfg in gw_cfg.platforms.items() if pcfg and pcfg.home_channel]
    result.sort(key=lambda r: r["platform"])
    return result


def _active_profile_name() -> str:
    """Current Hermes profile name for notify-sub ownership."""
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or "default"
    except Exception:
        return "default"


def _home_for_platform(platform: str, detail: str) -> dict:
    home = next((h for h in _configured_home_channels() if h["platform"] == platform), None)
    if not home:
        raise HTTPException(status_code=404, detail=detail)
    return home


@router.get("/home-channels")
def get_home_channels(task_id: Optional[str] = Query(None), board: Optional[str] = Query(None)):
    """Every platform with a home channel plus whether *task_id* (if given) is
    subscribed to it; without ``task_id`` every ``subscribed`` is false."""
    homes = _configured_home_channels()
    subscribed_homes: set[tuple[str, str, str]] = set()
    if task_id:
        with _board_conn(board) as (board, conn):
            subs = kbn.list_notify_subs(conn, task_id)
        subscribed_homes = {
            (str(sub.get("platform") or ""), str(sub.get("chat_id") or ""), str(sub.get("thread_id") or "")) for sub in subs}
    return {"home_channels": [
        {**home, "subscribed": (home["platform"], home["chat_id"], home["thread_id"]) in subscribed_homes} for home in homes]}


@router.post("/tasks/{task_id}/home-subscribe/{platform}")
def subscribe_home(task_id: str, platform: str, board: Optional[str] = Query(None)):
    """Subscribe *task_id* to *platform*'s home channel. Idempotent at the DB
    layer; 404 when the platform has no home or the task doesn't exist."""
    home = _home_for_platform(
        platform,
        f"No home channel configured for platform {platform!r}. "
        f"Set one from the messenger via /sethome, or configure "
        f"gateway.platforms.{platform}.home_channel in config.yaml.")
    with _board_conn(board) as (board, conn):
        _require_task(conn, task_id)
        kbn.add_notify_sub(
            conn, task_id=task_id, platform=platform, chat_id=home["chat_id"],
            thread_id=home["thread_id"] or None, notifier_profile=_active_profile_name())
        return {"ok": True, "task_id": task_id, "home_channel": home}


@router.delete("/tasks/{task_id}/home-subscribe/{platform}")
def unsubscribe_home(task_id: str, platform: str, board: Optional[str] = Query(None)):
    """Remove any notify subscription on *task_id* matching *platform*'s home."""
    home = _home_for_platform(platform, f"No home channel configured for platform {platform!r}.")
    with _board_conn(board) as (board, conn):
        kbn.remove_notify_sub(
            conn, task_id=task_id, platform=platform, chat_id=home["chat_id"], thread_id=home["thread_id"] or None)
        return {"ok": True, "task_id": task_id, "home_channel": home}


# --- Stats / assignees / worker log / dispatch / model options ---------------

@router.get("/stats")
def get_stats(board: Optional[str] = Query(None)):
    """Per-status + per-assignee counts + oldest-ready age (HUD and router profiles)."""
    with _board_conn(board) as (board, conn):
        return kanban_db.board_stats(conn)


@router.get("/assignees")
def get_assignees(board: Optional[str] = Query(None)):
    """Union of on-disk profiles and assignees used on the board, so a fresh
    profile appears in the picker before it has any task."""
    with _board_conn(board) as (board, conn):
        return {"assignees": kanban_db.known_assignees(conn)}


@router.get("/tasks/{task_id}/log")
def get_task_log(task_id: str, tail: Optional[int] = Query(None, ge=1, le=2_000_000), board: Optional[str] = Query(None)):
    """Worker stdout/stderr log. ``tail`` caps the response bytes; 404 if the
    task never spawned. On-disk log rotates at 2 MiB with one ``.log.1`` kept."""
    with _board_conn(board) as (board, conn):
        _require_task(conn, task_id)
    content = kanban_db.read_worker_log(task_id, tail_bytes=tail, board=board)
    log_path = kanban_db.worker_log_path(task_id, board=board)
    size = log_path.stat().st_size if log_path.exists() else 0
    return {
        "task_id": task_id, "path": str(log_path), "exists": content is not None,
        "size_bytes": size, "content": content or "", "truncated": bool(tail and size > tail)}



@router.post("/dispatch")
def dispatch(dry_run: bool = Query(False), max_n: int = Query(8, alias="max"), board: Optional[str] = Query(None)):
    """Dispatch nudge so the UI doesn't wait out the 60 s dispatcher tick.

    Resolves the same ``kanban.*`` caps the gateway tick and the CLI use.
    Without them this endpoint spawns uncapped: ``dispatch_once`` reads an
    omitted cap as unlimited, and the desktop fires this on a debounce after
    every board edit, so clicking around the board could push the host well
    past ``kanban.max_in_progress``. ``?max=`` is clamped rather than trusted —
    it is a browser-supplied ceiling, not an override of the host's.
    """
    caps = kbd.resolve_dispatch_caps()
    with _board_conn(board) as (board, conn):
        result = kbd.dispatch_once(
            conn,
            dry_run=dry_run,
            max_spawn=kbd.clamp_requested_max_spawn(max_n, caps),
            max_in_progress=caps.max_in_progress,
            max_in_progress_per_profile=caps.max_in_progress_per_profile,
            default_assignee=caps.default_assignee,
            default_reviewer=caps.default_reviewer,
            dispatch_start_budget=caps.dispatch_start_budget,
            dispatch_start_window_seconds=caps.dispatch_start_window_seconds,
            review_rework_escalation_profile=caps.review_rework_escalation_profile,
            max_review_rounds=caps.max_review_rounds,
            priority_reserved_slots=caps.priority_reserved_slots,
            priority_reserved_threshold=caps.priority_reserved_threshold,
            board=board,
        )
        try:
            payload = asdict(result)  # DispatchResult is a dataclass
            pause = payload.get("dispatch_paused")
            if isinstance(pause, dict):
                payload["dispatch_status"] = kbd.dispatch_pause_message(pause, board=board)
            return payload
        except TypeError:
            return {"result": str(result)}



@router.get("/model-options")
def model_options():
    """Providers + curated models for the override dropdown via ``inventory.build_models_payload``
    (same substrate as the Models page) so it can't offer a pair Hermes rejects. Skips pricing
    and custom-provider probes: a slow/offline local endpoint must not hang the drawer."""
    try:
        from hermes_cli.inventory import build_models_payload, load_picker_context

        payload = build_models_payload(
            load_picker_context(), explicit_only=True, canonical_order=True, probe_custom_providers=False)
        return {
            "providers": [
                {"slug": row.get("slug", ""), "label": row.get("label") or row.get("slug", ""),
                 "models": list(row.get("models") or [])}
                for row in payload.get("providers", [])
                if row.get("models")]}
    except Exception:
        log.exception("kanban model-options failed")
        return {"providers": []}  # empty catalog → the UI falls back to a free-text input



# --- Profile metadata & description editing (kanban orchestrator) ------------

class DescribeBody(BaseModel):
    description: Optional[str] = None  # explicit user-authored text


class DescribeAutoBody(BaseModel):
    overwrite: bool = False


@router.get("/profiles")
def list_profile_roster():
    """Every installed profile with its description (profiles without one are
    still routable on name alone, just less precisely)."""
    with _errors_to_500("failed to list profiles"):
        from hermes_cli import profiles as profiles_mod
        profiles = profiles_mod.list_profiles()
    return {"profiles": [
        {"name": p.name, "is_default": bool(p.is_default), "model": p.model or "", "provider": p.provider or "",
         "reasoning_effort": p.reasoning_effort or "",
         "description": p.description or "", "description_auto": bool(p.description_auto),
         "skill_count": int(p.skill_count or 0)}
        for p in profiles]}


@router.patch("/profiles/{profile_name}")
def update_profile_description(profile_name: str, payload: DescribeBody):
    """Set (``description_auto: false`` so the auto-describer won't overwrite it
    without ``--overwrite``) or clear (empty string) a profile's description."""
    with _errors_to_500("failed to update profile"):
        from hermes_cli import profiles as profiles_mod
        canon = profiles_mod.normalize_profile_name(profile_name)
        if canon == "default":
            from hermes_constants import get_hermes_home  # type: ignore
            profile_dir = Path(get_hermes_home())
        else:
            profile_dir = profiles_mod.get_profile_dir(canon)
        if not profile_dir.is_dir():
            raise HTTPException(status_code=404, detail=f"profile '{profile_name}' not found")
        text = (payload.description or "").strip()
        profiles_mod.write_profile_meta(profile_dir, description=text, description_auto=False)
    return {"ok": True, "profile": canon, "description": text}


@router.post("/profiles/{profile_name}/describe-auto")
def auto_describe_profile(profile_name: str, payload: DescribeAutoBody):
    """``hermes profile describe <name> --auto``: persist with ``description_auto: true``.
    Non-OK outcomes are NOT HTTP errors — the UI renders the reason inline."""
    with _errors_to_500("describer crashed"):
        from hermes_cli import profile_describer
        outcome = profile_describer.describe_profile(profile_name, overwrite=bool(payload.overwrite))
    return {"ok": bool(outcome.ok), "profile": outcome.profile_name, "reason": outcome.reason, "description": outcome.description}


# --- Decompose (built-in decomposer fan-out) ----------------------------------

class DecomposeBody(BaseModel):
    author: Optional[str] = None


@router.post("/tasks/{task_id}/decompose")
def decompose_task_endpoint(task_id: str, payload: DecomposeBody, board: Optional[str] = Query(None)):
    """Fan a triage task out into child tasks via the auxiliary LLM (``hermes kanban decompose``).
    Non-OK is NOT an HTTP error. Sync ``def`` → runs in the threadpool."""
    outcome = _run_aux(board, "kanban_decompose", "decompose_task", task_id, payload.author)
    return {
        "ok": bool(outcome.ok), "task_id": outcome.task_id, "reason": outcome.reason,
        "fanout": bool(outcome.fanout), "child_ids": outcome.child_ids or [], "new_title": outcome.new_title}


# --- Orchestration settings (kanban.orchestrator_profile / default_assignee /
#     auto_decompose / auto_promote_children) ----------------------------------

class OrchestrationSettingsBody(BaseModel):
    orchestrator_profile: Optional[str] = None
    default_assignee: Optional[str] = None
    auto_decompose: Optional[bool] = None
    auto_promote_children: Optional[bool] = None


_PROFILE_SETTINGS = ("orchestrator_profile", "default_assignee")


@router.get("/orchestration")
def get_orchestration_settings():
    """Current orchestration knobs from config.yaml plus the resolved effective
    values (fallbacks filled the same way the decomposer does)."""
    cfg = _load_config_or_empty()
    kanban_cfg = (cfg.get("kanban") or {}) if isinstance(cfg, dict) else {}
    explicit = {k: (kanban_cfg.get(k) or "").strip() for k in _PROFILE_SETTINGS}
    resolved = dict(explicit)
    try:
        from hermes_cli import profiles as profiles_mod
        active_default = profiles_mod.get_active_profile_name() or "default"
        for k, v in explicit.items():
            if not v or not profiles_mod.profile_exists(v):
                resolved[k] = active_default
    except Exception:
        active_default = "default"
        resolved = {k: v or active_default for k, v in resolved.items()}
    return {
        "orchestrator_profile": explicit["orchestrator_profile"],
        "default_assignee": explicit["default_assignee"],
        "auto_decompose": bool(kanban_cfg.get("auto_decompose", True)),
        "auto_promote_children": bool(kanban_cfg.get("auto_promote_children", True)),
        "resolved_orchestrator_profile": resolved["orchestrator_profile"],
        "resolved_default_assignee": resolved["default_assignee"],
        "active_profile": active_default}


def _validated_profile_name(raw: Optional[str], profiles_mod) -> str:
    """Strip a profile name; 400 if non-empty and unknown. Fails open when the lookup itself errors."""
    name = (raw or "").strip()
    if name and profiles_mod is not None:
        try:
            exists = profiles_mod.profile_exists(name)
        except Exception:
            exists = True
        if not exists:
            raise HTTPException(status_code=400, detail=f"profile '{name}' does not exist")
    return name


@router.put("/orchestration")
def set_orchestration_settings(payload: OrchestrationSettingsBody):
    """Update orchestration knobs in config.yaml. Only fields explicitly passed
    are written; empty profile strings clear the override."""
    with _errors_to_500("failed to load config"):
        from hermes_cli.config import load_config, save_config
        cfg = load_config() or {}
    kanban_section = cfg.setdefault("kanban", {})
    if not isinstance(kanban_section, dict):
        kanban_section = cfg["kanban"] = {}
    try:
        from hermes_cli import profiles as profiles_mod
    except Exception:
        profiles_mod = None  # type: ignore
    # Field order == write order (profiles validated first, then the booleans).
    for key, value in payload.model_dump(exclude_none=True).items():
        kanban_section[key] = _validated_profile_name(value, profiles_mod) if key in _PROFILE_SETTINGS else bool(value)
    with _errors_to_500("failed to save config"):
        save_config(cfg)
    return get_orchestration_settings()  # callers re-render from the resolved state


# --- WebSocket: /events?since=<event_id>&board=<slug>  (or ?boards=<csv|*>&cursors=<json>) --

# Event tail poll interval: WAL + 300 ms polling is the simplest robust approach (negligible CPU).
_EVENT_POLL_SECONDS = 0.3

# Cap the number of boards one socket tails — an unbounded ``boards=*`` on a fleet with many
# boards would open that many SQLite connections on a single request. The dashboard has a
# handful of boards in practice; this is a safety rail, not a tuned limit.
_MAX_TAILED_BOARDS = 25


def _int_param(ws: WebSocket, name: str) -> int:
    try:
        return int(ws.query_params.get(name, "0"))
    except ValueError:
        return 0


def _ws_board(raw: Optional[str]) -> Optional[str]:
    try:
        return kanban_db._normalize_board_slug(raw) if raw else None
    except ValueError:
        return None


def _ws_boards_param(raw: Optional[str]) -> Optional[list[str]]:
    """Resolve ``?boards=`` into an ordered, deduped, capped slug list, or ``None`` when the
    param is absent (selecting the legacy single-board path). ``boards=*`` means every board
    currently on disk; a CSV list is normalized/filtered the same way a single ``board=`` is."""
    if raw is None:
        return None
    if raw.strip() == "*":
        slugs = [meta["slug"] for meta in kanban_db.list_boards(include_archived=False)]
    else:
        slugs = []
        for part in raw.split(","):
            try:
                normed = kanban_db._normalize_board_slug(part)
            except ValueError:
                normed = None
            if normed and normed not in slugs:
                slugs.append(normed)
    if len(slugs) > _MAX_TAILED_BOARDS:
        log.warning("kanban /events: boards=%r requested %d boards, capping to %d", raw, len(slugs), _MAX_TAILED_BOARDS)
        slugs = slugs[:_MAX_TAILED_BOARDS]
    return slugs


def _ws_cursors_param(raw: Optional[str]) -> dict[str, int]:
    """Parse the per-board cursor seed map. Malformed/missing input degrades to ``{}`` (every
    board starts from 0) rather than failing the handshake — a bad seed costs a one-time replay,
    never a broken connection."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in parsed.items():
        try:
            out[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return out


class _EventTail:
    """Per-socket ``task_events`` tailer. One SQLite connection, used/closed only on a
    dedicated single-thread executor (connections are thread-affine); reusing it avoids
    churning WAL/SHM sidecars while an idle dashboard polls."""

    def __init__(self, board: Optional[str]) -> None:
        self._board = board
        self._conn: Optional[sqlite3.Connection] = None
        self._executor: Optional[ThreadPoolExecutor] = None

    def _fetch(self, cursor: int) -> tuple[int, list[dict]]:
        if self._conn is None:
            self._conn = kbc.connect(board=self._board)
        rows = self._conn.execute(
            "SELECT id, task_id, run_id, kind, payload, created_at "
            "FROM task_events WHERE id > ? ORDER BY id ASC LIMIT 200",
            (cursor,)).fetchall()
        out: list[dict] = []
        for r in rows:
            try:
                payload = json.loads(r["payload"]) if r["payload"] else None
            except Exception:
                payload = None
            out.append({**dict(r), "payload": payload})
        return (rows[-1]["id"] if rows else cursor), out

    def _close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def poll(self, cursor: int) -> tuple[int, list[dict]]:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kanban-events")
        return await asyncio.get_running_loop().run_in_executor(self._executor, self._fetch, cursor)

    async def shutdown(self) -> None:
        if self._executor is None:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(self._executor, self._close)
        except Exception as exc:
            log.warning("Kanban event stream connection cleanup failed: %s", exc)
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)


class _MultiEventTail:
    """Multi-board ``task_events`` tailer for the ``boards=`` fan-out path (consolidated All
    Boards view). Holds one thread-affine SQLite connection PER requested board, but all of
    them are opened/polled/closed on the SAME single-worker executor the single-board
    ``_EventTail`` uses — one executor for the whole socket, never a pool per board.

    A board that raises mid-poll (locked/corrupt DB) is isolated: its connection is dropped
    and that board is skipped on every subsequent poll, so one bad board never kills the
    stream for the others — mirroring ``GET /board/all``'s per-board try/except."""

    def __init__(self, boards: list[str]) -> None:
        self._boards = boards
        self._conns: dict[str, sqlite3.Connection] = {}
        self._dead: set[str] = set()  # boards that errored; skipped on later polls
        self._executor: Optional[ThreadPoolExecutor] = None

    def _fetch_one(self, board: str, cursor: int) -> tuple[int, list[dict]]:
        conn = self._conns.get(board)
        if conn is None:
            conn = kbc.connect(board=board)
            self._conns[board] = conn
        rows = conn.execute(
            "SELECT id, task_id, run_id, kind, payload, created_at "
            "FROM task_events WHERE id > ? ORDER BY id ASC LIMIT 200",
            (cursor,)).fetchall()
        out: list[dict] = []
        for r in rows:
            try:
                payload = json.loads(r["payload"]) if r["payload"] else None
            except Exception:
                payload = None
            out.append({**dict(r), "payload": payload, "board": board})
        return (rows[-1]["id"] if rows else cursor), out

    def _fetch_all(self, cursors: dict[str, int]) -> tuple[dict[str, int], list[dict]]:
        """Runs on the single worker thread: poll every live board in turn."""
        events: list[dict] = []
        new_cursors = dict(cursors)
        for board in self._boards:
            if board in self._dead:
                continue
            try:
                new_cursor, board_events = self._fetch_one(board, cursors.get(board, 0))
            except Exception as exc:
                log.warning("kanban /events: board %r failed mid-stream, dropping it from this socket: %s", board, exc)
                self._dead.add(board)
                conn = self._conns.pop(board, None)
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                continue
            new_cursors[board] = new_cursor
            events.extend(board_events)
        return new_cursors, events

    def _close_all(self) -> None:
        for conn in self._conns.values():
            try:
                conn.close()
            except Exception:
                pass
        self._conns.clear()

    async def poll(self, cursors: dict[str, int]) -> tuple[dict[str, int], list[dict]]:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kanban-events")
        return await asyncio.get_running_loop().run_in_executor(self._executor, self._fetch_all, cursors)

    async def shutdown(self) -> None:
        if self._executor is None:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(self._executor, self._close_all)
        except Exception as exc:
            log.warning("Kanban multi-board event stream connection cleanup failed: %s", exc)
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)


@router.websocket("/events")
async def stream_events(ws: WebSocket):
    if not _ws_upgrade_authorized(ws):
        await ws.close(code=http_status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()
    # ``boards=`` selects the NEW multi-board fan-out path, kept entirely separate from the
    # legacy single-board loop below so that loop's frame shape never changes for existing
    # clients that never send ``boards=``.
    boards = _ws_boards_param(ws.query_params.get("boards"))
    if boards is not None:
        await _stream_events_multi(ws, boards)
        return
    # Board is pinned at the handshake; the UI opens a new WS on board change
    # rather than reconciling two cursors mid-stream.
    tail = _EventTail(_ws_board(ws.query_params.get("board")))
    cursor = _int_param(ws, "since")
    try:
        while True:
            # Race receive() against the poll interval so a disconnect is detected even when no
            # events flow (else idle boards leak poll tasks). Other client messages are ignored.
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=_EVENT_POLL_SECONDS)
                if msg["type"] == "websocket.disconnect":
                    return
            except asyncio.TimeoutError:
                pass  # no client message — poll the DB
            cursor, events = await tail.poll(cursor)
            if events:
                await ws.send_json({"events": events, "cursor": cursor})
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        return  # normal shutdown; CancelledError is a BaseException the handler below wouldn't quiet
    except Exception as exc:  # never crash the dashboard worker
        log.warning("Kanban event stream error: %s", exc)
        try:
            await ws.close()
        except Exception:
            pass
    finally:
        await tail.shutdown()


async def _stream_events_multi(ws: WebSocket, boards: list[str]) -> None:
    """``boards=<csv>`` / ``boards=*`` fan-out: tails N boards on this ONE socket. Cursors seed
    from ``?cursors=<json>`` (the ``/board/all`` payload's ``cursors`` map — resumes exactly
    where the initial fetch ended, no gap, no replay). Frame shape is the new contract
    ``{"events": [{"board": ..., ...}], "cursors": {...}}``; kept in its own loop rather than
    retrofitted into the single-board one above so that one's byte-identical frame is never at
    risk of drifting."""
    tail = _MultiEventTail(boards)
    cursors = _ws_cursors_param(ws.query_params.get("cursors"))
    try:
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=_EVENT_POLL_SECONDS)
                if msg["type"] == "websocket.disconnect":
                    return
            except asyncio.TimeoutError:
                pass  # no client message — poll the DBs
            cursors, events = await tail.poll(cursors)
            if events:
                await ws.send_json({"events": events, "cursors": cursors})
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        return  # normal shutdown; CancelledError is a BaseException the handler below wouldn't quiet
    except Exception as exc:  # never crash the dashboard worker
        log.warning("Kanban multi-board event stream error: %s", exc)
        try:
            await ws.close()
        except Exception:
            pass
    finally:
        await tail.shutdown()
