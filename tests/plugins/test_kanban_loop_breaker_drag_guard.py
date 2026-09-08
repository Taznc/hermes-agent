"""A loop-broken triage card cannot be dragged back into the work queue.

``block_task`` parks a task that re-blocks for the same ``needs_input`` cause
``BLOCK_RECURRENCE_LIMIT`` times into ``triage``, so a human must decide before it
resumes. ``kanban_specify``/``kanban_decompose`` already refuse to hand such a card
back to the automation sweeps.

The dashboard's ordinary drag-drop / ``PATCH /tasks/{id}`` status write had no such
awareness. ``triage`` falls through every structured verb in ``_drag_to`` (unarchive,
unblock, unhold, reopen-review, roadmap spawn) to the raw ``_set_status_direct`` write,
so dragging a loop-broken card to ``ready`` re-armed exactly the loop the breaker
exists to stop — observed live on the dev-vm-console board, whose ``block_recurrences``
climbed 1 -> 2 -> 3 in ~70 minutes with no ``specified``/``promoted``/``decomposed``
event anywhere in its history, only the ``status``/``requested_status: ready`` payload
this write path emits.

These tests drive the real ``block_task``/``unblock_task`` transitions rather than
writing ``block_recurrences``/``block_kind`` by hand, so they assert the actual contract
between the breaker and the dashboard: **the gesture that re-arms the loop must require a
deliberate acknowledgment, and nothing else may change.**
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def _load_plugin_module():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_kanban_plugin_loop_guard_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return _load_plugin_module()


@pytest.fixture
def client(plugin):
    app = FastAPI()
    app.include_router(plugin.router, prefix="/api/plugins/kanban")
    return TestClient(app)


def _drive_into_loop_breaker(conn, task_id: str, *, kind: str = "needs_input") -> None:
    """Re-block a task for the same cause until the breaker parks it in triage.

    Mirrors a real board: a worker blocks, something unblocks the task, the next worker
    hits the identical wall. ``block_task`` only counts a recurrence when it fires from
    ``running``/``ready``, i.e. after an unblock returned the task to the work pool.
    """
    for _ in range(kb.BLOCK_RECURRENCE_LIMIT):
        kb.block_task(conn, task_id, reason="same unsatisfiable cause", kind=kind)
        kb.unblock_task(conn, task_id)
    kb.block_task(conn, task_id, reason="same unsatisfiable cause", kind=kind)


def _make_looped(conn, *, kind: str = "needs_input") -> str:
    tid = kb.create_task(conn, title="unsatisfiable", assignee="worker")
    _drive_into_loop_breaker(conn, tid, kind=kind)
    task = kb.get_task(conn, tid)
    assert task.status == "triage"
    assert task.block_kind == kind
    assert task.block_recurrences >= kb.BLOCK_RECURRENCE_LIMIT
    return tid


@pytest.mark.parametrize("target", ["ready", "todo"])
def test_direct_write_refuses_loop_broken_triage_card(plugin, target):
    """AC3, at the exact seam the incident used: the raw write itself refuses.

    ``todo`` is guarded alongside ``ready`` because ``recompute_ready()`` promotes a
    parent-satisfied ``todo`` card to ``ready`` on the next dispatcher tick — a
    ``ready``-only guard is bypassed by dropping the card one lane to the left.
    """
    with kbc.connect_closing() as conn:
        looped = _make_looped(conn)

        with pytest.raises(plugin._BlockLoopAckRequired):
            plugin._set_status_direct(conn, looped, target)

        assert kb.get_task(conn, looped).status == "triage"


def test_patch_to_ready_is_refused_with_409(client):
    """AC1 over the wire: the dashboard's ordinary drag payload gets an actionable 409."""
    with kbc.connect_closing() as conn:
        looped = _make_looped(conn)

    r = client.patch(f"/api/plugins/kanban/tasks/{looped}", json={"status": "ready"})

    assert r.status_code == 409, r.text
    assert "unblock-loop breaker" in r.json()["detail"]

    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, looped).status == "triage"


def test_patch_with_acknowledgment_is_allowed(client):
    """The explicit second gesture resumes the card — the guard gates, it does not wall off."""
    with kbc.connect_closing() as conn:
        looped = _make_looped(conn)

    r = client.patch(
        f"/api/plugins/kanban/tasks/{looped}",
        json={"status": "ready", "acknowledge_block_loop": True})

    assert r.status_code == 200, r.text
    assert r.json()["task"]["status"] == "ready"

    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, looped).status == "ready"


def test_acknowledgment_records_an_event_and_keeps_the_loop_history(client):
    """The override is auditable, and it does NOT forgive the loop counter.

    Resetting ``block_recurrences``/``block_kind`` here would be the same amnesia
    ``unblock_task`` deliberately avoids: the next re-block must still trip the breaker
    at the same count rather than restarting from zero.
    """
    with kbc.connect_closing() as conn:
        looped = _make_looped(conn)
        before = kb.get_task(conn, looped).block_recurrences

    r = client.patch(
        f"/api/plugins/kanban/tasks/{looped}",
        json={"status": "ready", "acknowledge_block_loop": True})
    assert r.status_code == 200, r.text

    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, looped)
        assert task.block_kind == "needs_input"
        assert task.block_recurrences == before

        acks = [e for e in kb.list_events(conn, looped) if e.kind == "block_loop_ack"]
        assert len(acks) == 1

        # And the breaker still works on the very next re-block, from its preserved count.
        kb.block_task(conn, looped, reason="same unsatisfiable cause", kind="needs_input")
        assert kb.get_task(conn, looped).status == "triage"


def test_ordinary_triage_card_is_unaffected(client):
    """AC2 (a): a rough idea filed by a human has no block history and drags normally."""
    with kbc.connect_closing() as conn:
        fresh = kb.create_task(conn, title="rough idea", triage=True)
        assert kb.get_task(conn, fresh).status == "triage"

    r = client.patch(f"/api/plugins/kanban/tasks/{fresh}", json={"status": "ready"})

    assert r.status_code == 200, r.text
    assert r.json()["task"]["status"] == "ready"


def _park_in_triage_without_the_breaker(conn, task_id: str) -> bool:
    """Move a task to ``triage`` without going through the breaker.

    Test-local setup only: there is no domain verb for "put this in triage", and going
    through ``block_task`` is precisely what would set the recurrence count this test
    needs to stay LOW. Written this way so the card under test differs from a guarded one
    in exactly one field.
    """
    with kbc.write_txn(conn):
        cur = conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (task_id,))
    return cur.rowcount == 1


def test_triage_card_below_the_recurrence_limit_is_unaffected(client):
    """AC2 (b): one block + unblock is ordinary work, not a loop."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="blocked once", assignee="worker")
        kb.block_task(conn, tid, reason="one off", kind="needs_input")
        kb.unblock_task(conn, tid)
        task = kb.get_task(conn, tid)
        assert task.block_kind == "needs_input"
        assert task.block_recurrences < kb.BLOCK_RECURRENCE_LIMIT
        assert _park_in_triage_without_the_breaker(conn, tid)

    r = client.patch(f"/api/plugins/kanban/tasks/{tid}", json={"status": "ready"})

    assert r.status_code == 200, r.text
    assert r.json()["task"]["status"] == "ready"


@pytest.mark.parametrize("kind", ["capability", "transient"])
def test_non_needs_input_loop_is_unaffected(client, kind):
    """AC2 (c): only an unresolved human decision is guarded.

    Mirrors the ``needs_input``-only scoping the sweeps fix already established: a
    ``capability``/``transient`` loop is a real scope problem a human may legitimately
    just re-run, and blanket-guarding those turns an ordinary drag into a confirmation
    dialog for no reason.
    """
    with kbc.connect_closing() as conn:
        looped = _make_looped(conn, kind=kind)

    r = client.patch(f"/api/plugins/kanban/tasks/{looped}", json={"status": "ready"})

    assert r.status_code == 200, r.text
    assert r.json()["task"]["status"] == "ready"


@pytest.mark.parametrize("target", ["idea", "roadmap", "done", "blocked", "on_hold"])
def test_other_targets_from_a_loop_broken_card_are_unchanged(client, target):
    """AC2 (d): the guard covers the work queue only.

    Every other status keeps whatever semantics it had — the guard must not become a
    general-purpose freeze on a loop-broken card, and ``on_hold`` in particular is
    explicitly out of scope (t_afa4e8a6).
    """
    with kbc.connect_closing() as conn:
        looped = _make_looped(conn)

    r = client.patch(f"/api/plugins/kanban/tasks/{looped}", json={"status": target})

    # The outcome per target is whatever the pre-existing verb did (some refuse from
    # triage on their own terms); what matters is that NONE of them is the new
    # acknowledgment refusal.
    assert "unblock-loop breaker" not in r.text


def test_bulk_move_reports_the_refusal_per_task_without_aborting_siblings(client):
    """The bulk endpoint shares ``_apply_status``; a guarded card must not kill the batch."""
    with kbc.connect_closing() as conn:
        looped = _make_looped(conn)
        fresh = kb.create_task(conn, title="rough idea", triage=True)

    r = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={"ids": [looped, fresh], "status": "ready"})

    assert r.status_code == 200, r.text
    results = {entry["id"]: entry for entry in r.json()["results"]}
    assert results[looped]["ok"] is False
    assert "unblock-loop breaker" in results[looped]["error"]
    assert results[fresh]["ok"] is True

    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, looped).status == "triage"
        assert kb.get_task(conn, fresh).status == "ready"


def test_board_payload_exposes_the_fields_the_desktop_gate_reads(client):
    """The desktop's confirmation dialog needs both fields on the CARD, not just the
    detail endpoint — otherwise it cannot tell which drag to intercept."""
    with kbc.connect_closing() as conn:
        looped = _make_looped(conn)

    board = client.get("/api/plugins/kanban/board").json()
    triage = next(col for col in board["columns"] if col["name"] == "triage")
    card = next(t for t in triage["tasks"] if t["id"] == looped)

    assert card["block_kind"] == "needs_input"
    assert card["block_recurrences"] >= kb.BLOCK_RECURRENCE_LIMIT
