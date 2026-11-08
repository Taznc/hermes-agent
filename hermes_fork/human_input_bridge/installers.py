"""Named T2 installers; all behavior remains fork-owned."""
import asyncio
import contextvars
import functools
import threading
import time

from .runtime import bind_agent, release_agent


def install_turn_runner(cls):
    if getattr(cls, "_fork_human_input_installed", False):
        return
    cls._fork_human_input_installed = True
    run = cls._run_conversation_with_approval
    @functools.wraps(run)
    def wrapped(turn, agent, *args, **kwargs):
        from gateway.run_turn_progress import local_viewer_turn
        local_viewer = local_viewer_turn(turn)
        owner = bind_agent(agent, messaging=not local_viewer)
        turn._fork_human_input_owner = owner
        events = None
        old_write = None
        if owner and local_viewer and turn._approval_owner is not None:
            authority, sid, _ = turn._approval_owner
            live = authority.sessions.get(sid)
            if live is not None:
                events = live.event_stream
                old_write = events.fanout.write
                def write(frame, **kw):
                    if frame.get("method") == "clarify":
                        from tools.clarify_gateway import get_clarify_timeout
                        timeout = get_clarify_timeout()
                        owner.watch_authority(events, sid, frame["id"], kind="clarify",
                                              payload={k: v for k, v in frame["params"].items() if k != "session_id"}, delay_seconds=owner.delay_seconds,
                                              expires_at=time.time() + timeout if timeout > 0 else None)
                    return old_write(frame, **kw)
                events.fanout.write = write
        try:
            return run(turn, agent, *args, **kwargs)
        finally:
            if events is not None:
                events.fanout.write = old_write
            turn._fork_human_input_owner = None
            release_agent(agent)
    cls._run_conversation_with_approval = wrapped
    notify = cls._approval_notify_sync
    @functools.wraps(notify)
    def approval(turn, data):
        notify(turn, data)
        owner = getattr(turn, "_fork_human_input_owner", None)
        if owner:
            owner.watch_native_approval(turn._ctx.session_key, data["request_id"],
                                        delay_seconds=owner.delay_seconds)
    cls._approval_notify_sync = approval
    clarify = cls._send_shared_clarify
    @functools.wraps(clarify)
    async def send_clarify(turn, entry, **kwargs):
        result = await clarify(turn, entry, **kwargs)
        owner = getattr(turn, "_fork_human_input_owner", None)
        if owner:
            from tools.clarify_gateway import get_clarify_timeout
            timeout = get_clarify_timeout()
            await asyncio.to_thread(owner.watch_native_clarify, entry.clarify_id,
                                    expires_at=time.time() + timeout if timeout > 0 else None,
                                    delay_seconds=owner.delay_seconds)
        return result
    cls._send_shared_clarify = send_clarify


def install_sidecar(server):
    """Called by existing plugin-ui installer after the facade has bound its functions."""
    if getattr(server, "_fork_human_input_installed", False):
        return
    server._fork_human_input_installed = True
    ticker = server._start_usage_ticker
    @functools.wraps(ticker)
    def start(sid, agent, *args, **kwargs):
        owner = bind_agent(agent)
        if owner:
            server._sessions[sid]["_fork_human_input_owner"] = owner
        session = server._sessions[sid]
        stop, thread = ticker(sid, agent, *args, **kwargs)
        def close():
            stop.wait()
            if owner:
                owner.close()
                if getattr(agent, "_fork_human_input_owner", None) is owner:
                    release_agent(agent)
            if server._sessions.get(sid) is session and session.get("_fork_human_input_owner") is owner:
                session.pop("_fork_human_input_owner", None)
        context = contextvars.copy_context()
        threading.Thread(target=context.run, args=(close,), daemon=True, name="human-input-teardown").start()
        return stop, thread
    server._start_usage_ticker = start
    from tui_gateway import server_requests as source
    register = source._register
    @functools.wraps(register)
    def registered(req):
        register(req)
        if req.method not in ("clarify", "approval"):
            return
        owner = server._sessions.get(req.sid, {}).get("_fork_human_input_owner")
        if owner:
            if req.method == "approval":
                owner.watch_native_approval(server._sessions[req.sid].get("session_key", ""), req.params.get("request_id"),
                                            delay_seconds=owner.delay_seconds)
            else:
                owner.watch_tui(req.sid, req.id, kind="clarify", payload=req.params,
                                delay_seconds=owner.delay_seconds)
    source._register = registered
