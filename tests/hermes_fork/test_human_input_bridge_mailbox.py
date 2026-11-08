"""Mailbox contract: delivery and response intents never constitute owner acceptance."""
import os

import pytest

from hermes_fork.human_input_bridge.mailbox import Mailbox

RECIPIENT = {"user_id": 11, "chat_id": 22, "thread_id": None, "bot_id": 33}


@pytest.fixture
def box(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("hermes_fork.human_input_bridge.mailbox.time.time", lambda: clock[0])
    return Mailbox(tmp_path / "private" / "mailbox.sqlite"), clock


def publish(box, **kw):
    return box.publish(kind="ask", payload={"title": "Pick", "questions": []},
                       owner_id="owner-epoch-1", request_id="srq-exact", recipient=RECIPIENT, **kw)


def test_delayed_delivery_and_owner_only_ack(box):
    mb, clock = box
    handle = publish(mb, expires_at=1300)
    assert mb.ready() == []
    clock[0] = 1120
    assert [r["handle"] for r in mb.ready()] == [handle]
    assert mb.ready()[0]["status"] == "pending"
    assert not mb.mark_delivered(handle, chat_id=999, message_id=1)
    assert mb.mark_delivered(handle, chat_id=22, message_id=1)
    assert not mb.mark_delivered(handle, chat_id=22, message_id=2)
    assert mb.ready() == []
    assert mb.propose(handle, response={"answer": "A"}, responder=RECIPIENT, revision=0)
    assert mb.get(handle)["status"] == "submitting"
    assert mb.get(handle)["result"] is None
    assert not mb.acknowledge(handle, owner_id="another-owner", status="submitted")
    intent, = mb.intents("owner-epoch-1")
    assert intent["request_id"] == "srq-exact"
    assert intent["response"] == {"answer": "A"}
    assert mb.acknowledge(handle, owner_id="owner-epoch-1", status="submitted", result={"answer": "A"})
    assert mb.get(handle)["status"] == "submitted"
    assert not mb.propose(handle, response={}, responder=RECIPIENT)
    assert not mb.acknowledge(handle, owner_id="owner-epoch-1", status="cancelled")
    assert mb.intents("owner-epoch-1") == []


@pytest.mark.parametrize("field", list(RECIPIENT))
def test_foreign_scope_rejected_before_consumption(box, field):
    mb, _ = box
    handle = publish(mb)
    foreign = {**RECIPIENT, field: 999}
    assert not mb.propose(handle, response={}, responder=foreign)
    assert mb.get(handle)["revision"] == 0
    assert mb.propose(handle, response={}, responder=RECIPIENT)
    assert not mb.propose(handle, response={}, responder=RECIPIENT)


def test_draft_cas_and_expiry_not_extended(box):
    mb, clock = box
    handle = publish(mb, expires_at=1200)
    updated = mb.update_draft(handle, draft={"q1": "A"}, revision=0)
    assert updated["revision"] == 1
    with pytest.raises(ValueError):
        mb.update_draft(handle, draft={}, revision=0)
    assert not mb.propose(handle, response={}, responder=RECIPIENT, revision=0)
    clock[0] = 1200
    assert mb.ready() == []
    assert not mb.mark_delivered(handle, chat_id=22, message_id=1)
    assert not mb.propose(handle, response={}, responder=RECIPIENT, revision=1)
    with pytest.raises(ValueError):
        mb.update_draft(handle, draft={}, revision=1)
    assert not mb.acknowledge(handle, owner_id="owner-epoch-1", status="submitted")
    assert mb.acknowledge(handle, owner_id="owner-epoch-1", status="timed_out")
    assert mb.get(handle)["expires_at"] == 1200


@pytest.mark.parametrize("revision", [False, 0.0, "0", -1])
def test_revision_cas_requires_exact_nonnegative_integer(box, revision):
    mb, _ = box
    handle = publish(mb)
    assert not mb.propose(handle, response={}, responder=RECIPIENT, revision=revision)
    with pytest.raises(ValueError):
        mb.update_draft(handle, draft={}, revision=revision)
    assert mb.get(handle)["revision"] == 0


def test_approval_choices_and_secret_rejection(box):
    mb, _ = box
    for kind in ("secret", "sudo", "password", "vault", "oauth", "two_factor"):
        with pytest.raises(ValueError):
            mb.publish(kind=kind, payload={}, owner_id="o", request_id="r", recipient=RECIPIENT)
    handle = mb.publish(kind="approval", payload={"command": "harmless"}, owner_id="o",
                        request_id="r", recipient=RECIPIENT, allowed_choices=["deny"])
    assert not mb.propose(handle, response={"choice": "always"}, responder=RECIPIENT)
    assert not mb.propose(handle, response={"choice": "deny", "extra": True}, responder=RECIPIENT)
    assert mb.propose(handle, response={"choice": "deny"}, responder=RECIPIENT)


@pytest.mark.platforms("posix")
def test_private_storage_and_reopen(box):
    mb, _ = box
    handle = publish(mb)
    assert os.stat(mb.path).st_mode & 0o777 == 0o600
    assert os.stat(mb.path.parent).st_mode & 0o777 == 0o700
    assert Mailbox(mb.path).get(handle)["request_id"] == "srq-exact"
