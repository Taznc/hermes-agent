"""A sandbox/MCP child must resolve the PINNED board, not the default one.

Fork regression guard. ``_build_child_env`` (execute_code) and ``_build_safe_env``
(MCP stdio) forward ``HERMES_KANBAN_DB``/``_BOARD`` to their children but built
those envs from an allowlist that dropped ``HERMES_KANBAN_HOME``. The fork's
stale-pin guard (``kanban_db._pin_is_honored``) resolves the DB pin against the
child's *own* ``kanban_home()``, so a child that lost the home var judged the
inherited pin out-of-home, dropped it, and silently resolved the DEFAULT
``~/.hermes/kanban.db`` — a real board — while every env var still looked right.

The stamp ``HERMES_KANBAN_PIN_HOME`` is deliberately NOT forwarded:
``scrub_kanban_env`` strips it so an INHERITED stamp cannot vouch for a stale
pin. ``test_stamp_is_not_forwarded_to_descendants`` pins that distinction.
"""
import json
from pathlib import Path
import subprocess
import sys

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


ROOT = Path(__file__).resolve().parents[2]

_CHILD = (
    "import os, json, sys\n"
    "sys.path.insert(0, {root!r})\n"
    "from hermes_cli import kanban_db as kb\n"
    "print('CHILD=' + json.dumps({{\n"
    "  'db_env': os.getenv('HERMES_KANBAN_DB'),\n"
    "  'pin_stamp': os.getenv('HERMES_KANBAN_PIN_HOME'),\n"
    "  'resolved_db': str(kb.kanban_db_path()),\n"
    "}}))\n"
)


def _pinned_board(tmp_path, monkeypatch):
    """A board pinned INSIDE its own kanban home, as a sandboxed caller sets up."""
    db = tmp_path / "board.db"
    conn = connect(db)
    kb.create_task(conn, title="fixture")
    conn.close()
    for key, value in {
        "HOME": str(tmp_path),
        "HERMES_KANBAN_HOME": str(tmp_path),
        "HERMES_KANBAN_DB": str(db),
        "HERMES_KANBAN_BOARD": "default",
        # The fence is what makes the forwarding branch under test run at all.
        "HERMES_DELEGATED_CHILD_CONTEXT": "1",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("HERMES_KANBAN_PIN_HOME", raising=False)
    return db


def _run(env, tmp_path):
    script = tmp_path / "child.py"
    script.write_text(_CHILD.format(root=str(ROOT)))
    proc = subprocess.run([sys.executable, str(script)], env=env, cwd=tmp_path,
                          stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    line = next(l for l in proc.stdout.splitlines() if l.startswith("CHILD="))
    return json.loads(line[len("CHILD="):])


def _builders(tmp_path):
    from tools.code_execution_env import _build_child_env
    from tools.mcp_tool_config import _build_safe_env
    return {
        "execute_code": _build_child_env(
            rpc_endpoint="fixture", rpc_token="fixture",
            tmpdir=str(tmp_path), child_python=sys.executable),
        "mcp_stdio": _build_safe_env(None),
    }


def test_child_resolves_the_pinned_board_not_the_default(tmp_path, monkeypatch):
    db = _pinned_board(tmp_path, monkeypatch)
    for name, env in _builders(tmp_path).items():
        row = _run(env, tmp_path)
        assert row["db_env"] == str(db), (name, row)
        # The load-bearing assertion: the pin is HONORED, not merely carried.
        assert row["resolved_db"] == str(db), (name, row)


def test_stamp_is_not_forwarded_to_descendants(tmp_path, monkeypatch):
    """The intent stamp must not ride along — an inherited one is not intent."""
    _pinned_board(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_PIN_HOME", str(tmp_path))
    for name, env in _builders(tmp_path).items():
        assert "HERMES_KANBAN_PIN_HOME" not in env, name
        assert _run(env, tmp_path)["pin_stamp"] is None, name
