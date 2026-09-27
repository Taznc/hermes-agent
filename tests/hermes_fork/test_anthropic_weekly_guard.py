"""Production pre-claim admission, OAuth usage parsing, and expiring operator overrides."""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as kbd
from agent import account_usage
from hermes_fork.kanban import weekly_usage


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    root.mkdir()
    (root / "config.yaml").write_text("kanban:\n  anthropic_weekly_guard:\n    enabled: true\n")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return root


def _profile(home, name, model, provider="anthropic"):
    path = home if name == "default" else home / "profiles" / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.yaml").write_text(
        f"model:\n  default: {model}\n  provider: {provider}\n"
        + ("kanban:\n  anthropic_weekly_guard:\n    enabled: true\n" if name == "default" else "")
    )
    return path


def _oauth(monkeypatch, percent, *, reset=None):
    reset = reset or (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
    def get(url, headers, *, timeout):
        assert url == "https://api.anthropic.com/api/oauth/usage"
        return {"seven_day": {"utilization": percent, "resets_at": reset},
                "five_hour": {"utilization": 90, "resets_at": reset}}
    monkeypatch.setattr(account_usage, "resolve_anthropic_token", lambda: "sk-test-oauth")
    monkeypatch.setattr(account_usage, "_is_oauth_token", lambda token: True)
    monkeypatch.setattr(account_usage, "_get_json", get)


def _dispatch(conn):
    return kbd.dispatch_once(conn, spawn_fn=lambda task, workspace, board=None: 31337)


def test_guard_is_opt_in(home, monkeypatch):
    _profile(home, "default", "claude-opus-4-1")
    (home / "config.yaml").write_text("model:\n  default: claude-opus-4-1\n  provider: anthropic\n")
    monkeypatch.setattr(account_usage, "resolve_anthropic_token", lambda: None)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="other home", assignee="default")
        assert [task_id for task_id, _, _ in _dispatch(conn).spawned] == [tid]
    assert weekly_usage._threshold() is None
    assert not (home / "kanban-weekly-usage.json").exists()


def test_claim_boundary_and_other_provider_unaffected(home, monkeypatch):
    _profile(home, "claude", "claude-sonnet-4-5")
    _profile(home, "reviewer", "gpt-5", "openai-codex")
    _oauth(monkeypatch, 72)
    with kbc.connect() as conn:
        blocked = kb.create_task(conn, title="Claude work", assignee="claude")
        allowed = kb.create_task(conn, title="Codex review", assignee="reviewer")
        result = _dispatch(conn)
        assert kb.get_task(conn, blocked).status == "ready"
        assert kb.get_task(conn, allowed).status == "running"
        assert any(t == blocked and "weekly" in reason for t, reason in result.respawn_guarded)
    assert not (home / "profiles" / "reviewer" / "kanban-weekly-usage.json").exists()


def test_under_threshold_then_network_failure_is_bounded_and_fail_closed(home, monkeypatch):
    _profile(home, "default", "claude-opus-4-1")
    _oauth(monkeypatch, 65)
    with kbc.connect() as conn:
        first = kb.create_task(conn, title="first", assignee="default")
        assert [x[0] for x in _dispatch(conn).spawned] == [first]
        second = kb.create_task(conn, title="second", assignee="default")
        monkeypatch.setattr(account_usage, "_get_json", lambda *a, **kw: (_ for _ in ()).throw(OSError("offline")))
        assert [x[0] for x in _dispatch(conn).spawned] == [second]  # short-lived last-good sample
        cache = home / "kanban-weekly-usage.json"
        data = json.loads(cache.read_text())
        data["checked_at"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        cache.write_text(json.dumps(data))
        third = kb.create_task(conn, title="third", assignee="default")
        assert not _dispatch(conn).spawned
        assert kb.get_task(conn, third).status == "ready"


def test_cli_override_threshold_and_expiry(home, monkeypatch, capsys):
    _profile(home, "default", "claude-opus-4-1")
    _oauth(monkeypatch, 75)
    args = argparse.Namespace(weekly_usage_action="override", threshold=90, bypass=False,
                              hours=2, until_reset=False, reason="finish the release")
    assert weekly_usage.command(args) == 0
    assert "expires" in capsys.readouterr().out
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="intentional", assignee="default")
        assert [x[0] for x in _dispatch(conn).spawned] == [tid]
    override = home / "kanban-weekly-override.json"
    payload = json.loads(override.read_text())
    assert payload["reason"] == "finish the release"
    assert payload["threshold_percent"] == 90
    audit = json.loads((home / "kanban-weekly-audit.jsonl").read_text().splitlines()[-1])
    assert audit["profile"] == "default" and audit["actor"]
    assert "finish the release" in audit["reason"]
    payload["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    override.write_text(json.dumps(payload))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="after expiry", assignee="default")
        assert _dispatch(conn).spawned == []
        assert kb.get_task(conn, tid).status == "ready"


def test_bypass_until_reset_and_invalid_override(home, monkeypatch):
    _profile(home, "default", "claude-sonnet-4-5")
    _oauth(monkeypatch, 99)
    args = argparse.Namespace(weekly_usage_action="override", threshold=None, bypass=True,
                              hours=None, until_reset=True, reason="urgent repair")
    assert weekly_usage.command(args) == 0
    data = json.loads((home / "kanban-weekly-override.json").read_text())
    assert data["bypass"] is True
    assert datetime.fromisoformat(data["expires_at"]) <= datetime.fromisoformat(data["reset_at"])
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="urgent", assignee="default")
        assert [x[0] for x in _dispatch(conn).spawned] == [tid]
    data["expires_at"] = "nonsense"
    (home / "kanban-weekly-override.json").write_text(json.dumps(data))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="blocked", assignee="default")
        assert not _dispatch(conn).spawned


def test_model_override_and_missing_usage(home, monkeypatch):
    _profile(home, "default", "gpt-5", "openai-codex")
    monkeypatch.setattr(account_usage, "resolve_anthropic_token", lambda: None)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Claude override", assignee="default", model_override="claude-sonnet-4-5", provider_override="anthropic")
        result = _dispatch(conn)
        assert result.spawned == []
        assert kb.get_task(conn, tid).status == "ready"
        other = kb.create_task(conn, title="Codex override", assignee="default")
        result = _dispatch(conn)
        assert [t for t, _, _ in result.spawned] == [other]


def test_per_profile_credential_scope_and_model_specific_window(home, monkeypatch):
    from hermes_constants import get_hermes_home
    _profile(home, "opus", "claude-opus-4-1")
    _profile(home, "sonnet", "claude-sonnet-4-5")
    seen = []
    def get(url, headers, *, timeout):
        seen.append(str(get_hermes_home()))
        return {"seven_day": {"utilization": 20}, "seven_day_opus": {"utilization": 75},
                "seven_day_sonnet": {"utilization": 10}}
    monkeypatch.setattr(account_usage, "resolve_anthropic_token", lambda: "sk-test-oauth")
    monkeypatch.setattr(account_usage, "_is_oauth_token", lambda token: True)
    monkeypatch.setattr(account_usage, "_get_json", get)
    with kbc.connect() as conn:
        blocked = kb.create_task(conn, title="opus", assignee="opus")
        allowed = kb.create_task(conn, title="sonnet", assignee="sonnet")
        result = _dispatch(conn)
        assert any(t == blocked for t, _ in result.respawn_guarded)
        assert [t for t, _, _ in result.spawned] == [allowed]
    assert set(seen) == {str(home / "profiles" / "opus"), str(home / "profiles" / "sonnet")}


def test_failure_retry_throttled_across_rows_and_ticks(home, monkeypatch):
    _profile(home, "default", "claude-opus-4-1")
    calls = []
    monkeypatch.setattr(account_usage, "resolve_anthropic_token", lambda: "sk-test-oauth")
    monkeypatch.setattr(account_usage, "_is_oauth_token", lambda token: True)
    def fail(*args, **kwargs):
        calls.append(1)
        raise OSError("offline")
    monkeypatch.setattr(account_usage, "_get_json", fail)
    with kbc.connect() as conn:
        kb.create_task(conn, title="a", assignee="default")
        kb.create_task(conn, title="b", assignee="default")
        assert not _dispatch(conn).spawned
        assert not _dispatch(conn).spawned
    assert len(calls) == 1


def test_invalid_override_cannot_be_enabled(home, monkeypatch):
    _profile(home, "default", "claude-sonnet-4-5")
    monkeypatch.setattr(account_usage, "resolve_anthropic_token", lambda: None)
    args = argparse.Namespace(weekly_usage_action="override", threshold=90, bypass=False,
                              until_reset=True, hours=None, reason="release")
    assert weekly_usage.command(args) == 2
    assert not (home / "kanban-weekly-override.json").exists()
    args.until_reset = False
    args.hours = 169
    assert weekly_usage.command(args) == 2
    args.hours = 1
    args.reason = "  "
    assert weekly_usage.command(args) == 2
    assert not (home / "kanban-weekly-override.json").exists()


def test_explicit_config_threshold_and_providers(home, monkeypatch):
    (home / "config.yaml").write_text("model:\n  default: claude-sonnet-4-5\n  provider: anthropic\nkanban:\n  anthropic_weekly_guard:\n    enabled: true\n    threshold_percent: 90\n")
    _oauth(monkeypatch, 75)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="under adjusted cap", assignee="default")
        assert [t for t, _, _ in _dispatch(conn).spawned] == [tid]
    assert weekly_usage._threshold() == 90


def test_effective_profile_config_drives_provider_gate(home, monkeypatch):
    """Managed/profile overlays must beat the raw per-profile YAML."""
    from hermes_cli import config as config_module
    from hermes_constants import get_hermes_home
    profile = _profile(home, "configured", "gpt-5", "openai-codex")
    original = config_module.load_config_readonly

    def effective(*args, **kwargs):
        if Path(get_hermes_home()) == profile:
            return {"model": {"default": "claude-opus-4-1", "provider": "anthropic"}}
        return original(*args, **kwargs)

    monkeypatch.setattr(config_module, "load_config_readonly", effective)
    _oauth(monkeypatch, 75)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="effective Claude", assignee="configured")
        result = _dispatch(conn)
        assert not result.spawned
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "ready"
        assert any(task_id == tid and "weekly" in reason for task_id, reason in result.respawn_guarded)


def test_real_cli_targets_assignee_without_switching_home(home):
    profile = _profile(home, "claude", "claude-opus-4-1")
    (profile / "kanban-weekly-usage.json").write_text('{"marker":"assignee"}')
    repo = Path(__file__).resolve().parents[2]
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": str(repo)}
    env.pop("HERMES_KANBAN_TASK", None)
    proc = subprocess.run(
        [sys.executable, "-c", "from hermes_cli.main import main; raise SystemExit(main())",
         "kanban", "weekly-usage", "--assignee", "claude", "status"],
        env=env, cwd=repo, text=True, capture_output=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["threshold_percent"] == 70
    assert data["sample"] == {"marker": "assignee"}


def test_cli_parser_and_worker_cannot_override(home, monkeypatch):
    from hermes_cli.kanban_parser import build_parser
    from hermes_cli.kanban import kanban_command
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["kanban", "weekly-usage", "status"])
    assert kanban_command(args) == 0
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-id")
    args = parser.parse_args(["kanban", "weekly-usage", "override", "--bypass", "--hours", "1", "--reason", "no"])
    assert kanban_command(args) == 2
    assert not (home / "kanban-weekly-override.json").exists()
