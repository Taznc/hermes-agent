"""Regression tests for ``kanban.default_reviewer`` (review cards must never
self-assign back to the implementer).

Auto-review (``kanban.review_dispatch: true``) spawns the card's EXISTING
assignee for the review lane. When that assignee is the profile that just
finished the implementation, the worker finds nothing left to do, exits
cleanly (rc=0), and the dispatcher scores it a ``protocol_violation`` —
after ``failure_limit`` the card parks in ``review`` waiting on a human.

``kanban.default_reviewer`` lets an operator name a profile that claims a
review-lane card still owned by its implementer. Contracts pinned here:

* set + different assignee -> the row is reassigned and dispatched under the
  reviewer.
* set + same assignee as the reviewer -> no-op, behaves exactly like unset.
* unset (default "") -> byte-identical to today: the review row dispatches
  under its own assignee (upgrade-safety requirement).
* a ``default_reviewer`` naming a nonexistent profile -> falls back to the
  card's own assignee; never crashes the tick, never strands the card.
* ``request_changes`` -> re-review still routes to the reviewer that was
  auto-assigned (no "no durable reviewer provenance" regression).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB and two real profile dirs
    (``claudeprimary`` the implementer, ``default`` the built-in profile) so
    ``profile_exists()`` — which the dispatcher's reviewer-routing consults —
    resolves them as real, spawnable profiles."""
    home = tmp_path / ".hermes"
    home.mkdir()
    os.makedirs(home / "profiles" / "claudeprimary", exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_spawn(*args, **kwargs):
    """Stand-in for the real worker spawn — returns a fake PID."""
    return 12345


def _row(conn, tid):
    return conn.execute(
        "SELECT status, assignee FROM tasks WHERE id = ?", (tid,),
    ).fetchone()


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
    conn, *, implementer: str = "claudeprimary", reasoning_effort: str | None = None,
) -> tuple[str, int]:
    """A task carried through running -> review via ``request_review``,
    still assigned to its implementer (auto-review's starting state)."""
    tid = kb.create_task(
        conn, title="impl a feature", assignee=implementer, reasoning_effort=reasoning_effort,
    )
    kb.claim_task(conn, tid)
    run_id = kb.get_task(conn, tid).current_run_id
    ok = kb.request_review(conn, tid, summary="done", expected_run_id=run_id)
    assert ok is True
    row = _row(conn, tid)
    assert row["status"] == "review"
    assert row["assignee"] == implementer
    return tid, run_id


# ---------------------------------------------------------------------------
# set + different assignee: reassign and dispatch under the reviewer
# ---------------------------------------------------------------------------


def test_default_reviewer_reassigns_when_different_from_implementer(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid, _ = _make_review_task(conn, implementer="claudeprimary", reasoning_effort="ultra")

    with kbc.connect() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_reviewer="default",
        )

    assert res.auto_assigned_reviewer == [(tid, "claudeprimary", "default")]
    assert len(res.spawned) == 1
    assert res.spawned[0][0] == tid
    assert res.spawned[0][1] == "default"

    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT assignee, reasoning_effort FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["assignee"] == "default"
        assert row["reasoning_effort"] is None

        # Audit trail: an 'assigned' event with implementer->reviewer provenance.
        evs = _events(conn, tid, kind="assigned")
        assert len(evs) == 1
        assert evs[0][1]["assignee"] == "default"
        assert evs[0][1]["previous_assignee"] == "claudeprimary"
        assert evs[0][1]["source"] == "kanban.default_reviewer"
        assert evs[0][1]["implementer_reasoning_effort"] == "ultra"


# ---------------------------------------------------------------------------
# set + same assignee: no-op, same as unset
# ---------------------------------------------------------------------------


def test_default_reviewer_noop_when_same_as_assignee(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid, _ = _make_review_task(conn, implementer="default")

    with kbc.connect() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_reviewer="default",
        )

    assert res.auto_assigned_reviewer == []
    assert any(s[0] == tid and s[1] == "default" for s in res.spawned)

    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["assignee"] == "default"
        # No spurious reassignment event.
        assert _events(conn, tid, kind="assigned") == []


# ---------------------------------------------------------------------------
# unset: byte-identical to today's behavior
# ---------------------------------------------------------------------------


def test_default_reviewer_unset_dispatches_own_assignee(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid, _ = _make_review_task(conn, implementer="claudeprimary")

    with kbc.connect() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_reviewer=None,
        )

    assert res.auto_assigned_reviewer == []
    assert any(s[0] == tid and s[1] == "claudeprimary" for s in res.spawned)

    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["assignee"] == "claudeprimary"
        assert _events(conn, tid, kind="assigned") == []


def test_default_reviewer_blank_string_matches_unset(kanban_home: Path) -> None:
    """The config schema default is "" — must behave exactly like None."""
    with kbc.connect() as conn:
        tid, _ = _make_review_task(conn, implementer="claudeprimary")

    with kbc.connect() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_reviewer="",
        )

    assert res.auto_assigned_reviewer == []
    assert any(s[0] == tid and s[1] == "claudeprimary" for s in res.spawned)


# ---------------------------------------------------------------------------
# missing profile: falls back to the card's own assignee, never crashes
# ---------------------------------------------------------------------------


def test_default_reviewer_missing_profile_falls_back_to_assignee(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid, _ = _make_review_task(conn, implementer="claudeprimary")

    with kbc.connect() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_reviewer="definitely-not-an-installed-profile",
        )

    # Falls back to the card's own assignee — not stranded, not crashed.
    assert res.auto_assigned_reviewer == []
    assert any(s[0] == tid and s[1] == "claudeprimary" for s in res.spawned)

    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["assignee"] == "claudeprimary"
        assert _events(conn, tid, kind="assigned") == []


# ---------------------------------------------------------------------------
# request_changes -> re-review provenance survives an auto-assigned reviewer
# ---------------------------------------------------------------------------


def test_auto_assigned_reviewer_survives_request_changes_round_trip(kanban_home: Path) -> None:
    """A reviewer auto-assigned by kanban.default_reviewer must still be a
    durable reviewer for the changes_requested -> re-review provenance
    check (kanban_db._prior_reviewer) — no "re-review has no durable
    reviewer provenance" regression."""
    with kbc.connect() as conn:
        tid, _ = _make_review_task(conn, implementer="claudeprimary", reasoning_effort="ultra")

    with kbc.connect() as conn:
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_reviewer="default",
        )
    assert res.auto_assigned_reviewer == [(tid, "claudeprimary", "default")]

    with kbc.connect() as conn:
        # dispatch_once's own lane task already claimed review -> running
        # under the reassigned reviewer (mirrors the real flow: the
        # dispatcher reassigns, then immediately claims + spawns the review
        # worker in the same tick).
        row = conn.execute(
            "SELECT status, assignee, current_run_id, reasoning_effort FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["status"] == "running"
        assert row["assignee"] == "default"
        assert row["reasoning_effort"] is None
        run_id = row["current_run_id"]
        assert run_id is not None

        ok, implementer = kb.request_changes(
            conn, tid, reason="needs more tests", expected_run_id=run_id,
        )
        assert ok is True
        assert implementer == "claudeprimary"

        row = conn.execute(
            "SELECT status, assignee, reasoning_effort FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["assignee"] == "claudeprimary"
        assert row["reasoning_effort"] == "ultra"

        # Implementer does another pass and re-requests review WITHOUT
        # explicitly naming a reviewer — request_review must fall back to
        # the auto-assigned reviewer's provenance from changes_requested,
        # not fail with "no durable reviewer provenance".
        kb.claim_task(conn, tid)
        run_id2 = kb.get_task(conn, tid).current_run_id
        ok2 = kb.request_review(conn, tid, summary="addressed feedback", expected_run_id=run_id2)
        assert ok2 is True

        row2 = conn.execute(
            "SELECT status, assignee, reasoning_effort FROM tasks WHERE id = ?", (tid,),
        ).fetchone()
        assert row2["status"] == "review"
        assert row2["assignee"] == "default"
        assert row2["reasoning_effort"] is None
