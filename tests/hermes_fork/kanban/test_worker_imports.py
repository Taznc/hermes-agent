"""Kanban worker bootstrap must import the runtime, not the task workspace.

``hermes_fork.kanban.worker_imports`` via the ``worker-import-isolation`` FORK
ANCHOR in ``_default_spawn``. Incident t_f05dab8d: a worker whose workspace was
a mid-merge Hermes checkout (conflict markers in ``hermes_state_sessions.py``)
died twice before its first tool call with "invalid decimal literal", because
``python -m hermes_cli.main`` puts the cwd (= workspace) first on ``sys.path``.

The child-process tests capture the exact argv / env / cwd ``_default_spawn``
hands ``Popen`` and then really launch that interpreter, in that env and cwd,
against a workspace full of syntax-broken shadow modules.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_fork.kanban.worker_imports import isolate_worker_imports

RUNTIME_ROOT = Path(kbd.__file__).resolve().parents[1]
CONFLICTED = (
    "def broken():\n"
    "<<<<<<< HEAD\n"
    "    return 1\n"
    "=======\n"
    "    return 2\n"
    ">>>>>>> upstream/main\n"
    "x = 932abc\n"
)
AGENTS_MARKER = "WORKSPACE-AGENTS-MARKER-t_ad07a3d9"


def _make_task():
    return kb.Task(
        id="t_import_iso", title="import isolation", body=None, assignee="elias",
        status="running", priority=0, created_by="test", created_at=1, started_at=None,
        completed_at=None, workspace_kind="dir", workspace_path=None, claim_lock="lock-xyz",
        claim_expires=None, tenant=None, current_run_id=11,
    )


@pytest.fixture
def poisoned_workspace(tmp_path: Path) -> Path:
    """A task workspace that is a conflicted Hermes checkout."""
    ws = tmp_path / "workspace"
    (ws / "hermes_cli").mkdir(parents=True)
    for rel in ("hermes_cli/__init__.py", "hermes_cli/main.py", "hermes_bootstrap.py",
                "hermes_state_sessions.py", "hermes_constants.py", "cron/__init__.py",
                "hermes_fork/__init__.py"):
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(CONFLICTED, encoding="utf-8")
    (ws / "AGENTS.md").write_text(f"# Workspace rules\n{AGENTS_MARKER}\n", encoding="utf-8")
    return ws


@pytest.fixture
def spawn_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, poisoned_workspace: Path):
    """Run the real ``_default_spawn`` and capture what it would launch."""
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_BIN",
                "TERMINAL_CWD", "PYTHONSAFEPATH"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    profile.joinpath(".env").write_text("ELIAS_SECRET_PROBE=elias-value\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    # Under a managed systemd gateway the argv gets a systemd-run wrapper; the
    # wrapper only prefixes the argv, so it is out of scope for the import test.
    monkeypatch.setattr(kbd, "_restart_safe_worker_argv", lambda task, cmd: cmd)

    def run(*, resolve=None):
        captured = {}

        class FakeProc:
            pid = 4321

        def fake_popen(cmd, *args, **kwargs):
            captured.update(cmd=list(cmd), env=dict(kwargs["env"]), cwd=kwargs.get("cwd"))
            return FakeProc()

        with monkeypatch.context() as m:
            m.setattr(subprocess, "Popen", fake_popen)
            if resolve is not None:
                m.setattr(kbd, "_resolve_hermes_argv", resolve)
            assert kbd._default_spawn(_make_task(), str(poisoned_workspace)) == 4321
        return captured

    return run


def _interpreter_prefix(cmd: list[str]) -> list[str]:
    """The worker's interpreter + interpreter options, without ``-m hermes_cli.main ...``."""
    return cmd[: cmd.index("-m")]


PROBE = r"""
import importlib.util, json, os, sys
out = {name: importlib.util.find_spec(name).origin
       for name in ("hermes_cli", "hermes_bootstrap", "hermes_state_sessions", "hermes_constants")}
import hermes_state_sessions  # the module that carried the conflict markers in t_f05dab8d
out["imported"] = hermes_state_sessions.__file__
from agent.runtime_cwd import resolve_context_cwd
from agent.prompt_builder import build_context_files_prompt
out["context_cwd"] = str(resolve_context_cwd())
out["context"] = build_context_files_prompt(cwd=out["context_cwd"], skip_soul=True)
out["process_cwd"] = os.getcwd()
print(json.dumps(out))
"""


def test_worker_imports_runtime_not_conflicted_workspace(spawn_capture, poisoned_workspace):
    """RED on upstream: the shadow hermes_cli/hermes_state_sessions in the workspace win."""
    cap = spawn_capture()
    assert cap["cwd"] == str(poisoned_workspace)  # tools still start in the workspace
    proc = subprocess.run(
        [*_interpreter_prefix(cap["cmd"]), "-c", PROBE],
        cwd=cap["cwd"], env=cap["env"], capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    import json
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    for name in ("hermes_cli", "hermes_bootstrap", "hermes_state_sessions", "hermes_constants"):
        assert Path(out[name]).resolve().is_relative_to(RUNTIME_ROOT), (name, out[name])
        assert not Path(out[name]).resolve().is_relative_to(poisoned_workspace.resolve())
    # Workspace stays the task's working root: process cwd, TERMINAL_CWD and AGENTS.md.
    assert Path(out["process_cwd"]).resolve() == poisoned_workspace.resolve()
    assert Path(out["context_cwd"]).resolve() == poisoned_workspace.resolve()
    assert AGENTS_MARKER in out["context"]


def test_real_worker_launch_boots_from_runtime(spawn_capture, poisoned_workspace):
    """The exact module launch ``_default_spawn`` builds runs from a conflicted workspace."""
    cap = spawn_capture()
    head = cap["cmd"][: cap["cmd"].index("hermes_cli.main") + 1]
    proc = subprocess.run(
        [*head, "--version"], cwd=cap["cwd"], env=cap["env"],
        capture_output=True, text=True, timeout=120,
    )
    assert "invalid decimal literal" not in proc.stderr
    assert "SyntaxError" not in proc.stderr
    assert proc.returncode == 0, proc.stderr[-2000:]


def test_spawn_identity_and_secrets_survive(spawn_capture, poisoned_workspace):
    cap = spawn_capture()
    env = cap["env"]
    assert cap["cmd"][1:4] == ["-P", "-m", "hermes_cli.main"]
    assert env["HERMES_KANBAN_TASK"] == "t_import_iso"
    assert env["HERMES_KANBAN_RUN_ID"] == "11"
    assert env["HERMES_KANBAN_CLAIM_LOCK"] == "lock-xyz"
    assert env["HERMES_KANBAN_WORKSPACE"] == str(poisoned_workspace)
    assert env["TERMINAL_CWD"] == str(poisoned_workspace)
    assert env["HERMES_KANBAN_BOARD"] and env["HERMES_KANBAN_DB"]
    assert env["HERMES_PROFILE"] == "elias"
    assert env["HERMES_HOME"].endswith(os.path.join("profiles", "elias"))
    # Interpreter flag, not env: the worker's own children keep normal sys.path[0].
    assert "PYTHONSAFEPATH" not in env
    entries = env["PYTHONPATH"].split(os.pathsep)
    assert Path(entries[0]).resolve() == RUNTIME_ROOT or RUNTIME_ROOT == Path(
        __import__("sysconfig").get_paths()["purelib"]).resolve()
    assert all(e and os.path.isabs(e) for e in entries)


def test_resolved_shim_argv_is_left_alone(spawn_capture):
    """HERMES_BIN / PATH shim: a console script owns its imports (no cwd entry)."""
    cap = spawn_capture(resolve=lambda: ["/opt/hermes/bin/hermes"])
    assert cap["cmd"][:4] == ["/opt/hermes/bin/hermes", "-p", "elias", "--cli"]
    assert "-P" not in cap["cmd"]


def test_hermes_bin_path_and_bare_name(monkeypatch, tmp_path):
    """HERMES_BIN as a path keeps the shim; an unresolvable bare name falls back to the
    module form, which is then isolated."""
    monkeypatch.setenv("HERMES_BIN", "/opt/hermes/bin/hermes")
    cmd = kbd._resolve_hermes_argv()
    env: dict[str, str] = {}
    assert isolate_worker_imports(list(cmd), env) == ["/opt/hermes/bin/hermes"]
    assert env == {}

    monkeypatch.setenv("HERMES_BIN", "hermes-not-on-path-xyz")
    monkeypatch.setenv("PATH", str(tmp_path))
    cmd = kbd._resolve_hermes_argv()
    assert cmd == [sys.executable, "-m", "hermes_cli.main"]
    assert isolate_worker_imports(cmd, env) == [sys.executable, "-P", "-m", "hermes_cli.main"]


@pytest.mark.parametrize("sep_entries,expected_tail", [
    (["", "relative/dir", "/abs/user/path"], ["/abs/user/path"]),
    ([""], []),
])
def test_relative_pythonpath_entries_are_dropped(sep_entries, expected_tail):
    """Empty / relative PYTHONPATH entries resolve against the cwd (the workspace)."""
    env = {"PYTHONPATH": os.pathsep.join(sep_entries)}
    isolate_worker_imports([sys.executable, "-m", "hermes_cli.main"], env)
    entries = env.get("PYTHONPATH", "").split(os.pathsep)
    assert all(e and os.path.isabs(e) for e in entries)
    assert [e for e in entries if Path(e).resolve() != RUNTIME_ROOT] == expected_tail
