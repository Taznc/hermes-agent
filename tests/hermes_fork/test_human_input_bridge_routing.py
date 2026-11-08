"""Opt-in Ask routing through existing fork-owned UI entry points."""
from concurrent.futures import ThreadPoolExecutor
import threading

from hermes_fork.human_input_bridge.mailbox import Mailbox
from hermes_fork.human_input_bridge.owner import OwnerBridge
from hermes_fork.human_input_bridge.routing import bind_ask_bridge
from hermes_fork.ui.bridge import PluginUI
from tests.hermes_fork.test_human_input_bridge_mailbox import RECIPIENT
from tests.waiting import wait_until
from tests.hermes_fork.ui.test_authority_bridge import turn  # isolated real wire() fixture


def test_identical_concurrent_asks_correlate_by_nonce_not_payload(tmp_path, monkeypatch):
    from tui_gateway import server_requests as sr
    from hermes_fork.ui import gateway
    from hermes_fork.ui.contracts import declare_contracts
    declare_contracts()
    first_entered, second_written = threading.Event(), threading.Event()
    calls, lock = [], threading.Lock()
    def sender(source, sid, params, timeout):
        with lock:
            calls.append(params["fork_input_nonce"])
            index = len(calls)
        if index == 1:
            first_entered.set()
            assert second_written.wait(5)
        return source.send("plugin.request", sid, params, timeout=timeout)
    def write(frame):
        if len(calls) == 2 and frame["params"]["fork_input_nonce"] == calls[1]:
            second_written.set()
    sr.bind_sinks(write, lambda *a: None, lambda sid: True)
    monkeypatch.setattr(gateway, "_request_sender", sender)
    monkeypatch.setattr(gateway, "session_for_agent", lambda agent: "same-session")
    class Agent:
        pass
    mb = Mailbox(tmp_path / "private" / "m.sqlite")
    first, second = Agent(), Agent()
    owners = [OwnerBridge(mb, RECIPIENT), OwnerBridge(mb, RECIPIENT)]
    for agent, owner in zip((first, second), owners):
        bind_ask_bridge(agent, owner)
    with ThreadPoolExecutor() as pool:
        one = pool.submit(PluginUI("fork-ask", first).request, "fork-ask/questions", {}, 5)
        assert first_entered.wait(5)
        two = pool.submit(PluginUI("fork-ask", second).request, "fork-ask/questions", {}, 5)
        for owner, answer in zip(owners, ("first", "second")):
            handle = wait_until(lambda: next(iter(owner._bindings), None), timeout=5)
            assert mb.propose(handle, response={"action": "submit", "payload": {"answer": answer}}, responder=RECIPIENT)
        assert one.result(5) == {"payload": {"answer": "first"}, "surface": "telegram"}
        assert two.result(5) == {"payload": {"answer": "second"}, "surface": "telegram"}


def test_authority_ask_routing_keeps_original_owner_and_cancel_semantics(tmp_path, turn):
    mb = Mailbox(tmp_path / "private" / "m.sqlite")
    owner = OwnerBridge(mb, RECIPIENT)
    bind_ask_bridge(turn.agent, owner)
    ui = PluginUI("fork-ask", turn.agent)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(ui.request, "fork-ask/questions", {}, 5)
        handle = wait_until(lambda: next(iter(owner._bindings), None), timeout=5)
        assert mb.propose(handle, response={"action": "cancel", "payload": {}}, responder=RECIPIENT)
        assert future.result(5) == {"outcome": "cancelled", "surface": "telegram"}
    assert mb.get(handle)["status"] == "cancelled"
    assert turn.cancels


def test_local_authoritative_cancel_is_not_submitted(tmp_path, turn):
    mb = Mailbox(tmp_path / "private" / "m.sqlite")
    owner = OwnerBridge(mb, RECIPIENT)
    bind_ask_bridge(turn.agent, owner)
    ui = PluginUI("fork-ask", turn.agent)
    from gateway import session_server_requests
    with ThreadPoolExecutor() as pool:
        future = pool.submit(ui.request, "fork-ask/questions", {}, 5)
        handle = wait_until(lambda: next(iter(owner._bindings), None), timeout=5)
        request_id = mb.get(handle)["request_id"]
        assert session_server_requests.resolve({"id": request_id, "result": {"outcome": "cancelled"}})
        assert future.result(5) == {"outcome": "cancelled"}
    assert mb.get(handle)["status"] == "cancelled"


def test_bound_ask_uses_real_tui_source_route(tmp_path, monkeypatch):
    from tui_gateway import server_requests as sr
    from hermes_fork.ui import gateway
    from hermes_fork.ui.contracts import declare_contracts
    declare_contracts()
    frames = []
    sr.bind_sinks(frames.append, lambda *a: None, lambda sid: True)
    monkeypatch.setattr(gateway, "_request_sender", lambda source, sid, params, timeout: source.send("plugin.request", sid, params, timeout=timeout))
    monkeypatch.setattr(gateway, "session_for_agent", lambda agent: "isolated")
    class Agent:
        pass
    agent = Agent()
    mb = Mailbox(tmp_path / "private" / "m.sqlite")
    owner = OwnerBridge(mb, RECIPIENT)
    bind_ask_bridge(agent, owner)
    ui = PluginUI("fork-ask", agent)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(ui.request, "fork-ask/questions", {"title": "Safe question"}, 5)
        handle = wait_until(lambda: next(iter(owner._bindings), None), timeout=5)
        row = mb.get(handle)
        assert row["kind"] == "ask"
        assert len(frames[-1]["params"]["fork_input_nonce"]) >= 24
        assert "fork_input_nonce" not in row["payload"]
        assert row["due_at"] - row["created_at"] == 120
        assert mb.propose(row["handle"], response={"payload": {"answer": "A"}}, responder=RECIPIENT)
        assert future.result(5) == {"payload": {"answer": "A"}}
    assert mb.get(row["handle"])["status"] == "submitted"


def test_bound_ask_local_answer_ack_and_other_kinds_do_not_project(tmp_path, monkeypatch):
    from tui_gateway import server_requests as sr
    from hermes_fork.ui import gateway
    from hermes_fork.ui.contracts import declare_contracts
    declare_contracts()
    written = threading.Event()
    frames = []
    def write(frame):
        frames.append(frame)
        written.set()
    sr.bind_sinks(write, lambda *a: None, lambda sid: True)
    monkeypatch.setattr(gateway, "_request_sender", lambda source, sid, params, timeout: source.send("plugin.request", sid, params, timeout=timeout))
    monkeypatch.setattr(gateway, "session_for_agent", lambda agent: "isolated-local")
    class Agent:
        pass
    agent = Agent()
    mb = Mailbox(tmp_path / "private" / "m.sqlite")
    owner = OwnerBridge(mb, RECIPIENT)
    bind_ask_bridge(agent, owner)
    ui = PluginUI("fork-ask", agent)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(ui.request, "fork-ask/questions", {}, 5)
        assert written.wait(5)
        handle = wait_until(lambda: next(iter(owner._bindings), None), timeout=5)
        row = mb.get(handle)
        assert sr.resolve_response({"id": frames[-1]["id"], "result": {"payload": {"local": True}}})
        assert future.result(5) == {"payload": {"local": True}}
    assert mb.get(row["handle"])["status"] == "submitted"
    assert mb.get(row["handle"])["result"] == {"payload": {"local": True}}
