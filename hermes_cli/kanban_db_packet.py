"""Canonical, bounded model-facing packet for one Kanban task.

The board remains the durable source of truth.  This module selects the
operative subset once for a worker and exposes opaque cursor pages for history
that is intentionally kept out of the initial model payload.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any, Optional


PACKET_VERSION = 1
_HISTORY_PREVIEW_COMMENTS = 3
_HISTORY_PREVIEW_FIELD_BYTES = 1024
_HISTORY_DEFAULT_LIMIT = 20
_HISTORY_MAX_LIMIT = 50
_HISTORY_KINDS = frozenset({"comments", "events", "runs"})


@dataclass(frozen=True)
class WorkerTaskPacket:
    """Typed wire contract returned to implementation and review workers."""

    packet_version: int
    identity: dict[str, Any]
    contract: dict[str, Any]
    handoff: Optional[dict[str, Any]]
    review: dict[str, Any]
    dependencies: list[dict[str, Any]]
    children: list[dict[str, Any]]
    workspace: dict[str, Any]
    authority: dict[str, Any]
    caps: dict[str, Any]
    attachments: list[dict[str, Any]]
    history: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _preview_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    encoded = value.encode("utf-8")
    if len(encoded) <= _HISTORY_PREVIEW_FIELD_BYTES:
        return value
    prefix = encoded[:_HISTORY_PREVIEW_FIELD_BYTES].decode("utf-8", errors="ignore")
    return prefix + (
        f"… [{len(encoded) - len(prefix.encode('utf-8'))} bytes omitted; use history cursor]"
    )


def _latest_closed_handoff(conn, task_id: str) -> Optional[dict[str, Any]]:
    rows = [run for run in _kb.list_runs(conn, task_id) if run.ended_at is not None]
    if not rows:
        return None
    run = rows[-1]
    if not (run.summary or run.metadata or run.outcome):
        return None
    return {
        "run_id": run.id,
        "outcome": run.outcome or run.status,
        "summary": run.summary,
        "metadata": run.metadata,
    }


def _dependency_packet(conn, task_id: str) -> dict[str, Any]:
    task = _kb.get_task(conn, task_id)
    handoff = _latest_closed_handoff(conn, task_id)
    if handoff is None and task is not None and task.result:
        handoff = {
            "run_id": None,
            "outcome": "completed",
            "summary": task.result,
            "metadata": None,
        }
    return {
        "task_id": task_id,
        "title": task.title if task is not None else None,
        "status": task.status if task is not None else "missing",
        "handoff": handoff,
        "retrieval": {"tool": "kanban_show", "arguments": {"task_id": task_id}},
    }


def _child_packet(conn, task_id: str) -> dict[str, Any]:
    task = _kb.get_task(conn, task_id)
    return {
        "task_id": task_id,
        "title": task.title if task is not None else None,
        "status": task.status if task is not None else "missing",
        "latest_summary": _kb.latest_summary(conn, task_id),
        "retrieval": {"tool": "kanban_show", "arguments": {"task_id": task_id}},
    }


def _unresolved_review_items(
    conn, task_id: str
) -> tuple[list[dict[str, Any]], set[int]]:
    row = conn.execute(
        "SELECT id, payload, created_at, run_id FROM task_events "
        "WHERE task_id = ? AND kind = 'changes_requested' "
        "AND id > COALESCE((SELECT MAX(id) FROM task_events "
        "WHERE task_id = ? AND kind = 'completed'), 0) "
        "ORDER BY id DESC LIMIT 1",
        (task_id, task_id),
    ).fetchone()
    if row is None:
        return [], set()
    payload = _kb._json_dict(row["payload"])
    return [
        {
            "event_id": int(row["id"]),
            "reason": payload.get("reason"),
            "blockers": payload.get("blockers") or [],
            "followups": payload.get("followups") or [],
            "metadata": payload.get("metadata"),
            "reviewer": payload.get("reviewer"),
            "review_path": payload.get("review_path") or "same_card",
            "review_round": payload.get("review_round"),
            "max_review_rounds": payload.get("max_review_rounds"),
            "run_id": row["run_id"],
            "created_at": int(row["created_at"]),
        }
    ], {int(row["id"])}


def _history_marker(task_id: str, kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "tool": "kanban_show",
        "arguments": {
            "task_id": task_id,
            "history_cursor": f"{kind}:0",
            "history_limit": _HISTORY_DEFAULT_LIMIT,
        },
    }


def _history_packet(
    conn, task_id: str, excluded_event_ids: set[int], excluded_run_ids: set[int]
) -> dict[str, Any]:
    comments = _kb.list_comments(conn, task_id)
    events = [
        event
        for event in _kb.list_events(conn, task_id)
        if event.id not in excluded_event_ids
    ]
    runs = [
        run for run in _kb.list_runs(conn, task_id) if run.id not in excluded_run_ids
    ]

    shown_comments = comments[-_HISTORY_PREVIEW_COMMENTS:]
    truncated_comment_ids = {
        row.id for row in shown_comments if _preview_text(row.body) != row.body
    }
    preview = {
        "comments": [
            {
                "id": row.id,
                "author": row.author,
                "body": _preview_text(row.body),
                "created_at": row.created_at,
            }
            for row in shown_comments
        ],
    }
    omitted = {
        "comments": max(0, len(comments) - len(shown_comments)),
        "events": len(events),
        "runs": len(runs),
    }
    retrieval = [
        _history_marker(task_id, kind)
        for kind, count in omitted.items()
        if count or (kind == "comments" and truncated_comment_ids)
    ]
    return {
        "preview": preview,
        "omitted": omitted,
        "truncated_comment_ids": sorted(truncated_comment_ids),
        "retrieval": retrieval,
    }


def _runtime_caps(task) -> tuple[dict[str, Any], int]:
    try:
        from hermes_cli.config import load_config

        raw = load_config()
        kanban_cfg = raw.get("kanban", {}) if isinstance(raw, dict) else {}
    except Exception:
        kanban_cfg = {}
    if not isinstance(kanban_cfg, dict):
        kanban_cfg = {}

    dispatch_caps = _kbd.resolve_dispatch_caps(kanban_cfg)
    failure_limit = task.max_retries
    if failure_limit is None:
        try:
            failure_limit = int(
                kanban_cfg.get("failure_limit", _kbd.DEFAULT_FAILURE_LIMIT)
            )
        except (TypeError, ValueError):
            failure_limit = _kbd.DEFAULT_FAILURE_LIMIT
    terminal_timeout = _kbd._worker_terminal_timeout_env(task.max_runtime_seconds, None)
    return (
        {
            "max_runtime_seconds": task.max_runtime_seconds,
            "terminal_timeout_seconds": int(terminal_timeout)
            if terminal_timeout
            else None,
            "max_retries": failure_limit,
            "max_in_progress": dispatch_caps.max_in_progress,
            "max_in_progress_per_profile": dispatch_caps.max_in_progress_per_profile,
            "max_spawn_per_tick": dispatch_caps.max_spawn,
            "goal_max_turns": task.goal_max_turns if task.goal_mode else None,
        },
        dispatch_caps.max_review_rounds,
    )


def _workspace_refs(task, land_target: Optional[str]) -> dict[str, Any]:
    """Best-effort exact refs from the already-materialized task worktree."""
    from hermes_cli.kanban_db_receipt import packet_preflight_receipt

    path = task.workspace_path
    head_sha = _kb._git_out(path, "rev-parse", "HEAD") if path else None
    base_sha = (
        _kb._git_out(path, "rev-parse", land_target) if path and land_target else None
    )
    return {
        "kind": task.workspace_kind,
        "path": path,
        "branch": task.branch_name,
        "project_id": task.project_id,
        "base_ref": land_target,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "preflight": packet_preflight_receipt(task.id),
    }


def _board_metadata_for_connection(conn, fallback_board: Optional[str]) -> dict[str, Any]:
    """Resolve metadata from the database the caller actually opened.

    An omitted board argument can still be pinned by ``HERMES_KANBAN_DB``, which
    outranks ``HERMES_KANBAN_BOARD``.  Reading metadata from either raw input
    would therefore let the packet describe a different board than ``conn``.
    """
    database_rows = conn.execute("PRAGMA database_list").fetchall()
    main_row = next((row for row in database_rows if row[1] == "main"), None)
    if main_row is None or not main_row[2]:
        return _kb.read_board_metadata(fallback_board)
    opened_path = Path(main_row[2]).expanduser().resolve()

    # Named boards come first because a path pin also makes the default board's
    # derived ``db_path`` point at the pin. Compare against canonical filesystem
    # locations instead of that env-sensitive display field.
    boards = _kb.list_boards(include_archived=True)
    for meta in (item for item in boards if item.get("slug") != _kb.DEFAULT_BOARD):
        slug = str(meta["slug"])
        if (_kb.board_dir(slug) / "kanban.db").expanduser().resolve() == opened_path:
            return meta
    if (_kb.kanban_home() / "kanban.db").expanduser().resolve() == opened_path:
        return _kb.read_board_metadata(_kb.DEFAULT_BOARD)

    # A hand-pinned arbitrary DB has no board.json identity to recover. Preserve
    # the prior fail-safe contract (no inferred landing authority).
    return _kb.read_board_metadata(fallback_board)


def build_worker_task_packet(
    conn, task_id: str, *, board: Optional[str] = None
) -> WorkerTaskPacket:
    """Build the single operative payload a worker receives from ``kanban_show``."""
    task = _kb.get_task(conn, task_id)
    if task is None:
        raise ValueError(f"unknown task {task_id}")

    source_state = (
        _kb._retry_status_for_run(conn, task_id, task.current_run_id)
        if task.status == "running"
        else task.status
    )
    handoff = _latest_closed_handoff(conn, task_id)
    changes_rounds, _ = _kbd._changes_requested_state(conn, task_id)
    caps, max_review_rounds = _runtime_caps(task)
    from hermes_cli import kanban_db_review as review_policy

    review_contract = review_policy.effective_review_contract(
        conn,
        task_id,
        task=task,
        source_state=source_state,
        changes_rounds=changes_rounds,
        max_review_rounds=max_review_rounds,
    )
    role = (
        "reviewer"
        if source_state == "review" or review_contract["path"] == "ready_child"
        else "implementer"
    )
    unresolved, unresolved_event_ids = _unresolved_review_items(conn, task_id)
    # On a rework dispatch the latest closed run is the reviewer verdict, which
    # is already represented losslessly in ``unresolved_items``. Do not emit
    # that same operative feedback a second time as a generic handoff.
    if (
        role == "implementer"
        and handoff
        and any(item.get("run_id") == handoff.get("run_id") for item in unresolved)
    ):
        handoff = None

    board_meta = _board_metadata_for_connection(conn, board)
    land_target = board_meta.get("land_target")
    completion_contract = task.completion_contract or "local-only"
    handoff_run_ids = (
        {int(handoff["run_id"])}
        if handoff and handoff.get("run_id") is not None
        else set()
    )

    attachments = [
        {
            "id": item.id,
            "filename": item.filename,
            "content_type": item.content_type,
            "size": item.size,
            "stored_path": item.stored_path,
            "uploaded_by": item.uploaded_by,
            "created_at": item.created_at,
        }
        for item in _kb.list_attachments(conn, task_id)
    ]

    return WorkerTaskPacket(
        packet_version=PACKET_VERSION,
        identity={
            "task_id": task.id,
            "title": task.title,
            "role": role,
            "assignee": task.assignee,
            "state": task.status,
            "source_state": source_state,
            "priority": task.priority,
            "tenant": task.tenant,
        },
        contract={
            "body": task.body,
            "skills": task.skills,
        },
        handoff=handoff,
        review={
            **review_contract,
            "unresolved_items": unresolved,
        },
        dependencies=[
            _dependency_packet(conn, item) for item in _kb.parent_ids(conn, task_id)
        ],
        children=[_child_packet(conn, item) for item in _kb.child_ids(conn, task_id)],
        workspace=_workspace_refs(task, land_target),
        authority={
            "completion_contract": completion_contract,
            "land_target": land_target,
            "may_land": role == "reviewer"
            and completion_contract != "local-only"
            and bool(land_target),
        },
        caps=caps,
        attachments=attachments,
        history=_history_packet(conn, task_id, unresolved_event_ids, handoff_run_ids),
    )


def render_worker_task_packet(packet: WorkerTaskPacket) -> str:
    """Human-readable compatibility view for ``hermes kanban context``."""
    data = packet.to_dict()
    identity = data["identity"]
    lines = [
        f"# Kanban task {identity['task_id']}: {identity['title']}",
        "",
        f"Role: {identity['role']}",
        f"Assignee: {identity['assignee'] or '(unassigned)'}",
        f"State: {identity['state']} (claimed from {identity['source_state']})",
        "",
    ]
    body = data["contract"].get("body")
    if body:
        lines.extend(["## Body", body, ""])
    if data["attachments"]:
        lines.append("## Attachments")
        for item in data["attachments"]:
            lines.append(f"- `{item['filename']}` → `{item['stored_path']}`")
        lines.append("")
    if data["handoff"]:
        lines.extend([
            "## Latest handoff",
            json.dumps(data["handoff"], ensure_ascii=False, sort_keys=True),
            "",
        ])
    if data["dependencies"]:
        lines.extend([
            "## Dependencies",
            json.dumps(data["dependencies"], ensure_ascii=False, sort_keys=True),
            "",
        ])
    lines.extend([
        "## Packet metadata",
        json.dumps(
            {
                "review": data["review"],
                "workspace": data["workspace"],
                "authority": data["authority"],
                "caps": data["caps"],
                "history": data["history"],
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    ])
    return "\n".join(lines).rstrip() + "\n"


def read_task_history_page(
    conn, task_id: str, cursor: str, limit: int = _HISTORY_DEFAULT_LIMIT
) -> dict[str, Any]:
    """Return one full-fidelity history page without repeating the task packet."""
    if _kb.get_task(conn, task_id) is None:
        raise ValueError(f"unknown task {task_id}")
    try:
        kind, raw_after = cursor.split(":", 1)
        after = int(raw_after)
    except (AttributeError, TypeError, ValueError):
        raise ValueError(
            "history_cursor must be comments:<id>, events:<id>, or runs:<id>"
        ) from None
    if kind not in _HISTORY_KINDS or after < 0:
        raise ValueError(
            "history_cursor must be comments:<id>, events:<id>, or runs:<id>"
        )
    try:
        page_limit = int(limit)
    except (TypeError, ValueError):
        raise ValueError("history_limit must be an integer") from None
    if not 1 <= page_limit <= _HISTORY_MAX_LIMIT:
        raise ValueError(f"history_limit must be between 1 and {_HISTORY_MAX_LIMIT}")

    table = {"comments": "task_comments", "events": "task_events", "runs": "task_runs"}[
        kind
    ]
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE task_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
        (task_id, after, page_limit + 1),
    ).fetchall()
    has_more = len(rows) > page_limit
    rows = rows[:page_limit]

    items: list[dict[str, Any]] = []
    for row in rows:
        if kind == "comments":
            items.append({
                key: row[key] for key in ("id", "author", "body", "created_at")
            })
        elif kind == "events":
            items.append({
                "id": row["id"],
                "kind": row["kind"],
                "payload": _kb._json_dict(row["payload"]) if row["payload"] else None,
                "created_at": row["created_at"],
                "run_id": row["run_id"],
            })
        else:
            run = _kb.Run.from_row(row)
            items.append(asdict(run))
    next_cursor = f"{kind}:{items[-1]['id']}" if has_more and items else None
    return {
        "kind": kind,
        "items": items,
        "next_cursor": next_cursor,
        "limit": page_limit,
    }


from hermes_cli import kanban_db as _kb  # noqa: E402
from hermes_cli import kanban_db_dispatch as _kbd  # noqa: E402
