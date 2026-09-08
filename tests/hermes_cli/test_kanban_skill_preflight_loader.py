"""Batch A: the preflight must agree with the worker's real skill loader, and
fail closed when the assignee profile cannot be inspected at all.

Appended to the existing preflight suite once RED is proven.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from tests.hermes_cli.test_kanban_skill_preflight import (  # noqa: F401
    _make_profile, _write_skill, kanban_home,
)


def _foreign_platform() -> str:
    """A ``platforms:`` token that is NOT this host (never fake the host OS)."""
    for token in ("windows", "macos", "linux"):
        if not sys.platform.startswith({"windows": "win", "macos": "darwin"}.get(token, token)):
            return token
    raise AssertionError("no foreign platform token")


def _write_platform_skill(root: Path, rel: str, platforms: str) -> None:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        textwrap.dedent(
            f"""\
            ---
            name: {Path(rel).name}
            description: "Platform-gated test skill."
            platforms: [{platforms}]
            ---

            # {rel}
            """
        ),
        encoding="utf-8",
    )


def test_a_missing_plugin_qualified_skill_is_reported_missing(kanban_home):
    """``plugin:skill`` was skipped as unverifiable, so a card naming a plugin
    skill no profile provides sailed through preflight and died at init. The
    real loader answers this: an unresolvable namespace is missing."""
    from hermes_cli.kanban_skill_preflight import missing_skills_for_profile

    _make_profile(kanban_home, "claudecode", ["github-code-review"])

    assert missing_skills_for_profile("claudecode", ["noplugin:nothing"]) == ["noplugin:nothing"]


def test_a_missing_category_qualified_skill_is_reported_missing(kanban_home):
    """``category:skill`` is a valid local spelling; when the category exists
    but the skill does not, the worker fails — preflight must too."""
    from hermes_cli.kanban_skill_preflight import missing_skills_for_profile

    profile_dir = _make_profile(kanban_home, "claudecode", [])
    _write_skill(profile_dir / "skills", "mlops/axolotl")

    assert missing_skills_for_profile("claudecode", ["mlops:axolotl"]) == []
    assert missing_skills_for_profile("claudecode", ["mlops:not-there"]) == ["mlops:not-there"]


def test_an_ambiguous_bare_name_is_reported_missing(kanban_home):
    """Two skills of the same bare name across the local dir and an external
    dir make the loader REFUSE to guess, so the worker gets nothing. A
    name-set preflight sees the name twice and calls it present."""
    from hermes_cli.kanban_skill_preflight import missing_skills_for_profile

    profile_dir = _make_profile(kanban_home, "claudecode", ["dup"])
    external = kanban_home / "external-skills"
    _write_skill(external, "dup")
    (profile_dir / "config.yaml").write_text(
        f"skills:\n  external_dirs:\n    - {external}\n", encoding="utf-8",
    )

    assert missing_skills_for_profile("claudecode", ["dup"]) == ["dup"]


def test_a_platform_incompatible_skill_is_reported_missing(kanban_home):
    """``platforms:`` is a hard compatibility gate in skill_view, so a skill
    tagged for another OS cannot load on this host however present it looks."""
    from hermes_cli.kanban_skill_preflight import missing_skills_for_profile

    profile_dir = _make_profile(kanban_home, "claudecode", [])
    _write_platform_skill(profile_dir / "skills", "elsewhere-only", _foreign_platform())

    assert missing_skills_for_profile("claudecode", ["elsewhere-only"]) == ["elsewhere-only"]


def test_the_preflight_does_not_write_into_the_inspected_profile(kanban_home):
    """Filing a card must not mutate somebody else's profile home. Loading a
    skill is not a read-only operation — it seeds the home skeleton plus
    SOUL.md and bumps that skill's Curator usage counters — so the inspection
    runs against a shadow home that resolves identically and absorbs the
    writes. Without it, merely inspecting a skill makes it look 'used' to the
    Curator, which is what decides staleness and archival."""
    from hermes_cli.kanban_skill_preflight import missing_skills_for_profile

    profile_dir = _make_profile(kanban_home, "claudecode", ["github-code-review"])

    def _snapshot():
        return {
            str(p.relative_to(profile_dir)): (p.stat().st_size if p.is_file() else "<dir>")
            for p in sorted(profile_dir.rglob("*")) if "__pycache__" not in p.parts
        }

    before = _snapshot()
    assert missing_skills_for_profile(
        "claudecode", ["github-code-review", "not-installed"],
    ) == ["not-installed"]
    assert _snapshot() == before


def test_the_shadow_home_still_honors_the_profiles_own_config(kanban_home):
    """The shadow home must not become a way to lose the profile's config: a
    skill this profile has DISABLED is still unloadable, and an external dir it
    configures is still searched."""
    from hermes_cli.kanban_skill_preflight import missing_skills_for_profile

    profile_dir = _make_profile(kanban_home, "claudecode", ["github-code-review"])
    external = kanban_home / "shared-skills"
    _write_skill(external, "team-review")
    (profile_dir / "config.yaml").write_text(
        "skills:\n"
        "  disabled:\n    - github-code-review\n"
        f"  external_dirs:\n    - {external}\n",
        encoding="utf-8",
    )

    assert missing_skills_for_profile(
        "claudecode", ["github-code-review", "team-review"],
    ) == ["github-code-review"]


def test_an_absent_assignee_profile_fails_closed(kanban_home):
    """A card carrying forced skills whose assignee has no profile home cannot
    be verified at all. Assuming the skill is present is exactly the failure
    this card exists to stop, so it fails closed with the distinct code."""
    from hermes_cli import kanban_db, kanban_db_connect
    from hermes_cli.kanban_skill_preflight import (
        MISSING_CODE, PROFILE_UNAVAILABLE_CODE, KanbanSkillPreflightError,
    )

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        with pytest.raises(KanbanSkillPreflightError) as excinfo:
            kanban_db.create_task(
                conn, title="card", assignee="never-created", skills=["some-skill"],
            )
        assert kanban_db.list_tasks(conn) == []
    assert excinfo.value.code == PROFILE_UNAVAILABLE_CODE
    assert excinfo.value.code != MISSING_CODE
    assert "never-created" in str(excinfo.value)


def test_a_tombstoned_assignee_profile_fails_closed(kanban_home):
    """A deleted profile leaves its directory behind with a tombstone marker;
    its registry is not authoritative and must not be read as one."""
    from hermes_constants import mark_named_profile_deleted

    from hermes_cli import kanban_db, kanban_db_connect
    from hermes_cli.kanban_skill_preflight import (
        PROFILE_UNAVAILABLE_CODE, KanbanSkillPreflightError,
    )

    profile_dir = _make_profile(kanban_home, "retired", ["github-code-review"])
    mark_named_profile_deleted(profile_dir)

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        with pytest.raises(KanbanSkillPreflightError) as excinfo:
            kanban_db.create_task(
                conn, title="card", assignee="retired", skills=["github-code-review"],
            )
    assert excinfo.value.code == PROFILE_UNAVAILABLE_CODE


def test_a_profile_registry_lookup_failure_fails_closed(kanban_home):
    """When the profile lookup itself raises, neither answer may be assumed."""
    from hermes_cli import kanban_db, kanban_db_connect, profiles
    from hermes_cli.kanban_skill_preflight import (
        PROFILE_UNAVAILABLE_CODE, KanbanSkillPreflightError,
    )

    _make_profile(kanban_home, "claudecode", ["github-code-review"])

    def _boom(_name):
        raise OSError("permission denied")

    original = profiles.profile_exists
    profiles.profile_exists = _boom
    try:
        with kanban_db_connect.connect_closing() as conn:
            kanban_db.create_board(slug="default", name="Test")
            with pytest.raises(KanbanSkillPreflightError) as excinfo:
                kanban_db.create_task(
                    conn, title="card", assignee="claudecode",
                    skills=["github-code-review"],
                )
    finally:
        profiles.profile_exists = original
    assert excinfo.value.code == PROFILE_UNAVAILABLE_CODE


def test_a_wishlist_lane_card_is_inert_and_not_preflighted(kanban_home):
    """A lane card is inert by construction — nothing dispatches it, and its
    skill may well be installed before anyone acts on it. Refusing to file one
    would block capture, which is the opposite of what the lane is for."""
    from hermes_cli import kanban_db, kanban_db_connect

    _make_profile(kanban_home, "claudecode", [])

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        task_id = kanban_db.create_task(
            conn, title="someday", assignee="claudecode",
            skills=["not-installed-yet"], lane="idea",
        )
        task = kanban_db.get_task(conn, task_id)
        assert task.status == "idea"
        assert task.skills == ["not-installed-yet"]


def test_a_wishlist_card_promoted_to_live_work_is_still_preflighted(kanban_home):
    """The lane exemption defers the check, it does not waive it: the moment a
    lane card becomes dispatchable, the dispatcher refuses it exactly as it
    refuses any other unloadable card."""
    from hermes_cli import kanban_db, kanban_db_connect, kanban_db_dispatch

    _make_profile(kanban_home, "claudecode", [])
    spawned = []

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        task_id = kanban_db.create_task(
            conn, title="someday", assignee="claudecode",
            skills=["not-installed-yet"], lane="idea",
        )
        # Promote out of the lane into live work.
        with kanban_db.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))

    with kanban_db_connect.connect_closing() as conn:
        result = kanban_db_dispatch.dispatch_once(
            conn, spawn_fn=lambda task, *a, **k: (spawned.append(task.id), 1)[1],
        )
    assert spawned == []
    assert task_id in [tid for tid, _reason in result.skill_preflight_blocked]


def test_the_explicit_inert_path_defers_the_check_to_the_dispatcher(kanban_home):
    """Import/relocation and deliberate card-before-profile ordering need a way
    in. It is an explicit opt-out, never an implicit one: the row is written,
    and the dispatcher still refuses to spawn it."""
    from hermes_cli import kanban_db, kanban_db_connect, kanban_db_dispatch

    spawned = []
    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        task_id = kanban_db.create_task(
            conn, title="deferred", assignee="never-created",
            skills=["some-skill"], skill_preflight=False,
        )
        assert kanban_db.get_task(conn, task_id).skills == ["some-skill"]

    with kanban_db_connect.connect_closing() as conn:
        result = kanban_db_dispatch.dispatch_once(
            conn, spawn_fn=lambda task, *a, **k: (spawned.append(task.id), 1)[1],
        )
    assert spawned == []
    assert task_id in [tid for tid, _reason in result.skill_preflight_blocked]


def test_a_skills_free_card_for_a_non_profile_assignee_still_dispatches_normally(kanban_home):
    """Control-plane lanes (assignees that pull via claim_task) are untouched:
    preflight only ever looks at cards that actually force skills."""
    from hermes_cli import kanban_db, kanban_db_connect, kanban_db_dispatch

    with kanban_db_connect.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        task_id = kanban_db.create_task(conn, title="lane card", assignee="orion-cc")

    with kanban_db_connect.connect_closing() as conn:
        result = kanban_db_dispatch.dispatch_once(conn, spawn_fn=lambda *a, **k: 1)
    # Skipped as non-spawnable (the pre-existing lane behavior), NOT preflighted.
    assert task_id in result.skipped_nonspawnable
    assert result.skill_preflight_blocked == []
