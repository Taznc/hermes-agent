"""Transport adapters share admission and canonical setters in one transaction."""

from __future__ import annotations

import asyncio

from hermes_constants import get_hermes_home, hermes_home_key

from . import SessionArchiveBlocked, TransactionView, assert_archivable
from .activity import owned_work_scope, record_home


def archive_mutation(db, session_id, mutate, aliases=()):
    events = []
    with owned_work_scope(db, session_id, aliases) as work:

        def update(conn):
            events.clear()  # the write may retry; keep only the committed attempt's flips
            assert_archivable(db, conn, session_id, work)
            view = TransactionView(db, conn)
            result = mutate(view)
            events.extend(view.archive_events)
            return result

        result = db._execute_write(update)
    # Upstream's on_session_delete (#124511) for each deliberate archive, after commit.
    from hermes_state_common import emit_session_deleted

    for ids, primary in events:
        emit_session_deleted(db, ids, reason="archive", primary=primary)
    return result


async def messaging_patch(adapter, db, session_id, body):
    from aiohttp import web

    from gateway.platforms.api_server import _error_response

    def mutate(view):
        if "title" in body:
            view.set_session_title(
                session_id, "" if body["title"] is None else str(body["title"])
            )
        for flag, setter in (
            ("archived", view.set_session_archived),
            ("hidden", view.set_session_hidden),
            ("pinned", view.set_session_pinned),
        ):
            if flag in body:
                setter(session_id, body[flag])
        if "unread" in body:
            view.set_session_read(session_id, read=not body["unread"])
        if body.get("end_reason"):
            view.end_session(session_id, str(body["end_reason"]))
        return view.get_session(session_id)

    try:
        session = await asyncio.to_thread(archive_mutation, db, session_id, mutate)
    except SessionArchiveBlocked as exc:
        return _error_response(str(exc), 409, code="session_archive_blocked")
    except ValueError as exc:
        return _error_response(str(exc), 400, code="invalid_title")
    return web.json_response({
        "object": "hermes.session",
        "session": adapter._session_response(session),
    })


def rpc_target(params, scope, *, writer=True):
    """Live id is an alias only inside the requested profile; durable fallback otherwise."""
    target = str(params.get("session_id") or params.get("session_key") or "")
    session = scope["_sessions"].get(target)
    if session is not None and params.get("profile"):
        home = scope["_profile_home"](params["profile"])
        if hermes_home_key(
            record_home(session) or get_hermes_home()
        ) != hermes_home_key(home):
            session = None
    context = (
        scope["_profile_db"](params, writer=writer)
        if session is None
        else scope["_session_db"](session)
    )
    return target, session, context


def archive_rpc(rid, params, scope):
    target, session, context = rpc_target(params, scope)
    if not target:
        return scope["_err"](rid, 4006, "session_id required")
    with context as db:
        if db is None:
            return scope["_db_unavailable_error"](rid, code=5007)
        key = (
            session["session_key"]
            if session is not None
            else (
                db.resolve_session_id(target)
                if hasattr(db, "resolve_session_id")
                else target
            )
        )
        if not key:
            return scope["_err"](rid, 4001, "session not found")
        try:
            changed = archive_mutation(
                db, key, lambda view: view.set_session_archived(key, True), (target,)
            )
            if session is not None and not changed:
                session["pending_archived"] = True
            return scope["_ok"](rid, {"archived": True, "session_key": key})
        except SessionArchiveBlocked as exc:
            return scope["_err"](rid, 4024, str(exc))
        except Exception as exc:
            return scope["_err"](rid, 5007, str(exc))
