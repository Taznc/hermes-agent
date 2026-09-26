"""Fork-owned tests for ``hermes_fork.kanban.worker_launcher``.

Extraction target: the optional operator-configured ``kanban.worker_launcher``
prefix and systemd-run user-scope helpers, the restart-safe supervised-child
argv wrapper, and the standalone worker-log timestamp filter argv/spawn,
moved out of ``hermes_cli.kanban_db_dispatch`` behind the
``# >>> FORK ANCHOR: kanban-worker-launcher <<<`` marker. These tests pin the
extracted module's own pure-function contracts and the identity of its
late-bound ``_kb`` origin reference; the fuller spawn-integration coverage
(argv actually reaching ``subprocess.Popen`` through ``_default_spawn``)
lives in ``tests/hermes_cli/test_kanban_worker_launcher.py``, which still
exercises this exact code via the ``kanban_db_dispatch`` re-export.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_fork.kanban import worker_launcher as wl


# ---------------------------------------------------------------------------
# Re-export identity: the facade attribute IS the extracted function, not a
# copy — proves the anchor import wires the fork module in rather than
# duplicating behavior that could drift.
# ---------------------------------------------------------------------------


def test_kanban_db_dispatch_reexports_the_extracted_launcher_helpers():
    assert kbd._worker_launcher_prefix is wl._worker_launcher_prefix
    assert kbd._apply_worker_launcher is wl._apply_worker_launcher
    assert kbd._restart_safe_worker_argv is wl._restart_safe_worker_argv
    assert kbd._systemd_user_bus_reachable is wl._systemd_user_bus_reachable
    assert kbd._start_worker_log_stamper is wl._start_worker_log_stamper


def test_extraction_late_bound_origin_resolves_to_the_real_module():
    """The cycle-breaking ``_kb`` module ref must point at the real,
    fully-initialized origin module, not a stand-in or partial import."""
    assert wl._kb is kb


# ---------------------------------------------------------------------------
# _worker_launcher_prefix
# ---------------------------------------------------------------------------


def test_worker_launcher_prefix_empty_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tmp_path.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    assert wl._worker_launcher_prefix() == []


def test_worker_launcher_prefix_drops_unresolvable_binary(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"worker_launcher": ["totally-not-a-real-binary"]}},
    )
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert wl._worker_launcher_prefix() == []


def test_worker_launcher_prefix_fails_closed_when_systemd_user_bus_unreachable(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"worker_launcher": ["systemd-run", "--user", "--scope"]}},
    )
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run")
    monkeypatch.setattr(wl, "_systemd_user_bus_reachable", lambda: False)
    assert wl._worker_launcher_prefix() == []


def test_worker_launcher_prefix_resolves_when_bus_reachable(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"worker_launcher": ["systemd-run", "--user", "--scope"]}},
    )
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run")
    monkeypatch.setattr(wl, "_systemd_user_bus_reachable", lambda: True)
    assert wl._worker_launcher_prefix() == ["systemd-run", "--user", "--scope"]


# ---------------------------------------------------------------------------
# systemd-run user-scope recognition + unit extraction
# ---------------------------------------------------------------------------


def test_is_systemd_user_scope_prefix():
    assert wl._is_systemd_user_scope_prefix(["systemd-run", "--user", "--scope"])
    assert not wl._is_systemd_user_scope_prefix(["systemd-run", "--scope"])  # no --user
    assert not wl._is_systemd_user_scope_prefix(["fake-launcher"])
    assert not wl._is_systemd_user_scope_prefix([])


def test_cmd_is_systemd_user_scope_wrapped_recognizes_scope_and_pipe():
    assert wl._cmd_is_systemd_user_scope_wrapped(
        ["systemd-run", "--user", "--scope", "--", "hermes"]
    )
    assert wl._cmd_is_systemd_user_scope_wrapped(
        ["systemd-run", "--user", "--pipe", "--", "hermes"]
    )
    assert not wl._cmd_is_systemd_user_scope_wrapped(["systemd-run", "--user", "--", "hermes"])
    assert not wl._cmd_is_systemd_user_scope_wrapped(["hermes"])
    assert not wl._cmd_is_systemd_user_scope_wrapped([])


def test_extract_unit_from_systemd_scope_argv_scope_vs_pipe_suffix():
    scope_cmd = ["systemd-run", "--user", "--scope", "--unit", "kanban-t1-run-1", "--", "hermes"]
    assert wl._extract_unit_from_systemd_scope_argv(scope_cmd) == "kanban-t1-run-1.scope"

    pipe_cmd = ["systemd-run", "--user", "--pipe", "--unit=kanban-t1-run-1", "--", "hermes"]
    assert wl._extract_unit_from_systemd_scope_argv(pipe_cmd) == "kanban-t1-run-1.service"

    # Already-suffixed values are passed through untouched.
    explicit = ["systemd-run", "--user", "--scope", "--unit", "kanban-t1-run-1.scope"]
    assert wl._extract_unit_from_systemd_scope_argv(explicit) == "kanban-t1-run-1.scope"

    assert wl._extract_unit_from_systemd_scope_argv(["systemd-run", "--user"]) is None


# ---------------------------------------------------------------------------
# _worker_launcher_env_overrides
# ---------------------------------------------------------------------------


def test_worker_launcher_env_overrides_only_for_systemd_run_user():
    assert wl._worker_launcher_env_overrides([]) == {}
    assert wl._worker_launcher_env_overrides(["fake-launcher"]) == {}


def test_worker_launcher_env_overrides_injects_resolved_bus_vars(monkeypatch):
    monkeypatch.setattr(
        wl, "_resolve_systemd_user_bus_env",
        lambda: ("/run/user/4242", "unix:path=/run/user/4242/bus"),
    )
    overrides = wl._worker_launcher_env_overrides(["systemd-run", "--user", "--scope"])
    assert overrides == {
        "XDG_RUNTIME_DIR": "/run/user/4242",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/4242/bus",
    }


# ---------------------------------------------------------------------------
# _worker_launcher_unit_name / _apply_worker_launcher
# ---------------------------------------------------------------------------


def _make_task(**overrides) -> kb.Task:
    base = dict(
        id="t_wl", title="wl test", body=None, assignee="coder", status="running",
        priority=0, created_by="test", created_at=1, started_at=1, completed_at=None,
        workspace_kind="dir", workspace_path=None, claim_lock="host:1", claim_expires=999,
        tenant=None, current_run_id=7,
    )
    base.update(overrides)
    return kb.Task(**base)


def test_worker_launcher_unit_name_always_carries_scope_suffix():
    task = _make_task(current_run_id=7)
    assert wl._worker_launcher_unit_name(task) == "kanban-t_wl-run-7.scope"

    task_no_run = _make_task(current_run_id=None)
    assert wl._worker_launcher_unit_name(task_no_run) == "kanban-t_wl-run-missing.scope"


def test_apply_worker_launcher_no_launcher_returns_command_unchanged(monkeypatch):
    monkeypatch.setattr(wl, "_worker_launcher_prefix", lambda: [])
    task = _make_task()
    command = ["hermes", "-p", "coder"]
    argv, unit = wl._apply_worker_launcher(task, command)
    assert argv is command
    assert unit is None


def test_apply_worker_launcher_prepends_prefix_and_mints_unit(monkeypatch):
    monkeypatch.setattr(wl, "_worker_launcher_prefix", lambda: ["fake-launcher"])
    task = _make_task(current_run_id=7)
    command = ["hermes", "-p", "coder"]
    argv, unit = wl._apply_worker_launcher(task, command)
    assert argv == ["fake-launcher", "--unit=kanban-t_wl-run-7.scope", "--", *command]
    assert unit == "kanban-t_wl-run-7.scope"


def test_apply_worker_launcher_skips_redundant_outer_scope(monkeypatch):
    """When the launcher is itself a systemd-run --user --scope entry AND the
    command is already scope-wrapped, do not nest — track the real inner unit."""
    monkeypatch.setattr(wl, "_worker_launcher_prefix", lambda: ["systemd-run", "--user", "--scope"])
    task = _make_task(current_run_id=7)
    already_wrapped = [
        "systemd-run", "--user", "--scope", "--quiet", "--unit",
        "kanban-t_wl-run-7.scope", "--collect", "--", "hermes", "-p", "coder",
    ]
    argv, unit = wl._apply_worker_launcher(task, already_wrapped)
    assert argv is already_wrapped  # pure pass-through, no rewrap
    assert unit == "kanban-t_wl-run-7.scope"
    assert argv.count("systemd-run") == 1


# ---------------------------------------------------------------------------
# _restart_safe_worker_argv
# ---------------------------------------------------------------------------


def test_restart_safe_worker_argv_refuses_untraceable_scope_without_run_id(monkeypatch):
    """A task with no current_run_id must never mint a scope it can't trace back."""
    def fake_scoped(command, unit_suffix, **kwargs):
        # Simulate the supervised-gateway topology actually wrapping it.
        return ["systemd-run", "--user", "--scope", "--", *command]

    monkeypatch.setattr(
        "tools.process_registry.restart_safe_supervised_child_argv", fake_scoped,
    )
    task = _make_task(current_run_id=None)
    with pytest.raises(RuntimeError, match="no current run id"):
        wl._restart_safe_worker_argv(task, ["hermes"])


def test_restart_safe_worker_argv_passes_through_when_unsupervised(monkeypatch):
    monkeypatch.setattr(
        "tools.process_registry.restart_safe_supervised_child_argv",
        lambda command, unit_suffix, **kwargs: command,
    )
    task = _make_task(current_run_id=None)
    command = ["hermes"]
    assert wl._restart_safe_worker_argv(task, command) == command


def test_restart_safe_worker_argv_wraps_with_run_scoped_unit_suffix(monkeypatch):
    captured = {}

    def fake_scoped(command, unit_suffix, **kwargs):
        captured["unit_suffix"] = unit_suffix
        return ["systemd-run", "--user", "--scope", "--unit", unit_suffix, "--", *command]

    monkeypatch.setattr(
        "tools.process_registry.restart_safe_supervised_child_argv", fake_scoped,
    )
    task = _make_task(current_run_id=42)
    argv = wl._restart_safe_worker_argv(task, ["hermes"])
    assert captured["unit_suffix"] == "kanban-t_wl-run-42"
    assert argv[0] == "systemd-run"
