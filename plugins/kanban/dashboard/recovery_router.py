"""Kanban dashboard — recovery actions: reclaim / specify / reassign / estimate."""

from __future__ import annotations

import json
import re
from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from hermes_cli import kanban_db

from plugins.kanban.dashboard._common import (
    _board_conn,
    _conflict,
    _require_task,
    _run_aux,
    _value_error_400,
)

router = APIRouter()


class ReclaimBody(BaseModel):
    reason: Optional[str] = None


@router.post("/tasks/{task_id}/reclaim")
def reclaim_task_endpoint(task_id: str, payload: ReclaimBody, board: Optional[str] = Query(None)):
    """Release an active worker claim without waiting for the claim TTL
    (``hermes kanban reclaim <task_id> --reason ...``)."""
    with _board_conn(board) as (board, conn):
        if not kanban_db.reclaim_task(conn, task_id, reason=payload.reason):
            raise _conflict(f"cannot reclaim {task_id}: not in a claimable state (not running, or unknown id)")
        return {"ok": True, "task_id": task_id}


class SpecifyBody(BaseModel):
    """Only the author is configurable; model + prompt come from
    ``auxiliary.triage_specifier`` in config.yaml, same as the CLI."""

    author: Optional[str] = None


@router.post("/tasks/{task_id}/specify")
def specify_task_endpoint(task_id: str, payload: SpecifyBody, board: Optional[str] = Query(None)):
    """Flesh out a triage task via the auxiliary LLM (``hermes kanban specify``). Non-OK is NOT
    an HTTP error — the UI renders the reason inline. Sync ``def`` → runs in the threadpool."""
    outcome = _run_aux(board, "kanban_specify", "specify_task", task_id, payload.author)
    return {"ok": bool(outcome.ok), "task_id": outcome.task_id, "reason": outcome.reason, "new_title": outcome.new_title}


class ReassignBody(BaseModel):
    profile: Optional[str] = None  # "" or None = unassign
    reclaim_first: bool = False
    reason: Optional[str] = None


@router.post("/tasks/{task_id}/reassign")
def reassign_task_endpoint(task_id: str, payload: ReassignBody, board: Optional[str] = Query(None)):
    """Reassign to another profile, optionally reclaiming first
    (``hermes kanban reassign <task_id> <profile> [--reclaim]``)."""
    with _board_conn(board) as (board, conn), _value_error_400():
        ok = kanban_db.reassign_task(
            conn, task_id, payload.profile or None, reclaim_first=bool(payload.reclaim_first), reason=payload.reason)
        if not ok:
            raise _conflict(
                f"cannot reassign {task_id}: unknown id, or still "
                "running (pass reclaim_first=true to release the claim first)")
        return {"ok": True, "task_id": task_id, "assignee": payload.profile or None}


# Estimate: rough token/complexity read via the auxiliary model. NOT a dollar cost.
_ESTIMATE_SYSTEM_PROMPT = (
    "You estimate how much work an autonomous coding agent will spend on a "
    "kanban task. Given the task title and description, respond with STRICT "
    "JSON only (no prose, no code fence):\n"
    '{"est_tokens": <integer total tokens across the whole run>, '
    '"complexity": "S"|"M"|"L", '
    '"rationale": "<one short sentence>"}\n'
    "Base the token figure on a realistic multi-turn agent run (reading files, "
    "tool calls, edits, retries) — not a single reply. S≈small/localized, "
    "M≈multi-file, L≈broad or ambiguous. Be honest that this is a rough guess.")


class EstimateBody(BaseModel):
    title: str = ""
    body: Optional[str] = None


@router.post("/estimate")
def estimate_text_endpoint(payload: EstimateBody):
    """Estimate from raw title/body (create dialog, before a task exists)."""
    return _run_estimate(payload.title, payload.body)


@router.post("/tasks/{task_id}/estimate")
def estimate_task_endpoint(task_id: str, board: Optional[str] = Query(None)):
    """Estimate for an existing task; ``{ok, est_tokens, complexity, rationale, model}``."""
    with _board_conn(board) as (board, conn):
        task = _require_task(conn, task_id)
    return _run_estimate(task.title, task.body)


def _cap(s: Optional[str], n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def _run_estimate(title: str, body: Optional[str]) -> dict:
    """Never raises — config/parse/API errors become ``{"ok": False, "reason"}`` so the UI renders them inline."""
    if not (title or "").strip():
        return {"ok": False, "reason": "a title is required to estimate"}
    try:
        from agent.auxiliary_client import call_llm
    except Exception:
        return {"ok": False, "reason": "auxiliary client unavailable"}
    user_msg = f"Title: {_cap(title, 400)}\n\nDescription:\n{_cap(body, 4000) or '(none)'}"
    try:
        resp = call_llm(
            task="kanban_estimator",
            messages=[{"role": "system", "content": _ESTIMATE_SYSTEM_PROMPT}, {"role": "user", "content": user_msg}],
            temperature=0.0, max_tokens=300, timeout=60)
    except Exception as exc:
        return {"ok": False, "reason": f"LLM error: {type(exc).__name__}"}
    try:
        raw = (resp.choices[0].message.content or "").strip()
        model = getattr(resp, "model", None)
    except Exception:
        raw, model = "", None

    # Same tolerant JSON-blob extraction the specifier uses.
    try:
        m = None if raw.lstrip().startswith("{") else re.search(r"\{.*\}", raw, re.DOTALL)
        obj = json.loads(m.group(0) if m else raw)
        parsed = obj if isinstance(obj, dict) else None
    except Exception:
        parsed = None
    if not parsed:
        return {"ok": False, "reason": "could not parse an estimate from the model"}
    try:
        est_tokens = int(parsed.get("est_tokens") or 0)
    except (TypeError, ValueError):
        est_tokens = 0
    complexity = str(parsed.get("complexity") or "").strip().upper()
    return {
        "ok": True, "est_tokens": est_tokens, "complexity": complexity if complexity in {"S", "M", "L"} else None,
        "rationale": str(parsed.get("rationale") or "").strip() or None, "model": model}
