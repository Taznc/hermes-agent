"""Post-drain restart/reboot actions need explicit operator consent before they fire."""

from __future__ import annotations

import dataclasses

import pytest

from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_dispatch_postdrain as pd
from tests.hermes_cli.test_kanban_post_drain import allowlisted, kanban_home  # noqa: F401

@pytest.fixture
def fired(monkeypatch):
    """Record every fire() call instead of running systemctl. Nothing real ever runs."""
    calls: list[str] = []
    for kind in ("reboot", "service_restart"):
        original = pd.ACTION_HANDLERS[kind]
        monkeypatch.setitem(pd.ACTION_HANDLERS, kind, dataclasses.replace(
            original,
            observe_before=lambda record, cfg: {},
            fire=lambda record, cfg, kind=kind: calls.append(f"{kind}:{record.get('target')}"),
            observe_after=lambda record, cfg: {"state": pd.SUCCEEDED},
        ))
    return calls


def test_allowlisted_restart_never_fires_without_operator_consent(kanban_home, allowlisted, fired):
    kbd.pause_dispatch(None)
    record = pd.queue_post_drain_action(None, action_kind="service_restart")
    assert record["consent"] == pd.CONSENT_PENDING

    for _ in range(3):
        assert pd.evaluate_post_drain_action(None) is None

    assert fired == []
    assert pd.read_post_drain_action(None)["state"] == pd.WAITING


def test_reboot_never_fires_without_operator_consent(kanban_home, fired):
    kbd.pause_dispatch(None)
    pd.queue_post_drain_action(None, action_kind="reboot")

    assert pd.evaluate_post_drain_action(None) is None
    assert fired == []


def test_consent_lets_a_drained_restart_fire_exactly_once(kanban_home, allowlisted, fired):
    kbd.pause_dispatch(None)
    pd.queue_post_drain_action(None, action_kind="service_restart")

    granted = pd.record_operator_consent(None, granted_by="operator")
    assert granted["consented"] is True
    assert granted["state"]["consented_by"] == "operator"

    pd.evaluate_post_drain_action(None)
    pd.evaluate_post_drain_action(None)
    assert fired == ["service_restart:hermes-gateway.service"]


def test_an_unconsented_action_still_expires_without_firing(kanban_home, fired):
    kbd.pause_dispatch(None)
    record = pd.queue_post_drain_action(None, action_kind="reboot", expires_in_seconds=60)

    settled = pd.evaluate_post_drain_action(None, now=record["expires_at"] + 1)
    assert settled["state"] == pd.EXPIRED
    assert fired == []


def test_run_script_needs_no_lifecycle_consent(kanban_home):
    """Only restart/reboot kinds carry the consent gate."""
    assert pd.ACTION_HANDLERS["run_script"].lifecycle is False
    assert {k for k, h in pd.ACTION_HANDLERS.items() if h.lifecycle} == {"reboot", "service_restart"}


def test_group_fires_only_after_consent(kanban_home, fired):
    kb = pytest.importorskip("hermes_cli.kanban_db")
    kb.create_board("other")
    kbd.pause_dispatch(None)
    kbd.pause_dispatch("other")
    pd.queue_post_drain_group(["default", "other"], action_kind="reboot", group_id="g1")

    assert pd.evaluate_post_drain_action("default") is None
    assert fired == []

    assert pd.record_operator_consent("other")["consented"] is True
    assert pd.read_post_drain_action("default")["consent"] == pd.CONSENT_GRANTED
    pd.evaluate_post_drain_action("default")
    assert fired == ["reboot:None"]


def test_cli_consent_refuses_inside_a_worker(kanban_home, monkeypatch, capsys):
    from hermes_cli import kanban_ops

    kbd.pause_dispatch(None)
    pd.queue_post_drain_action(None, action_kind="reboot")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")

    assert kanban_ops._cmd_consent_post_drain(None) == 1
    assert "Refused" in capsys.readouterr().err
    assert pd.read_post_drain_action(None)["consent"] == pd.CONSENT_PENDING


def test_cli_consent_refuses_without_a_terminal(kanban_home, monkeypatch, capsys):
    from hermes_cli import kanban_ops

    kbd.pause_dispatch(None)
    pd.queue_post_drain_action(None, action_kind="reboot")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert kanban_ops._cmd_consent_post_drain(None) == 1
    assert pd.read_post_drain_action(None)["consent"] == pd.CONSENT_PENDING


def test_cli_consent_requires_a_typed_yes(kanban_home, monkeypatch):
    from hermes_cli import kanban_ops

    kbd.pause_dispatch(None)
    pd.queue_post_drain_action(None, action_kind="reboot")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert kanban_ops._cmd_consent_post_drain(None) == 1
    assert pd.read_post_drain_action(None)["consent"] == pd.CONSENT_PENDING

    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    assert kanban_ops._cmd_consent_post_drain(None) == 0
    assert pd.read_post_drain_action(None)["consent"] == pd.CONSENT_GRANTED
