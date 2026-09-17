"""Kanban dashboard — quota circuits + dispatch pause circuit (maintenance drain) + post-drain actions.

The quota-circuit endpoints are folded in here (rather than a standalone module)
because they are tiny (~15 lines) and belong with the other host-wide dispatch
controls an operator reaches for during a drain/restart.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from hermes_cli import kanban_db
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_dispatch_postdrain as kbpd
from hermes_cli import kanban_quota_circuit as kqc

from plugins.kanban.dashboard._common import (
    _BOARD_Q_DESCRIPTION,
    _board_conn,
    _resolve_board,
    _with_board_pinned,
)

_BOARD_Q = Query(None, description=_BOARD_Q_DESCRIPTION)

router = APIRouter()


@router.get("/quota-circuits")
def quota_circuits():
    """Sanitized host-wide quota state shared by every Kanban board."""
    circuits = kqc.list_quota_circuits()
    return {"active": bool(circuits), "circuits": circuits}


@router.delete("/quota-circuits/{group_handle}")
def clear_quota_circuit(group_handle: str):
    """Manually clear one circuit by its opaque dashboard handle."""
    if not kqc.clear_quota_circuit(group_handle):
        raise HTTPException(status_code=404, detail="quota circuit not found")
    return {"cleared": True, "group": group_handle}


class DispatchPauseBody(BaseModel):
    note: Optional[str] = None


def _dispatch_board_slugs(board: Optional[str], boards: Optional[str]) -> Optional[list[str]]:
    """Return the explicit active-board fan-out, or ``None`` for one board."""
    if board is not None and boards is not None:
        raise HTTPException(status_code=400, detail="pass either board or boards, not both")
    if boards is None:
        return None
    if boards.strip() != "*":
        raise HTTPException(status_code=400, detail="dispatch aggregate scope requires boards=*")
    return [meta["slug"] for meta in kanban_db.list_boards(include_archived=False)]


def _dispatch_status_for_board(board: Optional[str]) -> dict[str, Any]:
    with _board_conn(board) as (resolved, conn):
        state = kbd.read_dispatch_pause(resolved)
        running = int(kanban_db.board_stats(conn)["by_status"].get("running", 0))
    return {
        "paused": state is not None,
        "state": state,
        "running_count": running,
        "message": kbd.dispatch_pause_message(state, board=resolved) if state else None,
        "post_drain": _post_drain_view(board),
    }


def _post_drain_view(board: Optional[str], *, now: Optional[int] = None) -> Optional[dict[str, Any]]:
    """The queued action as the panel renders it, or None.

    ``expires_in_seconds`` is derived server-side so the countdown the operator
    reads comes from the same clock that will actually expire the record — a
    renderer computing it from its own clock would drift against the trigger.
    """
    record = kbpd.read_post_drain_action(_resolve_board(board))
    if record is None:
        return None
    current = int(now if now is not None else time.time())
    expires_at = record.get("expires_at")
    remaining = (
        max(0, int(expires_at) - current) if isinstance(expires_at, int) else None
    )
    return {**record, "expires_in_seconds": remaining}


def _post_drain_action_catalog() -> list[dict[str, Any]]:
    """Action kinds this host will actually accept, for the UI selector.

    Derived from the same registry and config the queue route validates against,
    so the selector can never offer an action the backend would then reject.
    """
    cfg = kbpd.resolve_post_drain_config()
    catalog: list[dict[str, Any]] = []
    for kind, handler in kbpd.ACTION_HANDLERS.items():
        if not handler.takes_target:
            catalog.append({"action_kind": kind, "targets": []})
            continue
        targets = handler.config_targets(cfg)
        if targets:
            catalog.append({"action_kind": kind, "targets": targets})
    return catalog


@router.get("/dispatch/status")
def dispatch_status(board: Optional[str] = _BOARD_Q, boards: Optional[str] = Query(None)):
    """Pause state + live running counts for one board or every active board.

    ``running_count`` is the "is it safe to restart yet" signal: pausing fences
    NEW dispatch only, so an operator watches this reach 0 before restarting a
    service whose cgroup would otherwise SIGKILL those workers.
    """
    slugs = _dispatch_board_slugs(board, boards)
    if slugs is None:
        return {**_dispatch_status_for_board(board), "post_drain_actions": _post_drain_action_catalog()}

    statuses: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for slug in slugs:
        try:
            statuses.append({"board": slug, **_dispatch_status_for_board(slug)})
        except Exception as exc:
            errors.append({"board": slug, "error": str(exc)})
    paused_count = sum(1 for status in statuses if status["paused"])
    all_paused = bool(slugs) and paused_count == len(slugs)
    return {
        "paused": all_paused,
        "state": None,
        "message": None,
        "board_count": len(slugs),
        "paused_count": paused_count,
        "running_count": sum(status["running_count"] for status in statuses),
        "all_paused": all_paused,
        "boards": statuses,
        "errors": errors,
        "post_drain": _aggregate_post_drain(statuses),
        "post_drain_actions": _post_drain_action_catalog(),
    }


def _aggregate_post_drain(statuses: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """One headline record for the aggregate scope, or None.

    Per-board records stay in ``boards[]`` so outcomes are reported in isolation
    (a restart that succeeded on one board and failed on another must not be
    flattened into a single verdict). This headline exists only so the panel can
    render "reboot when drained" once instead of once per board, and it reports
    the LEAST-settled state across the group: while any board is still waiting,
    the group has not finished.
    """
    records = [status["post_drain"] for status in statuses if status.get("post_drain")]
    if not records:
        return None
    order = [kbpd.WAITING, kbpd.FIRING, kbpd.FAILED, kbpd.EXPIRED, kbpd.CANCELLED, kbpd.SUCCEEDED]

    def rank(record: dict[str, Any]) -> int:
        state = record.get("state")
        return order.index(state) if state in order else len(order)

    headline = min(records, key=rank)
    remaining = [
        record["expires_in_seconds"] for record in records
        if isinstance(record.get("expires_in_seconds"), int)
    ]
    return {
        **headline,
        "board_count": len(records),
        # The group can only fire once every board has drained, so the window
        # that bounds it is the SOONEST expiry, not this one record's.
        "expires_in_seconds": min(remaining) if remaining else None,
    }


def _dispatch_target_board(board: Optional[str]) -> str:
    """Resolve the board a pause/resume acts on to an explicit slug.

    An omitted param must land on the *current* board, exactly as
    ``GET /dispatch/status`` reads it: the Desktop's board switcher stores
    "the active board" as an empty slug, so the default UI path arrives here
    with no ``board`` at all. Leaving that as ``None`` under
    ``_with_board_pinned`` would pin ``DEFAULT_BOARD`` and pause a board the
    operator is not looking at, while status kept reporting the real one —
    a silent no-op right before a gateway restart. An explicit slug is still
    validated and used verbatim, so board isolation is unchanged.
    """
    return _resolve_board(board) or kanban_db.get_current_board()


@router.post("/dispatch/pause")
def dispatch_pause(
    payload: Optional[DispatchPauseBody] = None,
    board: Optional[str] = _BOARD_Q,
    boards: Optional[str] = Query(None),
):
    """Stop claiming/spawning on one board or every active board. Never kills a worker."""
    slugs = _dispatch_board_slugs(board, boards)
    note = payload.note if payload else None
    if slugs is None:
        target = _dispatch_target_board(board)
        return _with_board_pinned(target, lambda: kbd.pause_dispatch(target, note=note))

    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for slug in slugs:
        try:
            result = _with_board_pinned(slug, lambda slug=slug: kbd.pause_dispatch(slug, note=note))
            results.append({"board": slug, **result})
        except Exception as exc:
            failures.append({"board": slug, "error": str(exc)})
    paused_count = sum(1 for result in results if result.get("paused"))
    return {
        "paused": bool(slugs) and paused_count == len(slugs),
        "state": None,
        "board_count": len(slugs),
        "paused_count": paused_count,
        "results": results,
        "failures": failures,
    }


@router.post("/dispatch/resume")
def dispatch_resume(board: Optional[str] = _BOARD_Q, boards: Optional[str] = Query(None)):
    """Clear one board's pause or every active board pause."""
    slugs = _dispatch_board_slugs(board, boards)
    if slugs is None:
        target = _dispatch_target_board(board)
        return _with_board_pinned(target, lambda: kbd.resume_dispatch(target))

    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for slug in slugs:
        try:
            result = _with_board_pinned(slug, lambda slug=slug: kbd.resume_dispatch(slug))
            results.append({"board": slug, **result})
        except Exception as exc:
            failures.append({"board": slug, "error": str(exc)})
    resumed_count = sum(1 for result in results if result.get("resumed"))
    return {
        "resumed": bool(slugs) and resumed_count == len(slugs),
        "was_paused": any(result.get("was_paused") for result in results),
        "board_count": len(slugs),
        "resumed_count": resumed_count,
        "results": results,
        "failures": failures,
    }


class PostDrainBody(BaseModel):
    """Queue request. ``target`` may only NAME an allowlisted unit, never define one."""

    action_kind: str
    target: Optional[str] = None
    expires_in_seconds: Optional[int] = None


@router.post("/dispatch/post-drain")
def dispatch_queue_post_drain(
    payload: PostDrainBody,
    board: Optional[str] = _BOARD_Q,
    boards: Optional[str] = Query(None),
):
    """Queue an action to fire automatically once this scope drains to 0 running.

    Only the INTENT is stored here. The trigger itself lives in the dispatcher
    tick, so the action fires whether or not this dashboard — or any browser —
    is still connected when the board finally drains.
    """
    slugs = _dispatch_board_slugs(board, boards)
    requested_by = kanban_db._hook_profile_name()

    def _queue(slug: Optional[str], group_id: Optional[str] = None) -> dict[str, Any]:
        try:
            return kbpd.queue_post_drain_action(
                slug,
                action_kind=payload.action_kind,
                target=payload.target,
                requested_by=requested_by,
                expires_in_seconds=payload.expires_in_seconds,
                group_id=group_id,
            )
        except kbpd.PostDrainActionRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if slugs is None:
        target_board = _dispatch_target_board(board)
        return {"queued": True, "state": _queue(target_board)}

    # Validate ONCE against the shared registry/config before writing anything:
    # a rejected request must not leave half the boards armed.
    if payload.action_kind not in kbpd.ACTION_HANDLERS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown post-drain action {payload.action_kind!r}",
        )
    group_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    try:
        group = kbpd.queue_post_drain_group(
            slugs,
            action_kind=payload.action_kind,
            target=payload.target,
            requested_by=requested_by,
            expires_in_seconds=payload.expires_in_seconds,
            group_id=group_id,
        )
    except kbpd.PostDrainActionRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    results = [
        {"board": slug, "state": state}
        for slug, state in group["records"].items()
    ]
    failures = group["failures"]
    return {
        "queued": group["queued"],
        "board_count": len(slugs),
        "queued_count": len(results) if group["queued"] else 0,
        "group_id": group_id,
        "results": results,
        "failures": failures,
    }


@router.delete("/dispatch/post-drain")
def dispatch_cancel_post_drain(
    board: Optional[str] = _BOARD_Q,
    boards: Optional[str] = Query(None),
):
    """Cancel a waiting action. An action already firing is left alone."""
    slugs = _dispatch_board_slugs(board, boards)
    if slugs is None:
        target_board = _dispatch_target_board(board)
        return kbpd.cancel_post_drain_action(target_board)

    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for slug in slugs:
        try:
            results.append({"board": slug, **kbpd.cancel_post_drain_action(slug)})
        except Exception as exc:
            failures.append({"board": slug, "error": str(exc)})
    cancelled_count = sum(1 for result in results if result.get("cancelled"))
    return {
        "cancelled": bool(slugs) and cancelled_count == len(slugs),
        "board_count": len(slugs),
        "cancelled_count": cancelled_count,
        "results": results,
        "failures": failures,
    }
