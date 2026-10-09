"""Transport adapters share admission and canonical setters in one transaction."""

from __future__ import annotations

import asyncio

from hermes_constants import get_hermes_home, hermes_home_key

from . import SessionArchiveBlocked, TransactionView, assert_archivable
from .activity import owned_work_scope, record_home


def archive_mutation(db, session_id, mutate, aliases=()):
    with owned_work_scope(db, session_id, aliases) as work:

        def update(conn):
            assert_archivable(db, conn, session_id, work)
            return mutate(TransactionView(db, conn))

        return db._execute_write(update)


async def rest_patch(session_id, body, scope):
    from fastapi import HTTPException

    def update(db):
        sid = scope["_resolve_session_id"](db, session_id)
        if not sid:
            raise HTTPException(status_code=404, detail="Session not found")

        def mutate(view):
            if body.title is not None:
                view.set_session_title(sid, body.title or "")
            result = {"ok": True, "title": None}
            for flag, setter in scope["_RENAME_FLAG_SETTERS"]:
                value = getattr(body, flag)
                if value is not None:
                    setter(view, sid, value)
                    result[flag] = bool(value)
            result["title"] = view.get_session_title(sid) or ""
            return result

        try:
            return archive_mutation(db, sid, mutate)
        except SessionArchiveBlocked as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "session_archive_blocked",
                    "message": str(exc),
                    "blockers": exc.blockers,
                },
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return await asyncio.to_thread(
        scope["_with_db"], body.profile, update, read_only=False
    )


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
