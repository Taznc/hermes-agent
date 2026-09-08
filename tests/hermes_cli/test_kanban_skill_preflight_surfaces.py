"""Batch C: the refusal carries the same machine-readable contract out of every
public surface — CLI ``--json``, the agent tool, and the dashboard HTTP API.

An operator or automation must be able to key on ``code`` and act on
``missing_skills`` without parsing English prose out of a message.
"""
from __future__ import annotations

import json

import pytest

from tests.hermes_cli.test_kanban_skill_preflight import (  # noqa: F401
    _make_profile, kanban_home,
)

EXPECTED_KEYS = {"error", "code", "profile", "missing_skills"}


def _assert_contract(payload: dict, *, code: str, profile: str, missing: list[str]) -> None:
    assert EXPECTED_KEYS <= set(payload), payload
    assert payload["code"] == code
    assert payload["profile"] == profile
    assert payload["missing_skills"] == missing
    assert missing[0] in payload["error"]


def test_the_create_tool_returns_the_structured_error_fields(kanban_home, monkeypatch):
    from hermes_cli import kanban_db, kanban_db_connect
    from hermes_cli.kanban_skill_preflight import MISSING_CODE

    _make_profile(kanban_home, "claudecode", [])
    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")

    monkeypatch.setenv("HERMES_KANBAN_MODE", "1")
    from tools import kanban_tools

    payload = json.loads(kanban_tools._handle_create({
        "title": "fleet card", "assignee": "claudecode", "skills": ["hermes-kanban-fleet"],
    }))
    _assert_contract(
        payload, code=MISSING_CODE, profile="claudecode", missing=["hermes-kanban-fleet"],
    )


def _run_cli(argv: list[str]) -> int:
    """Drive the real ``hermes kanban`` entry point through its own parser."""
    import argparse

    from hermes_cli import kanban

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kanban.build_parser(parser.add_subparsers(dest="command"))
    return kanban.kanban_command(parser.parse_args(["kanban", *argv]))


def test_the_cli_emits_the_structured_error_as_json(kanban_home, capsys):
    """``hermes kanban create --json`` is the automation surface; a refusal must
    stay parseable rather than degrading to a prose line on stderr."""
    from hermes_cli import kanban_db, kanban_db_connect
    from hermes_cli.kanban_skill_preflight import MISSING_CODE

    _make_profile(kanban_home, "claudecode", [])
    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")

    rc = _run_cli([
        "create", "fleet card", "--assignee", "claudecode",
        "--skill", "hermes-kanban-fleet", "--json",
    ])
    assert rc != 0
    payload = json.loads(capsys.readouterr().out)
    _assert_contract(
        payload, code=MISSING_CODE, profile="claudecode", missing=["hermes-kanban-fleet"],
    )


def test_the_cli_emits_the_structured_error_for_assign(kanban_home, capsys):
    from hermes_cli import kanban_db, kanban_db_connect
    from hermes_cli.kanban_skill_preflight import MISSING_CODE

    _make_profile(kanban_home, "alpha", ["github-code-review"])
    _make_profile(kanban_home, "beta", [])
    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        task_id = kanban_db.create_task(
            conn, title="card", assignee="alpha", skills=["github-code-review"],
        )

    assert _run_cli(["assign", task_id, "beta", "--json"]) != 0
    payload = json.loads(capsys.readouterr().out)
    _assert_contract(
        payload, code=MISSING_CODE, profile="beta", missing=["github-code-review"],
    )


def test_the_cli_still_prints_a_readable_message_without_json(kanban_home, capsys):
    """The structured form is additive: the human surface keeps its prose."""
    from hermes_cli import kanban_db, kanban_db_connect

    _make_profile(kanban_home, "claudecode", [])
    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")

    assert _run_cli([
        "create", "fleet card", "--assignee", "claudecode",
        "--skill", "hermes-kanban-fleet",
    ]) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hermes-kanban-fleet" in captured.err
    assert "hermes -p claudecode skills list" in captured.err


def test_the_dashboard_returns_the_structured_error_on_create(kanban_home):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from hermes_cli import kanban_db, kanban_db_connect
    from hermes_cli.kanban_skill_preflight import MISSING_CODE

    _make_profile(kanban_home, "claudecode", [])
    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")

    from plugins.kanban.dashboard import plugin_api

    app = fastapi.FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/kanban")
    response = TestClient(app).post("/api/plugins/kanban/tasks", json={
        "title": "fleet card", "assignee": "claudecode", "skills": ["hermes-kanban-fleet"],
    })
    assert response.status_code == 400
    detail = response.json()["detail"]
    _assert_contract(
        detail, code=MISSING_CODE, profile="claudecode", missing=["hermes-kanban-fleet"],
    )


def test_the_dashboard_returns_the_structured_error_on_reassign(kanban_home):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from hermes_cli import kanban_db, kanban_db_connect
    from hermes_cli.kanban_skill_preflight import MISSING_CODE

    _make_profile(kanban_home, "alpha", ["github-code-review"])
    _make_profile(kanban_home, "beta", [])
    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        task_id = kanban_db.create_task(
            conn, title="card", assignee="alpha", skills=["github-code-review"],
        )

    from plugins.kanban.dashboard import plugin_api

    app = fastapi.FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/kanban")
    response = TestClient(app).post(
        f"/api/plugins/kanban/tasks/{task_id}/reassign", json={"profile": "beta"},
    )
    assert response.status_code == 400
    _assert_contract(
        response.json()["detail"], code=MISSING_CODE, profile="beta",
        missing=["github-code-review"],
    )


def test_an_ordinary_validation_error_keeps_its_plain_shape(kanban_home):
    """The structured fields are additive: an unrelated ValueError must not
    grow a bogus ``code``/``missing_skills`` and must stay a plain detail."""
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from hermes_cli import kanban_db, kanban_db_connect

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")

    from plugins.kanban.dashboard import plugin_api

    app = fastapi.FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/kanban")
    response = TestClient(app).post("/api/plugins/kanban/tasks", json={
        "title": "card", "reasoning_effort": "not-a-level",
    })
    assert response.status_code == 400
    assert isinstance(response.json()["detail"], str)
