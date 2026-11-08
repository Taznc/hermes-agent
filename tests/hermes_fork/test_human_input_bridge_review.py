"""Reproduced source-owner races and truthful terminal receipts."""
import threading
from types import SimpleNamespace

import pytest

from hermes_fork.human_input_bridge.mailbox import Mailbox
from hermes_fork.human_input_bridge.owner import OwnerBridge
from tests.waiting import wait_until

SCOPE = dict(user_id=1, chat_id=2, thread_id=None, bot_id=3)
PAYLOAD = {"questions": [{"qid": "q", "question": "Pick?", "choices": ["A", "B"]}]}


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    from tui_gateway import server_requests as source
    from hermes_fork.human_input_bridge.installers import install_sidecar
    owner = OwnerBridge(Mailbox(tmp_path / "private" / "box.db"), SCOPE)
    owner.delay_seconds = 120
    server = SimpleNamespace(_sessions={"s": {"_fork_human_input_owner": owner}},
                             _start_usage_ticker=lambda *a, **kw: (threading.Event(), None))
    monkeypatch.setattr(source, "_register", source._register)
    install_sidecar(server)
    monkeypatch.setattr(source, "_write", lambda frame: None)
    monkeypatch.setattr(source, "_emit", lambda *args: None)
    monkeypatch.setattr(source, "_answerable", lambda sid: True)
    yield server, owner, source
    source.cancel("s")
    owner.close()


def test_fast_local_response_does_not_break_original_send(sidecar, monkeypatch):
    _, owner, source = sidecar
    def answer(frame):
        assert source.resolve_response({"id": frame["id"], "result": {"answers": {"q": "B"}, "outcome": "submitted"}})
    monkeypatch.setattr(source, "_write", answer)
    assert source.send("clarify", "s", PAYLOAD, timeout=1, qids=["q"]) == {"answers": {"q": "B"}, "outcome": "submitted"}
    assert not owner._bindings


@pytest.mark.parametrize("outcome", ["submitted", "cancelled", "timed_out"])
def test_tui_original_waiter_terminal_receipt(sidecar, outcome):
    _, owner, source = sidecar
    result = []
    worker = threading.Thread(target=lambda: result.append(source.send("clarify", "s", PAYLOAD,
        timeout=0 if outcome == "timed_out" else 10, qids=["q"])))
    worker.start()
    try:
        wait_until(lambda: bool(owner._bindings), timeout=5)
        handle = next(iter(owner._bindings))
        request_id = owner.mailbox.get(handle)["request_id"]
        if outcome == "submitted":
            assert source.lock_answer(request_id, "q", "A") == []
        elif outcome == "cancelled":
            source.cancel("s")
        worker.join(5)
        assert not worker.is_alive()
        owner.drain()
        assert result[0]["outcome"] == outcome
        assert owner.mailbox.get(handle)["status"] == outcome
    finally:
        source.cancel("s")
        worker.join(5)


@pytest.mark.parametrize("outcome", ["submitted", "cancelled", "timed_out"])
def test_authority_original_waiter_terminal_receipt(tmp_path, outcome):
    from gateway import session_server_requests as source
    from gateway.session_events import SessionEvents
    events = SessionEvents()
    owner = OwnerBridge(Mailbox(tmp_path / "private" / "box.db"), SCOPE)
    handles, result = [], []
    class Viewer:
        def write(self, frame):
            if frame.get("method") == "clarify":
                handles.append(owner.watch_authority(events, "s", frame["id"], kind="clarify", payload=PAYLOAD))
            return True
    events.fanout.attach(Viewer())
    cancelled = threading.Event()
    worker = threading.Thread(target=lambda: result.append(source.ask_viewer_result(events, "s", "clarify", PAYLOAD,
        timeout=0 if outcome == "timed_out" else 10, qids=["q"], cancelled=cancelled.is_set)))
    worker.start()
    try:
        wait_until(lambda: bool(handles), timeout=5)
        if outcome == "submitted":
            assert source.lock_answer(owner.mailbox.get(handles[0])["request_id"], "q", "A", session_ok=lambda *a: True) == []
        elif outcome == "cancelled":
            cancelled.set()
        worker.join(5)
        assert not worker.is_alive()
        owner.drain()
        assert result[0]["outcome"] == outcome
        assert owner.mailbox.get(handles[0])["status"] == outcome
    finally:
        cancelled.set()
        worker.join(5)
        owner.close()
        events.release()


@pytest.mark.parametrize("outcome", ["submitted", "cancelled", "timed_out"])
def test_native_original_waiter_terminal_receipt(tmp_path, outcome):
    from tools import clarify_gateway as source
    owner = OwnerBridge(Mailbox(tmp_path / "private" / "box.db"), SCOPE)
    source.register("native", "s", "Pick?", ["A", "B"])
    handle = owner.watch_native_clarify("native")
    result = []
    worker = threading.Thread(target=lambda: result.append(source.wait_for_response("native", 0.001 if outcome == "timed_out" else 10)))
    worker.start()
    try:
        if outcome == "submitted":
            assert source.resolve_gateway_clarify("native", "A")
        elif outcome == "cancelled":
            source.clear_session("s")
        worker.join(5)
        assert not worker.is_alive()
        owner.drain()
        assert result == [{"submitted": "A", "cancelled": source.CANCELLED, "timed_out": None}[outcome]]
        assert owner.mailbox.get(handle)["status"] == outcome
    finally:
        source.clear_session("s")
        worker.join(5)
        owner.close()


@pytest.mark.parametrize("timeout", [0, -1, None])
def test_unlimited_messaging_ask_keeps_real_waiter(tmp_path, timeout):
    from hermes_fork.human_input_bridge.runtime import RuntimeOwner
    from gateway import session_server_requests as source
    polled = threading.Event()
    class Agent:
        checks = 0
        @property
        def _interrupt_requested(self):
            self.checks += 1
            if self.checks >= 2:
                polled.set()
            return False
    owner = RuntimeOwner(Agent(), {"mailbox_path": tmp_path / "private" / "box.db", "telegram": SCOPE, "delay_seconds": 120}, messaging=True)
    result = []
    worker = threading.Thread(target=lambda: result.append(owner.request("fork-ask/questions", {"questions": [{"id": "q", "question": "Pick?"}]}, timeout)))
    worker.start()
    try:
        wait_until(lambda: bool(owner._bindings), timeout=5)
        handle = next(iter(owner._bindings))
        row = owner.mailbox.get(handle)
        assert row["expires_at"] is None
        assert polled.wait(5)  # Survive multiple actual source timeout/cancellation polls.
        owner.drain()
        assert row["status"] == "pending"
        assert worker.is_alive()
        assert source.open_requests(owner.events, owner.source_id)[0]["id"] == row["request_id"]
        assert owner.mailbox.propose(handle, response={"action": "submit", "payload": {"answers": {"q": "A"}}}, responder=SCOPE)
        worker.join(5)
        assert not worker.is_alive()
        assert result == [{"payload": {"answers": {"q": "A"}}, "surface": "telegram"}]
    finally:
        owner.close()
        worker.join(5)


def test_old_teardown_preserves_next_session_owner(tmp_path, monkeypatch):
    from hermes_fork.human_input_bridge import installers
    from hermes_fork.human_input_bridge.runtime import bind_agent
    from tui_gateway import server_requests as source
    old_stopped, resume = threading.Event(), threading.Event()
    class DelayedStop:
        def wait(self):
            old_stopped.set()
            assert resume.wait(5)
    stops = iter([DelayedStop(), threading.Event()])
    agent = SimpleNamespace()
    owners = []
    config = {"human_input_bridge": {"enabled": True, "telegram": SCOPE}}
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    def bind(agent):
        owner = bind_agent(agent, config=config)
        owners.append(owner)
        return owner
    monkeypatch.setattr(installers, "bind_agent", bind)
    monkeypatch.setattr(source, "_register", source._register)
    server = SimpleNamespace(_sessions={"s": {}}, _start_usage_ticker=lambda *a: (next(stops), None))
    installers.install_sidecar(server)
    server._start_usage_ticker("s", agent)
    assert old_stopped.wait(5)
    # The already-created old thread can be joined by name after release.
    old_thread = next(t for t in threading.enumerate() if t.name == "human-input-teardown")
    new_stop, _ = server._start_usage_ticker("s", agent)
    try:
        resume.set()
        old_thread.join(5)
        assert not old_thread.is_alive()
        assert server._sessions["s"]["_fork_human_input_owner"] is owners[1]
        assert agent._fork_human_input_owner is owners[1]
        assert not owners[1]._closed
        monkeypatch.setattr(source, "_write", lambda frame: None)
        req = source.ServerRequest("s", "clarify", PAYLOAD, qids=["q"])
        source._register(req)
        assert owners[1]._bindings
    finally:
        source.cancel("s")
        new_stop.set()
        owners[1].close()


@pytest.mark.parametrize("teardown", [False, True])
def test_local_clarify_receipt_survives_late_observation(tmp_path, monkeypatch, teardown):
    from tui_gateway import server_requests as source
    clock = [100.0]
    monkeypatch.setattr("hermes_fork.human_input_bridge.mailbox.time.time", lambda: clock[0])
    monkeypatch.setattr(source, "_write", lambda frame: None)
    owner = OwnerBridge(Mailbox(tmp_path / "private" / "box.db"), SCOPE)
    req = source.ServerRequest("late", "clarify", PAYLOAD, qids=["q"])
    source._register(req)
    handle = owner.watch_tui("late", req.id, kind="clarify", payload=PAYLOAD, expires_at=120)
    assert source.lock_answer(req.id, "q", "A") == []
    clock[0] = 121
    owner.close() if teardown else owner.drain()
    assert owner.mailbox.get(handle)["status"] == "submitted"
    owner.close()


def test_retired_registry_entry_without_receipt_is_not_cancelled(tmp_path, monkeypatch):
    from gateway import session_server_requests as source
    from gateway.session_events import SessionEvents
    events = SessionEvents()
    owner = OwnerBridge(Mailbox(tmp_path / "private" / "box.db"), SCOPE)
    pending = source._Pending("s", ["q"], events, "clarify", {"session_id": "s", **PAYLOAD})
    with source._lock:
        source._open["srq-gap"] = pending
    handle = owner.watch_authority(events, "s", "srq-gap", kind="clarify", payload=PAYLOAD)
    removed, resume = threading.Event(), threading.Event()
    original = source.settle_batch
    def paused(*args, **kwargs):
        removed.set()
        assert resume.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(source, "settle_batch", paused)
    worker = threading.Thread(target=lambda: source.resolve({"id": "srq-gap", "result": {"answers": {"q": "A"}, "outcome": "submitted"}}))
    worker.start()
    try:
        assert removed.wait(5)
        owner.drain()
        assert owner.mailbox.get(handle)["status"] == "pending"
        resume.set()
        worker.join(5)
        owner.drain()
        assert owner.mailbox.get(handle)["status"] == "submitted"
    finally:
        resume.set()
        worker.join(5)
        owner.close()
        events.release()
