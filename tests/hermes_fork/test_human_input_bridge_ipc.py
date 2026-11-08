"""Cross-process tests use spawn, disk IPC, and real source server requests."""
import multiprocessing
import os

from hermes_fork.human_input_bridge.mailbox import Mailbox
from tests.hermes_fork.test_human_input_bridge_mailbox import RECIPIENT


def _source(path, pipe):
    from tui_gateway import server_requests as sr
    from hermes_fork.ui.contracts import declare_contracts
    from hermes_fork.human_input_bridge.owner import OwnerBridge
    declare_contracts()
    frames, results = [], []
    sr.bind_sinks(frames.append, lambda *a: None, lambda sid: True)
    sr.send_async("plugin.request", "isolated", {"kind": "fork-ask/questions", "payload": {"title": "Safe question"}}, results.append)
    mb = Mailbox(path)
    owner = OwnerBridge(mb, RECIPIENT)
    handle = owner.watch_tui("isolated", frames[-1]["id"], kind="ask", payload={"title": "Safe question"}, delay_seconds=0)
    pipe.send((os.getpid(), handle))
    assert pipe.poll(15)
    assert pipe.recv() == "drain"
    assert mb.get(handle)["status"] == "submitting"
    assert results == []
    pipe.send((owner.drain(), results, mb.get(handle)["status"]))
    pipe.close()


def _responder(path, handle, pipe):
    mb = Mailbox(path)
    foreign = {**RECIPIENT, "user_id": 999}
    pipe.send((os.getpid(), mb.propose(handle, response={"payload": {}}, responder=foreign)))
    response = {"payload": {"answer": "safe A"}}
    pipe.send((mb.propose(handle, response=response, responder=RECIPIENT, revision=0),
               mb.propose(handle, response=response, responder=RECIPIENT, revision=0),
               mb.get(handle)["status"]))
    pipe.close()


def test_two_real_processes_intent_then_exact_source_ack(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    source_pipe, source_child = ctx.Pipe()
    path = str(tmp_path / "private" / "mailbox.sqlite")
    source = ctx.Process(target=_source, args=(path, source_child))
    source.start()
    responder = None
    try:
        assert source_pipe.poll(15)
        source_pid, handle = source_pipe.recv()
        remote_pipe, remote_child = ctx.Pipe()
        responder = ctx.Process(target=_responder, args=(path, handle, remote_child))
        responder.start()
        assert remote_pipe.poll(15)
        responder_pid, foreign = remote_pipe.recv()
        assert source_pid != responder_pid != os.getpid()
        assert foreign is False
        assert remote_pipe.poll(15)
        assert remote_pipe.recv() == (True, False, "submitting")
        assert Mailbox(path).get(handle)["result"] is None
        source_pipe.send("drain")
        assert source_pipe.poll(15)
        assert source_pipe.recv() == (1, [{"payload": {"answer": "safe A"}}], "submitted")
        source.join(15)
        responder.join(15)
        assert source.exitcode == responder.exitcode == 0
    finally:
        for process in (source, responder):
            if process is not None and process.is_alive():
                process.terminate()
                process.join(5)


def _race(path, handle, gate, pipe, value):
    mb = Mailbox(path)
    assert gate.wait(10)
    pipe.send(mb.propose(handle, response={"value": value}, responder=RECIPIENT, revision=0))


def test_process_cas_has_exactly_one_winner(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    path = str(tmp_path / "private" / "mailbox.sqlite")
    mb = Mailbox(path)
    handle = mb.publish(kind="ask", payload={}, owner_id="epoch", request_id="exact", recipient=RECIPIENT)
    gate = ctx.Event()
    processes, pipes = [], []
    try:
        for value in ("A", "B"):
            parent, child = ctx.Pipe()
            process = ctx.Process(target=_race, args=(path, handle, gate, child, value))
            process.start()
            processes.append(process)
            pipes.append(parent)
        gate.set()
        for pipe in pipes:
            assert pipe.poll(15)
        assert sorted(pipe.recv() for pipe in pipes) == [False, True]
        for process in processes:
            process.join(15)
            assert process.exitcode == 0
        assert mb.get(handle)["revision"] == 1
        assert len(mb.intents("epoch")) == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
