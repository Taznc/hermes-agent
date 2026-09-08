"""Interpreter-resolution contract for ``scripts/run_tests.sh``.

The canonical runner picks the interpreter every test in the suite is graded
by. It used to consult ``$HERMES_PYTHON`` only as a LAST resort, after
``$REPO_ROOT/.venv``, ``$REPO_ROOT/venv`` and ``$HOME/.hermes/hermes-agent/venv``.
A per-task git worktree has no ``.venv`` of its own, so the probe fell through
to the release venv and silently graded the run against that venv's package
set — two minor versions behind ``pyproject.toml``'s pins in the reported case
— while the operator's explicit ``HERMES_PYTHON=<dev>/.venv/bin/python`` was
ignored entirely. An explicitly-set ``HERMES_PYTHON`` is a deliberate operator
instruction and must beat an inferred directory.

These tests drive the REAL ``scripts/run_tests.sh`` inside a sandboxed fake
repo whose candidate interpreters are shell shims that announce their own
identity. They assert WHICH interpreter the runner chose (a behaviour
contract), never a path snapshot or the script's source text.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_TESTS_SH = REPO_ROOT / "scripts" / "run_tests.sh"

# Printed by a shim when it is invoked as the CHOSEN interpreter (i.e. handed
# run_tests_parallel.py), which is the only observation these tests make.
_MARKER = "SELECTED_INTERPRETER="


def _write_shim(path: Path, name: str, *, has_pytest: bool) -> None:
    """A fake ``python`` that answers the runner's two questions.

    * ``python -c 'import pytest'`` → exit 0/1 per ``has_pytest``.
    * invoked with ``run_tests_parallel.py`` → print its identity and exit 0.

    Anything else (the ``-m compileall`` warm-up) is a silent no-op, so the
    marker is emitted exactly once, by the interpreter the runner selected.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "-c" ]; then\n'
        f"  exit {0 if has_pytest else 1}\n"
        "fi\n"
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        f'    *run_tests_parallel.py) echo "{_MARKER}{name}"; exit 0 ;;\n'
        "  esac\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _make_venv(root: Path, name: str, *, has_pytest: bool) -> Path:
    """A POSIX-layout venv the runner's existence probe will accept."""
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "bin" / "activate").write_text("# fake\n", encoding="utf-8")
    _write_shim(root / "bin" / "python", name, has_pytest=has_pytest)
    return root / "bin" / "python"


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A fake repo root carrying a verbatim copy of the real runner."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(RUN_TESTS_SH, repo / "scripts" / "run_tests.sh")
    (repo / "scripts" / "run_tests.sh").chmod(0o755)
    # The runner execs run_tests_parallel.py by path; it need only exist for
    # the shim to see it on argv.
    (repo / "scripts" / "run_tests_parallel.py").write_text("", encoding="utf-8")
    (tmp_path / "home").mkdir()
    return repo


def _run(sandbox: Path, hermes_python: str | None) -> subprocess.CompletedProcess[str]:
    home = sandbox.parent / "home"
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
    }
    if hermes_python is not None:
        env["HERMES_PYTHON"] = hermes_python
    return subprocess.run(
        ["bash", str(sandbox / "scripts" / "run_tests.sh")],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def _selected(result: subprocess.CompletedProcess[str]) -> str | None:
    for line in result.stdout.splitlines():
        if line.startswith(_MARKER):
            return line[len(_MARKER) :].strip()
    return None


def test_explicit_hermes_python_beats_every_inferred_venv(sandbox: Path) -> None:
    """AC1: an explicit HERMES_PYTHON outranks .venv, venv AND the release venv.

    All three inferred candidates have pytest here, so under the old
    last-resort ordering the first of them won and HERMES_PYTHON was never
    consulted at all.
    """
    home = sandbox.parent / "home"
    _make_venv(sandbox / ".venv", "repo-dotvenv", has_pytest=True)
    _make_venv(sandbox / "venv", "repo-venv", has_pytest=True)
    _make_venv(home / ".hermes" / "hermes-agent" / "venv", "release", has_pytest=True)
    explicit = sandbox.parent / "explicit" / "bin" / "python"
    _write_shim(explicit, "explicit", has_pytest=True)

    result = _run(sandbox, str(explicit))

    assert result.returncode == 0, result.stderr
    assert _selected(result) == "explicit"


def test_worktree_case_explicit_hermes_python_beats_release_venv(sandbox: Path) -> None:
    """AC1 (the reported defect): a worktree has no .venv, only the release venv.

    This is the exact shape that graded runs against the release venv's stale
    package set while the operator had asked for the dev interpreter.
    """
    home = sandbox.parent / "home"
    _make_venv(home / ".hermes" / "hermes-agent" / "venv", "release", has_pytest=True)
    explicit = sandbox.parent / "dev" / ".venv" / "bin" / "python"
    _write_shim(explicit, "dev-venv", has_pytest=True)

    result = _run(sandbox, str(explicit))

    assert result.returncode == 0, result.stderr
    assert _selected(result) == "dev-venv"


def test_unset_hermes_python_keeps_dotvenv_first(sandbox: Path) -> None:
    """AC2: with HERMES_PYTHON unset the historical order is unchanged."""
    home = sandbox.parent / "home"
    _make_venv(sandbox / ".venv", "repo-dotvenv", has_pytest=True)
    _make_venv(sandbox / "venv", "repo-venv", has_pytest=True)
    _make_venv(home / ".hermes" / "hermes-agent" / "venv", "release", has_pytest=True)

    result = _run(sandbox, None)

    assert result.returncode == 0, result.stderr
    assert _selected(result) == "repo-dotvenv"


def test_unset_hermes_python_falls_through_dotvenv_to_venv_to_release(
    sandbox: Path,
) -> None:
    """AC2: the pytest-import guard still skips candidates and keeps probing."""
    home = sandbox.parent / "home"
    _make_venv(sandbox / ".venv", "repo-dotvenv", has_pytest=False)
    _make_venv(sandbox / "venv", "repo-venv", has_pytest=False)
    _make_venv(home / ".hermes" / "hermes-agent" / "venv", "release", has_pytest=True)

    result = _run(sandbox, None)

    assert result.returncode == 0, result.stderr
    assert _selected(result) == "release"
    assert "skipping venv without pytest" in result.stderr


@pytest.mark.parametrize("dotvenv", ["absent", "without-pytest"])
def test_unset_hermes_python_prefers_repo_venv_over_release_venv(
    sandbox: Path,
    dotvenv: str,
) -> None:
    """AC2/AC3: ``venv`` outranks the release venv, not merely ``.venv``.

    The other unset-HERMES_PYTHON cases can be satisfied by any order whose
    first element is ``.venv`` and whose last is the release venv, so they
    leave the middle candidate's rank unpinned. Here ``.venv`` cannot win and
    BOTH remaining candidates are usable, so the selection is decided purely
    by their relative order: swapping them in the runner turns this red.
    """
    home = sandbox.parent / "home"
    if dotvenv == "without-pytest":
        _make_venv(sandbox / ".venv", "repo-dotvenv", has_pytest=False)
    _make_venv(sandbox / "venv", "repo-venv", has_pytest=True)
    _make_venv(home / ".hermes" / "hermes-agent" / "venv", "release", has_pytest=True)

    result = _run(sandbox, None)

    assert result.returncode == 0, result.stderr
    assert _selected(result) == "repo-venv"


def test_hermes_python_without_pytest_is_ignored_and_probe_continues(
    sandbox: Path,
) -> None:
    """AC2: the import guard that motivated the last-resort placement survives.

    HERMES_PYTHON may point at a release venv with no pytest (inherited from a
    wrapped `hermes` binary). That must not shadow a usable local venv.
    """
    _make_venv(sandbox / ".venv", "repo-dotvenv", has_pytest=True)
    explicit = sandbox.parent / "nopytest" / "bin" / "python"
    _write_shim(explicit, "nopytest", has_pytest=False)

    result = _run(sandbox, str(explicit))

    assert result.returncode == 0, result.stderr
    assert _selected(result) == "repo-dotvenv"


def test_nonexistent_hermes_python_is_ignored_and_probe_continues(
    sandbox: Path,
) -> None:
    """AC2: a stale/dangling HERMES_PYTHON path is ignored, not fatal."""
    _make_venv(sandbox / ".venv", "repo-dotvenv", has_pytest=True)

    result = _run(sandbox, str(sandbox.parent / "does" / "not" / "exist"))

    assert result.returncode == 0, result.stderr
    assert _selected(result) == "repo-dotvenv"


def test_no_usable_interpreter_anywhere_still_fails_with_guidance(
    sandbox: Path,
) -> None:
    """AC2: the terminal failure path and its message are unchanged."""
    explicit = sandbox.parent / "nopytest" / "bin" / "python"
    _write_shim(explicit, "nopytest", has_pytest=False)

    result = _run(sandbox, str(explicit))

    assert result.returncode == 1
    assert _selected(result) is None
    assert "no virtualenv with pytest found" in result.stderr
    assert "HERMES_PYTHON" in result.stderr
