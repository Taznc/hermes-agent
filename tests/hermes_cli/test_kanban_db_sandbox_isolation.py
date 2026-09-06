"""A ``HERMES_KANBAN_*`` path pin must not defeat an explicit sandbox home.

The dispatcher injects ``HERMES_KANBAN_DB``, ``HERMES_KANBAN_WORKSPACES_ROOT``
and friends into EVERY worker env. A worker that writes a probe/test script and
isolates itself the documented way (point ``HERMES_HOME`` at a temp dir) used to
still open the production board, because the shared resolver gave the inherited
pin unconditional precedence. That is silent and destructive: one such probe ran
``UPDATE tasks SET status='ready'`` with no WHERE clause against the live board.

Contract asserted here: a pin is honored when it resolves under the current
kanban home (the dispatcher's normal case), or when ``HERMES_KANBAN_PIN_HOME``
explicitly vouches for it by naming the home this process actually declares.
Anything else is stale inheritance and is dropped, loudly.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

# The stamp the dispatcher writes beside the pins it injects.
PIN_HOME_ENV = "HERMES_KANBAN_PIN_HOME"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A fresh sandbox home, exactly how a probe script isolates itself."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def _production_db(tmp_path: Path) -> Path:
    db = tmp_path / "production" / ".hermes" / "kanban" / "boards" / "hermes-fork" / "kanban.db"
    db.parent.mkdir(parents=True)
    db.touch()
    return db


def test_stamped_inherited_pin_does_not_escape_a_redeclared_sandbox(
    sandbox, tmp_path, monkeypatch
):
    """The dispatcher-injected pin must not survive the worker re-declaring its home."""
    production = _production_db(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(production))
    monkeypatch.setenv(PIN_HOME_ENV, str(tmp_path / "production" / ".hermes"))

    resolved = kb.kanban_db_path()

    assert kb.kanban_home() == sandbox
    assert resolved != production
    assert sandbox in resolved.parents


def test_unstamped_inherited_pin_does_not_escape_the_sandbox(sandbox, tmp_path, monkeypatch):
    """Every worker alive when the fix deploys carries an UNSTAMPED pin.

    A stamp-only rule would leave exactly that population — the one that caused
    the breach — still driving production, so containment must hold with no
    stamp present at all.
    """
    production = _production_db(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(production))
    monkeypatch.delenv(PIN_HOME_ENV, raising=False)

    resolved = kb.kanban_db_path()

    assert resolved != production
    assert sandbox in resolved.parents


def test_workspaces_and_attachments_roots_do_not_escape_the_sandbox(
    sandbox, tmp_path, monkeypatch
):
    """The guard lives in the shared resolver, so the sibling pins are covered too.

    ``HERMES_KANBAN_WORKSPACES_ROOT`` is injected by the same dispatcher block as
    the DB pin, so guarding only the DB still let a sandboxed probe write into
    the live board's workspaces tree.
    """
    board = tmp_path / "production" / ".hermes" / "kanban" / "boards" / "hermes-fork"
    live_ws = board / "workspaces"
    live_at = board / "attachments"
    live_ws.mkdir(parents=True)
    live_at.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(live_ws))
    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", str(live_at))

    assert sandbox in kb.workspaces_root().parents
    assert sandbox in kb.attachments_root().parents


def test_pin_inside_the_current_home_is_honored(sandbox, monkeypatch):
    """The pin's real job — dispatcher/worker convergence — keeps working."""
    pinned = sandbox / "kanban" / "boards" / "hermes-fork" / "kanban.db"
    pinned.parent.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned))
    monkeypatch.delenv(PIN_HOME_ENV, raising=False)

    assert kb.kanban_db_path() == pinned


def test_out_of_home_pin_is_honored_when_vouched_for(sandbox, tmp_path, monkeypatch):
    """The explicit escape hatch: symlink/Docker layouts put the board outside the home.

    Containment alone cannot tell that apart from stale inheritance; the stamp
    can, because it names the home the caller is actually running under.
    """
    forced = tmp_path / "elsewhere" / "custom.db"
    forced.parent.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(forced))
    monkeypatch.setenv(PIN_HOME_ENV, str(sandbox))

    assert kb.kanban_db_path() == forced
    # An EXPLICIT board still reaches across (t_05ebe370) — see the matching
    # note in tests/hermes_cli/test_kanban_boards.py. Vouching gates whether the
    # out-of-home pin is usable at all; it does not outrank a named board.
    assert kb.kanban_db_path(board="ignored") == (
        sandbox / "kanban" / "boards" / "ignored" / "kanban.db"
    )


def test_dropped_pin_is_not_silent(sandbox, tmp_path, monkeypatch, caplog):
    """The original breach was silent; dropping the pin quietly would just move the silence."""
    production = _production_db(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(production))
    monkeypatch.setenv(PIN_HOME_ENV, str(tmp_path / "production" / ".hermes"))

    with caplog.at_level("WARNING", logger="hermes_cli.kanban_db"):
        kb.kanban_db_path()

    assert any(str(production) in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("stamped", [True, False])
def test_probe_isolation_pattern_end_to_end(tmp_path, stamped):
    """Real subprocess, real imports: the exact worker-probe pattern stays sandboxed.

    Run both with and without the stamp — an old worker spawned before the
    dispatcher wrote one is the population this breach actually affected.
    """
    production = tmp_path / "prod" / ".hermes" / "kanban" / "boards" / "hermes-fork" / "kanban.db"
    production.parent.mkdir(parents=True)
    live_ws = production.parent / "workspaces"
    live_ws.mkdir()
    repo_root = Path(__file__).resolve().parents[2]
    script = tmp_path / "probe.py"
    script.write_text(
        textwrap.dedent(
            """
            import os, sys, tempfile
            from pathlib import Path

            TMP = tempfile.mkdtemp(prefix="probe-")
            HOME = Path(TMP) / ".hermes"
            HOME.mkdir(parents=True)
            os.environ["HERMES_HOME"] = str(HOME)
            Path.home = classmethod(lambda cls: Path(TMP))

            from hermes_cli import kanban_db as kb
            from hermes_cli import kanban_db_connect as kbc

            kbc.init_db()
            print(TMP)
            print(kb.kanban_db_path())
            print(kb.workspaces_root())
            """
        ),
        encoding="utf-8",
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "PYTHONPATH": str(repo_root),
        # Exactly what the dispatcher puts in a worker env.
        "HERMES_KANBAN_DB": str(production),
        "HERMES_KANBAN_WORKSPACES_ROOT": str(live_ws),
    }
    if stamped:
        env[PIN_HOME_ENV] = str(tmp_path / "prod" / ".hermes")
    out = subprocess.run(
        [sys.executable, str(script)], check=True, capture_output=True, text=True, env=env
    ).stdout.split()
    sandbox_tmp, db, workspaces = out[0], out[1], out[2]

    assert db.startswith(sandbox_tmp), f"probe escaped its sandbox: {db}"
    assert workspaces.startswith(sandbox_tmp), f"probe escaped its sandbox: {workspaces}"
    assert not production.exists(), "probe created/opened the production board"
