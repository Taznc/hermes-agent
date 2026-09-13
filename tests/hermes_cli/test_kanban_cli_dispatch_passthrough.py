"""Regression tests for #33488 (CLI max_in_progress / max_spawn / per-profile
config passthrough) and #29415 (kanban_swarm humanizer skill ref).

These two fixes are bundled because they're both small, both touch the
kanban dispatcher's CLI surface, and they each guard against a silent
operator footgun that only manifests in long-running setups.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Spin up a fresh HERMES_HOME with a clean kanban DB."""
    test_home = tempfile.mkdtemp(prefix="kanban_cli_passthrough_")
    os.makedirs(os.path.join(test_home, "profiles", "default"), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    yield test_home


def test_cli_dispatch_passes_max_in_progress_from_config(isolated_kanban_home, monkeypatch):
    """#33488: hermes kanban dispatch must pass kanban.max_in_progress from
    config to dispatch_once. Without this, the global concurrency cap is
    unreachable from the CLI even though it works from the gateway."""
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db
    from hermes_cli import kanban_db_dispatch as kbd

    # Configure max_in_progress in the loaded config.
    fake_config = {
        "kanban": {
            "max_in_progress": 3,
            "max_spawn": 5,
            "default_assignee": "default",
            "max_in_progress_per_profile": 2,
        }
    }
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: fake_config
    )

    captured = {}

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kanban_db.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)

    args = argparse.Namespace(dry_run=True, max=None, failure_limit=2, json=False)
    kb_cli._cmd_dispatch(args)

    # Every config value must have reached dispatch_once.
    assert captured.get("max_in_progress") == 3, (
        f"CLI must pass kanban.max_in_progress from config; got {captured.get('max_in_progress')!r}"
    )
    assert captured.get("max_spawn") == 5, (
        f"CLI must pass kanban.max_spawn from config when --max is not provided; got {captured.get('max_spawn')!r}"
    )
    assert captured.get("default_assignee") == "default"
    assert captured.get("max_in_progress_per_profile") == 2


def test_cli_max_flag_overrides_config_max_spawn(isolated_kanban_home, monkeypatch):
    """--max on the CLI takes precedence over kanban.max_spawn in config.
    The CLI flag is the explicit operator signal; config is the default."""
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db
    from hermes_cli import kanban_db_dispatch as kbd

    fake_config = {"kanban": {"max_spawn": 10}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: fake_config)

    captured = {}
    monkeypatch.setattr(
        kbd, "dispatch_once",
        lambda conn, **kw: (captured.update(kw), kanban_db.DispatchResult())[1],
    )

    args = argparse.Namespace(dry_run=True, max=2, failure_limit=2, json=False)
    kb_cli._cmd_dispatch(args)

    assert captured.get("max_spawn") == 2, (
        f"CLI --max=2 must override config kanban.max_spawn=10; got {captured.get('max_spawn')!r}"
    )


def test_cli_dispatch_passes_nondefault_board_to_connection_and_dispatch(
    isolated_kanban_home, monkeypatch,
):
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_ops

    captured = {}

    @contextlib.contextmanager
    def fake_connect_closing(*, board=None):
        captured["connection_board"] = board
        yield object()

    monkeypatch.setattr(kanban_ops.kbc, "connect_closing", fake_connect_closing)
    monkeypatch.setattr(
        kbd,
        "dispatch_once",
        lambda conn, **kwargs: (
            captured.update({"dispatch_board": kwargs.get("board")}),
            kanban_db.DispatchResult(),
        )[1],
    )

    args = argparse.Namespace(
        board="secondary",
        dry_run=True,
        max=None,
        failure_limit=2,
        json=False,
        resume_circuit=False,
        circuit_status=False,
    )
    assert kb_cli._cmd_dispatch(args) == 0

    assert captured == {
        "connection_board": "secondary",
        "dispatch_board": "secondary",
    }


def test_cli_circuit_status_prints_fault_time_and_recovery(monkeypatch, capsys):
    """The operator-facing status must make an existing circuit actionable."""
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setattr(
        kbd,
        "read_dispatch_pause",
        lambda board: {
            "reason": "restart_safe_scope_unavailable",
            "fault_code": "systemd_user_scope_unavailable",
            "tripped_at": 123,
            "recovery": "repair then resume",
        },
    )
    args = argparse.Namespace(
        board="secondary", dry_run=False, max=None, failure_limit=2, json=False,
        resume_circuit=False, circuit_status=True,
    )

    assert kb_cli._cmd_dispatch(args) == 0
    output = capsys.readouterr().out
    assert "reason=restart_safe_scope_unavailable" in output
    assert "fault_code=systemd_user_scope_unavailable" in output
    assert "time=123" in output
    assert "recovery=repair then resume" in output


def test_cli_resume_returns_failure_when_a_dispatch_tick_still_owns_the_lock(monkeypatch):
    """Automation must not mistake a contended resume for a recovered circuit."""
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setattr(
        kbd,
        "resume_dispatch",
        lambda board: {
            "was_paused": True,
            "resumed": False,
            "reason": "dispatch_in_progress",
        },
    )
    args = argparse.Namespace(
        board="secondary", dry_run=False, max=None, failure_limit=2, json=True,
        resume_circuit=True, circuit_status=False,
    )

    assert kb_cli._cmd_dispatch(args) == 1


def test_cli_circuit_status_distinguishes_rate_limit_from_manual_pause(
    isolated_kanban_home, monkeypatch, capsys,
):
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db_dispatch as kbd

    args = argparse.Namespace(
        board="secondary", resume_circuit=False, circuit_status=True, json=False,
    )
    monkeypatch.setattr(
        kbd,
        "read_dispatch_pause",
        lambda _board: {"reason": "start_budget_exceeded", "next_eligible_at": 1_800_000_000},
    )

    assert kb_cli._cmd_dispatch(args) == 0
    assert "rate limited until" in capsys.readouterr().out

    monkeypatch.setattr(
        kbd,
        "read_dispatch_pause",
        lambda _board: {"reason": "terminal_card_replay"},
    )
    assert kb_cli._cmd_dispatch(args) == 0
    output = capsys.readouterr().out
    assert "manual intervention required" in output
    assert "hermes kanban --board secondary dispatch --resume-circuit" in output
