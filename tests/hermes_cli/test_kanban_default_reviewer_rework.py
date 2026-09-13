"""Regression tests for 3 defects found in an independent review of
``kanban.default_reviewer`` (review card t_95d1c554, rework card t_6ee72d07).

B1 (blocker): the auto-assigned reviewer's worker inherited the implementer's
pinned model/provider because ``_apply_default_reviewer`` reassigned the row
without clearing ``model_override``/``provider_override`` the way
``kanban_db.request_review`` does for an explicit cross-profile handoff.

B2 (blocker): the reassignment gate was a bare ``default_reviewer !=
row_assignee`` inequality, which can't tell "still the implementer, never
routed" apart from "explicitly routed to a real reviewer that isn't the
config's pick" — so it (a) overrode a worker's own
``kanban_request_review(reviewer=...)`` choice, and (b) hijacked a re-review
that ``_prior_reviewer`` provenance had correctly routed back to the same
reviewer profile.

B3 (should-fix): ``_resolve_default_reviewer`` copied
``_resolve_default_assignee``'s fail-open ("profiles module unimportable ->
trust the operator's config"), but that is only safe when the value fills a
BLANK assignee. Here it overwrites a REAL one, and the downstream
``profile_exists`` safety net the docstring pointed to is unavailable for the
identical reason (``_profile_exists_fn`` also returns ``None``) — so a
typo'd config value would replace the implementer's name and spawn a
nonspawnable profile.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with real profile dirs for every profile name
    used below, so ``profile_exists()`` resolves them as real/spawnable."""
    home = tmp_path / ".hermes"
    home.mkdir()
    for name in ("claudeprimary", "default", "codexreview"):
        os.makedirs(home / "profiles" / name, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_spawn(*args, **kwargs):
    return 12345


def _events(conn, tid, kind=None):
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
        (tid,),
    ).fetchall()
    out = [
        (r["kind"], json.loads(r["payload"]) if r["payload"] else None)
        for r in rows
    ]
    if kind is not None:
        out = [e for e in out if e[0] == kind]
    return out


def _make_review_task(
    conn, *, implementer: str = "claudeprimary", reviewer: str | None = None,
    model_override: str | None = None, provider_override: str | None = None,
) -> tuple[str, int]:
    """A task carried through running -> review via ``request_review``."""
    tid = kb.create_task(
        conn, title="impl a feature", assignee=implementer,
        model_override=model_override, provider_override=provider_override,
    )
    kb.claim_task(conn, tid)
    run_id = kb.get_task(conn, tid).current_run_id
    ok = kb.request_review(
        conn, tid, summary="done", expected_run_id=run_id, reviewer=reviewer,
    )
    assert ok is True
    return tid, run_id


def _spawn_and_capture(monkeypatch, tmp_path, task):
    """Build the real worker argv via ``_worker_argv`` (B1's acceptance
    criterion requires asserting on the argv, not just DB columns) —
    mirrors ``test_kanban_review_model_override.py``'s helper."""
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4245

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    kbd._default_spawn(task, str(workspace))
    return captured["cmd"]


def _model_flags(cmd: list[str]) -> tuple[str | None, str | None]:
    model = cmd[cmd.index("-m") + 1] if "-m" in cmd else None
    provider = cmd[cmd.index("--provider") + 1] if "--provider" in cmd else None
    return model, provider


# ---------------------------------------------------------------------------
# B1: auto-assigned reviewer must not inherit the implementer's model pin
# ---------------------------------------------------------------------------


def test_auto_assigned_reviewer_does_not_inherit_implementer_model(
    kanban_home: Path, monkeypatch, tmp_path,
) -> None:
    with kbc.connect() as conn:
        tid, _ = _make_review_task(
            conn, implementer="claudeprimary",
            model_override="claude-opus-5", provider_override="anthropic",
        )

    with kbc.connect() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_reviewer="default",
        )
    assert res.auto_assigned_reviewer == [(tid, "claudeprimary", "default")]

    with kbc.connect() as conn:
        t = kb.get_task(conn, tid)
        assert t.assignee == "default"
        # DB columns cleared for the reassigned reviewer, same as an
        # explicit request_review(reviewer=...) cross-profile handoff.
        assert t.model_override is None
        assert t.provider_override is None

        row = conn.execute(
            "SELECT status, assignee, current_run_id FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["status"] == "running"
        assert row["assignee"] == "default"
        run_id = row["current_run_id"]

        # The real worker argv must not carry the implementer's pin.
        reclaimed = kb.get_task(conn, tid)
        cmd = _spawn_and_capture(monkeypatch, tmp_path, reclaimed)
        model, provider = _model_flags(cmd)
        assert model is None, "auto-assigned reviewer must run its own profile's model"
        assert provider is None


def test_request_changes_restores_implementer_override_after_auto_assign(
    kanban_home: Path, monkeypatch, tmp_path,
) -> None:
    """The round trip back to the implementer after an auto-assigned review
    must restore the pin the dispatcher snapshotted, exactly like the
    explicit-reviewer cross-profile path in
    ``test_kanban_review_model_override.py``."""
    with kbc.connect() as conn:
        tid, _ = _make_review_task(
            conn, implementer="claudeprimary",
            model_override="claude-opus-5", provider_override="anthropic",
        )

    with kbc.connect() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_reviewer="default",
        )
    assert res.auto_assigned_reviewer == [(tid, "claudeprimary", "default")]

    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        run_id = row["current_run_id"]
        ok, implementer = kb.request_changes(
            conn, tid, reason="needs more tests", expected_run_id=run_id,
        )
        assert ok is True
        assert implementer == "claudeprimary"

        t = kb.get_task(conn, tid)
        assert t.assignee == "claudeprimary"
        assert t.model_override == "claude-opus-5", "implementer's pin must be restored"
        assert t.provider_override == "anthropic"

        reclaimed = kb.claim_task(conn, tid, claimer="claudeprimary:retry")
        assert reclaimed is not None
        cmd = _spawn_and_capture(monkeypatch, tmp_path, reclaimed)
        model, provider = _model_flags(cmd)
        assert model == "claude-opus-5"
        assert provider == "anthropic"


# ---------------------------------------------------------------------------
# B2(a): an explicitly-named reviewer must not be overridden on the next tick
# ---------------------------------------------------------------------------


def test_explicit_reviewer_survives_next_tick(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid, _ = _make_review_task(
            conn, implementer="claudeprimary", reviewer="codexreview",
        )
        row = conn.execute(
            "SELECT status, assignee FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["status"] == "review"
        assert row["assignee"] == "codexreview"

    with kbc.connect() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_reviewer="default",
        )
    # Must NOT be reassigned — an explicit reviewer= choice always wins.
    assert res.auto_assigned_reviewer == []

    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["assignee"] == "codexreview"


def test_later_explicit_reassignment_survives_default_reviewer(kanban_home: Path) -> None:
    """A reviewer-less request may be intentionally reassigned before dispatch.

    The nullable ``review_requested.reviewer`` field records only the original
    request. It is not proof that the row remains implementer-owned after an
    operator or dashboard explicitly changes its assignee.
    """
    with kbc.connect() as conn:
        tid, _ = _make_review_task(conn, implementer="claudeprimary")
        assert kb.assign_task(conn, tid, "codexreview") is True
        row = conn.execute(
            "SELECT status, assignee FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["status"] == "review"
        assert row["assignee"] == "codexreview"

    with kbc.connect() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_reviewer="default",
        )

    assert res.auto_assigned_reviewer == []
    assert any(s[0] == tid and s[1] == "codexreview" for s in res.spawned)
    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["assignee"] == "codexreview"


# ---------------------------------------------------------------------------
# B2(b): re-review provenance (_prior_reviewer) must not be hijacked
# ---------------------------------------------------------------------------


def test_rereview_provenance_survives_default_reviewer(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid, _ = _make_review_task(
            conn, implementer="claudeprimary", reviewer="codexreview",
        )
        review_run = kb.claim_review_task(conn, tid)
        assert review_run is not None
        ok, implementer = kb.request_changes(
            conn, tid, reason="needs work", expected_run_id=review_run.current_run_id,
        )
        assert ok is True
        assert implementer == "claudeprimary"

        # Implementer re-requests review with NO explicit reviewer= --
        # _prior_reviewer routes it back to codexreview.
        kb.claim_task(conn, tid, claimer="claudeprimary:retry")
        run_id2 = kb.get_task(conn, tid).current_run_id
        ok2 = kb.request_review(conn, tid, summary="v2", expected_run_id=run_id2)
        assert ok2 is True
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["assignee"] == "codexreview"

    with kbc.connect() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_reviewer="default",
        )
    # Must NOT be stolen by the config value.
    assert res.auto_assigned_reviewer == []

    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["assignee"] == "codexreview"


# ---------------------------------------------------------------------------
# B3: profiles-module-unimportable must fail CLOSED (never overwrite a real
# assignee with an unverified name)
# ---------------------------------------------------------------------------


def test_unimportable_profiles_module_does_not_overwrite_assignee(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with kbc.connect() as conn:
        tid, _ = _make_review_task(conn, implementer="claudeprimary")

    saved = sys.modules.get("hermes_cli.profiles")
    sys.modules["hermes_cli.profiles"] = None  # type: ignore
    try:
        with kbc.connect() as conn:
            res = kbd.dispatch_once(
                conn, spawn_fn=_fake_spawn, dry_run=False,
                default_reviewer="typoed-reviewer-profile",
            )
    finally:
        if saved is not None:
            sys.modules["hermes_cli.profiles"] = saved
        else:
            sys.modules.pop("hermes_cli.profiles", None)

    # Never reassigned, never spawned under a nonspawnable name.
    assert res.auto_assigned_reviewer == []
    assert res.skipped_nonspawnable == []
    assert any(s[0] == tid and s[1] == "claudeprimary" for s in res.spawned)

    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT status, assignee FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["assignee"] == "claudeprimary"
        assert _events(conn, tid, kind="assigned") == []
