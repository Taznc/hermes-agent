"""The dashboard dispatch nudge must honour the host-level ``kanban.*`` caps.

``POST /api/plugins/kanban/dispatch`` is fired on a debounce by the desktop
after *every* board edit (``autoNudge`` in
``apps/desktop/src/plugins/kanban/api.ts``). It used to pass only ``max_spawn``
into ``dispatch_once``, and an omitted cap there means *unlimited* — so
clicking around the board spawned workers with no host-level bound at all,
measured at 8 concurrent workers against ``max_in_progress: 4``.

These are behaviour contracts on the endpoint, not on its wiring: each one
creates more spawnable tasks than the cap allows, drives a real HTTP request
through the real router, and asserts on how many workers actually spawned.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


def _load_plugin_router():
    """Load plugins/kanban/dashboard/plugin_api.py and return (module, router)."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_caps_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod, mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def spawns(monkeypatch):
    """Record every worker the dispatcher would spawn, without spawning one."""
    recorded: list[str] = []

    def fake_spawn(task, workspace, board=None):
        recorded.append(task.id)
        return 4242

    monkeypatch.setattr(kbd, "_default_spawn", fake_spawn)
    # Memory pressure is a real host property; pin it so a loaded CI box can't
    # clamp the budget to 1 and make a cap test pass for the wrong reason.
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda: "unknown")
    # Synthetic assignees have no profile dir on disk; without this the
    # profile-exists guard routes every task to skipped_nonspawnable and the
    # cap assertions would pass against zero spawns.
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    return recorded


@pytest.fixture
def configured(monkeypatch):
    """Install a fake ``kanban`` config section the endpoint will resolve."""

    def _apply(**kanban_cfg):
        monkeypatch.setattr(
            "hermes_cli.config.load_config", lambda: {"kanban": dict(kanban_cfg)}
        )

    return _apply


@pytest.fixture
def client(kanban_home):
    _mod, router = _load_plugin_router()
    app = FastAPI()
    app.include_router(router, prefix="/api/plugins/kanban")
    return TestClient(app)


def _seed_ready(count: int, assignee: str = "claudeprimary", board: str | None = None) -> list[str]:
    """Create *count* ready, assigned, unclaimed tasks on ``board``."""
    ids = []
    with kbc.connect_closing(board=board) as conn:
        for i in range(count):
            ids.append(kb.create_task(conn, title=f"task-{i}", assignee=assignee))
        conn.execute("UPDATE tasks SET status = 'ready' WHERE status = 'todo'")
        conn.commit()
    return ids


def _running_count() -> int:
    with kbc.connect_closing() as conn:
        return int(
            conn.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0]
        )


def test_dashboard_nudge_reports_an_actionable_rate_limit(client, configured, spawns):
    configured(
        max_in_progress=10,
        max_in_progress_per_profile=10,
        dispatch_start_budget=1,
        dispatch_start_window_seconds=600,
    )
    _seed_ready(1)

    response = client.post("/api/plugins/kanban/dispatch?max=8")

    assert response.status_code == 200
    payload = response.json()
    assert payload["dispatch_paused"]["reason"] == "start_budget_exceeded"
    assert "rate limited until" in payload["dispatch_status"]
    assert "automatically" in payload["dispatch_status"]


def test_dashboard_nudge_names_the_board_in_manual_recovery_status(client, configured, spawns):
    board = "manual-recovery"
    kb.create_board(board)
    configured(
        max_in_progress=10,
        max_in_progress_per_profile=10,
        dispatch_start_budget=1,
        dispatch_start_window_seconds=600,
    )
    with kbc.connect_closing(board=board) as conn:
        task_id = kb.create_task(conn, title="corrupt replay", assignee="worker")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        kb._append_event(conn, task_id, "completed", {})
        conn.commit()

    response = client.post(f"/api/plugins/kanban/dispatch?max=8&board={board}")

    assert response.status_code == 200
    assert "manual intervention required" in response.json()["dispatch_status"]
    assert f"hermes kanban --board {board} dispatch --resume-circuit" in response.json()["dispatch_status"]


# ---------------------------------------------------------------------------
# The host-wide cap
# ---------------------------------------------------------------------------


def test_nudge_cannot_exceed_max_in_progress(client, configured, spawns):
    """``?max=8`` must not beat ``kanban.max_in_progress: 2``.

    The reported failure exactly: the UI's default nudge asks for 8, config
    allows far fewer, and before the fix the request won.
    """
    configured(max_in_progress=2, max_in_progress_per_profile=99)
    _seed_ready(6)

    r = client.post("/api/plugins/kanban/dispatch?max=8")

    assert r.status_code == 200
    assert len(spawns) == 2, (
        f"nudge spawned {len(spawns)} workers against kanban.max_in_progress=2"
    )
    assert _running_count() == 2


def test_nudge_respects_running_workers_already_at_the_cap(client, configured, spawns):
    """A cap bounds CONCURRENCY, not spawns-per-request.

    With the cap already saturated by live workers the nudge must spawn
    nothing — otherwise every board edit adds N more on top of whatever is
    already running, which is how a cap of 4 reached 8.
    """
    configured(max_in_progress=3)
    _seed_ready(4)
    with kbc.connect_closing() as conn:
        # Three workers already running; claim_lock set so they aren't reclaimed.
        rows = [r[0] for r in conn.execute("SELECT id FROM tasks LIMIT 3")]
        for tid in rows:
            conn.execute(
                "UPDATE tasks SET status='running', claim_lock=?, worker_pid=? WHERE id=?",
                (f"{kb._host_prefix()}1", 1, tid),
            )
        conn.commit()

    r = client.post("/api/plugins/kanban/dispatch?max=8")

    assert r.status_code == 200
    assert spawns == [], f"nudge spawned {spawns} while already at max_in_progress=3"


# ---------------------------------------------------------------------------
# The per-profile cap
# ---------------------------------------------------------------------------


def test_nudge_cannot_exceed_per_profile_cap(client, configured, spawns):
    """All-one-assignee fan-out is bounded by ``max_in_progress_per_profile``.

    The measured breach had all 8 workers on a single profile against a
    per-profile cap of 2, so the host cap alone is not a sufficient contract.
    """
    configured(max_in_progress=99, max_in_progress_per_profile=2)
    _seed_ready(6, assignee="claudeprimary")

    r = client.post("/api/plugins/kanban/dispatch?max=8")

    assert r.status_code == 200
    assert len(spawns) == 2, (
        f"nudge spawned {len(spawns)} for one profile against a per-profile cap of 2"
    )


def test_nudge_respects_per_profile_cap_consumed_on_another_board(client, configured, spawns):
    """A dashboard nudge sees a profile already running on another board."""
    kb.create_board("second")
    _seed_ready(1, assignee="claudeprimary", board="second")
    with kbc.connect_closing(board="second") as conn:
        task_id = conn.execute("SELECT id FROM tasks").fetchone()[0]
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, worker_pid=? WHERE id=?",
            (f"{kb._host_prefix()}1", 1, task_id),
        )
        conn.commit()
    _seed_ready(1, assignee="claudeprimary")
    configured(max_in_progress=2, max_in_progress_per_profile=1)

    response = client.post("/api/plugins/kanban/dispatch?max=8")

    assert response.status_code == 200
    assert spawns == []
    assert response.json()["skipped_per_profile_capped"][0][1:] == ["claudeprimary", 1]


def test_per_profile_cap_is_per_profile_not_global(client, configured, spawns):
    """The per-profile cap must not be misapplied as a second global cap.

    Two profiles at a cap of 2 each may run 4 workers total; a fix that
    clamped globally would pass the breach tests while breaking throughput.
    """
    configured(max_in_progress=99, max_in_progress_per_profile=2)
    _seed_ready(3, assignee="alpha")
    _seed_ready(3, assignee="beta")

    r = client.post("/api/plugins/kanban/dispatch?max=8")

    assert r.status_code == 200
    assert len(spawns) == 4, f"expected 2 per profile across 2 profiles, got {len(spawns)}"


# ---------------------------------------------------------------------------
# The query parameter is a ceiling, never an override
# ---------------------------------------------------------------------------


def test_hand_crafted_max_query_param_cannot_widen_the_cap(client, configured, spawns):
    """``?max=99`` is browser-supplied input, not an authority on host capacity."""
    configured(max_in_progress=2)
    _seed_ready(8)

    r = client.post("/api/plugins/kanban/dispatch?max=99")

    assert r.status_code == 200
    assert len(spawns) == 2, f"?max=99 overrode the configured cap; spawned {len(spawns)}"


def test_max_query_param_still_narrows_below_the_cap(client, configured, spawns):
    """Clamping must keep ``?max=`` effective when it is the *stricter* bound."""
    configured(max_in_progress=10)
    _seed_ready(6)

    r = client.post("/api/plugins/kanban/dispatch?max=1")

    assert r.status_code == 200
    assert len(spawns) == 1, f"?max=1 should bound the tick; spawned {len(spawns)}"


# ---------------------------------------------------------------------------
# The resolver contract shared by every dispatch entry point
# ---------------------------------------------------------------------------


def test_clamp_never_widens_and_honours_the_host_cap():
    """``clamp_requested_max_spawn`` bounds a request by the host cap only."""
    caps = kbd.DispatchCaps(
        max_in_progress=4, max_in_progress_per_profile=2, max_spawn=6, default_assignee=None
    )
    assert kbd.clamp_requested_max_spawn(99, caps) == 4   # host cap wins over request
    assert kbd.clamp_requested_max_spawn(2, caps) == 2    # request wins when stricter
    assert kbd.clamp_requested_max_spawn(None, caps) == 4 # no request -> host cap

    unbounded = kbd.DispatchCaps(None, None, None, None)
    assert kbd.clamp_requested_max_spawn(5, unbounded) == 5
    assert kbd.clamp_requested_max_spawn(None, unbounded) is None


def test_clamp_does_not_fold_in_configured_max_spawn():
    """``max_spawn`` is a separate per-board axis, enforced by ``dispatch_once``.

    Folding it into the clamp would silently tighten the host bound to a
    per-board number and quietly reduce throughput the operator asked for.
    """
    caps = kbd.DispatchCaps(
        max_in_progress=8, max_in_progress_per_profile=None, max_spawn=2, default_assignee=None
    )
    assert kbd.clamp_requested_max_spawn(6, caps) == 6


def test_resolve_dispatch_caps_reads_config(monkeypatch, kanban_home):
    """The shared resolver reads the same keys the gateway tick reads."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"max_in_progress": 4, "max_in_progress_per_profile": 2,
                            "default_assignee": " planner "}},
    )
    caps = kbd.resolve_dispatch_caps()
    assert caps.max_in_progress == 4
    assert caps.max_in_progress_per_profile == 2
    assert caps.default_assignee == "planner"


def test_resolve_dispatch_caps_fails_open_on_broken_config(monkeypatch, kanban_home):
    """A config read error must not wedge dispatch; it degrades to the derived default."""

    def boom():
        raise RuntimeError("unreadable")

    monkeypatch.setattr("hermes_cli.config.load_config", boom)
    caps = kbd.resolve_dispatch_caps()
    # max_in_progress still routes through the memory-derived default.
    assert caps.max_in_progress == kbd.derive_default_max_in_progress()
    assert caps.max_in_progress_per_profile is None
