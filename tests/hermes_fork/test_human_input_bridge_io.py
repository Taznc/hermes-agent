"""Bounded owner I/O: reads do not contend as writers; async hooks retain scope."""
import asyncio
import contextvars
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from hermes_fork.human_input_bridge.mailbox import Mailbox


def test_owner_reads_do_not_acquire_sqlite_writer_lock(tmp_path, monkeypatch):
    box = Mailbox(tmp_path / "private" / "box.db")
    scope = dict(user_id=1, chat_id=2, thread_id=None, bot_id=3)
    handle = box.publish(kind="ask", payload={}, owner_id="owner", request_id="r", recipient=scope)
    connect = sqlite3.connect
    # _transaction itself sets a timeout; fail immediately on its BEGIN IMMEDIATE.
    class NoWriter(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                super().execute("PRAGMA busy_timeout=0")
            return super().execute(sql, *args)
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: connect(*a, **{**kw, "factory": NoWriter}))
    with connect(box.path, isolation_level=None) as writer:
        writer.execute("BEGIN IMMEDIATE")
        assert box.get(handle)["owner_id"] == "owner"
        assert box.side_requests("owner") == []
        writer.rollback()


@pytest.mark.asyncio
async def test_native_projection_is_off_loop_and_preserves_context(monkeypatch):
    from hermes_fork.human_input_bridge.installers import install_turn_runner
    scope = contextvars.ContextVar("projection_scope", default="wrong")
    scope.set("owning-profile")
    event_loop_thread = threading.get_ident()
    observed = []
    class Turn:
        def _run_conversation_with_approval(self):
            pass
        def _approval_notify_sync(self, data):
            pass
        async def _send_shared_clarify(self, entry, **kwargs):
            return "rendered"
    def watch(*args, **kwargs):
        observed.append((threading.get_ident(), scope.get()))
    install_turn_runner(Turn)
    turn = Turn()
    turn._fork_human_input_owner = SimpleNamespace(delay_seconds=120, watch_native_clarify=watch)
    monkeypatch.setattr("tools.clarify_gateway.get_clarify_timeout", lambda: 10)
    assert await turn._send_shared_clarify(SimpleNamespace(clarify_id="exact")) == "rendered"
    assert observed[0][0] != event_loop_thread
    assert observed[0][1] == "owning-profile"
