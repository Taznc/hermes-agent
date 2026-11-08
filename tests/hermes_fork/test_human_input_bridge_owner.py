"""Real upstream resolver contracts, never a second settlement authority."""
from concurrent.futures import ThreadPoolExecutor
import threading

from hermes_fork.human_input_bridge.mailbox import Mailbox
from hermes_fork.human_input_bridge.owner import OwnerBridge
from tests.hermes_fork.test_human_input_bridge_mailbox import RECIPIENT


def test_owner_teardown_invalidates_pending_and_queued_handles(tmp_path):
    from tui_gateway import server_requests as sr
    from hermes_fork.ui.contracts import declare_contracts
    declare_contracts()
    frames, results = [], []
    sr.bind_sinks(frames.append, lambda *a: None, lambda sid: True)
    mb = Mailbox(tmp_path / "private" / "m.sqlite")
    owner = OwnerBridge(mb, RECIPIENT)
    handles = []
    for index in range(2):
        sr.send_async("plugin.request", "closing", {"kind": "fork-ask/questions", "payload": {}}, results.append)
        handles.append(owner.watch_tui("closing", frames[-1]["id"], kind="ask", payload={}))
    assert mb.propose(handles[1], response={"payload": {}}, responder=RECIPIENT)
    owner.close()
    assert all(mb.get(h)["status"] == "cancelled" for h in handles)
    assert owner.drain() == 0
    assert results == []
    assert not mb.propose(handles[0], response={}, responder=RECIPIENT)
    sr.cancel("closing", "interrupted")


def test_source_binding_digest_rejects_mutated_identity(tmp_path):
    import json
    import sqlite3
    from tui_gateway import server_requests as sr
    from hermes_fork.ui.contracts import declare_contracts
    declare_contracts()
    frames, results = [], []
    sr.bind_sinks(frames.append, lambda *a: None, lambda sid: True)
    sr.send_async("plugin.request", "tamper", {"kind": "fork-ask/questions", "payload": {}}, results.append)
    mb = Mailbox(tmp_path / "private" / "m.sqlite")
    owner = OwnerBridge(mb, RECIPIENT)
    handle = owner.watch_tui("tamper", frames[-1]["id"], kind="ask", payload={})
    assert mb.propose(handle, response={"payload": {}}, responder=RECIPIENT)
    row = mb.get(handle)
    row["request_id"] = "different-generation"
    with sqlite3.connect(mb.path) as db:
        db.execute("UPDATE requests SET body=? WHERE handle=?", (json.dumps(row), handle))
    assert owner.drain() == 0
    assert results == []
    assert mb.get(handle)["status"] == "cancelled"
    sr.cancel("tamper", "interrupted")


def test_native_cancel_is_cancelled_not_empty_submitted_answer(tmp_path):
    from tui_gateway import server_requests as sr
    from hermes_fork.ui.contracts import declare_contracts
    declare_contracts()
    frames, results = [], []
    sr.bind_sinks(frames.append, lambda *a: None, lambda sid: True)
    sr.send_async("plugin.request", "cancel-s", {"kind": "fork-ask/questions", "payload": {}}, results.append)
    mb = Mailbox(tmp_path / "private" / "m.sqlite")
    owner = OwnerBridge(mb, RECIPIENT)
    handle = owner.watch_tui("cancel-s", frames[-1]["id"], kind="ask", payload={})
    assert mb.propose(handle, response={"action": "cancel", "payload": {}}, responder=RECIPIENT)
    assert owner.drain() == 1
    assert results == [{"outcome": "cancelled", "surface": "telegram"}]
    assert mb.get(handle)["status"] == "cancelled"


def test_tui_adapter_exact_request_and_owner_ack(tmp_path):
    from tui_gateway import server_requests as sr
    from hermes_fork.ui.contracts import declare_contracts
    declare_contracts()
    frames = []
    sr.bind_sinks(frames.append, lambda *a: None, lambda sid: True)
    results = []
    retire = sr.send_async("plugin.request", "isolated", {"kind": "fork-ask/questions", "payload": {"title": "Pick"}}, results.append)
    rid = frames[-1]["id"]
    mb = Mailbox(tmp_path / "private" / "mailbox.sqlite")
    owner = OwnerBridge(mb, RECIPIENT)
    handle = owner.watch_tui("isolated", rid, kind="ask", payload={"title": "Pick"})
    assert mb.propose(handle, response={"payload": {"answers": ["A"]}}, responder=RECIPIENT)
    assert results == []
    assert owner.drain() == 1
    assert results == [{"payload": {"answers": ["A"]}}]
    assert mb.get(handle)["status"] == "submitted"
    assert owner.drain() == 0
    retire("resolved")


def test_local_settlement_wins_and_stale_intent_does_not_hit_next(tmp_path):
    from tui_gateway import server_requests as sr
    from hermes_fork.ui.contracts import declare_contracts
    declare_contracts()
    frames, results = [], []
    sr.bind_sinks(frames.append, lambda *a: None, lambda sid: True)
    sr.send_async("plugin.request", "s", {"kind": "fork-ask/questions", "payload": {}}, results.append)
    rid = frames[-1]["id"]
    mb = Mailbox(tmp_path / "private" / "m.sqlite")
    owner = OwnerBridge(mb, RECIPIENT)
    handle = owner.watch_tui("s", rid, kind="ask", payload={})
    assert sr.resolve_response({"id": rid, "result": {"payload": {"local": True}}})
    sr.send_async("plugin.request", "s", {"kind": "fork-ask/questions", "payload": {}}, results.append)
    next_id = frames[-1]["id"]
    assert mb.propose(handle, response={"payload": {}}, responder=RECIPIENT)
    assert owner.drain() == 0
    assert mb.get(handle)["status"] == "cancelled"
    assert any(r["id"] == next_id for r in sr.open_requests("s"))
    sr.cancel("s", "interrupted")


def test_authority_adapter_preserves_stream_identity(tmp_path):
    from gateway import session_server_requests as sr
    frames, published = [], []
    written = threading.Event()

    class Fanout:
        def transports(self):
            return [object()]
        def write(self, frame):
            frames.append(frame)
            written.set()

    class Events:
        fanout = Fanout()
        def publish(self, *args, **kwargs):
            published.append((args, kwargs))

    events = Events()
    mb = Mailbox(tmp_path / "private" / "m.sqlite")
    owner = OwnerBridge(mb, RECIPIENT)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(sr.ask_viewer_result, events, "s", "plugin.request", {"kind": "fork-ask/questions", "payload": {}}, timeout=5)
        assert written.wait(5)
        handle = owner.watch_authority(events, "s", frames[-1]["id"], kind="ask", payload={})
        assert mb.propose(handle, response={"payload": {"a": "A"}}, responder=RECIPIENT)
        assert owner.drain() == 1
        assert future.result(5) == {"payload": {"a": "A"}}
    assert mb.get(handle)["status"] == "submitted"
    assert published


def test_approval_adapter_observes_real_denial_without_replacing_hook(tmp_path, monkeypatch):
    from tools import approval as source
    from tools import approval_context
    from tools.approval_gateway_wait import _await_gateway_decision
    import agent.terminal_approval_batch  # load optional agent imports before the bounded wait
    monkeypatch.setattr(approval_context, "_fire_approval_hook", lambda *a, **kw: None)
    clock = [100.0]
    monkeypatch.setattr("hermes_fork.human_input_bridge.mailbox.time.time", lambda: clock[0])
    mb = Mailbox(tmp_path / "private" / "m.sqlite")
    owner = OwnerBridge(mb, RECIPIENT)
    published, release = threading.Event(), threading.Event()
    handles, originals = [], []

    def notify(data):
        request_id = data["request_id"]
        source.register_gateway_settle("isolated-approval", request_id, lambda reason: originals.append(reason))
        handles.append(owner.watch_approval("isolated-approval", request_id, payload={"title": "Deny only"},
                                            allowed_choices=["deny"], expires_at=120))
        published.set()
        assert release.wait(5)

    with ThreadPoolExecutor() as pool:
        future = pool.submit(_await_gateway_decision, "isolated-approval", notify, {"command": "not executed", "pattern_key": "isolated"})
        assert published.wait(5)
        handle = handles[0]
        assert mb.propose(handle, response={"choice": "deny"}, responder=RECIPIENT)
        assert owner.drain() == 1
        # The original waiter has not acknowledged yet. A second drain must not call it cancelled.
        assert mb.get(handle)["status"] == "submitting"
        assert owner.drain() == 0
        assert mb.get(handle)["status"] == "submitting"
        # Delivery of the source's authoritative receipt can lag its accepted intent.
        clock[0] = 121
        release.set()
        result = future.result(5)
    assert result["choice"] == "deny"
    assert result["resolved"] is True
    assert originals == ["resolved"]
    assert mb.get(handle)["status"] == "submitted"


def test_expired_intent_never_calls_source_and_epoch_is_not_reused(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("hermes_fork.human_input_bridge.mailbox.time.time", lambda: clock[0])
    from tui_gateway import server_requests as sr
    from hermes_fork.ui.contracts import declare_contracts
    declare_contracts()
    frames, results = [], []
    sr.bind_sinks(frames.append, lambda *a: None, lambda sid: True)
    sr.send_async("plugin.request", "s", {"kind": "fork-ask/questions", "payload": {}}, results.append)
    mb = Mailbox(tmp_path / "private" / "m.sqlite")
    owner = OwnerBridge(mb, RECIPIENT)
    handle = owner.watch_tui("s", frames[-1]["id"], kind="ask", payload={}, expires_at=120)
    assert mb.propose(handle, response={"payload": {}}, responder=RECIPIENT)
    assert OwnerBridge(mb, RECIPIENT).drain() == 0
    clock[0] = 120
    assert owner.drain() == 0
    assert results == []
    assert mb.get(handle)["status"] == "timed_out"
    sr.cancel("s", "interrupted")
