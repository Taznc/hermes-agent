"""Tests for the multi-board kanban layer (``hermes kanban boards …``).

Covers the pieces added when boards became a first-class concept:

* Slug validation and normalisation.
* Path resolution for ``default`` (legacy ``<root>/kanban.db``) vs
  named boards (``<root>/kanban/boards/<slug>/kanban.db``).
* Current-board persistence via ``<root>/kanban/current`` and
  ``HERMES_KANBAN_BOARD`` env var.
* ``connect(board=)`` isolation — writes on one board don't leak.
* ``create_board`` / ``list_boards`` / ``remove_board`` round trip.
* CLI surface: ``hermes kanban boards list/create/switch/rm``.
* ``_default_spawn`` injects ``HERMES_KANBAN_BOARD`` into worker env.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Ensure the worktree (not the stale global clone) is first on sys.path.
_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_transfer as kt


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with no prior kanban state.

    The autouse hermetic conftest already nukes credentials + TZ; this
    fixture layers a per-test HERMES_HOME plus a path-init cache reset
    so each test sees a truly empty board set.
    """
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    # Also reset hermes_constants cache so get_default_hermes_root() re-reads.
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    # Kanban module-level init cache must not leak between tests.
    kb._INITIALIZED_PATHS.clear()
    return home


# ---------------------------------------------------------------------------
# Slug validation
# ---------------------------------------------------------------------------

class TestSlugValidation:
    @pytest.mark.parametrize("good", [
        "default", "atm10-server", "hermes-agent", "proj_1", "a",
        "very-long-but-still-ok-slug-with-hyphens-and-numbers-1234",
    ])
    def test_accepts_valid(self, good):
        assert kb._normalize_board_slug(good) == good


    def test_empty_returns_none(self):
        assert kb._normalize_board_slug(None) is None
        assert kb._normalize_board_slug("") is None
        assert kb._normalize_board_slug("   ") is None


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

class TestPathResolution:
    def test_default_board_legacy_path(self, fresh_home):
        """The default board's DB lives at ``<root>/kanban.db`` for back-compat."""
        assert kb.kanban_db_path() == fresh_home / "kanban.db"
        assert kb.kanban_db_path(board="default") == fresh_home / "kanban.db"

    def test_named_board_under_boards_dir(self, fresh_home):
        p = kb.kanban_db_path(board="atm10-server")
        assert p == fresh_home / "kanban" / "boards" / "atm10-server" / "kanban.db"


    def test_env_var_db_override_wins_when_under_current_home(
        self, fresh_home, monkeypatch,
    ):
        """``HERMES_KANBAN_DB`` pins the file for callers with no board opinion,
        as long as it lives under the currently-resolved kanban home — the
        dispatcher's happy path, where it computes the override against its
        own home before spawning the worker. See
        ``test_stale_env_var_db_override_is_dropped`` for the case where the
        override was computed against a DIFFERENT (e.g. production) home, and
        ``test_explicit_board_beats_env_path_pin`` for why an explicit
        ``board=`` argument is NOT redirected by this pin."""
        forced = fresh_home / "kanban" / "boards" / "hermes-fork" / "kanban.db"
        forced.parent.mkdir(parents=True)
        monkeypatch.setenv("HERMES_KANBAN_DB", str(forced))
        assert kb.kanban_db_path() == forced
        assert kb.kanban_db_path(board="default") == forced

    def test_explicit_board_beats_env_path_pin(self, fresh_home, monkeypatch):
        """An explicit board must not be redirected by a worker's inherited pin.

        The pin is deliberately placed UNDER ``fresh_home`` so it clears the
        containment guard (``_pin_is_honored``): this test must isolate the
        precedence rule, not accidentally pass because the pin was dropped for
        being out-of-home.
        """
        pinned_db = fresh_home / "kanban" / "boards" / "pinned" / "kanban.db"
        pinned_workspaces = fresh_home / "kanban" / "boards" / "pinned" / "workspaces"
        pinned_db.parent.mkdir(parents=True)
        monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned_db))
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(pinned_workspaces))

        assert kb.kanban_db_path() == pinned_db
        assert kb.workspaces_root() == pinned_workspaces
        assert kb.kanban_db_path(board="target") == (
            fresh_home / "kanban" / "boards" / "target" / "kanban.db"
        )
        assert kb.workspaces_root(board="target") == (
            fresh_home / "kanban" / "boards" / "target" / "workspaces"
        )
        # The CLI represents ``--board target`` as a dedicated explicit override
        # (`scoped_explicit_board`), so it must have the same precedence as
        # ``board=target``.
        with kb.scoped_explicit_board("target"):
            assert kb.kanban_db_path() == (
                fresh_home / "kanban" / "boards" / "target" / "kanban.db"
            )

    def test_implicit_current_board_scope_does_not_beat_env_path_pin(
        self, fresh_home, monkeypatch,
    ):
        """``scoped_current_board`` alone (no explicit-board override) is used by
        callers with no board opinion of their own — the dashboard's
        ``_with_board_pinned`` pins ``default`` on every unparameterised request,
        and the watchers scope ``HERMES_KANBAN_BOARD`` per tick. Neither may
        discard an inherited ``HERMES_KANBAN_DB``/``HERMES_KANBAN_WORKSPACES_ROOT``
        pin: that would split writes (which land on the pin) from slug-addressed
        reads (which would land on a different, empty file).

        Pin kept under ``fresh_home`` for the same reason as the test above.
        """
        pinned_db = fresh_home / "kanban" / "boards" / "pinned" / "kanban.db"
        pinned_db.parent.mkdir(parents=True)
        monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned_db))

        assert kb.kanban_db_path(board="default") == pinned_db
        with kb.scoped_current_board("default"):
            assert kb.kanban_db_path() == pinned_db
        assert kb.kanban_db_path() == pinned_db

    def test_env_var_db_override_outside_home_wins_when_vouched_for(
        self, fresh_home, tmp_path, monkeypatch,
    ):
        """An out-of-home pin is honored only when it declares its intent.

        Symlink/Docker layouts legitimately put the board outside the home the
        process resolves. Containment alone cannot tell that apart from the
        stale inherited env that let sandboxed probes drive the live board, so
        ``HERMES_KANBAN_PIN_HOME`` names the home the caller is running under.
        """
        forced = tmp_path / "custom.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(forced))
        monkeypatch.setenv("HERMES_KANBAN_PIN_HOME", str(fresh_home))
        assert kb.kanban_db_path() == forced
        # An EXPLICIT board still reaches across (t_05ebe370): vouching decides
        # whether an out-of-home pin may be honored at all, not whether it may
        # override a caller who named a board. This line previously asserted
        # board="ignored" -> forced, which encoded the ambient-beats-explicit
        # bug rather than this test's vouching contract.
        assert kb.kanban_db_path(board="ignored") == (
            fresh_home / "kanban" / "boards" / "ignored" / "kanban.db"
        )

    def test_stale_env_var_db_override_is_dropped(
        self, fresh_home, tmp_path, monkeypatch, caplog,
    ):
        """Regression for the live-board leak: a probe that sandboxes itself via
        HERMES_HOME must not still resolve HERMES_KANBAN_DB against a DIFFERENT
        (e.g. production) home it inherited from a parent/dispatcher process.

        Simulates exactly the footgun in the bug report: the caller repoints
        HERMES_HOME to its own sandbox but a stale HERMES_KANBAN_DB, computed
        against a different home, is still sitting in the environment.
        """
        stale_production_db = tmp_path / "other-home" / "kanban.db"
        stale_production_db.parent.mkdir(parents=True)
        monkeypatch.setenv("HERMES_KANBAN_DB", str(stale_production_db))
        # fresh_home already points HERMES_HOME at a sandbox unrelated to
        # stale_production_db's parent, mirroring the sandboxed-probe setup.
        import logging
        with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
            resolved = kb.kanban_db_path()
        assert resolved == fresh_home / "kanban.db"
        assert resolved != stale_production_db
        assert any("stale" in rec.message for rec in caplog.records)

    def test_stale_env_var_applies_to_workspaces_and_attachments_roots(
        self, fresh_home, tmp_path, monkeypatch,
    ):
        """The fix is in the shared resolver, so every sibling using
        ``_board_path`` (not just ``kanban_db_path``) drops a stale pin."""
        stale = tmp_path / "other-home" / "workspaces"
        stale.parent.mkdir(parents=True)
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(stale))
        monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", str(stale))
        assert kb.workspaces_root() == fresh_home / "kanban" / "workspaces"
        assert kb.attachments_root() == fresh_home / "kanban" / "attachments"


# ---------------------------------------------------------------------------
# Current-board resolution
# ---------------------------------------------------------------------------

class TestCurrentBoard:



    def test_stale_file_pointer_falls_back_to_default(self, fresh_home):
        current = fresh_home / "kanban" / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_text("missing-board\n", encoding="utf-8")

        assert kb.get_current_board() == "default"
        assert not kb.board_exists("missing-board")
        assert [b["slug"] for b in kb.list_boards()] == ["default"]



    def test_kanban_db_path_reads_current(self, fresh_home):
        """kanban_db_path() with no args respects the on-disk pointer."""
        kb.create_board("my-proj")
        kb.set_current_board("my-proj")
        expected = fresh_home / "kanban" / "boards" / "my-proj" / "kanban.db"
        assert kb.kanban_db_path() == expected


# ---------------------------------------------------------------------------
# Board CRUD
# ---------------------------------------------------------------------------

class TestBoardCRUD:






    @pytest.mark.parametrize("archive", [True, False])
    def test_remove_clears_init_cache_for_recreated_db(self, fresh_home, archive):
        # Regression for #23833: poll loops that call connect(board=slug) right
        # after remove_board() recreate an empty kanban.db at the same path
        # (connect() does mkdir(exist_ok=True)). If _INITIALIZED_PATHS still
        # contains the resolved path, the CREATE TABLE pass is skipped and
        # downstream readers hit `no such table: task_events`.
        kb.create_board("recycle")
        # First connect populates _INITIALIZED_PATHS for this DB.
        with kbc.connect(board="recycle") as conn:
            kb.create_task(conn, title="t1", assignee="dev")
        db_path = kb.board_dir("recycle") / "kanban.db"
        assert str(db_path.resolve()) in kb._INITIALIZED_PATHS

        kb.remove_board("recycle", archive=archive)
        # remove_board must drop the cache entry so a re-create through
        # connect() gets a fresh schema-init pass.
        assert str(db_path.resolve()) not in kb._INITIALIZED_PATHS

        # Simulate the event-stream poll: re-open the same slug. connect()
        # recreates the directory + empty .db; the schema must be re-applied.
        with kbc.connect(board="recycle") as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "task_events" in tables
        assert "tasks" in tables

    def test_rename_updates_metadata(self, fresh_home):
        kb.create_board("slug-immutable")
        kb.write_board_metadata("slug-immutable", name="New Display Name")
        assert kb.read_board_metadata("slug-immutable")["name"] == "New Display Name"
        # Slug must not change.
        assert kb.board_exists("slug-immutable")


# ---------------------------------------------------------------------------
# Connection isolation
# ---------------------------------------------------------------------------

class TestConnectionIsolation:
    def test_tasks_do_not_leak_across_boards(self, fresh_home):
        kb.create_board("alpha")
        kb.create_board("beta")

        with kbc.connect(board="alpha") as conn:
            kb.create_task(conn, title="alpha-task-1", assignee="dev")
            kb.create_task(conn, title="alpha-task-2", assignee="dev")

        with kbc.connect(board="beta") as conn:
            kb.create_task(conn, title="beta-only", assignee="dev")

        with kbc.connect(board="alpha") as conn:
            a = kb.list_tasks(conn)
        with kbc.connect(board="beta") as conn:
            b = kb.list_tasks(conn)
        with kbc.connect(board="default") as conn:
            d = kb.list_tasks(conn)

        assert {t.title for t in a} == {"alpha-task-1", "alpha-task-2"}
        assert {t.title for t in b} == {"beta-only"}
        assert d == []

    def test_connect_without_args_uses_current(self, fresh_home):
        kb.create_board("curr")
        kb.set_current_board("curr")
        with kbc.connect() as conn:
            kb.create_task(conn, title="implicit", assignee="x")
        with kbc.connect(board="curr") as conn:
            tasks = kb.list_tasks(conn)
        assert [t.title for t in tasks] == ["implicit"]

    def test_connect_env_var_overrides_current(self, fresh_home, monkeypatch):
        kb.create_board("persist")
        kb.create_board("envwin")
        kb.set_current_board("persist")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "envwin")
        with kbc.connect() as conn:
            kb.create_task(conn, title="via-env", assignee="x")
        with kbc.connect(board="envwin") as conn:
            assert [t.title for t in kb.list_tasks(conn)] == ["via-env"]
        with kbc.connect(board="persist") as conn:
            assert kb.list_tasks(conn) == []


# ---------------------------------------------------------------------------
# Worker spawn env injection
# ---------------------------------------------------------------------------

class TestWorkerSpawnEnv:
    """Ensure the dispatcher pins ``HERMES_KANBAN_BOARD`` / DB / workspaces on spawn.

    We monkey-patch ``subprocess.Popen`` to capture the child env without
    actually spawning anything.
    """

    def test_default_spawn_sets_env_vars(self, fresh_home, monkeypatch):
        captured = {}

        class FakeProc:
            pid = 12345

        def fake_popen(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env", {})
            return FakeProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        kb.create_board("spawntest")

        task = kb.Task(
            id="t_abc",
            title="worker test",
            body=None,
            assignee="teknium",
            status="ready",
            priority=0,
            created_by="user",
            created_at=0,
            started_at=None,
            completed_at=None,
            workspace_kind="scratch",
            workspace_path=None,
            claim_lock=None,
            claim_expires=None,
            tenant=None,
        )

        kbd._default_spawn(task, str(fresh_home / "ws"), board="spawntest")

        env = captured["env"]
        assert env["HERMES_KANBAN_BOARD"] == "spawntest"
        assert env["HERMES_KANBAN_TASK"] == "t_abc"
        # DB path should match the per-board DB, not the legacy default.
        expected_db = fresh_home / "kanban" / "boards" / "spawntest" / "kanban.db"
        assert env["HERMES_KANBAN_DB"] == str(expected_db)
        expected_ws = fresh_home / "kanban" / "boards" / "spawntest" / "workspaces"
        assert env["HERMES_KANBAN_WORKSPACES_ROOT"] == str(expected_ws)


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

def _cli(args: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Run ``hermes kanban …`` with PYTHONPATH pinned to the worktree."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_WORKTREE)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban"] + args,
        env=env,
        capture_output=True,
        text=True,
        cwd=str(_WORKTREE),
        timeout=30,
    )


class TestCLI:
    def test_boards_list_default_only(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        res = _cli(["boards", "list", "--json"], env_extra=env)
        assert res.returncode == 0, res.stderr
        data = json.loads(res.stdout)
        slugs = [b["slug"] for b in data]
        assert slugs == ["default"]
        assert data[0]["is_current"] is True


    def test_board_flag_beats_inherited_db_pin_via_cli(self, tmp_path):
        """A dispatched worker's DB pin remains the no-flag default, but must not
        swallow the operator's explicit ``--board`` target.

        This exercises the real CLI scope rather than the lower-level resolver:
        the former bug set only ``scoped_current_board``, which is intentionally
        still implicit, so a pin made the command silently write to ``source``.
        """
        env = {"HERMES_HOME": str(tmp_path)}
        assert _cli(["boards", "create", "source"], env_extra=env).returncode == 0
        assert _cli(["boards", "create", "target"], env_extra=env).returncode == 0
        pinned_db = tmp_path / "kanban" / "boards" / "source" / "kanban.db"
        pinned_env = {**env, "HERMES_KANBAN_DB": str(pinned_db)}

        created = _cli(
            ["--board", "target", "create", "Target task", "--assignee", "dev"],
            env_extra=pinned_env,
        )
        assert created.returncode == 0, created.stderr

        target = _cli(["--board", "target", "list", "--json"], env_extra=pinned_env)
        inherited = _cli(["list", "--json"], env_extra=pinned_env)
        assert target.returncode == inherited.returncode == 0
        assert [task["title"] for task in json.loads(target.stdout)] == ["Target task"]
        assert json.loads(inherited.stdout) == []

    def test_per_board_task_isolation_via_cli(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        assert _cli(["boards", "create", "projA"], env_extra=env).returncode == 0
        assert _cli(["boards", "create", "projB"], env_extra=env).returncode == 0

        # Create one task on each via --board.
        r = _cli(["--board", "projA", "create", "Task A", "--assignee", "dev"], env_extra=env)
        assert r.returncode == 0, r.stderr
        r = _cli(["--board", "projB", "create", "Task B", "--assignee", "dev"], env_extra=env)
        assert r.returncode == 0, r.stderr

        # list on each board only shows its own.
        listA = _cli(["--board", "projA", "list", "--json"], env_extra=env)
        listB = _cli(["--board", "projB", "list", "--json"], env_extra=env)
        listD = _cli(["list", "--json"], env_extra=env)

        titlesA = [t["title"] for t in json.loads(listA.stdout)]
        titlesB = [t["title"] for t in json.loads(listB.stdout)]
        titlesD = [t["title"] for t in json.loads(listD.stdout)]

        assert titlesA == ["Task A"]
        assert titlesB == ["Task B"]
        assert titlesD == []


# ---------------------------------------------------------------------------
# Board-inventory lock (cross-process)
# ---------------------------------------------------------------------------

# Real subprocesses, not threads or ``multiprocessing`` forks: the lock's
# contract is a *kernel* lock released when the owning process dies, and only
# separate processes exercise that. The child re-derives every path from its
# own env, so it proves the lock identity is shared without either side being
# told a path.
_CHILD_SCRIPT = '''\
"""Test child: perform one board-inventory operation and report the outcome."""
import json
import os
import sys
import time
from pathlib import Path

worktree, spec_path = sys.argv[1], sys.argv[2]
sys.path.insert(0, worktree)
spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))

# Fleet rule: a kanban probe must clear EVERY inherited HERMES_KANBAN_* var,
# not just the home, or it resolves the live board.
for _var in (
    "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_ATTACHMENTS_ROOT", "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_PIN_HOME",
):
    os.environ.pop(_var, None)
os.environ["HERMES_HOME"] = spec["home"]
os.environ["HERMES_KANBAN_HOME"] = spec["home"]

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_inventory as kbi
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_transfer as kt

if not str(kb.boards_root()).startswith(spec["home"]):
    raise SystemExit(f"NOT ISOLATED: {kb.boards_root()}")

if spec.get("default_timeout") is not None:
    kbi.DEFAULT_INVENTORY_LOCK_TIMEOUT_SECONDS = float(spec["default_timeout"])

if spec.get("pause_after_existing_check"):
    original_path_is_new = kbi.path_is_new_board_entry
    paused = False

    def path_is_new_with_pause(path):
        global paused
        result = original_path_is_new(path)
        if not result and not paused:
            paused = True
            Path(spec["observed"]).write_text("existing", encoding="utf-8")
            resume = Path(spec["resume"])
            while not resume.exists():
                time.sleep(0.02)
            Path(spec["resumed"]).write_text("go", encoding="utf-8")
        return result

    kbi.path_is_new_board_entry = path_is_new_with_pause

out = Path(spec["out"])


def record(**payload):
    out.write_text(json.dumps(payload), encoding="utf-8")


op = spec["op"]

if op == "hold":
    with kb.board_inventory_lock():
        Path(spec["ready"]).write_text("held", encoding="utf-8")
        release = Path(spec["release"])
        while not release.exists():
            time.sleep(0.02)
    record(ok=True)
    raise SystemExit(0)

started = time.monotonic()
try:
    if op == "create":
        kb.create_board(spec["slug"])
    elif op == "metadata":
        kb.write_board_metadata(spec["slug"], name=spec["name"])
    elif op == "archive":
        kb.remove_board(spec["slug"], archive=True)
    elif op == "delete":
        kb.remove_board(spec["slug"], archive=False)
    elif op == "import":
        kt.import_board(spec["archive"])
    elif op == "init":
        kb.init_db(board=spec["slug"])
    elif op == "connect":
        kbc.connect(board=spec["slug"]).close()
    elif op == "acquire":
        with kb.board_inventory_lock(timeout=spec["timeout"]):
            pass
    else:
        raise AssertionError("unknown op " + repr(op))
except BaseException as exc:
    record(ok=False, error=type(exc).__name__, message=str(exc),
           elapsed=time.monotonic() - started)
    raise SystemExit(0)
record(ok=True, elapsed=time.monotonic() - started)
'''


def _write_child_script(tmp_path: Path) -> Path:
    script = tmp_path / "inventory_lock_child.py"
    script.write_text(_CHILD_SCRIPT, encoding="utf-8")
    return script


def _child_env(home: Path) -> dict:
    """Env for a child, with every inherited kanban pin stripped."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_KANBAN_")}
    env["PYTHONPATH"] = str(_WORKTREE)
    env["HERMES_HOME"] = str(home)
    return env


def _spawn(script: Path, tmp_path: Path, home: Path, label: str, **spec) -> tuple:
    """Start a child; returns ``(proc, out_path)``."""
    out_path = tmp_path / f"{label}.out.json"
    spec_path = tmp_path / f"{label}.spec.json"
    spec_path.write_text(
        json.dumps({**spec, "home": str(home), "out": str(out_path)}), encoding="utf-8"
    )
    proc = subprocess.Popen(
        [sys.executable, str(script), str(_WORKTREE), str(spec_path)],
        env=_child_env(home), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return proc, out_path


def _wait_for(path: Path, timeout: float = 60.0, proc=None) -> None:
    """Block until ``path`` appears, or fail.

    ``proc`` short-circuits the wait when the child it names has already
    exited: a holder that died never took the lock, so waiting out the full
    timeout only makes the real failure slower to read.
    """
    deadline = time.monotonic() + timeout
    while not path.exists():
        if proc is not None and proc.poll() is not None:
            stdout, stderr = proc.communicate()
            raise AssertionError(
                f"child exited ({proc.returncode}) before creating {path.name}:"
                f"\n{stdout}\n{stderr}"
            )
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {path}")
        time.sleep(0.02)


def _result(proc, out_path: Path, timeout: float = 90.0) -> dict:
    stdout, stderr = proc.communicate(timeout=timeout)
    assert proc.returncode == 0, f"child failed ({proc.returncode}):\n{stdout}\n{stderr}"
    assert out_path.exists(), f"child wrote no result:\n{stdout}\n{stderr}"
    return json.loads(out_path.read_text(encoding="utf-8"))


def _slugs_on_disk(home: Path) -> set[str]:
    root = home / "kanban" / "boards"
    return {p.name for p in root.iterdir() if p.is_dir()} if root.is_dir() else set()


class TestBoardInventoryLock:
    """The public cross-process seam a fleet-wide safety reader needs.

    A reader that enumerates every board and locks each DB has no protection
    from the board *set* changing underneath it — SQLite serializes writers
    within a board, not the directory inventory. These pin that every mutator
    honours one shared lock, and that a caller which cannot get it changes
    nothing rather than proceeding.
    """

    def test_public_api_shape(self, fresh_home):
        """AC4: importable off ``kanban_db``, yields a context manager, and the
        lock path is derived from the canonical boards root (never passed in)."""
        from hermes_cli import kanban_db_inventory as kbi

        assert kb.board_inventory_lock is kbi.board_inventory_lock
        assert kb.BoardInventoryLockTimeout is kbi.BoardInventoryLockTimeout
        with kb.board_inventory_lock() as handle:
            assert handle is None  # a bare guard, not a resource
            # Re-entrant for one thread: create_board nests write_board_metadata.
            with kb.board_inventory_lock(timeout=0):
                kb.create_board("nested-ok")
        assert kb.board_exists("nested-ok")

        # Identity follows boards_root(), and lives BESIDE it so remove_board's
        # rename into boards/_archived/ and list_boards()'s walk never see it.
        lock_path = kbi._lock_path()
        assert lock_path == kb.boards_root().parent / "boards.lock"
        assert kb.boards_root() not in lock_path.parents

    def test_reads_stay_lock_free(self, fresh_home, tmp_path):
        """AC3: ``list_boards()`` is read-only — a foreign holder cannot block it."""
        script = _write_child_script(tmp_path)
        kb.create_board("readable")
        ready, release = tmp_path / "r.flag", tmp_path / "go.flag"
        holder, holder_out = _spawn(
            script, tmp_path, fresh_home, "reader-holder",
            op="hold", ready=str(ready), release=str(release),
        )
        try:
            _wait_for(ready, proc=holder)
            slugs = {b["slug"] for b in kb.list_boards()}
            assert {"default", "readable"} <= slugs
            assert kb.read_board_metadata("readable")["slug"] == "readable"
            assert holder.poll() is None
        finally:
            release.write_text("go", encoding="utf-8")
        assert _result(holder, holder_out)["ok"] is True

    def test_board_inventory_lock_serializes_create_remove_and_import(
        self, fresh_home, tmp_path
    ):
        """AC1: while one process holds the lock, no other process can create,
        archive, delete or import a board — and each proceeds after release."""
        script = _write_child_script(tmp_path)
        kb.create_board("source", name="Source")
        kb.create_board("to-archive")
        kb.create_board("to-delete")
        archive = tmp_path / "source-export.tar.gz"
        kt.export_board("source", str(archive))

        boards = fresh_home / "kanban" / "boards"
        ready, release = tmp_path / "held.flag", tmp_path / "release.flag"
        holder, holder_out = _spawn(
            script, tmp_path, fresh_home, "holder",
            op="hold", ready=str(ready), release=str(release),
        )
        mutators = {}
        try:
            _wait_for(ready, proc=holder)
            for label, spec in {
                "create": {"op": "create", "slug": "brand-new"},
                "archive": {"op": "archive", "slug": "to-archive"},
                "delete": {"op": "delete", "slug": "to-delete"},
                "import": {"op": "import", "archive": str(archive)},
                # connect()/init_db() auto-create a missing named board, so they
                # are inventory mutators too — a lock the direct DB entry points
                # bypass does not freeze the inventory at all.
                "init": {"op": "init", "slug": "init-bypass"},
                "connect": {"op": "connect", "slug": "connect-bypass"},
            }.items():
                mutators[label] = _spawn(script, tmp_path, fresh_home, label, **spec)

            # Let every mutator reach (and block on) its acquisition.
            time.sleep(3.0)

            assert holder.poll() is None, "holder exited early"
            for label, (proc, _out) in mutators.items():
                assert proc.poll() is None, f"{label} did not block on the lock"
            # The inventory is frozen: nothing added, nothing removed.
            assert _slugs_on_disk(fresh_home) == {"source", "to-archive", "to-delete"}
            assert not (boards / "brand-new").exists()
            assert not (boards / "source-2").exists()
            assert not (boards / "_archived").exists()
            assert not (boards / "init-bypass" / "kanban.db").exists()
            assert not (boards / "connect-bypass" / "kanban.db").exists()
            assert (boards / "to-archive" / "kanban.db").exists()
            assert (boards / "to-delete" / "kanban.db").exists()
        finally:
            release.write_text("go", encoding="utf-8")

        assert _result(holder, holder_out)["ok"] is True
        for label, (proc, out_path) in mutators.items():
            res = _result(proc, out_path)
            assert res["ok"] is True, f"{label} failed after release: {res}"

        # Each mutation landed once the lock was free.
        assert (boards / "brand-new" / "kanban.db").exists()
        assert not (boards / "to-archive").exists()
        archived = sorted((boards / "_archived").iterdir())
        assert [p.name.rsplit("-", 1)[0] for p in archived] == ["to-archive"]
        assert not (boards / "to-delete").exists()
        assert (boards / "source-2" / "kanban.db").exists()
        assert (boards / "source-2" / "board.json").exists()
        assert (boards / "init-bypass" / "kanban.db").exists()
        assert (boards / "connect-bypass" / "kanban.db").exists()
        assert {b["slug"] for b in kb.list_boards()} == {
            "default", "source", "source-2", "brand-new",
            "init-bypass", "connect-bypass",
        }

    def test_connecting_to_an_existing_board_stays_lock_free(
        self, fresh_home, tmp_path
    ):
        """The gate is scoped to *creation*, not to every open.

        Gating every ``connect`` on the inventory lock would let one fleet
        reader freeze all board reads across the fleet — the opposite of the
        card's read-only requirement. An existing board adds no entry, so it
        must open while a foreign holder is mid-sweep.
        """
        script = _write_child_script(tmp_path)
        kb.create_board("already-here")
        ready, release = tmp_path / "held.flag", tmp_path / "go.flag"
        holder, holder_out = _spawn(
            script, tmp_path, fresh_home, "existing-holder",
            op="hold", ready=str(ready), release=str(release),
        )
        try:
            _wait_for(ready, proc=holder)
            # Bounded well under the default: a wrongly-gated open would raise
            # BoardInventoryLockTimeout here rather than return a connection.
            proc, out_path = _spawn(
                script, tmp_path, fresh_home, "existing-connect",
                op="connect", slug="already-here", default_timeout=0.25,
            )
            res = _result(proc, out_path, timeout=30.0)
            assert res["ok"] is True, f"existing-board connect was blocked: {res}"
            # Same for the default board's legacy <root>/kanban.db.
            proc, out_path = _spawn(
                script, tmp_path, fresh_home, "default-connect",
                op="connect", slug="default", default_timeout=0.25,
            )
            res = _result(proc, out_path, timeout=30.0)
            assert res["ok"] is True, f"default-board connect was blocked: {res}"
            assert holder.poll() is None
        finally:
            release.write_text("go", encoding="utf-8")
        assert _result(holder, holder_out)["ok"] is True

    @pytest.mark.parametrize("op", ["connect", "init"])
    def test_existing_board_open_cannot_recreate_after_concurrent_remove(
        self, fresh_home, tmp_path, op
    ):
        """An existing-board fast path may race with removal, but it must not
        recreate the board while a fleet reader holds the inventory lock.

        The child pauses immediately after observing ``victim`` as existing.
        The parent then deletes it and starts a foreign inventory-lock holder
        before allowing the child to continue. The existing path must use a
        no-create open, notice that its board vanished, and retry creation only
        after the holder releases. Both direct entry points exercise the race.
        """
        script = _write_child_script(tmp_path)
        kb.create_board("victim")
        boards = fresh_home / "kanban" / "boards"

        observed = tmp_path / f"{op}-observed.flag"
        resume = tmp_path / f"{op}-resume.flag"
        resumed = tmp_path / f"{op}-resumed.flag"
        opener, opener_out = _spawn(
            script, tmp_path, fresh_home, f"racing-{op}",
            op=op, slug="victim", pause_after_existing_check=True,
            observed=str(observed), resume=str(resume), resumed=str(resumed),
        )
        holder = None
        holder_out = None
        holder_release = tmp_path / f"{op}-holder-release.flag"
        try:
            _wait_for(observed, proc=opener)
            kb.remove_board("victim", archive=False)
            assert not (boards / "victim").exists()

            holder_ready = tmp_path / f"{op}-holder-ready.flag"
            holder, holder_out = _spawn(
                script, tmp_path, fresh_home, f"{op}-race-holder",
                op="hold", ready=str(holder_ready), release=str(holder_release),
            )
            _wait_for(holder_ready, proc=holder)
            resume.write_text("go", encoding="utf-8")
            _wait_for(resumed, proc=opener)

            # The operation has resumed past its stale observation. Give it a
            # loose, scheduler-safe interval to reach the lock. It must neither
            # finish nor create even an empty visible board directory.
            time.sleep(2.0)
            assert opener.poll() is None, f"{op} bypassed the inventory lock"
            assert not (boards / "victim").exists()
            assert holder.poll() is None
        finally:
            resume.write_text("go", encoding="utf-8")
            holder_release.write_text("go", encoding="utf-8")

        assert holder is not None and holder_out is not None
        assert _result(holder, holder_out)["ok"] is True
        result = _result(opener, opener_out)
        assert result["ok"] is True, f"{op} failed after release: {result}"
        assert (boards / "victim" / "kanban.db").exists()

    def test_board_inventory_lock_times_out_without_mutating_inventory(
        self, fresh_home, tmp_path
    ):
        """AC2: a bounded acquisition against a foreign holder refuses
        deterministically, and leaves no board added, removed or half-imported."""
        script = _write_child_script(tmp_path)
        kb.create_board("keeper")
        kb.create_board("exportable")
        archive = tmp_path / "exportable.tar.gz"
        kt.export_board("exportable", str(archive))

        boards = fresh_home / "kanban" / "boards"
        before = _slugs_on_disk(fresh_home)
        keeper_db_bytes = (boards / "keeper" / "kanban.db").read_bytes()

        ready, release = tmp_path / "held.flag", tmp_path / "release.flag"
        holder, holder_out = _spawn(
            script, tmp_path, fresh_home, "timeout-holder",
            op="hold", ready=str(ready), release=str(release),
        )
        try:
            _wait_for(ready, proc=holder)
            # Bounded, sequential: each must refuse rather than hang. The raw
            # context manager takes the bound directly; the mutators inherit it
            # from the module default, which is what proves THEY are bounded too.
            cases = {
                "raw": {"op": "acquire", "timeout": 0.25},
                "create": {"op": "create", "slug": "never-created", "default_timeout": 0.25},
                "metadata": {"op": "metadata", "slug": "also-never", "name": "X",
                             "default_timeout": 0.25},
                "archive": {"op": "archive", "slug": "keeper", "default_timeout": 0.25},
                "delete": {"op": "delete", "slug": "keeper", "default_timeout": 0.25},
                "import": {"op": "import", "archive": str(archive), "default_timeout": 0.25},
                # The direct DB entry points must fail CLOSED too: proceeding
                # unlocked here is exactly the race being guarded.
                "init": {"op": "init", "slug": "never-inited", "default_timeout": 0.25},
                "connect": {"op": "connect", "slug": "never-connected",
                            "default_timeout": 0.25},
            }
            for label, spec in cases.items():
                proc, out_path = _spawn(script, tmp_path, fresh_home, f"to-{label}", **spec)
                res = _result(proc, out_path, timeout=30.0)
                assert res["ok"] is False, f"{label} should have been refused: {res}"
                assert res["error"] == "BoardInventoryLockTimeout", f"{label}: {res}"
                assert "was not changed" in res["message"]
                # Deterministic: it returns on its own deadline, it does not hang.
                assert 0.2 <= res["elapsed"] < 20.0, f"{label} elapsed {res['elapsed']}"
            assert holder.poll() is None, "holder exited early"
        finally:
            release.write_text("go", encoding="utf-8")
        assert _result(holder, holder_out)["ok"] is True

        # Nothing added, nothing removed, nothing partially imported.
        assert _slugs_on_disk(fresh_home) == before
        assert not (boards / "never-created").exists()
        assert not (boards / "also-never").exists()
        assert not (boards / "never-inited" / "kanban.db").exists()
        assert not (boards / "never-connected" / "kanban.db").exists()
        assert not (boards / "_archived").exists()
        assert not (boards / "exportable-2").exists()
        assert (boards / "keeper" / "board.json").exists()
        assert (boards / "keeper" / "kanban.db").read_bytes() == keeper_db_bytes
        assert {b["slug"] for b in kb.list_boards()} == {
            "default", "keeper", "exportable"
        }

    def test_lock_is_released_when_the_holder_dies(self, fresh_home, tmp_path):
        """Kernel-managed, per the card's decision: killing the holder must free
        the lock with no stale-lock reaping (an in-memory mutex cannot do this)."""
        script = _write_child_script(tmp_path)
        ready, release = tmp_path / "held.flag", tmp_path / "release.flag"
        holder, _out = _spawn(
            script, tmp_path, fresh_home, "doomed",
            op="hold", ready=str(ready), release=str(release),
        )
        _wait_for(ready, proc=holder)
        with pytest.raises(kb.BoardInventoryLockTimeout):
            with kb.board_inventory_lock(timeout=0.25):
                pass
        holder.kill()
        holder.communicate(timeout=30)
        # No unlink, no pid file, no reaper: the kernel dropped it.
        with kb.board_inventory_lock(timeout=10):
            kb.create_board("after-death")
        assert kb.board_exists("after-death")
