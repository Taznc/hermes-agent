"""Fork RPCs for the Desktop "Keep full context" chip (spec t_07c75c42 §4).

``fork.session.compaction_defer`` cancels the focused session's automatic compaction before it commits and
raises that session's watermark; ``fork.session.compaction_watermark`` reads (and lazily loads) or clears
it. Both run inline on the gateway reader: the only blocking is the defer's bounded <= 250 ms fence retry,
and durable writes go to the fork's single-worker executor.
"""

from __future__ import annotations

from typing import Any, Optional

from hermes_fork.compaction import watermark as wm
from hermes_fork.compaction.defer import defer_active_compaction
from hermes_fork.gateway.registry import ForkRpcError, fork_method
from tui_gateway.contracts.base import Params, Result, WireEnum


class DeferStatus(WireEnum):
    deferred = "deferred"
    too_late = "too_late"
    busy = "busy"
    not_running = "not_running"
    refused = "refused"


class DeferReason(WireEnum):
    manual = "manual"
    overflow = "overflow"
    at_ceiling = "at_ceiling"
    unsupported = "unsupported"


class WatermarkAction(WireEnum):
    get = "get"
    clear = "clear"


class CompactionDeferParams(Params):
    session_id: str


class CompactionDeferResult(Result):
    status: DeferStatus
    reason: DeferReason | None = None
    watermark_tokens: int | None = None
    previous_threshold_tokens: int | None = None
    ceiling_tokens: int | None = None
    usable_tokens: int | None = None
    context_length: int | None = None


class CompactionWatermarkParams(Params):
    session_id: str
    action: WatermarkAction = WatermarkAction.get


class CompactionWatermarkResult(Result):
    active: bool
    watermark_tokens: int | None = None
    threshold_tokens: int | None = None
    base_threshold_tokens: int | None = None
    ceiling_tokens: int | None = None
    context_length: int | None = None


def _server():
    import tui_gateway.server as server

    return server


def _live_session(params: dict) -> tuple[Any, dict]:
    server = _server()
    session = server._sessions.get(str(params.get("session_id") or ""))
    if session is None:
        raise ForkRpcError(4001, "session not found")
    return server, session


def _uses_compute_host(session: dict) -> bool:
    try:
        from tui_gateway.compute_host_bridge import _session_uses_compute_host
    except Exception:
        return False
    try:
        return bool(_session_uses_compute_host(session))
    except Exception:
        return False


def _empty_watermark() -> dict:
    return {"active": False, "watermark_tokens": None, "threshold_tokens": None, "base_threshold_tokens": None,
            "ceiling_tokens": None, "context_length": None}


@fork_method(
    "fork.session.compaction_defer",
    params=CompactionDeferParams,
    result=CompactionDeferResult,
    doc="Skip the focused session's running automatic compaction (keep full context) and raise its watermark.",
)
def compaction_defer(params: dict) -> dict:
    _server_mod, session = _live_session(params)
    if _uses_compute_host(session):
        return {"status": "refused", "reason": "unsupported"}
    agent = session.get("agent")
    if agent is None:
        return {"status": "not_running", "reason": None}
    return defer_active_compaction(agent)


def _owner_db(session: dict):
    """Profile-aware SessionDB context for *session*'s row (same helper the workdir RPCs use)."""
    from tui_gateway.session_workdir import _session_db

    return _session_db(session)


def _durable_record(session: dict) -> Optional[dict]:
    key = str(session.get("session_key") or "")
    if not key:
        return None
    with _owner_db(session) as db:
        if db is None:
            return None
        raw = db.get_session_model_config_value(key, wm.KEY)
    record, _reason = wm.validate_record(raw) if raw is not None else (None, None)
    return record


def _clear_durable(session: dict) -> None:
    key = str(session.get("session_key") or "")
    if not key:
        return

    def _write() -> None:
        with _owner_db(session) as db:
            if db is not None:
                db.patch_session_model_config(key, {wm.KEY: None})

    wm._get_executor().submit(_write)


@fork_method(
    "fork.session.compaction_watermark",
    params=CompactionWatermarkParams,
    result=CompactionWatermarkResult,
    doc="Read (lazily loading the durable record) or clear the session's compaction watermark.",
)
def compaction_watermark(params: dict) -> dict:
    _server_mod, session = _live_session(params)
    action = str(params.get("action") or "get")
    agent = session.get("agent")
    cc = getattr(agent, "context_compressor", None) if agent is not None else None
    if cc is None or _uses_compute_host(session):
        # No in-process compressor: answer from the durable row only.
        if action == "clear":
            _clear_durable(session)
            return _empty_watermark()
        try:
            record = _durable_record(session)
        except Exception:
            record = None
        if record is None:
            return _empty_watermark()
        return {"active": True, "watermark_tokens": record["tokens"], "threshold_tokens": None,
                "base_threshold_tokens": record.get("base_threshold_tokens"),
                "ceiling_tokens": record.get("ceiling_tokens"), "context_length": record["context_length"]}
    if action == "clear":
        wm.clear_watermark(cc)
    return wm.watermark_status(cc)
