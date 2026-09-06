"""``HERMES_KANBAN_DB`` must not defeat an explicit sandbox home.

The dispatcher injects ``HERMES_KANBAN_DB`` into EVERY worker env. A worker that
writes a probe/test script and isolates itself the documented way (point
``HERMES_HOME`` at a temp dir) used to still open the production board, because
``kanban_db_path()`` gave the inherited pin unconditional precedence. That is
silent and destructive: one such probe ran ``UPDATE tasks SET status='ready'``
with no WHERE clause against the live board.

Contract asserted here: the dispatcher stamps the kanban home its pin was
computed under (``HERMES_KANBAN_DB_HOME``). A stamped pin is dropped once the
process re-declares a different home; an unstamped, hand-set pin is intent and
still wins.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

# The provenance stamp the dispatcher writes beside HERMES_KANBAN_DB.
PIN_HOME_ENV = "HERMES_KANBAN_DB_HOME"


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


def test_inherited_pin_does_not_escape_a_redeclared_sandbox(sandbox, tmp_path, monkeypatch):
    """The dispatcher-injected pin must not survive the worker re-declaring its home."""
    production = _production_db(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(production))
    monkeypatch.setenv(PIN_HOME_ENV, str(tmp_path / "production" / ".hermes"))

    resolved = kb.kanban_db_path()

    assert kb.kanban_home() == sandbox
    assert resolved != production
    assert sandbox in resolved.parents


def test_hand_set_pin_without_provenance_still_wins(sandbox, tmp_path, monkeypatch):
    """No stamp means a human/test typed it: explicit intent, honored anywhere."""
    forced = tmp_path / "custom.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(forced))

    assert kb.kanban_db_path() == forced
    assert kb.kanban_db_path(board="ignored") == forced


def test_pin_wins_when_the_stamp_matches_the_home(sandbox, monkeypatch):
    """The pin's real job — dispatcher/worker convergence — keeps working."""
    pinned = sandbox / "kanban" / "boards" / "hermes-fork" / "kanban.db"
    pinned.parent.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned))
    monkeypatch.setenv(PIN_HOME_ENV, str(sandbox))

    assert kb.kanban_db_path() == pinned


def test_dropped_pin_is_not_silent(sandbox, tmp_path, monkeypatch, caplog):
    """The original breach was silent; dropping the pin quietly would just move the silence."""
    production = _production_db(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(production))
    monkeypatch.setenv(PIN_HOME_ENV, str(tmp_path / "production" / ".hermes"))

    with caplog.at_level("WARNING", logger="hermes_cli.kanban_db"):
        kb.kanban_db_path()

    assert any(str(production) in r.getMessage() for r in caplog.records)


def test_probe_isolation_pattern_end_to_end(tmp_path):
    """Real subprocess, real imports: the exact worker-probe pattern stays sandboxed."""
    production = tmp_path / "prod" / ".hermes" / "kanban" / "boards" / "hermes-fork" / "kanban.db"
    production.parent.mkdir(parents=True)
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
        "HERMES_KANBAN_DB_HOME": str(tmp_path / "prod" / ".hermes"),
    }
    out = subprocess.run(
        [sys.executable, str(script)], check=True, capture_output=True, text=True, env=env
    ).stdout.split()
    sandbox_tmp, resolved = out[0], out[1]

    assert resolved.startswith(sandbox_tmp), f"probe escaped its sandbox: {resolved}"
    assert not production.exists(), "probe created/opened the production board"
