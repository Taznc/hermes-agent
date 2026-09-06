"""Unit tests for ``kanban.worker_launcher`` spawn wrapping + non-child reaping.

Covers the hermes-workers.slice spec (docs/rfcs/hermes-workers-slice-spec.md,
t_cb47a946) test plan items reachable without a real systemd user session:
fake-launcher argv construction, the strictly-optional default-``[]``
regression pin, ``worker_unit`` persistence (always carrying the explicit
``.scope`` suffix), the launcher applying even when
``_restart_safe_worker_argv`` already rewrapped the argv, the
``systemd-run --user`` reachability probe (fail closed on an unreachable
bus), the gateway-restart re-adoption host-prefix (not full-claimer)
contract, and the termination-path routing (unit-stop vs. bare-PID kill,
with the "not loaded" + still-alive corroboration). A systemd-gated
integration test exercising a real ``systemd-run --user --scope`` lives in
``test_kanban_worker_launcher_systemd_live.py``.

``_scope_exit_status()`` (a prior attempt at classifying a ``--scope``
unit's exit via ``systemctl --user show -p ExecMain*``) was removed: those
properties are never populated for a ``--scope`` unit (systemd adopts,
never forks, the target process into it) and were proven ``None`` on live
systemd 255 for both scopes and services. ``--scope`` is a transparent
exec, so in the common case the worker remains a real, direct,
waitpid-able child and the existing ``_classify_worker_exit`` path already
classifies it with full fidelity; the narrow case where it genuinely isn't
this process's child anymore intentionally resolves to ``"unknown"``
rather than a fabricated verdict.
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
    # The unit id always carries the explicit .scope suffix (B2): every
    # subsequent systemctl --user query/stop must use this exact string, or
    # it silently resolves to a same-named .service unit that never existed.
    assert cmd[unit_index] == "--unit=kanban-t_launcher-run-7.scope"
    separator = cmd.index("--")
    assert separator > unit_index
    assert cmd[separator + 1 : separator + 4] == ["hermes", "-p", "coder"]
    # The Task object is mutated so the caller can persist worker_unit.
    assert task.worker_unit == "kanban-t_launcher-run-7.scope"


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
        assert row["worker_unit"] == f"kanban-{task_id}-run-{claimed.current_run_id}.scope"
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
# B4: the launcher must apply AFTER _restart_safe_worker_argv has already
# rewrapped the argv, not gated on `cmd is base_cmd` identity with the
# pre-rewrap argv (that gate made the launcher unreachable in exactly the
# supervised-systemd-gateway topology it targets).
# --------------------------------------------------------------------------


def test_worker_launcher_applies_even_when_restart_safe_argv_already_rewrapped(worker_setup, monkeypatch):
    root, workspace, task = worker_setup
    _set_worker_launcher(root, ["fake-launcher"])
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/fake-launcher" if name == "fake-launcher" else None)

    # Simulate the supervised-gateway topology: _restart_safe_worker_argv
    # returns a DIFFERENT list object (as it does when it really rewraps).
    def fake_restart_safe(_task, command):
        return ["restart-safe-wrapper", "--", *command]

    monkeypatch.setattr(kbd, "_restart_safe_worker_argv", fake_restart_safe)

    captured = {}

    class FakeProc:
        pid = 9999

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    pid = kbd._default_spawn(task, str(workspace))

    assert pid == 9999
    cmd = captured["cmd"]
    # The launcher prefix must be present even though _restart_safe_worker_argv
    # already produced a new (non-identity) argv — restart-safe wrapping
    # first, launcher wrapping second, applied to ITS output.
    assert cmd[0] == "fake-launcher"
    assert "restart-safe-wrapper" in cmd
    assert cmd.index("fake-launcher") < cmd.index("restart-safe-wrapper")
    assert task.worker_unit == "kanban-t_launcher-run-7.scope"


# --------------------------------------------------------------------------
# Re-audit BLOCKER-1: B4's identity-gate removal makes the launcher wrap
# argv that _restart_safe_worker_argv has ALREADY wrapped in a real
# `systemd-run --user --scope`. If the launcher itself is ALSO a
# `systemd-run --user --scope` entry, nesting a second one is not a
# stronger wrap: `--scope` is a transparent exec, so the outer invocation
# execs straight into the inner one and only the INNER unit ever registers
# with systemd. The persisted `worker_unit` must track a unit that
# genuinely exists, never the phantom outer name.
# --------------------------------------------------------------------------


def test_worker_launcher_skips_redundant_outer_scope_when_already_scope_wrapped(worker_setup, monkeypatch):
    root, workspace, task = worker_setup
    _set_worker_launcher(root, ["systemd-run", "--user", "--scope"])
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run" if name == "systemd-run" else None)
    monkeypatch.setattr(kbd, "_systemd_user_bus_reachable", lambda: True)

    # Simulate the supervised-gateway topology where _restart_safe_worker_argv
    # has already produced a real systemd-run --user --scope invocation.
    inner_unit = "kanban-t_launcher-run-7.scope"

    def fake_restart_safe(_task, command):
        return [
            "systemd-run", "--user", "--scope", "--quiet", "--unit", inner_unit,
            "--collect", "--", *command,
        ]

    monkeypatch.setattr(kbd, "_restart_safe_worker_argv", fake_restart_safe)

    captured = {}

    class FakeProc:
        pid = 3131

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    pid = kbd._default_spawn(task, str(workspace))

    assert pid == 3131
    cmd = captured["cmd"]
    # Exactly one `systemd-run --user --scope` invocation in the argv, not
    # nested two deep -- there is only one "systemd-run" token at all.
    assert cmd.count("systemd-run") == 1
    # worker_unit must be the REAL (inner) unit, which genuinely registers,
    # not a fabricated outer name that would resolve LoadState=not-found.
    assert task.worker_unit == inner_unit


def test_apply_worker_launcher_skips_double_scope_directly():
    task = _make_task(current_run_id=7)
    already_wrapped = [
        "systemd-run", "--user", "--scope", "--quiet", "--unit",
        "kanban-t_launcher-run-7.scope", "--collect", "--", "hermes", "-p", "coder",
    ]

    import hermes_cli.kanban_db_dispatch as kbd_module

    orig_prefix = kbd_module._worker_launcher_prefix
    try:
        kbd_module._worker_launcher_prefix = lambda: ["systemd-run", "--user", "--scope"]
        argv, unit = kbd_module._apply_worker_launcher(task, already_wrapped)
    finally:
        kbd_module._worker_launcher_prefix = orig_prefix

    assert argv is already_wrapped  # no rewrap at all -- pure pass-through
    assert unit == "kanban-t_launcher-run-7.scope"
    assert argv.count("systemd-run") == 1


# --------------------------------------------------------------------------
# Re-audit BLOCKER-2: a resolved `systemd-run --user` launcher entry must
# carry XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS into the SPAWNED CHILD's
# own environment, not just pass the reachability check against the
# spawning process's environment and then discard the resolved values.
# --------------------------------------------------------------------------


def test_worker_launcher_env_overrides_only_for_systemd_run_user():
    assert kbd._worker_launcher_env_overrides([]) == {}
    assert kbd._worker_launcher_env_overrides(["fake-launcher"]) == {}
    overrides = kbd._worker_launcher_env_overrides(["systemd-run", "--user", "--scope"])
    assert set(overrides) == {"XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}


def test_worker_launcher_systemd_run_user_injects_bus_env_into_child(worker_setup, monkeypatch):
    root, workspace, task = worker_setup
    _set_worker_launcher(root, ["systemd-run", "--user", "--scope"])
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run" if name == "systemd-run" else None)
    monkeypatch.setattr(kbd, "_systemd_user_bus_reachable", lambda: True)
    monkeypatch.setattr(
        kbd, "_resolve_systemd_user_bus_env",
        lambda: ("/run/user/4242", "unix:path=/run/user/4242/bus"),
    )
    # Simulate the gateway's actually-stripped environment (#B3's premise):
    # neither bus var present before the spawn.
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)

    captured = {}

    class FakeProc:
        pid = 4141

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = kwargs.get("env") or {}
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    pid = kbd._default_spawn(task, str(workspace))

    assert pid == 4141
    assert captured["env"].get("XDG_RUNTIME_DIR") == "/run/user/4242"
    assert captured["env"].get("DBUS_SESSION_BUS_ADDRESS") == "unix:path=/run/user/4242/bus"


def test_default_spawn_no_launcher_does_not_inject_bus_env(worker_setup, monkeypatch):
    """Default `[]` path: no bus vars are force-injected -- byte-identical to before."""
    root, workspace, task = worker_setup
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)

    captured = {}

    class FakeProc:
        pid = 5151

    def fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    kbd._default_spawn(task, str(workspace))

    assert "XDG_RUNTIME_DIR" not in captured["env"]
    assert "DBUS_SESSION_BUS_ADDRESS" not in captured["env"]


# --------------------------------------------------------------------------
# B3: systemd-run --user launcher entries fail CLOSED against an
# unreachable user D-Bus, not just shutil.which() on the binary.
# --------------------------------------------------------------------------


def test_worker_launcher_systemd_run_user_fails_closed_without_reachable_bus(worker_setup, monkeypatch):
    root, workspace, task = worker_setup
    _set_worker_launcher(root, ["systemd-run", "--user", "--scope"])
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run" if name == "systemd-run" else None)
    monkeypatch.setattr(kbd, "_systemd_user_bus_reachable", lambda: False)

    captured = {}

    class FakeProc:
        pid = 1111

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    pid = kbd._default_spawn(task, str(workspace))

    assert pid == 1111
    # Bus unreachable -> fails closed -> plain Popen, no launcher, no unit.
    assert captured["cmd"][:3] == ["hermes", "-p", "coder"]
    assert task.worker_unit is None


def test_worker_launcher_systemd_run_user_applies_when_bus_reachable(worker_setup, monkeypatch):
    root, workspace, task = worker_setup
    _set_worker_launcher(root, ["systemd-run", "--user", "--scope"])
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run" if name == "systemd-run" else None)
    monkeypatch.setattr(kbd, "_systemd_user_bus_reachable", lambda: True)

    captured = {}

    class FakeProc:
        pid = 2222

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    pid = kbd._default_spawn(task, str(workspace))

    assert pid == 2222
    assert captured["cmd"][:3] == ["systemd-run", "--user", "--scope"]
    assert task.worker_unit == "kanban-t_launcher-run-7.scope"


def test_systemd_user_bus_reachable_checks_socket_on_disk(monkeypatch, tmp_path):
    fake_socket = tmp_path / "bus"
    fake_socket.write_text("", encoding="utf-8")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={fake_socket}")

    assert kbd._systemd_user_bus_reachable() is True

    fake_socket.unlink()
    assert kbd._systemd_user_bus_reachable() is False


# --------------------------------------------------------------------------
# _classify_worker_exit: no systemd fallback. A pid this process never
# reaped resolves to "unknown" — the bounded case the sibling
# infra-interruption classification (kanban.max_infra_interruptions) exists
# to absorb, not a systemd-fabricated verdict.
# --------------------------------------------------------------------------


def test_classify_worker_exit_unknown_for_unreaped_pid():
    kind, code = kbd._classify_worker_exit(999999)
    assert (kind, code) == ("unknown", None)


def test_reclaim_cold_worker_unit_classifies_as_unknown_crash(tmp_path, monkeypatch):
    """End-to-end through ``_reclaim_dead_workers``: a task row carrying a
    ``worker_unit`` but with no ``_recent_worker_exits`` entry (simulating a
    cold/restarted dispatcher process that never reaped this pid itself)
    classifies as the bounded ``"unknown"``/``crashed`` outcome — no
    systemd-fabricated verdict is invented for it."""
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
                (fake_pid, "kanban-cold-run-1.scope", 0, task_id),
            )

        monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
        monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)

        crashed = kbd.detect_crashed_workers(conn)
        assert task_id in crashed

        events = kb.list_events(conn, task_id)
        crash_events = [e for e in events if e.kind == "crashed"]
        assert crash_events, "expected a crashed event"
        # No exit_kind/exit_code stamped for an "unknown" classification.
        assert "exit_kind" not in (crash_events[-1].payload or {})
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
        4242, claim_lock, worker_unit="kanban-t1-run-1.scope", stop_unit_fn=fake_stop_unit,
        signal_fn=lambda *a: calls["kill"].append(a),
    )

    assert calls["stop_unit"] == ["kanban-t1-run-1.scope"]
    assert calls["kill"] == []  # bare-PID path must not fire when a unit is set
    assert info["terminated"] is True
    assert info["worker_unit"] == "kanban-t1-run-1.scope"


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


def test_terminate_reclaimed_worker_not_loaded_with_pid_alive_is_not_success(monkeypatch):
    """B2 regression: ``_stop_systemd_unit`` returning True for a "not
    loaded" unit (e.g. because the wrong unit id was queried, or the unit
    was never actually created) must NOT be reported as a successful
    termination when the worker PID is still alive — only the corroborated
    pairing (stop reported success AND the PID is actually gone) counts."""
    import socket

    host = socket.gethostname() or "unknown"
    claim_lock = f"{host}:123"

    # _stop_systemd_unit says "stopped" (e.g. it read "not loaded" as success
    # against a unit id that was never actually running), but the real
    # worker PID is still alive — the corroborating check must catch this.
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)

    info = kbd._terminate_reclaimed_worker(
        4242, claim_lock, worker_unit="kanban-t1-run-1.scope", stop_unit_fn=lambda unit: True,
    )

    assert info["terminated"] is False
    assert info["termination_attempted"] is True
