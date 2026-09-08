"""Profile-scoped preflight of a Kanban card's forced skills.

A card created with ``skills=[...]`` spawns ``hermes -p <assignee> ... --skills X``.
When X does not exist in THAT profile's isolated home, the worker dies during
initialization with ``Unknown skill(s): X``, which the dispatcher scores as a
crash: retry budget burned, start budget burned, failure breaker tripped, and
zero implementation work done (card t_d4c6a3a5).

These tests pin the contract: the skill set is resolved against the ASSIGNEE's
profile home, never the creating process's, and a mismatch is a configuration
error raised before any row is written or any worker is spawned.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


def _write_skill(root: Path, rel: str, *, name: str | None = None) -> Path:
    """Create ``<root>/<rel>/SKILL.md`` with frontmatter; returns the SKILL.md."""
    skill_dir = root / rel
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        textwrap.dedent(
            f"""\
            ---
            name: {name or Path(rel).name}
            description: "Test skill {name or rel}."
            ---

            # {name or rel}
            """
        ),
        encoding="utf-8",
    )
    return skill_md


@pytest.fixture()
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME whose profiles root holds the profiles we create.

    ``_get_default_hermes_root()`` returns HERMES_HOME itself for a custom root,
    so ``profiles/<name>`` under it is what ``get_profile_dir`` resolves.
    """
    home = tmp_path / "hermes-home"
    (home / "profiles").mkdir(parents=True)
    (home / "skills").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)
    return home


def _make_profile(home: Path, name: str, skills: "tuple[str, ...] | list[str]" = ()) -> Path:
    profile_dir = home / "profiles" / name
    skills_dir = profile_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    for rel in skills:
        _write_skill(skills_dir, rel)
    return profile_dir


def test_create_task_rejects_skill_missing_from_the_assignee_profile(kanban_home):
    """The exact t_d4c6a3a5 shape: skill present in the CREATING profile's home,
    absent from the assignee's. Creation must fail before the row is written."""
    from hermes_cli import kanban_db, kanban_db_connect

    # The creating/orchestrating profile has the skill...
    _write_skill(kanban_home / "skills", "hermes-kanban-fleet")
    # ...the assignee profile does not.
    _make_profile(kanban_home, "claudecode", ["github-code-review"])

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        with pytest.raises(ValueError) as excinfo:
            kanban_db.create_task(
                conn, title="fleet card", assignee="claudecode",
                skills=["hermes-kanban-fleet"],
            )
        message = str(excinfo.value)
        assert "hermes-kanban-fleet" in message
        assert "claudecode" in message
        # No row written: the card must not exist to be dispatched later.
        assert kanban_db.list_tasks(conn) == []


def test_create_task_accepts_a_skill_installed_in_the_assignee_profile(kanban_home):
    """The available-skill path stays green — preflight must not block real work."""
    from hermes_cli import kanban_db, kanban_db_connect

    _make_profile(kanban_home, "claudecode", ["github-code-review"])

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        task_id = kanban_db.create_task(
            conn, title="review card", assignee="claudecode",
            skills=["github-code-review"],
        )
        assert kanban_db.get_task(conn, task_id).skills == ["github-code-review"]


def test_assign_task_rejects_a_profile_that_cannot_load_the_cards_skills(kanban_home):
    """Reassignment re-runs the check against the NEW profile: the same card is
    fine for one profile and a guaranteed init crash for another."""
    from hermes_cli import kanban_db, kanban_db_connect

    _make_profile(kanban_home, "claudecode", ["github-code-review"])
    _make_profile(kanban_home, "reviewer", [])

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        task_id = kanban_db.create_task(
            conn, title="review card", assignee="claudecode",
            skills=["github-code-review"],
        )
        with pytest.raises(ValueError) as excinfo:
            kanban_db.assign_task(conn, task_id, "reviewer")
        assert "github-code-review" in str(excinfo.value)
        assert "reviewer" in str(excinfo.value)
        # The card keeps its original, working assignment.
        assert kanban_db.get_task(conn, task_id).assignee == "claudecode"


def test_assign_task_accepts_a_profile_that_has_the_skill(kanban_home):
    from hermes_cli import kanban_db, kanban_db_connect

    _make_profile(kanban_home, "claudecode", ["github-code-review"])
    _make_profile(kanban_home, "reviewer", ["github-code-review"])

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        task_id = kanban_db.create_task(
            conn, title="review card", assignee="claudecode",
            skills=["github-code-review"],
        )
        assert kanban_db.assign_task(conn, task_id, "reviewer") is True
        assert kanban_db.get_task(conn, task_id).assignee == "reviewer"


def _legacy_card_with_unloadable_skill(kb, conn, *, assignee: str, skill: str) -> str:
    """A card carrying a skill its assignee cannot load, written the way an
    imported/legacy board row arrives: straight into the table, bypassing
    ``create_task``'s validation."""
    import json

    task_id = kb.create_task(conn, title="legacy card", assignee=assignee)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET skills = ? WHERE id = ?", (json.dumps([skill]), task_id),
        )
    return task_id


def test_dispatcher_refuses_to_spawn_a_card_whose_skill_the_profile_cannot_load(kanban_home):
    """The defensive half: a row that never passed create-time validation must
    not become a worker that dies during initialization. Zero spawns, and
    neither the retry counter nor the start budget is charged."""
    from hermes_cli import kanban_db, kanban_db_connect, kanban_db_dispatch

    _make_profile(kanban_home, "claudecode", ["github-code-review"])

    spawn_calls = []

    def _spawn(task, workspace, board=None):
        spawn_calls.append(task.id)
        return 4242

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        task_id = _legacy_card_with_unloadable_skill(
            kb=kanban_db, conn=conn, assignee="claudecode", skill="hermes-kanban-fleet",
        )

    with kanban_db_connect.connect_closing() as conn:
        result = kanban_db_dispatch.dispatch_once(conn, spawn_fn=_spawn)

    assert spawn_calls == []
    assert result.spawned == []
    assert task_id in [tid for tid, _reason in result.skill_preflight_blocked]

    with kanban_db_connect.connect_closing() as conn:
        task = kanban_db.get_task(conn, task_id)
        assert task.status == "blocked"
        assert task.consecutive_failures == 0
        events = kanban_db.list_events(conn, task_id)
        # Exactly one diagnostic, and no spawn event (so no start-budget charge).
        blocks = [e for e in events if e.kind == "blocked"]
        assert len(blocks) == 1
        assert blocks[0].payload["code"] == "kanban_skill_missing"
        assert "hermes-kanban-fleet" in blocks[0].payload["reason"]
        assert [e for e in events if e.kind == "spawned"] == []


def test_dispatcher_preflight_block_is_emitted_once_not_every_tick(kanban_home):
    """The card leaves the dispatchable lane, so a second tick neither re-blocks
    it nor re-charges anything — the crash-loop this replaces did both."""
    from hermes_cli import kanban_db, kanban_db_connect, kanban_db_dispatch

    _make_profile(kanban_home, "claudecode", ["github-code-review"])

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        task_id = _legacy_card_with_unloadable_skill(
            kb=kanban_db, conn=conn, assignee="claudecode", skill="hermes-kanban-fleet",
        )
    for _ in range(3):
        with kanban_db_connect.connect_closing() as conn:
            kanban_db_dispatch.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)

    with kanban_db_connect.connect_closing() as conn:
        events = kanban_db.list_events(conn, task_id)
        assert len([e for e in events if e.kind == "blocked"]) == 1
        assert kanban_db.get_task(conn, task_id).consecutive_failures == 0


def test_dispatcher_spawns_normally_once_the_skill_is_installed(kanban_home):
    """Correcting the configuration is enough: the card dispatches with a clean
    counter, no leftover failure state from the preflight refusal."""
    from hermes_cli import kanban_db, kanban_db_connect, kanban_db_dispatch

    profile_dir = _make_profile(kanban_home, "claudecode", ["github-code-review"])

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        task_id = _legacy_card_with_unloadable_skill(
            kb=kanban_db, conn=conn, assignee="claudecode", skill="hermes-kanban-fleet",
        )
    with kanban_db_connect.connect_closing() as conn:
        kanban_db_dispatch.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)

    # Operator installs the skill for that profile and unblocks the card.
    _write_skill(profile_dir / "skills", "hermes-kanban-fleet")
    with kanban_db_connect.connect_closing() as conn:
        kanban_db.unblock_task(conn, task_id)

    spawned = []
    with kanban_db_connect.connect_closing() as conn:
        result = kanban_db_dispatch.dispatch_once(
            conn, spawn_fn=lambda task, *a, **k: (spawned.append(task.id), 4242)[1],
        )
    assert spawned == [task_id]
    assert [tid for tid, _a, _w in result.spawned] == [task_id]


def test_dispatcher_preflight_does_not_consume_the_board_start_budget(kanban_home):
    """A refusal must leave the sliding start-budget window untouched, so an
    unloadable card cannot rate-limit the whole board (three crash-restarts of
    t_d4c6a3a5 did exactly that)."""
    from hermes_cli import kanban_db, kanban_db_connect, kanban_db_dispatch

    _make_profile(kanban_home, "claudecode", ["github-code-review"])

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        bad_id = _legacy_card_with_unloadable_skill(
            kb=kanban_db, conn=conn, assignee="claudecode", skill="hermes-kanban-fleet",
        )
        good_id = kanban_db.create_task(
            conn, title="ok card", assignee="claudecode", skills=["github-code-review"],
        )
    with kanban_db_connect.connect_closing() as conn:
        result = kanban_db_dispatch.dispatch_once(
            conn, spawn_fn=lambda *a, **k: 4242,
            dispatch_start_budget=1, dispatch_start_window_seconds=600,
        )
        # The refused card consumed no slot, so the healthy card still spawns
        # within a budget of one.
        assert [tid for tid, _a, _w in result.spawned] == [good_id]
        assert bad_id in [tid for tid, _r in result.skill_preflight_blocked]
        assert kanban_db_dispatch._recent_dispatch_starts(conn, window_seconds=600) == 1


def test_preflight_is_scoped_to_the_assignee_profile_not_the_calling_one(kanban_home):
    """Profile isolation: the skill registry consulted is the assignee's own.
    Never fall back to the caller's or the default profile's."""
    from hermes_cli.kanban_skill_preflight import available_skill_identifiers

    _write_skill(kanban_home / "skills", "only-in-default")
    _make_profile(kanban_home, "alpha", ["only-in-alpha"])
    _make_profile(kanban_home, "beta", ["only-in-beta"])

    alpha = available_skill_identifiers("alpha")
    beta = available_skill_identifiers("beta")

    assert "only-in-alpha" in alpha
    assert "only-in-beta" not in alpha
    assert "only-in-default" not in alpha
    assert "only-in-beta" in beta
    assert "only-in-alpha" not in beta


def test_multiple_missing_skills_are_all_named_in_one_error(kanban_home):
    """One actionable error, not a fix-one-rerun-find-the-next loop."""
    from hermes_cli import kanban_db, kanban_db_connect
    from hermes_cli.kanban_skill_preflight import KanbanSkillPreflightError

    _make_profile(kanban_home, "claudecode", ["github-code-review"])

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        with pytest.raises(KanbanSkillPreflightError) as excinfo:
            kanban_db.create_task(
                conn, title="card", assignee="claudecode",
                skills=["github-code-review", "missing-one", "missing-two"],
            )
    error = excinfo.value
    assert error.missing == ("missing-one", "missing-two")
    assert "missing-one" in str(error) and "missing-two" in str(error)
    # The installed one is not reported as a problem.
    assert "github-code-review" not in str(error).split("cannot load forced skills:")[1].split(".")[0]


def test_error_explains_how_to_inspect_install_or_remove_the_skill(kanban_home):
    from hermes_cli import kanban_db, kanban_db_connect
    from hermes_cli.kanban_skill_preflight import MISSING_CODE, KanbanSkillPreflightError

    _make_profile(kanban_home, "claudecode", [])

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        with pytest.raises(KanbanSkillPreflightError) as excinfo:
            kanban_db.create_task(
                conn, title="card", assignee="claudecode", skills=["hermes-kanban-fleet"],
            )
    error = excinfo.value
    assert error.code == MISSING_CODE
    assert error.profile == "claudecode"
    message = str(error)
    assert "hermes -p claudecode skills list" in message
    assert "remove" in message.lower()


def test_disabled_skill_counts_as_unavailable(kanban_home):
    """``hermes --skills X`` bypasses the scan-time disabled filter and treats a
    disabled skill as MISSING (build_preloaded_skills_prompt, #59156), so the
    worker would still die at init. Preflight must agree with that behavior."""
    from hermes_cli import kanban_db, kanban_db_connect
    from hermes_cli.kanban_skill_preflight import KanbanSkillPreflightError

    profile_dir = _make_profile(kanban_home, "claudecode", ["github-code-review"])
    (profile_dir / "config.yaml").write_text(
        "skills:\n  disabled:\n    - github-code-review\n", encoding="utf-8",
    )

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        with pytest.raises(KanbanSkillPreflightError) as excinfo:
            kanban_db.create_task(
                conn, title="card", assignee="claudecode", skills=["github-code-review"],
            )
    assert excinfo.value.missing == ("github-code-review",)


def test_unavailable_profile_registry_fails_closed_with_a_distinct_code(kanban_home):
    """A profile that cannot be authoritatively inspected must NOT be assumed to
    have the skill, and its diagnostic must be distinguishable from a plain
    missing skill so an operator can tell "wrong profile" from "install this"."""
    from hermes_cli import kanban_db, kanban_db_connect
    from hermes_cli.kanban_skill_preflight import (
        MISSING_CODE, PROFILE_UNAVAILABLE_CODE, KanbanSkillPreflightError,
    )

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        with pytest.raises(KanbanSkillPreflightError) as excinfo:
            kanban_db.create_task(
                conn, title="card", assignee="never-created",
                skills=["github-code-review"],
            )
    error = excinfo.value
    assert error.code == PROFILE_UNAVAILABLE_CODE
    assert error.code != MISSING_CODE
    assert "never-created" in str(error)


def test_categorized_and_frontmatter_names_both_resolve(kanban_home):
    """A card may name a skill the way skill_view accepts it: bare directory
    name, categorized path, or frontmatter name. None of those may be reported
    missing when the skill is genuinely installed."""
    from hermes_cli.kanban_skill_preflight import missing_skills_for_profile

    profile_dir = _make_profile(kanban_home, "claudecode", [])
    _write_skill(profile_dir / "skills", "mlops/axolotl", name="axolotl-trainer")

    assert missing_skills_for_profile(
        "claudecode", ["axolotl", "mlops/axolotl", "axolotl-trainer", "mlops:axolotl"],
    ) == []


def test_a_card_without_forced_skills_is_never_rejected(kanban_home):
    """The overwhelmingly common card shape must be untouched by preflight,
    including when the assignee profile has no skills dir at all."""
    from hermes_cli import kanban_db, kanban_db_connect

    (kanban_home / "profiles" / "bare").mkdir()

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        assert kanban_db.create_task(conn, title="plain", assignee="bare")
        assert kanban_db.create_task(conn, title="empty skills", assignee="bare", skills=[])
        # Unassigned: no profile to validate against; the dispatcher re-checks.
        assert kanban_db.create_task(conn, title="unassigned", skills=["anything"])


def test_kanban_create_tool_returns_a_structured_error_not_a_traceback(kanban_home, monkeypatch):
    """The agent-facing surface must report the same refusal as a clean
    ``tool_error`` payload naming the profile and the missing skill."""
    import json

    from hermes_cli import kanban_db, kanban_db_connect

    _make_profile(kanban_home, "claudecode", [])
    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")

    monkeypatch.setenv("HERMES_KANBAN_MODE", "1")
    from tools import kanban_tools

    payload = json.loads(kanban_tools._handle_create({
        "title": "fleet card", "assignee": "claudecode", "skills": ["hermes-kanban-fleet"],
    }))
    assert "error" in payload
    assert "hermes-kanban-fleet" in payload["error"]
    assert "claudecode" in payload["error"]

    with kanban_db_connect.connect_closing() as conn:
        assert kanban_db.list_tasks(conn) == []


def test_the_dispatcher_injected_review_skill_is_not_preflighted(kanban_home):
    """``sdlc-review`` is force-loaded by the dispatcher, not authored on the
    card. Its absence is a Hermes install problem, so blocking review cards for
    it would stall the whole lane on a board whose profiles never installed the
    bundled skill — preflight covers card-authored skills only."""
    from hermes_cli.kanban_skill_preflight import REVIEW_LANE_SKILL, preflight_task_skills

    _make_profile(kanban_home, "reviewer", [])
    # No raise: the card itself declares no skills.
    preflight_task_skills("reviewer", [])
    # Named explicitly ON the card, it IS checked — the exemption is about who
    # injected it, not about the name.
    with pytest.raises(ValueError):
        preflight_task_skills("reviewer", [REVIEW_LANE_SKILL])





