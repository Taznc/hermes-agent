from __future__ import annotations

import json
from pathlib import Path
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn


def test_worker_packet_is_typed_and_lossless_for_operative_contract(board):
    body = "## Acceptance criteria\n- preserve every byte: λ\n" + ("required\n" * 1400)
    task_id = kb.create_task(
        board,
        title="canonical packet",
        body=body,
        assignee="implementer",
        workspace_kind="worktree",
        workspace_path="/repo/.worktrees/t_packet",
        branch_name="wt/t_packet",
        completion_contract="local-only",
        max_runtime_seconds=7200,
        max_retries=2,
    )
    claimed = kb.claim_task(board, task_id)

    packet = kb.build_worker_task_packet(board, task_id)
    data = packet.to_dict()

    assert isinstance(packet, kb.WorkerTaskPacket)
    assert data["packet_version"] == 1
    assert data["identity"] == {
        "task_id": task_id,
        "title": "canonical packet",
        "role": "implementer",
        "assignee": "implementer",
        "state": "running",
        "source_state": "ready",
        "priority": 0,
        "tenant": None,
    }
    assert data["contract"]["body"] == body
    assert data["contract"]["body"].count("preserve every byte") == 1
    assert data["workspace"]["branch"] == "wt/t_packet"
    assert data["authority"]["completion_contract"] == "local-only"
    assert "completion_contract" not in data["contract"]
    assert data["authority"]["may_land"] is False
    assert data["caps"]["max_runtime_seconds"] == 7200
    assert data["caps"]["max_retries"] == 2
    assert claimed is not None


def test_review_packet_has_review_role_round_and_latest_handoff(board):
    task_id = kb.create_task(
        board, title="review me", body="AC: works", assignee="impl"
    )
    claimed = kb.claim_task(board, task_id)
    assert claimed is not None
    assert kb.request_review(
        board,
        task_id,
        reviewer="reviewer",
        summary="implementation receipt",
        metadata={"commit": "abc123"},
        expected_run_id=claimed.current_run_id,
    )
    review_claim = kb.claim_review_task(board, task_id)
    assert review_claim is not None

    packet = kb.build_worker_task_packet(board, task_id).to_dict()

    assert packet["identity"]["role"] == "reviewer"
    assert packet["identity"]["source_state"] == "review"
    assert packet["handoff"] == {
        "run_id": claimed.current_run_id,
        "outcome": "review_requested",
        "summary": "implementation receipt",
        "metadata": {"commit": "abc123"},
    }
    assert packet["review"]["current_round"] == 1
    assert isinstance(packet["review"]["max_rounds"], int)
    assert "max_review_rounds" not in packet["caps"]

    ok, _ = kb.request_changes(
        board,
        task_id,
        reason="add the missing regression",
        metadata={"test": "test_regression"},
        expected_run_id=review_claim.current_run_id,
    blockers=[{"basis": "original_ac", "reference": "test acceptance contract"}],
    )
    assert ok
    retry_claim = kb.claim_task(board, task_id)
    assert retry_claim is not None
    retry_packet = kb.build_worker_task_packet(board, task_id).to_dict()
    assert retry_packet["identity"]["role"] == "implementer"
    assert retry_packet["contract"]["body"] == "AC: works"
    assert retry_packet["handoff"] is None
    assert retry_packet["review"]["current_round"] == 1
    assert (
        retry_packet["review"]["unresolved_items"][0]["reason"]
        == "add the missing regression"
    )

    assert kb.request_review(
        board,
        task_id,
        summary="regression added",
        metadata={"commit": "def456"},
        expected_run_id=retry_claim.current_run_id,
    )
    second_review_claim = kb.claim_review_task(board, task_id)
    assert second_review_claim is not None
    second_review_packet = kb.build_worker_task_packet(board, task_id).to_dict()
    assert second_review_packet["identity"]["role"] == "reviewer"
    assert second_review_packet["review"]["current_round"] == 2
    assert (
        second_review_packet["review"]["max_rounds"] == packet["review"]["max_rounds"]
    )
    assert second_review_packet["handoff"]["summary"] == "regression added"


def test_packet_bounds_history_and_preserves_parent_handoff(board):
    parent = kb.create_task(board, title="parent", assignee="upstream")
    parent_summary = "parent evidence " + ("E" * 9000)
    assert kb.complete_task(
        board,
        parent,
        summary=parent_summary,
        metadata={"receipt": "R" * 4000},
    )
    second_parent = kb.create_task(board, title="second parent", assignee="upstream")
    assert kb.complete_task(
        board,
        second_parent,
        summary="second parent evidence",
        metadata={"receipt": "second"},
    )
    child = kb.create_task(
        board,
        title="child",
        body="## Acceptance criteria\n- all retained",
        assignee="worker",
        parents=(parent, second_parent),
    )
    for index in range(45):
        kb.add_comment(
            board, child, author="operator", body=f"comment-{index}:" + ("x" * 3000)
        )

    packet = kb.build_worker_task_packet(board, child).to_dict()
    encoded = json.dumps(packet, ensure_ascii=False).encode("utf-8")

    dependencies = {item["task_id"]: item for item in packet["dependencies"]}
    assert dependencies[parent]["handoff"]["summary"] == parent_summary
    assert dependencies[parent]["handoff"]["metadata"] == {"receipt": "R" * 4000}
    assert dependencies[second_parent]["handoff"]["summary"] == "second parent evidence"
    assert len(encoded) < 40_000
    comments_marker = next(
        item for item in packet["history"]["retrieval"] if item["kind"] == "comments"
    )
    assert comments_marker["tool"] == "kanban_show"
    assert comments_marker["arguments"] == {
        "task_id": child,
        "history_cursor": "comments:0",
        "history_limit": 20,
    }
    assert packet["history"]["omitted"]["comments"] > 0

    # Legacy kanban_show serialized raw task/comments/events/runs and then a
    # second rendered context containing the same body/history.  Reconstruct
    # that former public payload to keep the size win behavior-based.
    from tools import kanban_tools as kt

    task = kb.get_task(board, child)
    assert task is not None
    lines: list[str] = []
    now = int(time.time())
    kb._ctx_header(lines, task)
    kb._ctx_attachments(lines, kb.list_attachments(board, child))
    kb._ctx_prior_attempts(lines, board, child, now)
    kb._ctx_parent_results(lines, board, child, now)
    kb._ctx_role_history(lines, board, task, now)
    kb._ctx_comments(lines, kb.list_comments(board, child), now)
    old_context = "\n".join(lines).rstrip() + "\n"
    legacy = json.dumps(
        {
            "task": kt._fields(task, kt._TASK_FIELDS),
            "parents": kb.parent_ids(board, child),
            "children": kb.child_ids(board, child),
            "comments": [
                kt._fields(row, kt._COMMENT_FIELDS)
                for row in kb.list_comments(board, child)
            ],
            "events": [
                kt._fields(row, kt._EVENT_FIELDS)
                for row in kb.list_events(board, child)[-50:]
            ],
            "runs": [
                kt._fields(row, kt._RUN_FIELDS) for row in kb.list_runs(board, child)
            ],
            "worker_context": old_context,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    assert len(encoded) < len(legacy)
    packet_json = json.dumps(packet, ensure_ascii=False)
    escaped_body = json.dumps(packet["contract"]["body"], ensure_ascii=False)[1:-1]
    assert packet_json.count(escaped_body) == 1


def test_history_cursor_returns_each_full_comment_without_repeating_packet(
    board, monkeypatch
):
    from tools import kanban_tools as kt

    task_id = kb.create_task(board, title="history", assignee="worker")
    bodies = [f"full-{index}:" + (str(index) * 2000) for index in range(5)]
    for body in bodies:
        kb.add_comment(board, task_id, author="operator", body=body)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)

    first = json.loads(
        kt._handle_show({"history_cursor": "comments:0", "history_limit": 2})
    )
    second = json.loads(
        kt._handle_show({
            "history_cursor": first["history_page"]["next_cursor"],
            "history_limit": 3,
        })
    )

    assert (
        "packet" not in first and "task" not in first and "worker_context" not in first
    )
    assert [row["body"] for row in first["history_page"]["items"]] == bodies[:2]
    assert [row["body"] for row in second["history_page"]["items"]] == bodies[2:]
    assert second["history_page"]["next_cursor"] is None


def test_truncated_comment_always_has_full_fidelity_retrieval_marker(board):
    task_id = kb.create_task(board, title="unicode history", assignee="worker")
    body = "λ" * 2000
    kb.add_comment(board, task_id, author="operator", body=body)

    packet = kb.build_worker_task_packet(board, task_id).to_dict()

    preview = packet["history"]["preview"]["comments"][0]
    assert len(preview["body"].encode("utf-8")) < len(body.encode("utf-8"))
    assert packet["history"]["omitted"]["comments"] == 0
    assert packet["history"]["truncated_comment_ids"] == [preview["id"]]
    assert any(item["kind"] == "comments" for item in packet["history"]["retrieval"])


def test_dispatch_prompt_stays_id_only_and_packet_data_out_of_stable_prefix(
    board, monkeypatch
):
    from hermes_cli import kanban_db_dispatch as kbd

    body = "dynamic acceptance criteria must not enter argv"
    task_id = kb.create_task(board, title="prompt safety", body=body, assignee="worker")
    task = kb.get_task(board, task_id)
    assert task is not None
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kbd, "_resolve_worker_cli_toolsets", lambda _home: None)

    argv = kbd._worker_argv(task, "worker", None)

    assert argv[1:3] == ["-p", "worker"]
    assert argv[-3:] == ["chat", "-q", f"work kanban task {task_id}"]
    assert body not in " ".join(argv)
