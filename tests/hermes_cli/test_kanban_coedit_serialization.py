"""Co-edit serialization: two cards that declare the same edit target must not
run concurrently.

The failure this pins: two cards were fanned out concurrently, both touching
``apps/desktop/src/app/chat/right-rail/preview-pane.tsx`` and both needing the
same new helper. The shared decision was written into BOTH card bodies as prose
and it still produced an add/add conflict — neither worker could see the other's
worktree. Prose cannot serialize two agents who cannot see each other; the
board's ``parents=[...]`` edge is the only mechanism that can.

The contract asserted here is a *behaviour* contract, not a snapshot:

* overlapping declared paths  -> the later card gains a dependency edge and waits
* different declared paths    -> both still run concurrently (no false serialization)
* no declared paths at all    -> byte-identical behaviour to a board without the guard
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest


@pytest.fixture()
def kanban_home(monkeypatch):
    """Fresh HERMES_HOME with a kanban DB and two real-looking profiles."""
    test_home = tempfile.mkdtemp(prefix="kanban_coedit_test_")
    for prof in ("alpha", "beta", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import kanban_db
    kanban_db.create_board(slug="default", name="Test")
    yield kanban_db


def _fake_spawn(*_args, **_kwargs):
    return 12345


def _status(conn, task_id: str) -> str:
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()["status"]


def _parents(kb, conn, task_id: str) -> list[str]:
    return kb.parent_ids(conn, task_id)


PANE = "apps/desktop/src/app/chat/right-rail/preview-pane.tsx"


def _body(*paths: str) -> str:
    return (
        "Do the work.\n\n"
        f"Edit-Targets: {', '.join(paths)}\n\n"
        "Import the shared helper, do not define a second one.\n"
    )


def test_overlapping_declared_paths_serialize(kanban_home):
    """The exact incident: two ready cards declaring the same file. Only one
    goes ``running``; the other is parked behind it by a real dependency edge.

    Pre-fix this test fails — both cards go ``running`` concurrently, which is
    precisely the add/add conflict the guard exists to prevent.
    """
    kb = kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    with kbc.connect_closing() as conn:
        first = kb.create_task(conn, title="preview agent tools", assignee="alpha", body=_body(PANE))
        second = kb.create_task(conn, title="preview browser pane", assignee="beta", body=_body(PANE))

    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn)

    with kbc.connect_closing() as conn:
        statuses = {t: _status(conn, t) for t in (first, second)}
        second_parents = _parents(kb, conn, second)

    spawned_ids = [s[0] for s in res.spawned]
    assert spawned_ids == [first], f"only the first card may spawn, got {spawned_ids}"
    assert statuses[first] == "running"
    assert statuses[second] == "todo", (
        "the co-editing card must wait, not run concurrently; "
        f"got status={statuses[second]!r}"
    )
    assert second_parents == [first], (
        "serialization must be expressed as a real dependency edge so the second "
        f"worker starts from a tree containing the first's work; parents={second_parents}"
    )
    assert (second, first, PANE) in res.serialized_coedit


def test_serialized_card_runs_once_the_holder_is_done(kanban_home):
    """Serialization defers, it does not strand: once the holder completes, the
    parked card promotes and dispatches normally on a later tick."""
    kb = kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    with kbc.connect_closing() as conn:
        first = kb.create_task(conn, title="first", assignee="alpha", body=_body(PANE))
        second = kb.create_task(conn, title="second", assignee="beta", body=_body(PANE))
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn)
        kb.complete_task(conn, first, summary="done")

    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn)
        assert _status(conn, second) == "running"
    assert [s[0] for s in res.spawned] == [second]


def test_different_declared_paths_still_run_concurrently(kanban_home):
    """No false serialization: declaring paths must not become a global lock on
    the repo — only the genuinely overlapping pair is serialized."""
    kb = kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    with kbc.connect_closing() as conn:
        a = kb.create_task(conn, title="a", assignee="alpha", body=_body("apps/desktop/src/a.tsx"))
        b = kb.create_task(conn, title="b", assignee="beta", body=_body("apps/desktop/src/b.tsx"))

    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn)
        assert _status(conn, a) == "running"
        assert _status(conn, b) == "running"

    assert sorted(s[0] for s in res.spawned) == sorted([a, b])
    assert res.serialized_coedit == []


def test_undeclared_cards_dispatch_exactly_as_before(kanban_home):
    """Zero behaviour change for the common case: a card with no declared paths
    and no hotspot history is dispatched with no edge added and no deferral."""
    kb = kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    with kbc.connect_closing() as conn:
        a = kb.create_task(conn, title="a", assignee="alpha", body="just some prose, no field")
        b = kb.create_task(conn, title="b", assignee="beta", body=None)

    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn)
        assert _status(conn, a) == "running"
        assert _status(conn, b) == "running"
        assert _parents(kb, conn, a) == []
        assert _parents(kb, conn, b) == []

    assert sorted(s[0] for s in res.spawned) == sorted([a, b])
    assert res.serialized_coedit == []


def test_hotspot_comment_declares_surface_for_a_card_that_never_declared_one(kanban_home):
    """The signal already in the DB is honoured: a card that filed a
    ``hotspot: <path>`` comment on a previous attempt is treated as declaring
    that path, so a later card touching it serializes behind it."""
    kb = kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    with kbc.connect_closing() as conn:
        first = kb.create_task(conn, title="first", assignee="alpha", body="no declared field")
        kb.add_comment(
            conn, first, "claudeprimary",
            f"hotspot: {PANE} — three sibling branches keep colliding here",
        )
        second = kb.create_task(conn, title="second", assignee="beta", body=_body(PANE))

    with kbc.connect_closing() as conn:
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn)
        assert _status(conn, first) == "running"
        assert _status(conn, second) == "todo"
        assert _parents(kb, conn, second) == [first]


def test_a_running_card_holds_the_path_across_ticks(kanban_home):
    """The holder need not be dispatched in the same tick: a card already
    ``running`` from an earlier tick still owns its declared paths."""
    kb = kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    with kbc.connect_closing() as conn:
        first = kb.create_task(conn, title="first", assignee="alpha", body=_body(PANE))
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn)
        assert _status(conn, first) == "running"
        second = kb.create_task(conn, title="second", assignee="beta", body=_body(PANE))

    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn)
        assert _status(conn, second) == "todo"
        assert _parents(kb, conn, second) == [first]
    assert res.spawned == []


def test_reviewer_metadata_hotspot_is_read_back(kanban_home):
    """Reviewers report collisions in completion metadata rather than a comment.
    That signal is already in the DB and must count as a declared surface too."""
    kb = kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    with kbc.connect_closing() as conn:
        first = kb.create_task(conn, title="first", assignee="alpha", body="no declared field")
        # The DB state a finished earlier run leaves behind: a closed run row
        # whose completion metadata names the file that card collided on.
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, "
            "outcome, summary, metadata) VALUES (?, 'alpha', 'completed', 1, 2, "
            "'completed', 'earlier attempt', ?)",
            (first, json.dumps({"hotspot": f"{PANE} — three branches collided here"})),
        )
        conn.commit()
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn)
        second = kb.create_task(conn, title="second", assignee="beta", body=_body(PANE))

    with kbc.connect_closing() as conn:
        kbd.dispatch_once(conn, spawn_fn=_fake_spawn)
        assert _status(conn, first) == "running"
        assert _status(conn, second) == "todo"
        assert _parents(kb, conn, second) == [first]


def test_metadata_hotspot_shapes(kanban_home):
    from hermes_cli import kanban_coedit as kc

    assert kc._metadata_hotspot_paths('{"hotspot": "a/b.ts — reason"}') == ["a/b.ts"]
    assert kc._metadata_hotspot_paths('{"hotspots": ["a/b.ts", "c/d.ts"]}') == ["a/b.ts", "c/d.ts"]
    assert kc._metadata_hotspot_paths('{"summary": "no hotspot key"}') == []
    assert kc._metadata_hotspot_paths("not json") == []
    assert kc._metadata_hotspot_paths(None) == []


def test_two_tenants_naming_the_same_path_are_not_coediting(kanban_home):
    """Tenants are separate workspaces on one board — the same repo-relative
    path in two tenants is two different files, not a collision."""
    kb = kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    with kbc.connect_closing() as conn:
        a = kb.create_task(conn, title="a", assignee="alpha", body=_body(PANE), tenant="acme")
        b = kb.create_task(conn, title="b", assignee="beta", body=_body(PANE), tenant="globex")

    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn)
        assert _status(conn, a) == "running"
        assert _status(conn, b) == "running"
    assert res.serialized_coedit == []


def test_path_normalization_matches_equivalent_spellings(kanban_home):
    """``./a/b.ts``, ``a//b.ts`` and ``a/b.ts`` are the same file — a guard that
    missed that would be trivially defeated by formatting."""
    from hermes_cli import kanban_coedit as kc

    assert kc.normalize_edit_path("./a/b.ts") == kc.normalize_edit_path("a/b.ts")
    assert kc.normalize_edit_path("a//b.ts") == kc.normalize_edit_path("a/b.ts")
    assert kc.normalize_edit_path("`a/b.ts`") == kc.normalize_edit_path("a/b.ts")
    assert kc.normalize_edit_path("a/b.ts") != kc.normalize_edit_path("a/c.ts")


def test_declared_paths_parse_from_both_inline_and_bullet_forms(kanban_home):
    from hermes_cli import kanban_coedit as kc

    inline = "blah\nEdit-Targets: a/b.ts, c/d.tsx\nmore prose\n"
    assert kc.parse_declared_paths(inline) == ["a/b.ts", "c/d.tsx"]

    bullets = "blah\n\nEdit targets:\n- a/b.ts\n- `c/d.tsx`\n\nnext section\n"
    assert kc.parse_declared_paths(bullets) == ["a/b.ts", "c/d.tsx"]

    assert kc.parse_declared_paths("no field here") == []
    assert kc.parse_declared_paths(None) == []


def test_hotspot_comment_parsing_keeps_hyphenated_paths_intact(kanban_home):
    """``hotspot: <path> — <reason>`` must not lose a hyphenated directory name
    to a naive split on ``-``."""
    from hermes_cli import kanban_coedit as kc

    assert kc.parse_hotspot_paths(
        "hotspot: apps/desktop/src/right-rail/preview-pane.tsx — everyone edits it"
    ) == ["apps/desktop/src/right-rail/preview-pane.tsx"]
    assert kc.parse_hotspot_paths("hotspot: a/b-c.ts -- reason") == ["a/b-c.ts"]
    assert kc.parse_hotspot_paths("hotspot: a/b-c.ts") == ["a/b-c.ts"]
    assert kc.parse_hotspot_paths("not a hotspot line") == []
