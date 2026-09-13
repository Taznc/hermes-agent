from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_packet import build_worker_task_packet


@pytest.fixture
def conn(tmp_path: Path):
    db = kbc.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


def _claim_review(conn, *, title: str = "Scoped review") -> tuple[str, object]:
    task_id = kb.create_task(
        conn,
        title=title,
        body="## Acceptance criteria\n- Export rejects traversal.\n",
        assignee="builder",
    )
    implementation = kb.claim_task(conn, task_id, claimer="builder:1")
    assert implementation is not None
    assert kb.request_review(
        conn,
        task_id,
        summary="ready",
        reviewer="reviewer",
        expected_run_id=implementation.current_run_id,
    )
    review = kb.claim_review_task(conn, task_id, claimer="reviewer:1")
    assert review is not None
    return task_id, review


def _blocker(reference: str, *, basis: str = "original_ac", **extra):
    return {"basis": basis, "reference": reference, **extra}


def test_request_changes_requires_structured_blockers(conn):
    task_id, review = _claim_review(conn)

    ok, detail = kb.request_changes(
        conn,
        task_id,
        reason="Add traversal coverage.",
        expected_run_id=review.current_run_id,
    )

    assert ok is False
    assert detail == "at least one structured blocker is required"
    assert kb.get_task(conn, task_id).status == "running"


def test_first_review_persists_all_blockers_and_inert_followups(conn):
    task_id, review = _claim_review(conn)
    blockers = [
        _blocker("AC1: Export rejects traversal"),
        _blocker(
            "Required behavior: preserve the destination", basis="required_behavior"
        ),
    ]

    assert kb.request_changes(
        conn,
        task_id,
        reason="Two scoped corrections are required.",
        blockers=blockers,
        followups=["Consider a progress indicator."],
        expected_run_id=review.current_run_id,
    ) == (True, "builder")

    event = [
        event
        for event in kb.list_events(conn, task_id)
        if event.kind == "changes_requested"
    ][-1]
    assert isinstance(event.payload, dict)
    assert event.payload["blockers"] == blockers
    assert event.payload["followups"] == ["Consider a progress indicator."]
    assert event.payload["review_path"] == "same_card"
    assert event.payload["review_round"] == 1
    assert event.payload["max_review_rounds"] >= 0
    assert kb.child_ids(conn, task_id) == []


def test_rereview_rejects_new_out_of_contract_blocker(conn):
    task_id, review = _claim_review(conn)
    cited = "AC1: Export rejects traversal"
    assert kb.request_changes(
        conn,
        task_id,
        reason="Fix traversal.",
        blockers=[_blocker(cited)],
        expected_run_id=review.current_run_id,
    )[0]
    rework = kb.claim_task(conn, task_id, claimer="builder:2")
    assert rework is not None
    assert kb.request_review(
        conn,
        task_id,
        summary="fixed",
        expected_run_id=rework.current_run_id,
    )
    rereview = kb.claim_review_task(conn, task_id, claimer="reviewer:2")
    assert rereview is not None

    ok, detail = kb.request_changes(
        conn,
        task_id,
        reason="Add an unrelated export format.",
        blockers=[
            _blocker("Required behavior: YAML export", basis="required_behavior")
        ],
        expected_run_id=rereview.current_run_id,
    )

    assert ok is False
    assert "outside the first-round review contract" in detail
    assert kb.get_task(conn, task_id).status == "running"


def test_rereview_accepts_cited_blocker_and_rework_regression(conn):
    task_id, review = _claim_review(conn)
    cited = "AC1: Export rejects traversal"
    assert kb.request_changes(
        conn,
        task_id,
        reason="Fix traversal.",
        blockers=[_blocker(cited)],
        expected_run_id=review.current_run_id,
    )[0]
    rework = kb.claim_task(conn, task_id, claimer="builder:2")
    assert rework is not None
    assert kb.request_review(
        conn, task_id, summary="fixed", expected_run_id=rework.current_run_id
    )
    rereview = kb.claim_review_task(conn, task_id, claimer="reviewer:2")
    assert rereview is not None

    assert kb.request_changes(
        conn,
        task_id,
        reason="The cited blocker remains and its rework regressed atomic writes.",
        blockers=[
            _blocker(cited),
            _blocker(
                "Regression: atomic destination write",
                basis="base_regression",
                rework_of=cited,
            ),
        ],
        expected_run_id=rereview.current_run_id,
    ) == (True, "builder")


def test_legacy_prose_event_remains_readable_and_next_verdict_seeds_contract(conn):
    task_id, review = _claim_review(conn)
    assert kb.request_changes(
        conn,
        task_id,
        reason="Legacy verdict.",
        blockers=[_blocker("AC1: old structured value")],
        expected_run_id=review.current_run_id,
    )[0]
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_events SET payload = ? WHERE task_id = ? AND kind = 'changes_requested'",
            (
                '{"reason":"legacy prose","reviewer":"reviewer","implementer":"builder"}',
                task_id,
            ),
        )
    packet = build_worker_task_packet(conn, task_id)
    assert packet.review["unresolved_items"][0]["blockers"] == []

    rework = kb.claim_task(conn, task_id, claimer="builder:legacy")
    assert rework is not None
    assert kb.request_review(
        conn,
        task_id,
        summary="legacy fixed",
        reviewer="reviewer",
        expected_run_id=rework.current_run_id,
    )
    rereview = kb.claim_review_task(conn, task_id, claimer="reviewer:legacy")
    assert rereview is not None
    assert kb.request_changes(
        conn,
        task_id,
        reason="Seed structured scope after upgrade.",
        blockers=[_blocker("AC1: seeded after upgrade")],
        expected_run_id=rereview.current_run_id,
    )[0]
    event = [
        event
        for event in kb.list_events(conn, task_id)
        if event.kind == "changes_requested"
    ][-1]
    assert event.payload["contract_seeded_from_legacy"] is True


def test_same_card_and_ready_child_packets_share_effective_contract(conn):
    same_id, _review = _claim_review(conn)
    same = build_worker_task_packet(conn, same_id).review

    implementation = kb.create_task(conn, title="Implementation", assignee="builder")
    claimed = kb.claim_task(conn, implementation, claimer="builder:parent")
    assert claimed is not None
    assert kb.complete_task(
        conn,
        implementation,
        summary="implemented",
        expected_run_id=claimed.current_run_id,
    )
    child = kb.create_task(
        conn,
        title="Review implementation",
        parents=[implementation],
        skills=["sdlc-review"],
    )
    ready_child = build_worker_task_packet(conn, child).review

    assert same["contract"]["allowed_bases"] == ready_child["contract"]["allowed_bases"]
    assert same["current_round"] == ready_child["current_round"] == 1
    assert same["max_rounds"] == ready_child["max_rounds"]
    assert same["path"] == "same_card"
    assert ready_child["path"] == "ready_child"
    assert ready_child["target_task_ids"] == [implementation]


def test_linking_repair_before_ready_review_child_reverses_deadlocking_edge(conn):
    implementation = kb.create_task(conn, title="Implementation", assignee="builder")
    claimed = kb.claim_task(conn, implementation, claimer="builder:parent")
    assert claimed is not None
    assert kb.complete_task(
        conn,
        implementation,
        summary="implemented",
        expected_run_id=claimed.current_run_id,
    )
    review_child = kb.create_task(
        conn,
        title="Review release",
        parents=[implementation],
        skills=["sdlc-review"],
    )
    repair = kb.create_task(
        conn,
        title="Repair release",
        assignee="builder",
        parents=[review_child],
    )
    review_run = kb.claim_task(conn, review_child, claimer="reviewer:ready-child")
    assert review_run is not None

    kb.link_tasks(conn, repair, review_child)

    assert kb.parent_ids(conn, repair) == []
    assert kb.get_task(conn, repair).status == "ready"
    assert set(kb.parent_ids(conn, review_child)) == {implementation, repair}
    assert not kb.complete_task(
        conn,
        review_child,
        summary="must wait for repair",
        expected_run_id=review_run.current_run_id,
    )
    event = [
        event
        for event in kb.list_events(conn, review_child)
        if event.kind == "repair_dependency_reordered"
    ][-1]
    assert event.payload == {"repair": repair, "review": review_child}


def test_ready_child_rejects_reviewer_owned_repair(conn, monkeypatch):
    implementation = kb.create_task(conn, title="Implementation", assignee="builder")
    implementation_run = kb.claim_task(conn, implementation, claimer="builder:parent")
    assert implementation_run is not None
    assert kb.complete_task(
        conn,
        implementation,
        summary="implemented",
        expected_run_id=implementation_run.current_run_id,
    )
    from hermes_cli import kanban_skill_preflight as skill_preflight

    monkeypatch.setattr(
        skill_preflight, "preflight_task_skills", lambda *_args, **_kwargs: None
    )
    review_child = kb.create_task(
        conn,
        title="Review release",
        parents=[implementation],
        skills=["sdlc-review"],
        assignee="reviewer",
    )
    review_run = kb.claim_task(conn, review_child, claimer="reviewer:first")
    assert review_run is not None
    self_repair = kb.create_task(
        conn,
        title="Reviewer self-repair",
        assignee="reviewer",
        parents=[review_child],
    )
    kb.link_tasks(conn, self_repair, review_child)

    ok, detail = kb.request_changes(
        conn,
        review_child,
        reason="A separate repair is required.",
        blockers=[_blocker("AC1: preserve the release behavior")],
        expected_run_id=review_run.current_run_id,
    )

    assert ok is False
    assert detail == "ready-child repair must be assigned to a different profile"
    rejected_review = kb.get_task(conn, review_child)
    assert rejected_review is not None
    assert rejected_review.status == "running"


def test_ready_child_repair_cycle_persists_contract_and_advances_rereview(
    conn, monkeypatch
):
    implementation = kb.create_task(conn, title="Implementation", assignee="builder")
    implementation_run = kb.claim_task(conn, implementation, claimer="builder:parent")
    assert implementation_run is not None
    assert kb.complete_task(
        conn,
        implementation,
        summary="implemented",
        expected_run_id=implementation_run.current_run_id,
    )
    from hermes_cli import kanban_skill_preflight as skill_preflight

    monkeypatch.setattr(
        skill_preflight, "preflight_task_skills", lambda *_args, **_kwargs: None
    )
    review_child = kb.create_task(
        conn,
        title="Review release",
        parents=[implementation],
        skills=["sdlc-review"],
        assignee="reviewer",
    )
    first_review = kb.claim_task(conn, review_child, claimer="reviewer:first")
    assert first_review is not None
    cited = "AC1: preserve the release behavior"
    ok, detail = kb.request_changes(
        conn,
        review_child,
        reason="The reviewer must not repair in-card.",
        blockers=[_blocker(cited)],
        expected_run_id=first_review.current_run_id,
    )
    assert ok is False
    assert detail == "ready-child review requires a linked unfinished repair"

    repair = kb.create_task(
        conn,
        title="Repair release",
        assignee="builder",
        parents=[review_child],
    )
    kb.link_tasks(conn, repair, review_child)

    assert kb.request_changes(
        conn,
        review_child,
        reason="A separate repair is required.",
        blockers=[_blocker(cited)],
        expected_run_id=first_review.current_run_id,
    ) == (True, "reviewer")
    first_verdict = [
        event
        for event in kb.list_events(conn, review_child)
        if event.kind == "changes_requested"
    ][-1]
    assert isinstance(first_verdict.payload, dict)
    assert first_verdict.payload["review_path"] == "ready_child"
    assert first_verdict.payload["review_round"] == 1
    assert first_verdict.payload["blockers"] == [_blocker(cited)]
    waiting_review = kb.get_task(conn, review_child)
    assert waiting_review is not None
    assert waiting_review.status == "todo"

    repair_run = kb.claim_task(conn, repair, claimer="builder:repair")
    assert repair_run is not None
    assert kb.complete_task(
        conn,
        repair,
        summary="repaired",
        expected_run_id=repair_run.current_run_id,
    )
    resumed_review = kb.get_task(conn, review_child)
    assert resumed_review is not None
    assert resumed_review.status == "ready"

    ready_packet = build_worker_task_packet(conn, review_child)
    assert ready_packet.identity["role"] == "reviewer"
    assert ready_packet.review["current_round"] == 2
    assert ready_packet.review["changes_requested_rounds"] == 1
    assert ready_packet.review["cited_references"] == [cited]

    second_review = kb.claim_task(conn, review_child, claimer="reviewer:second")
    assert second_review is not None
    running_packet = build_worker_task_packet(conn, review_child)
    assert running_packet.review["current_round"] == 2
    assert running_packet.review["cited_references"] == [cited]

    ok, detail = kb.request_changes(
        conn,
        review_child,
        reason="An unrelated requirement appeared.",
        blockers=[
            _blocker("Required behavior: unrelated export", basis="required_behavior")
        ],
        expected_run_id=second_review.current_run_id,
    )
    assert ok is False
    assert detail is not None
    assert "outside the first-round review contract" in detail

    second_repair = kb.create_task(
        conn,
        title="Repair release again",
        assignee="builder",
        parents=[review_child],
    )
    kb.link_tasks(conn, second_repair, review_child)
    assert kb.request_changes(
        conn,
        review_child,
        reason="The cited behavior still regressed.",
        blockers=[_blocker(cited)],
        followups=["Consider a progress indicator."],
        expected_run_id=second_review.current_run_id,
    ) == (True, "reviewer")
    second_verdict = [
        event
        for event in kb.list_events(conn, review_child)
        if event.kind == "changes_requested"
    ][-1]
    assert isinstance(second_verdict.payload, dict)
    assert second_verdict.payload["review_path"] == "ready_child"
    assert second_verdict.payload["review_round"] == 2
    assert second_verdict.payload["blockers"] == [_blocker(cited)]
    assert second_verdict.payload["followups"] == ["Consider a progress indicator."]
    assert set(kb.parent_ids(conn, review_child)) == {
        implementation,
        repair,
        second_repair,
    }
    assert kb.child_ids(conn, review_child) == []

    second_repair_run = kb.claim_task(conn, second_repair, claimer="builder:repair-2")
    assert second_repair_run is not None
    assert kb.complete_task(
        conn,
        second_repair,
        summary="repaired again",
        expected_run_id=second_repair_run.current_run_id,
    )
    resumed_terminal_review = kb.get_task(conn, review_child)
    assert resumed_terminal_review is not None
    assert resumed_terminal_review.status == "ready"

    def unexpected_spawn(*_args, **_kwargs):
        raise AssertionError("the cap must stop dispatch before spawning")

    cap_result = kbd.dispatch_once(
        conn,
        spawn_fn=unexpected_spawn,
        max_review_rounds=2,
    )
    assert cap_result.blocked_review_round_cap == [(review_child, 2)]
    capped_review = kb.get_task(conn, review_child)
    assert capped_review is not None
    assert capped_review.status == "blocked"
    assert capped_review.block_kind == "review_round_cap"


def test_standalone_ready_skill_tagged_task_keeps_ordinary_cycle_rejection(conn):
    review_child = kb.create_task(
        conn, title="Review release", skills=["sdlc-review"]
    )
    repair = kb.create_task(
        conn, title="Ordinary child", assignee="builder", parents=[review_child]
    )
    with pytest.raises(ValueError, match="would create a cycle"):
        kb.link_tasks(conn, repair, review_child)

    assert kb.parent_ids(conn, repair) == [review_child]
    assert kb.parent_ids(conn, review_child) == []
