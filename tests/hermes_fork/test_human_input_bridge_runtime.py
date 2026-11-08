import threading
from types import SimpleNamespace

from tests.waiting import wait_until

from hermes_fork.human_input_bridge.mailbox import Mailbox

RECIPIENT = dict(user_id=1, chat_id=2, thread_id=None, bot_id=3)


def publish(box):
    return box.publish(kind="ask", payload={"questions": [{"id": "q", "question": "Why?"}]},
                       owner_id="owner", request_id="request", recipient=RECIPIENT, delay_seconds=0)


def test_native_ask_has_real_source_waiter_and_owner_side_rpc(tmp_path, monkeypatch):
    from hermes_fork.human_input_bridge.runtime import bind_agent, release_agent
    from hermes_fork.ui.bridge import PluginUI
    from gateway import session_server_requests as source
    agent = SimpleNamespace(_interrupt_requested=False, conversation_history=[{"role": "assistant", "content": "context"}])
    config = {"human_input_bridge": {"enabled": True, "mailbox_path": str(tmp_path / "private" / "box.db"),
                                    "telegram": RECIPIENT}}
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    owner = bind_agent(agent, config=config, messaging=True)
    seen = []
    def answer(question, history, **kwargs):
        seen.append((question, history, kwargs["parent_agent"]))
        return "A real side mechanism stub"
    monkeypatch.setattr("agent.side_question.answer_side_question", answer)
    result = []
    worker = threading.Thread(target=lambda: result.append(PluginUI("fork-ask", agent).request(
        "fork-ask/questions", {"questions": [{"id": "q", "question": "Why?"}]}, timeout=20)))
    worker.start()
    try:
        wait_until(lambda: bool(owner.mailbox.ready()), timeout=5)
        row = owner.mailbox.ready()[0]
        assert source.open_requests(owner.events, owner.source_id)[0]["id"] == row["request_id"]
        side = owner.mailbox.propose_side(row["handle"], "q", "Explain?", RECIPIENT)
        wait_until(lambda: owner.mailbox.get_sides(row["handle"])[0]["status"] == "answered", timeout=5)
        assert seen[0][2] is agent
        assert worker.is_alive()
        assert owner.mailbox.propose(row["handle"], response={"action": "submit", "payload": {"answers": {"q": "yes"}}}, responder=RECIPIENT)
        worker.join(5)
        assert not worker.is_alive()
        assert result == [{"payload": {"answers": {"q": "yes"}}, "surface": "telegram"}]
        assert agent.conversation_history == [{"role": "assistant", "content": "context"}]
    finally:
        agent._interrupt_requested = True
        release_agent(agent)
        worker.join(5)
    assert owner._closed


def test_delivery_claim_survives_restart(tmp_path):
    box = Mailbox(tmp_path / "private" / "mailbox.db")
    handle = publish(box)
    assert box.claim_delivery(handle)
    fresh = Mailbox(box.path)
    assert not fresh.claim_delivery(handle)
    assert fresh.ready() == []
    assert fresh.mark_delivered(handle, chat_id=2, message_id=10)
    assert fresh.delivered()[0]["handle"] == handle


def test_side_rpc_preserves_draft_revision_and_identity(tmp_path):
    box = Mailbox(tmp_path / "private" / "mailbox.db")
    handle = publish(box)
    box.update_draft(handle, draft={"q": "unfinished"}, revision=0)
    side = box.propose_side(handle, "q", "Explain?", RECIPIENT)
    assert side
    assert not box.propose_side(handle, "q", "Again?", RECIPIENT)
    assert not box.propose_side(handle, "foreign", "Explain?", RECIPIENT)
    assert not box.propose_side(handle, "q", "Explain?", {**RECIPIENT, "user_id": 9})
    assert box.side_requests("foreign") == []
    assert box.side_requests("owner")[0]["side_id"] == side
    assert not box.acknowledge_side(handle, side, "foreign", "Answer")
    assert box.acknowledge_side(handle, side, "owner", "Answer")
    assert box.get_sides(handle) == [{"id": side, "question_id": "q", "question": "Explain?", "answer": "Answer", "status": "answered"}]
    assert box.get(handle)["revision"] == 1
    assert box.get(handle)["draft"] == {"q": "unfinished"}


def test_production_turn_runner_binds_and_releases_messaging_ask(tmp_path, monkeypatch):
    from gateway.run_turn_runner import TurnRunner
    from hermes_fork.ui.bridge import PluginUI
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "human_input_bridge": {"enabled": True, "telegram": RECIPIENT}})
    turn = object.__new__(TurnRunner)
    turn._approval_owner = None
    turn._ctx = SimpleNamespace(session_key="source", session_id="source", message="question",
        title_user_message=None, persist_user_display_kind=None, persist_user_display_metadata=None,
        moa_config=None, inbound_message_id=None, mute_notification_reply=False, reply_expected=True)
    monkeypatch.setattr(turn, "_native_image_run_message", lambda: "question")
    agent = SimpleNamespace(_interrupt_requested=False)
    owners = []
    def run(message, conversation_history=None, task_id=None):
        owners.append(agent._fork_human_input_owner)
        return PluginUI("fork-ask", agent).request("fork-ask/questions", {"questions": [{"id": "q", "question": "Why?"}]}, timeout=10)
    agent.run_conversation = run
    result = []
    worker = threading.Thread(target=lambda: result.append(turn._run_conversation_with_approval(agent, [], None, None, None)))
    worker.start()
    try:
        wait_until(lambda: bool(owners) and bool(owners[0].mailbox.ready()), timeout=5)
        owner = owners[0]
        row = owner.mailbox.ready()[0]
        assert row["due_at"] == row["created_at"]
        assert owner.mailbox.propose(row["handle"], response={"action": "submit", "payload": {"answers": {"q": "yes"}}}, responder=RECIPIENT)
        worker.join(5)
        assert result == [{"payload": {"answers": {"q": "yes"}}, "surface": "telegram"}]
        assert owner._closed
        assert agent._fork_human_input_owner is None
    finally:
        agent._interrupt_requested = True
        worker.join(5)


def test_production_sidecar_clarify_registration_and_locked_answers(tmp_path, monkeypatch):
    from tui_gateway import server
    from tui_gateway import server_requests as source
    from hermes_fork.human_input_bridge.installers import install_sidecar
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "human_input_bridge": {"enabled": True, "delay_seconds": 120, "telegram": RECIPIENT}})
    install_sidecar(server)
    agent = SimpleNamespace(_interrupt_requested=False)
    monkeypatch.setitem(server._sessions, "sidecar", {"agent": agent, "session_key": "original"})
    monkeypatch.setattr(source, "_write", lambda frame: None)
    monkeypatch.setattr(source, "_answerable", lambda sid: True)
    stop, ticker = server._start_usage_ticker("sidecar", agent, interval=60)
    owner = agent._fork_human_input_owner
    payload = {"questions": [{"qid": "a", "question": "A?"}, {"qid": "b", "question": "B?"}]}
    result = []
    worker = threading.Thread(target=lambda: result.append(source.send("clarify", "sidecar", payload, timeout=10, qids=["a", "b"])))
    worker.start()
    try:
        wait_until(lambda: bool(owner._bindings), timeout=5)
        handle = next(iter(owner._bindings))
        row = owner.mailbox.get(handle)
        assert row["due_at"] - row["created_at"] == 120
        assert source.lock_answer(row["request_id"], "a", "locked") == ["b"]
        assert owner.mailbox.propose(handle, response={"action": "submit", "payload": {"answers": {"a": {"response": "overwrite"}, "b": {"response": "remote"}}}}, responder=RECIPIENT)
        worker.join(5)
        assert result == [{"answers": {"a": "locked", "b": "remote"}, "outcome": "submitted"}]
    finally:
        source.cancel("sidecar")
        worker.join(5)
        stop.set()
        ticker.join(5)
        wait_until(lambda: owner._closed, timeout=5)


def test_production_authority_clarify_preserves_local_lock(tmp_path, monkeypatch):
    from gateway.run_turn_runner import TurnRunner
    from gateway.session_events import SessionEvents
    from gateway import session_server_requests as source
    from gateway.config import Platform
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "human_input_bridge": {"enabled": True, "delay_seconds": 120, "telegram": RECIPIENT}})
    monkeypatch.setattr("gateway.session_onboarding.arm_onboarding_turn", lambda *args: None)
    events = SessionEvents()
    class Viewer:
        def write(self, frame):
            return True
    events.fanout.attach(Viewer())
    turn = object.__new__(TurnRunner)
    turn._approval_owner = (SimpleNamespace(sessions={"canonical": SimpleNamespace(event_stream=events)}), "canonical", "epoch")
    turn._ctx = SimpleNamespace(session_key="canonical", session_id="canonical", message="question",
        source=SimpleNamespace(platform=Platform.LOCAL), title_user_message=None,
        persist_user_display_kind=None, persist_user_display_metadata=None, moa_config=None,
        inbound_message_id=None, mute_notification_reply=False, reply_expected=True)
    monkeypatch.setattr(turn, "_native_image_run_message", lambda: "question")
    agent = SimpleNamespace(_interrupt_requested=False)
    owners = []
    payload = {"questions": [{"qid": "a", "question": "A?"}, {"qid": "b", "question": "B?"}]}
    def run(message, conversation_history=None, task_id=None):
        owners.append(agent._fork_human_input_owner)
        return source.ask_viewer_result(events, "canonical", "clarify", payload, timeout=10, qids=["a", "b"])
    agent.run_conversation = run
    result = []
    worker = threading.Thread(target=lambda: result.append(turn._run_conversation_with_approval(agent, [], None, None, None)))
    worker.start()
    try:
        wait_until(lambda: bool(owners) and bool(owners[0]._bindings), timeout=5)
        owner = owners[0]
        handle = next(iter(owner._bindings))
        row = owner.mailbox.get(handle)
        assert row["due_at"] - row["created_at"] == 120
        assert source.lock_answer(row["request_id"], "a", "locked", session_ok=lambda sid, stream: stream is events) == ["b"]
        assert owner.mailbox.propose(handle, response={"action": "submit", "payload": {"answers": {"a": {"response": "overwrite"}, "b": {"response": "remote"}}}}, responder=RECIPIENT)
        worker.join(5)
        assert result == [{"answers": {"a": "locked", "b": "remote"}, "outcome": "submitted"}]
        assert owner._closed
    finally:
        agent._interrupt_requested = True
        worker.join(5)
        events.release()


def test_production_approval_notify_keeps_original_waiter(tmp_path, monkeypatch):
    from gateway.run_turn_runner import TurnRunner
    from hermes_fork.human_input_bridge.runtime import bind_agent, release_agent
    from tools.approval_gateway_wait import _await_gateway_decision
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    agent = SimpleNamespace()
    owner = bind_agent(agent, config={"human_input_bridge": {"enabled": True, "telegram": RECIPIENT}})
    turn = object.__new__(TurnRunner)
    turn._approval_owner = None
    turn._fork_human_input_owner = owner
    turn._ctx = SimpleNamespace(session_key="notify-source")
    # Only SDK rendering is replaced: the production notifier and blocking approval
    # waiter, exact-id queue resolver, settle observer and source receipt are real.
    monkeypatch.setattr(turn, "_render_approval_sync", lambda data: None)
    result = []
    worker = threading.Thread(target=lambda: result.append(_await_gateway_decision(
        "notify-source", turn._approval_notify_sync, {"command": "echo harmless", "description": "test"})))
    worker.start()
    try:
        wait_until(lambda: bool(owner._bindings), timeout=5)
        handle = next(iter(owner._bindings))
        row = owner.mailbox.get(handle)
        assert row["due_at"] - row["created_at"] == 120
        assert owner.mailbox.propose(handle, response={"choice": "deny"}, responder=RECIPIENT)
        worker.join(5)
        assert result[0]["choice"] == "deny"
        assert owner.mailbox.get(handle)["status"] == "submitted"
    finally:
        from tools import approval
        approval.clear_session("notify-source")
        worker.join(5)
        release_agent(agent)


def test_disabled_and_profile_scoped_config(tmp_path, monkeypatch):
    import pytest
    from hermes_fork.human_input_bridge.runtime import bind_agent
    from hermes_fork.config import human_input_bridge
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    agent = SimpleNamespace()
    assert bind_agent(agent, config={}) is None
    assert not (tmp_path / "human-input-bridge").exists()
    settings = human_input_bridge({"human_input_bridge": {"enabled": True, "telegram": RECIPIENT}}, home=tmp_path)
    assert settings["mailbox_path"] == tmp_path / "human-input-bridge" / "mailbox.sqlite3"
    with pytest.raises(ValueError, match="inside the owning profile"):
        human_input_bridge({"human_input_bridge": {"enabled": True, "telegram": RECIPIENT,
                           "mailbox_path": str(tmp_path.parent / "foreign.db")}}, home=tmp_path)


def test_native_clarify_exact_id_and_redacted_approval(tmp_path):
    from hermes_fork.human_input_bridge.owner import OwnerBridge
    from tools import clarify_gateway as clarify
    from tools import approval
    from tools.approval_gateway_wait import _ApprovalEntry
    box = Mailbox(tmp_path / "private" / "mailbox.db")
    owner = OwnerBridge(box, RECIPIENT)
    first = clarify.register("first", "source", "First?", None)
    second = clarify.register("second", "source", "Second?", None)
    try:
        handle = owner.watch_native_clarify("second", delay_seconds=0)
        assert box.propose(handle, response={"action": "submit", "payload": {"answers": {"second": {"response": "answer"}}}}, responder=RECIPIENT)
        assert owner.drain() == 1
        assert second.response == "answer"
        assert not first.event.is_set()
    finally:
        clarify.clear_session("source")
    entry = _ApprovalEntry({"request_id": "approval-exact", "command": "curl -H 'Authorization: Bearer sk-secretsecretsecretsecretsecretsecret'", "description": "test"})
    with approval._lock:
        approval._gateway_queues["source"] = [entry]
    try:
        handle = owner.watch_native_approval("source", "approval-exact", delay_seconds=0)
        row = box.get(handle)
        assert "sk-secretsecretsecretsecretsecretsecret" not in row["payload"]["command"]
        assert row["allowed_choices"] == ["once", "deny"]
        assert not box.propose(handle, response={"choice": "always"}, responder=RECIPIENT)
        assert box.propose(handle, response={"choice": "deny"}, responder=RECIPIENT)
        assert owner.drain() == 1
        assert entry.result == "deny"
    finally:
        approval.clear_session("source")
        owner.close()

