"""Unit tests for ``kanban.worker_launcher`` spawn wrapping + non-child reaping.

Covers the hermes-workers.slice spec (docs/rfcs/hermes-workers-slice-spec.md,
t_cb47a946) test plan items reachable without a real systemd user session:
fake-launcher argv construction, the strictly-optional default-``[]``
regression pin, ``worker_unit`` persistence, the ``_classify_worker_exit``
scope-status fallback for a worker this dispatcher process never reaped, the
gateway-restart re-adoption host-prefix (not full-claimer) contract, and the
termination-path routing (unit-stop vs. bare-PID kill). A systemd-gated
integration test exercising a real ``systemd-run --user --scope`` lives in
``tests/tools/test_process_registry.py`` / the systemd-only lane described in
the spec and is skipped when ``systemd-run`` is unavailable.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


def _make_task(**overrides) -> kb.Task:
    base = dict(
        id="t_launcher",
        title="launcher test",
        body=None,
        assignee="coder",
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=1,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="host:1",
        claim_expires=999,
        tenant=None,
        current_run_id=7,
    )
    base.update(overrides)
    return kb.Task(**base)


@pytest.fixture
def worker_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    profile.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process", lambda: False)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = _make_task(workspace_path=str(workspace))
    return root, workspace, task


def _set_worker_launcher(root: Path, launcher: list[str]) -> None:
    import yaml

    cfg = {"kanban": {"worker_launcher": launcher}}
    root.joinpath("config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


# --------------------------------------------------------------------------
# Default ([]) is a byte-identical no-op — the strictly-optional contract.
# --------------------------------------------------------------------------


def test_default_worker_launcher_is_empty_and_spawn_unchanged(worker_setup, monkeypatch):
    root, workspace, task = worker_setup
    captured = {}

    class FakeProc:
        pid = 4321

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    assert kbd._worker_launcher_prefix() == []
    pid = kbd._default_spawn(task, str(workspace))

    assert pid == 4321
    # No launcher prefix, no --unit=, no trailing "--" separator injected.
    assert captured["cmd"][:3] == ["hermes", "-p", "coder"]
    assert "--unit=" not in " ".join(captured["cmd"])
    assert task.worker_unit is None


# --------------------------------------------------------------------------
# Fake-launcher argv construction.
# --------------------------------------------------------------------------


def test_worker_launcher_prefix_wraps_argv_and_appends_unit_and_separator(worker_setup, monkeypatch):
    root, workspace, task = worker_setup
    _set_worker_launcher(root, ["fake-launcher", "--scope"])
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}" if name == "fake-launcher" else None)

    captured = {}

    class FakeProc:
        pid = 5555

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    pid = kbd._default_spawn(task, str(workspace))

    assert pid == 5555
    cmd = captured["cmd"]
    assert cmd[:2] == ["fake-launcher", "--scope"]
    unit_index = next(i for i, part in enumerate(cmd) if part.startswith("--unit="))
    assert cmd[unit_index] == "--unit=kanban-t_launcher-run-7"
    separator = cmd.index("--")
    assert separator > unit_index
    assert cmd[separator + 1 : separator + 4] == ["hermes", "-p", "coder"]
    # The Task object is mutated so the caller can persist worker_unit.
    assert task.worker_unit == "kanban-t_launcher-run-7"


def test_worker_launcher_missing_binary_falls_back_to_plain_popen(worker_setup, monkeypatch):
    """Fail OPEN: an unresolvable launcher binary degrades to today's Popen,
    it must never block the task from making progress."""
    root, workspace, task = worker_setup
    _set_worker_launcher(root, ["nonexistent-launcher-binary"])
    monkeypatch.setattr("shutil.which", lambda name: None)

    captured = {}

    class FakeProc:
        pid = 6666

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    pid = kbd._default_spawn(task, str(workspace))

    assert pid == 6666
    assert captured["cmd"][:3] == ["hermes", "-p", "coder"]
    assert task.worker_unit is None


def test_worker_unit_persisted_only_when_launcher_produces_unit(worker_setup, monkeypatch, tmp_path):
    """``tasks.worker_unit`` is populated iff the launcher argv contains
    ``--unit=``; absent for the ``[]`` default."""
    import hermes_cli.kanban_db_connect as kbc

    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="spawn test", assignee="coder")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None

        root, workspace, _unused_task = worker_setup
        _set_worker_launcher(root, ["fake-launcher"])
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/fake-launcher" if name == "fake-launcher" else None)

        class FakeProc:
            pid = 7777

        monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: FakeProc())

        pid = kbd._default_spawn(claimed, str(workspace))
        kbd._set_worker_pid(conn, task_id, pid, worker_unit=claimed.worker_unit)

        row = conn.execute("SELECT worker_pid, worker_unit FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert row["worker_pid"] == 7777
        assert row["worker_unit"] == f"kanban-{task_id}-run-{claimed.current_run_id}"
    finally:
        conn.close()


def test_worker_unit_absent_for_default_spawn(tmp_path):
    import hermes_cli.kanban_db_connect as kbc

    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="no launcher", assignee="coder")
        kbd._set_worker_pid(conn, task_id, 8888)
        row = conn.execute("SELECT worker_pid, worker_unit FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert row["worker_pid"] == 8888
        assert row["worker_unit"] is None
    finally:
        conn.close()


# --------------------------------------------------------------------------
# _classify_worker_exit scope-status fallback for a cold/never-reaped pid.
# --------------------------------------------------------------------------


def test_classify_worker_exit_falls_back_to_scope_status_when_unit_set(monkeypatch):
    """A pid never reaped by THIS process (e.g. after a gateway restart) with a
    ``worker_unit`` on the task row must consult ``_scope_exit_status``
    instead of returning ``unknown``."""
    monkeypatch.setattr(
        kbd, "_scope_exit_status", lambda unit: ("nonzero_exit", 3) if unit == "kanban-t1-run-1" else None,
    )
    kind, code = kbd._classify_worker_exit(999999, "kanban-t1-run-1")
    assert (kind, code) == ("nonzero_exit", 3)


def test_classify_worker_exit_stays_unknown_without_worker_unit(monkeypatch):
    """Regression pin: the no-launcher default keeps existing ``"unknown"``
    behavior byte-for-byte — no worker_unit means no scope-status consult."""
    called = []
    monkeypatch.setattr(kbd, "_scope_exit_status", lambda unit: called.append(unit) or ("clean_exit", 0))
    kind, code = kbd._classify_worker_exit(999999, None)
    assert (kind, code) == ("unknown", None)
    assert called == []


def test_scope_exit_status_maps_systemctl_show_properties(monkeypatch):
    monkeypatch.setattr(
        "tools.process_registry.scope_unit_show_properties",
        lambda unit: {"ActiveState": "inactive", "ExecMainCode": "exited", "ExecMainStatus": "0"},
    )
    assert kbd._scope_exit_status("kanban-t1-run-1") == ("clean_exit", 0)

    monkeypatch.setattr(
        "tools.process_registry.scope_unit_show_properties",
        lambda unit: {
            "ActiveState": "inactive", "ExecMainCode": "exited",
            "ExecMainStatus": str(kb.KANBAN_RATE_LIMIT_EXIT_CODE),
        },
    )
    assert kbd._scope_exit_status("kanban-t1-run-1") == ("rate_limited", kb.KANBAN_RATE_LIMIT_EXIT_CODE)

    monkeypatch.setattr(
        "tools.process_registry.scope_unit_show_properties",
        lambda unit: {"ActiveState": "failed", "ExecMainCode": "killed", "ExecMainStatus": "9"},
    )
    assert kbd._scope_exit_status("kanban-t1-run-1") == ("signaled", 9)


def test_scope_exit_status_none_while_still_running(monkeypatch):
    monkeypatch.setattr(
        "tools.process_registry.scope_unit_show_properties",
        lambda unit: {"ActiveState": "active", "ExecMainCode": "", "ExecMainStatus": ""},
    )
    assert kbd._scope_exit_status("kanban-t1-run-1") is None


def test_scope_exit_status_none_when_unit_gone(monkeypatch):
    """``--collect`` self-cleans the unit shortly after exit; a query after
    that window must return None gracefully, not raise."""
    monkeypatch.setattr("tools.process_registry.scope_unit_show_properties", lambda unit: None)
    assert kbd._scope_exit_status("kanban-t1-run-1") is None


def test_reclaim_dead_workers_uses_worker_unit_fallback_for_cold_dispatcher(tmp_path, monkeypatch):
    """End-to-end through ``_reclaim_dead_workers``: a task row carrying a
    ``worker_unit`` but with no ``_recent_worker_exits`` entry (simulating a
    cold/restarted dispatcher process that never reaped this pid itself)
    must classify via the scope fallback, not fall into the generic
    'unknown' -> 'crashed' path with no detail."""
    import hermes_cli.kanban_db_connect as kbc

    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="cold reclaim", assignee="coder")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        fake_pid = 424242
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid = ?, worker_unit = ?, started_at = ? WHERE id = ?",
                (fake_pid, "kanban-cold-run-1", 0, task_id),
            )

        monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
        monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
        monkeypatch.setattr(
            kbd, "_scope_exit_status",
            lambda unit: ("nonzero_exit", 3) if unit == "kanban-cold-run-1" else None,
        )

        crashed = kbd.detect_crashed_workers(conn)
        assert task_id in crashed

        events = kb.list_events(conn, task_id)
        crash_events = [e for e in events if e.kind == "crashed"]
        assert crash_events, "expected a crashed event"
        assert crash_events[-1].payload.get("exit_kind") == "nonzero_exit"
        assert crash_events[-1].payload.get("exit_code") == 3
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Re-adoption regression: host-prefix (not full claimer) matching survives a
# gateway restart that changes the dispatcher's own PID.
# --------------------------------------------------------------------------


def test_reclaim_does_not_double_dispatch_after_simulated_gateway_restart(tmp_path, monkeypatch):
    """Pins the do-not-regress contract from the spec: ``_reclaim_dead_workers``
    (host-local claim reclaim) must match on hostname prefix only, so a claim
    lock embedding a stale (pre-restart) dispatcher PID is still recognized
    as host-local and, while the worker PID is alive, is left alone rather
    than reclaimed (which would double-dispatch a duplicate worker)."""
    import socket

    import hermes_cli.kanban_db_connect as kbc

    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="restart survivor", assignee="coder")
        host = socket.gethostname() or "unknown"
        # Simulate a claim minted by a dispatcher PID that no longer exists
        # (pre-restart) — only the hostname prefix should matter for reclaim.
        stale_claimer = f"{host}:999999999"
        claimed = kb.claim_task(conn, task_id, claimer=stale_claimer)
        assert claimed is not None
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (555555, task_id))

        # The worker PID itself is genuinely still alive (survived the restart).
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == 555555)

        crashed = kbd.detect_crashed_workers(conn)
        assert task_id not in crashed

        row = conn.execute("SELECT status, claim_lock, worker_pid FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert row["status"] == "running"
        assert row["claim_lock"] == stale_claimer
        assert row["worker_pid"] == 555555
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Termination-path routing: unit-stop vs. bare-PID kill.
# --------------------------------------------------------------------------


def test_terminate_reclaimed_worker_uses_unit_stop_when_worker_unit_set(monkeypatch):
    import socket

    host = socket.gethostname() or "unknown"
    claim_lock = f"{host}:123"
    calls = {"stop_unit": [], "kill": []}

    def fake_stop_unit(unit_name):
        calls["stop_unit"].append(unit_name)
        return True

    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)

    info = kbd._terminate_reclaimed_worker(
        4242, claim_lock, worker_unit="kanban-t1-run-1", stop_unit_fn=fake_stop_unit,
        signal_fn=lambda *a: calls["kill"].append(a),
    )

    assert calls["stop_unit"] == ["kanban-t1-run-1"]
    assert calls["kill"] == []  # bare-PID path must not fire when a unit is set
    assert info["terminated"] is True
    assert info["worker_unit"] == "kanban-t1-run-1"


def test_terminate_reclaimed_worker_uses_bare_kill_without_worker_unit(monkeypatch):
    import socket

    host = socket.gethostname() or "unknown"
    claim_lock = f"{host}:123"
    calls = {"stop_unit": [], "kill": []}

    def fake_stop_unit(unit_name):
        calls["stop_unit"].append(unit_name)
        return True

    def fake_kill(pid, sig):
        calls["kill"].append((pid, sig))

    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kbd, "_poll_worker_exit", lambda pid: True)

    info = kbd._terminate_reclaimed_worker(
        4242, claim_lock, worker_unit=None, stop_unit_fn=fake_stop_unit, signal_fn=fake_kill,
    )

    assert calls["stop_unit"] == []  # unit-stop path must not fire without a unit
    assert calls["kill"] and calls["kill"][0][0] == 4242
    assert info["terminated"] is True
    assert "worker_unit" not in info
