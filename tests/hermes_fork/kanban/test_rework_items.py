"""Count-bound rework admission through the shared DB seam, tool and CLI."""
import pytest

from tests.hermes_fork.kanban.test_review_gate import (
    VALID, board, _claimed_worker, _cli_request_review, _snapshot, _tool_request_review,
)

ITEM = {"item": "reviewer finding", "evidence": "focused test: passed"}
REASON = "1. fix first\n" + "context " * 100 + "\n2) far finding past 600 characters"


def setup_rework(board, monkeypatch):
    kb, kbc, home = board(on=True)
    with (home / "config.yaml").open("a") as f:
        f.write("  require_rework_items_for_review: true\n")
    tid, run = _claimed_worker(kb, kbc, monkeypatch)
    with kbc.connect_closing() as conn:
        assert kb.request_review(conn, tid, metadata=VALID, reviewer="verifier", expected_run_id=run)
        reviewer = kb.claim_review_task(conn, tid)
        assert kb.request_changes(conn, tid, reason=REASON, expected_run_id=reviewer.current_run_id)[0]
        worker = kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(worker.current_run_id))
    return kb, kbc, home, tid


@pytest.mark.parametrize("items", [None, {}, [], ["bad"], [{"item": "x"}],
                                  [{"item": " ", "evidence": "x"}],
                                  [{"item": "x", "evidence": 1}], [ITEM]])
def test_tool_rejects_shape_or_count_without_mutation(board, monkeypatch, items):
    kb, kbc, _, tid = setup_rework(board, monkeypatch)
    before = _snapshot(kb, kbc, tid)
    out = _tool_request_review(metadata={**VALID, "rework_items": items})
    assert out.get("ok") is not True
    assert "rework_items" in out["error"]
    assert "far finding past 600 characters" in out["error"]
    assert "not semantic verification" in out["error"]
    assert _snapshot(kb, kbc, tid) == before


def test_cli_rejection_then_valid_tool_preserves_receipt(board, monkeypatch, capsys):
    kb, kbc, _, tid = setup_rework(board, monkeypatch)
    before = _snapshot(kb, kbc, tid)
    assert _cli_request_review(tid, VALID) != 0
    assert "rework_items" in capsys.readouterr().err
    assert _snapshot(kb, kbc, tid) == before
    # Duplicates and unrelated text pass: this does not claim semantic coverage.
    out = _tool_request_review(metadata={**VALID, "rework_items": [ITEM, ITEM]})
    assert out["ok"] is True, out
    with kbc.connect_closing() as conn:
        assert kb.latest_run(conn, tid).metadata["rework_items"] == [ITEM, ITEM]
        assert kb.get_task(conn, tid).assignee == "verifier"


@pytest.mark.parametrize("mode", ["force", "disabled", "reset", "diagnosis", "freeform"])
def test_loop_semantics_and_overrides(board, monkeypatch, mode):
    kb, kbc, home, tid = setup_rework(board, monkeypatch)
    if mode == "disabled":
        text = (home / "config.yaml").read_text().replace(
            "require_rework_items_for_review: true", "require_rework_items_for_review: false")
        (home / "config.yaml").write_text(text)
    with kbc.connect_closing() as conn:
        if mode in ("reset", "diagnosis"):
            kb._append_event(conn, tid, "completed", {})
            event = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0]
            if mode == "diagnosis":
                kb._append_event(conn, tid, "review_rework_returned", {
                    "terminal": "completed", "diagnosis_event": event})
        if mode == "freeform":
            kb._append_event(conn, tid, "changes_requested", {"reason": "Please fix this"})
        metadata = {**VALID, "rework_items": [ITEM]} if mode == "freeform" else VALID
        ok, reason = kb.request_review(conn, tid, metadata=metadata, reviewer="verifier",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
            force=mode == "force", with_reason=True)
        assert ok is (mode != "diagnosis"), reason
        if mode == "diagnosis":
            assert "rework_items" in reason


def test_receipt_off_does_not_disable_rework_and_cli_can_accept(board, monkeypatch, capsys):
    kb, kbc, home, tid = setup_rework(board, monkeypatch)
    (home / "config.yaml").write_text(
        "kanban:\n  require_pre_review_gate: false\n  require_rework_items_for_review: true\n")
    with kbc.connect_closing() as conn:
        # Release this test's live claim to exercise the human CLI without force.
        conn.execute("UPDATE tasks SET claim_lock = NULL, worker_pid = NULL WHERE id = ?", (tid,))
        conn.commit()
    assert _cli_request_review(tid) != 0
    assert "rework_items" in capsys.readouterr().err
    assert _cli_request_review(tid, {"rework_items": [ITEM, ITEM]}) == 0


@pytest.mark.parametrize("reason, count", [
    ("1. one\n2) two", 2), ("  7. indented", 1), ("    1. code", 0),
    ("item 1 of 3\n- bullet", 0), ("1. \n2.no space", 0), ("", 0),
])
def test_numbering_is_bounded(reason, count):
    from hermes_fork.kanban.rework_items import enumerated_item_count
    assert enumerated_item_count(reason) == count


def test_receipt_still_required_with_valid_rework(board, monkeypatch):
    kb, kbc, _, tid = setup_rework(board, monkeypatch)
    before = _snapshot(kb, kbc, tid)
    out = _tool_request_review(metadata={"rework_items": [ITEM, ITEM]})
    assert "pre_review_gate.revision" in out["error"]
    assert _snapshot(kb, kbc, tid) == before


def test_status_and_missing_task_are_upstream_errors(board):
    kb, kbc, home = board(on=False)
    (home / "config.yaml").write_text("kanban:\n  require_rework_items_for_review: true\n")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="not reviewable")
        kb._append_event(conn, tid, "changes_requested", {"reason": REASON})
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (tid,))
        conn.commit()
        for task_id in (tid, "missing"):
            ok, reason = kb.request_review(conn, task_id, reviewer="verifier", with_reason=True)
            assert not ok and "rework_items" not in reason
