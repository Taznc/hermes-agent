"""Tests for the decomposer module + `hermes kanban decompose` CLI surface.

The auxiliary LLM client is mocked — no network calls. Tests exercise the
prompt plumbing, response parsing, DB writes (via the real DB helper),
and the assignee-fallback logic.
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_db_graph import decompose_triage_task
from hermes_cli import kanban_decompose as decomp


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _mock_client_returning(content: str):
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=_fake_aux_response(content))
    return client


def _patch_aux_client(content: str, *, model: str = "test-model"):
    # decompose_task now routes through call_llm (see #35566) — mock it at
    # the source module so task config, extra_body, and retries stay out of
    # unit-test scope.
    return patch(
        "agent.auxiliary_client.call_llm",
        return_value=_fake_aux_response(content),
    )


def _patch_extra_body():
    # No-op shim retained for call-site compatibility: extra_body plumbing
    # now lives inside call_llm, which _patch_aux_client already mocks.
    return patch("agent.auxiliary_client.get_auxiliary_extra_body", return_value={})


def _patch_list_profiles(names: list[str]):
    """Pretend the named profiles exist. The decomposer uses
    profiles_mod.list_profiles() to build the roster + valid-set, and
    profiles_mod.profile_exists() to resolve orchestrator/default."""
    from types import SimpleNamespace
    fake_profiles = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc for {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
        )
        for i, n in enumerate(names)
    ]
    return [
        patch("hermes_cli.profiles.list_profiles", return_value=fake_profiles),
        patch("hermes_cli.profiles.profile_exists", side_effect=lambda x: x in names),
        patch("hermes_cli.profiles.get_active_profile_name", return_value=names[0] if names else "default"),
    ]


def test_decompose_with_fanout_creates_children(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ship a feature", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": _CONFORMING_BODY, "assignee": "researcher", "parents": []},
            {"title": "build", "body": _CONFORMING_BODY, "assignee": "engineer", "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "researcher", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    assert outcome.child_ids and len(outcome.child_ids) == 2

    with kbc.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert root.status == "todo"
    assert c0.status == "ready"
    assert c1.status == "todo"
    assert c0.assignee == "researcher"
    assert c1.assignee == "engineer"


def test_decompose_fanout_false_invalid_llm_assignee_uses_default(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="route me safely", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to fallback.",
        "assignee": "made_up",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "fallback"


def test_decompose_returns_false_when_task_not_triage(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x")  # ready, not triage

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()
    assert outcome.ok is False
    assert "not in triage" in outcome.reason


def test_decompose_triage_task_children_inherit_root_priority(kanban_home):
    """AC1: decompose_triage_task inserts the root's priority for each child,
    and a per-child ``priority`` key overrides it for that child only."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="critical work", triage=True, priority=2)
        child_ids = decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[
                {"title": "child a"},
                {"title": "child b"},
                {"title": "child c", "priority": -1},
            ],
            author="me",
        )
        assert child_ids and len(child_ids) == 3
        rows = {cid: kb.get_task(conn, cid) for cid in child_ids}
    assert rows[child_ids[0]].priority == 2
    assert rows[child_ids[1]].priority == 2
    assert rows[child_ids[2]].priority == -1


def test_clean_children_normalizes_priority(kanban_home):
    """AC2: ``_clean_children`` accepts and normalizes an optional integer
    ``priority`` per child; a non-int value is dropped silently (absent key
    downstream means inherit the root)."""
    routing = decomp._Routing(
        orchestrator="orchestrator", default_assignee="orchestrator",
        auto_promote=True, roster=[], valid_names={"orchestrator"},
    )
    raw_tasks = [
        {"title": "a", "priority": 2},
        {"title": "b", "priority": "high"},  # non-int -> dropped
        {"title": "c"},  # absent -> no key at all
    ]
    children, reason = decomp._clean_children("t_root", raw_tasks, routing)
    assert reason == ""
    assert children[0]["priority"] == 2
    assert "priority" not in children[1]
    assert "priority" not in children[2]


_CONFORMING_BODY = """Wire the classifier route into the dispatcher.

Edit-Targets: hermes_cli/router.py

## Acceptance criteria
AC1. `route()` returns "mechanical" for a payload whose `route` key is "mechanical".
     Tests: test_router.py::test_route_mechanical
AC2. `route()` returns "default" for every malformed classifier output: empty
     string, non-JSON text, JSON without a `route` key, `route` not in
     {default, mechanical}.
     Tests: test_router.py::test_route_malformed_falls_back

## Out of scope
Changing which model the classifier uses.
"""

_OVER_CAP_BODY = """Do a lot of things at once.

## Acceptance criteria
AC1. one. Tests: test_x.py::test_one
AC2. two. Tests: test_x.py::test_two
AC3. three. Tests: test_x.py::test_three
AC4. four. Tests: test_x.py::test_four
AC5. five. Tests: test_x.py::test_five
AC6. six. Tests: test_x.py::test_six

## Out of scope
Nothing much.
"""

_NO_OUT_OF_SCOPE_BODY = """Wire the classifier route into the dispatcher.

## Acceptance criteria
AC1. `route()` returns "mechanical" for a mechanical payload.
     Tests: test_router.py::test_route_mechanical
"""


def test_child_body_contract_accepts_conforming():
    """A body with <= 5 numbered ACs and an '## Out of scope' section passes."""
    assert decomp._child_body_violation(_CONFORMING_BODY) == ""


def test_child_body_contract_rejects_over_cap():
    """A 6th acceptance criterion is a violation naming the cap."""
    violation = decomp._child_body_violation(_OVER_CAP_BODY)
    assert violation
    assert "6" in violation and "5" in violation


def test_child_body_contract_rejects_missing_out_of_scope():
    """A body with no '## Out of scope' heading is a violation naming it."""
    violation = decomp._child_body_violation(_NO_OUT_OF_SCOPE_BODY)
    assert violation
    assert "out of scope" in violation.lower()


def _fanout_payload(bodies: list[str]) -> str:
    return jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": f"child {i}", "body": b, "assignee": "engineer", "parents": []}
            for i, b in enumerate(bodies)
        ],
    })


def _patch_aux_sequence(contents: list[str]):
    """Mock the aux LLM with one canned reply per call, in order."""
    return patch(
        "agent.auxiliary_client.call_llm",
        side_effect=[_fake_aux_response(c) for c in contents],
    )


def test_decompose_retry_then_single_fallback(kanban_home):
    """A contract-violating fan-out is re-prompted exactly once with the
    violation named; a second violation falls back to no-fanout and writes no
    child rows."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ship a feature", triage=True)

    bad = _fanout_payload([_OVER_CAP_BODY])
    worse = _fanout_payload([_NO_OUT_OF_SCOPE_BODY])

    patches = _patch_list_profiles(["orchestrator", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_sequence([bad, worse]) as mock_llm, _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    # Exactly one retry — not zero, not a loop.
    assert mock_llm.call_count == 2
    retry_user_msg = mock_llm.call_args_list[1].kwargs["messages"][-1]["content"]
    assert "AC6" in retry_user_msg, retry_user_msg

    assert outcome.ok is False
    assert outcome.fanout is False
    assert "contract" in outcome.reason.lower()

    # No children written on either attempt; the task is untouched in triage.
    with kbc.connect() as conn:
        rows = kb.list_tasks(conn, limit=100)
        root = kb.get_task(conn, tid)
    assert [r.id for r in rows] == [tid]
    assert root.status == "triage"


def test_decompose_retry_succeeds_creates_children(kanban_home):
    """A conforming retry is accepted and its children are created."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ship a feature", triage=True)

    bad = _fanout_payload([_NO_OUT_OF_SCOPE_BODY])
    good = _fanout_payload([_CONFORMING_BODY, _CONFORMING_BODY])

    patches = _patch_list_profiles(["orchestrator", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_sequence([bad, good]) as mock_llm, _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert mock_llm.call_count == 2
    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    assert outcome.child_ids and len(outcome.child_ids) == 2
