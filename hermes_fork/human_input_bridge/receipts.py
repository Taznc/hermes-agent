"""Observe retained source primitives, not disappearance from pending registries."""
import functools


def tui_cancellations(source):
    """TUI timeout does not set its event/result; retain its explicit cancel reason."""
    emit = source._emit_cancel
    if getattr(emit, "_fork_receipt", False):
        return
    @functools.wraps(emit)
    def observed(req, reason):
        req.event._fork_terminal_reason = reason
        return emit(req, reason)
    observed._fork_receipt = True
    source._emit_cancel = observed


def structured_receipt(pending):
    result = pending.result
    if isinstance(result, dict):
        return result.get("outcome", "submitted"), result
    reason = getattr(pending.event, "_fork_terminal_reason", None)
    if reason is not None:
        return ("timed_out" if reason == "timeout" else "cancelled"), None
    if pending.event.is_set():
        return "cancelled", None
    return None


def native_receipt(entry, source):
    if not entry.event.is_set():
        return None
    if entry.response == source.CANCELLED:
        return "cancelled", None
    if entry.response is None:
        return "timed_out", None
    return "submitted", None
