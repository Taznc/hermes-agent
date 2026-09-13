"""Tests for the multi-board fan-out on the kanban events WebSocket (``/events?boards=``).

Covers the behavior contract from the "live WebSocket updates for the All Boards view" card:
  - the legacy single-board request shape (``?since=&board=``) is byte-identical to before
    this change — it is a completely separate code path, never retrofitted;
  - a multi-board request (``?boards=a,b``) emits frames shaped
    ``{"events": [{"board": ..., ...}], "cursors": {...}}``;
  - a board that raises mid-poll is dropped from that socket without killing the stream for
    the other boards;
  - disconnect closes every per-board connection (no leak), on the SAME single-worker
    executor as the single-board path (never a pool per board).
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
from pathlib import Path

import pytest


def _load_plugin_module():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_ws_multi_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _PollingWebSocket:
    """Accepts, echoes query params, and answers receive() only when told to disconnect."""

    def __init__(self, query_params: dict[str, str] | None = None):
        self.accepted = False
        self.sent: list[dict] = []
        self.query_params: dict[str, str] = query_params or {}
        self._disconnect = asyncio.Event()

    async def accept(self):
        self.accepted = True

    async def receive(self):
        await self._disconnect.wait()
        return {"type": "websocket.disconnect"}

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=None):
        pass


class _TrackingConnection:
    """A fake sqlite3 connection whose rows-per-poll are pre-scripted per board."""

    def __init__(self, rows_by_poll=None, raise_on_poll: int | None = None):
        self.rows_by_poll = list(rows_by_poll or [])
        self.raise_on_poll = raise_on_poll
        self.execute_calls = 0
        self.close_calls = 0
        self.thread_ids: list[int] = []
        self._rows: list[dict] = []

    def execute(self, sql, params):
        self.execute_calls += 1
        self.thread_ids.append(threading.get_ident())
        if self.raise_on_poll is not None and self.execute_calls == self.raise_on_poll:
            raise RuntimeError("simulated board failure")
        poll_index = self.execute_calls - 1
        self._rows = self.rows_by_poll[poll_index] if poll_index < len(self.rows_by_poll) else []
        return self

    def fetchall(self):
        return self._rows

    def close(self):
        self.close_calls += 1
        self.thread_ids.append(threading.get_ident())


def _row(id_, task_id, kind="updated", payload='{"status": "running"}'):
    return {"id": id_, "task_id": task_id, "run_id": None, "kind": kind, "payload": payload, "created_at": 1234}


@pytest.mark.asyncio
async def test_single_board_shape_unchanged_without_boards_param(monkeypatch):
    """``?since=&board=`` (no ``boards=``) must still hit the legacy single-board loop and
    return today's exact frame shape: {"events": [...], "cursor": <int>} — no "board" key on
    the frame, no "cursors" map."""
    mod = _load_plugin_module()
    monkeypatch.setattr(mod, "_ws_upgrade_authorized", lambda ws: True)

    conn = _TrackingConnection(rows_by_poll=[[_row(7, "task-1")]])
    monkeypatch.setattr(mod.kbc, "connect", lambda *, board=None: conn)

    wait_calls = 0

    async def _poll_once_then_disconnect(awaitable, timeout):
        nonlocal wait_calls
        wait_calls += 1
        awaitable.close()
        if wait_calls == 1:
            raise asyncio.TimeoutError
        return {"type": "websocket.disconnect"}

    monkeypatch.setattr(mod.asyncio, "wait_for", _poll_once_then_disconnect)
    ws = _PollingWebSocket({"board": "default", "since": "0"})

    await mod.stream_events(ws)

    assert ws.accepted
    assert ws.sent == [{
        "events": [{
            "id": 7, "task_id": "task-1", "run_id": None, "kind": "updated",
            "payload": {"status": "running"}, "created_at": 1234,
        }],
        "cursor": 7,
    }]
    # Byte-identical shape check: no multi-board keys leaked onto the legacy frame.
    assert "board" not in ws.sent[0]["events"][0] or True  # legacy fetch doesn't tag "board" — see below
    assert set(ws.sent[0].keys()) == {"events", "cursor"}
    assert "board" not in ws.sent[0]["events"][0]


@pytest.mark.asyncio
async def test_multi_board_frames_carry_board_and_cursor_map(monkeypatch):
    mod = _load_plugin_module()
    monkeypatch.setattr(mod, "_ws_upgrade_authorized", lambda ws: True)

    conn_a = _TrackingConnection(rows_by_poll=[[_row(3, "task-a")]])
    conn_b = _TrackingConnection(rows_by_poll=[[_row(9, "task-b")]])
    conns = {"board-a": conn_a, "board-b": conn_b}
    monkeypatch.setattr(mod.kbc, "connect", lambda *, board=None: conns[board])

    wait_calls = 0

    async def _poll_once_then_disconnect(awaitable, timeout):
        nonlocal wait_calls
        wait_calls += 1
        awaitable.close()
        if wait_calls == 1:
            raise asyncio.TimeoutError
        return {"type": "websocket.disconnect"}

    monkeypatch.setattr(mod.asyncio, "wait_for", _poll_once_then_disconnect)
    ws = _PollingWebSocket({"boards": "board-a,board-b"})

    await mod.stream_events(ws)

    assert ws.accepted
    assert len(ws.sent) == 1
    frame = ws.sent[0]
    assert set(frame.keys()) == {"events", "cursors"}
    boards_seen = {e["board"] for e in frame["events"]}
    assert boards_seen == {"board-a", "board-b"}
    assert frame["cursors"] == {"board-a": 3, "board-b": 9}
    task_ids = {e["task_id"] for e in frame["events"]}
    assert task_ids == {"task-a", "task-b"}


@pytest.mark.asyncio
async def test_multi_board_seeds_cursors_from_query_param(monkeypatch):
    """``cursors=<json>`` seeds the per-board starting point (as returned by /board/all),
    so events at-or-below the seed are never re-delivered."""
    mod = _load_plugin_module()
    monkeypatch.setattr(mod, "_ws_upgrade_authorized", lambda ws: True)

    # Only events with id > seed should ever be selected — enforce that by having the
    # fake connection assert the cursor it receives matches the seed on the first poll.
    seen_cursors: list[int] = []

    class _SeedCheckingConnection(_TrackingConnection):
        def execute(self, sql, params):
            seen_cursors.append(params[0])
            return super().execute(sql, params)

    conn = _SeedCheckingConnection(rows_by_poll=[[_row(101, "task-x")]])
    monkeypatch.setattr(mod.kbc, "connect", lambda *, board=None: conn)

    wait_calls = 0

    async def _poll_once_then_disconnect(awaitable, timeout):
        nonlocal wait_calls
        wait_calls += 1
        awaitable.close()
        if wait_calls == 1:
            raise asyncio.TimeoutError
        return {"type": "websocket.disconnect"}

    monkeypatch.setattr(mod.asyncio, "wait_for", _poll_once_then_disconnect)
    ws = _PollingWebSocket({"boards": "seeded", "cursors": '{"seeded": 100}'})

    await mod.stream_events(ws)

    assert seen_cursors == [100]
    assert ws.sent[0]["cursors"] == {"seeded": 101}


@pytest.mark.asyncio
async def test_multi_board_failing_board_does_not_kill_the_socket(monkeypatch):
    """A board that raises mid-stream is dropped from THIS socket; the other boards keep
    streaming, and no exception escapes to close the connection."""
    mod = _load_plugin_module()
    monkeypatch.setattr(mod, "_ws_upgrade_authorized", lambda ws: True)

    good = _TrackingConnection(rows_by_poll=[[_row(1, "task-good")], [_row(2, "task-good-2")]])
    bad = _TrackingConnection(rows_by_poll=[[_row(1, "task-bad")]], raise_on_poll=2)
    conns = {"good": good, "bad": bad}
    monkeypatch.setattr(mod.kbc, "connect", lambda *, board=None: conns[board])

    wait_calls = 0

    async def _poll_twice_then_disconnect(awaitable, timeout):
        nonlocal wait_calls
        wait_calls += 1
        awaitable.close()
        if wait_calls <= 2:
            raise asyncio.TimeoutError
        return {"type": "websocket.disconnect"}

    monkeypatch.setattr(mod.asyncio, "wait_for", _poll_twice_then_disconnect)
    ws = _PollingWebSocket({"boards": "good,bad"})

    await mod.stream_events(ws)  # must not raise despite "bad" erroring on its 2nd poll

    assert ws.accepted
    # First frame: both boards contributed (bad succeeded on poll 1).
    assert ws.sent[0]["events"] and {e["board"] for e in ws.sent[0]["events"]} == {"good", "bad"}
    # Second frame: only "good" contributed — "bad" errored and was dropped, but the socket
    # kept running and "good"'s new event still arrived.
    later_boards = {e["board"] for frame in ws.sent[1:] for e in frame["events"]}
    assert later_boards == {"good"}
    assert bad.close_calls == 1  # the failed board's connection was closed when dropped


@pytest.mark.asyncio
async def test_multi_board_closes_every_connection_on_disconnect(monkeypatch):
    """Disconnect must close EVERY per-board connection — a leaked connection per idle board
    is exactly what the single-connection design elsewhere in this module avoids."""
    mod = _load_plugin_module()
    monkeypatch.setattr(mod, "_ws_upgrade_authorized", lambda ws: True)

    conn_a = _TrackingConnection(rows_by_poll=[[_row(1, "a")]])
    conn_b = _TrackingConnection(rows_by_poll=[[_row(1, "b")]])
    conn_c = _TrackingConnection(rows_by_poll=[[_row(1, "c")]])
    conns = {"a": conn_a, "b": conn_b, "c": conn_c}
    connect_threads: list[int] = []

    def _connect(*, board=None):
        connect_threads.append(threading.get_ident())
        return conns[board]

    monkeypatch.setattr(mod.kbc, "connect", _connect)

    wait_calls = 0

    async def _poll_once_then_disconnect(awaitable, timeout):
        nonlocal wait_calls
        wait_calls += 1
        awaitable.close()
        if wait_calls == 1:
            raise asyncio.TimeoutError
        return {"type": "websocket.disconnect"}

    monkeypatch.setattr(mod.asyncio, "wait_for", _poll_once_then_disconnect)
    ws = _PollingWebSocket({"boards": "a,b,c"})

    await mod.stream_events(ws)

    assert conn_a.close_calls == 1
    assert conn_b.close_calls == 1
    assert conn_c.close_calls == 1
    # ONE executor for the whole socket: every connect/execute/close ran on the same thread.
    all_threads = set(connect_threads + conn_a.thread_ids + conn_b.thread_ids + conn_c.thread_ids)
    assert len(all_threads) == 1


@pytest.mark.asyncio
async def test_boards_star_resolves_to_every_board_on_disk(monkeypatch):
    mod = _load_plugin_module()
    monkeypatch.setattr(
        mod.kanban_db, "list_boards",
        lambda include_archived=False: [{"slug": "one"}, {"slug": "two"}, {"slug": "three"}],
    )
    resolved = mod._ws_boards_param("*")
    assert resolved == ["one", "two", "three"]


def test_boards_param_absent_selects_legacy_path():
    mod = _load_plugin_module()
    assert mod._ws_boards_param(None) is None


def test_boards_param_dedupes_and_normalizes():
    mod = _load_plugin_module()
    assert mod._ws_boards_param("Foo, foo, bar") == ["foo", "bar"]


def test_boards_param_caps_at_max():
    mod = _load_plugin_module()
    many = ",".join(f"board{i}" for i in range(mod._MAX_TAILED_BOARDS + 10))
    resolved = mod._ws_boards_param(many)
    assert resolved is not None
    assert len(resolved) == mod._MAX_TAILED_BOARDS


def test_cursors_param_parses_and_degrades_gracefully():
    mod = _load_plugin_module()
    assert mod._ws_cursors_param('{"a": 5, "b": "10"}') == {"a": 5, "b": 10}
    assert mod._ws_cursors_param(None) == {}
    assert mod._ws_cursors_param("") == {}
    assert mod._ws_cursors_param("not json") == {}
    assert mod._ws_cursors_param("[1, 2]") == {}
    assert mod._ws_cursors_param('{"a": "not-an-int"}') == {}
