"""Profile-scoped preflight of a Kanban card's forced skills.

A card's ``skills`` list becomes ``hermes -p <assignee> --skills X ...`` on the
worker command line. Profiles are isolated homes, so a skill that exists for the
profile FILING the card may not exist for the profile that will RUN it; the
worker then dies during initialization with ``Unknown skill(s): X`` before doing
any work, and the dispatcher scores that as a crash — retry budget, start budget
and the failure breaker all charged for a configuration mistake.

The answer must be the worker's own, not an approximation of it. "Can this
profile load this skill?" is decided by running the loader the worker itself
runs (``build_preloaded_skills_prompt``) in a subprocess under that profile's
home — see :mod:`hermes_cli.kanban_skill_probe`. A name-set approximation
silently disagrees with it on every interesting case: qualified ``ns:skill``
spellings, names that collide across skill dirs (the loader refuses to guess),
and skills gated to another OS by ``platforms:``.

When the profile cannot be inspected at all — no home, tombstoned, unreadable,
probe failure — this fails CLOSED. Assuming a skill is present is exactly the
mistake that produces the crash-loop this module exists to prevent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional

# The skill every dispatcher-spawned review worker is force-loaded with
# (``_dispatch_lane_task``). NOT preflighted: it is injected by the dispatcher
# itself, so its absence is a Hermes install problem, not card configuration,
# and blocking cards for it would stall the whole review lane on a board whose
# profiles simply never installed the bundled skill.
REVIEW_LANE_SKILL = "sdlc-review"

MISSING_CODE = "kanban_skill_missing"
PROFILE_UNAVAILABLE_CODE = "kanban_skill_profile_unavailable"

# Generous for a cold import of the skills stack on a loaded box; the probe
# itself runs in well under a second. Exceeding it means "cannot inspect".
PROBE_TIMEOUT_SECONDS = 120


class KanbanSkillPreflightError(ValueError):
    """A card's forced skills cannot be satisfied by its assignee profile.

    ``ValueError`` so every existing surface reports it correctly without new
    plumbing: the CLI prints it, ``tools/kanban_tools.py`` turns it into a
    ``tool_error`` without a traceback, and the dashboard router maps it to
    HTTP 400. ``code``/``profile``/``missing`` carry the structured form, and
    :meth:`as_dict` is the machine-readable payload those surfaces emit.
    """

    def __init__(self, message: str, *, code: str, profile: str, missing: tuple[str, ...] = ()):
        super().__init__(message)
        self.code = code
        self.profile = profile
        self.missing = tuple(missing)

    def as_dict(self) -> dict:
        """The structured error contract shared by the CLI, tool and HTTP APIs."""
        return {
            "error": str(self),
            "code": self.code,
            "profile": self.profile,
            "missing_skills": list(self.missing),
        }


def structured_error_payload(exc: BaseException) -> Optional[dict]:
    """:meth:`KanbanSkillPreflightError.as_dict` for *exc*, else ``None``.

    Lets a surface add the structured fields to its error response without
    importing the exception type or branching on it.
    """
    return exc.as_dict() if isinstance(exc, KanbanSkillPreflightError) else None


def _how_to_fix(profile: str, missing: Iterable[str]) -> str:
    names = list(missing)
    first = names[0] if names else "<skill>"
    return (
        f"Inspect that profile's skills with `hermes -p {profile} skills list`, "
        f"install or enable the skill for it (`hermes -p {profile} skills install {first}`, "
        f"or copy it into that profile's skills/ directory, or remove it from "
        f"`skills.disabled` in its config.yaml), or drop the skill from the card."
    )


def _unavailable(profile: str, detail: str) -> "KanbanSkillPreflightError":
    return KanbanSkillPreflightError(
        f"Cannot verify forced skills for assignee profile {profile!r}: {detail} "
        "Skill preflight fails closed rather than assuming the skill is installed. "
        f"Create or repair the profile (`hermes profile create {profile}`), reassign "
        "the card to a profile that can be inspected, or drop the skill from the card.",
        code=PROFILE_UNAVAILABLE_CODE, profile=str(profile),
    )


def _profile_home(profile: str) -> tuple[str, Path]:
    """``(canonical_name, home_dir)`` for a live, inspectable profile.

    Raises :class:`KanbanSkillPreflightError` (``PROFILE_UNAVAILABLE_CODE``)
    when the profile does not exist, is tombstoned, or cannot be resolved.
    """
    from hermes_cli.profiles import get_profile_dir, normalize_profile_name, profile_exists

    try:
        canon = normalize_profile_name(profile)
    except Exception as exc:
        raise _unavailable(str(profile), f"the profile name could not be resolved ({exc}).") from exc
    try:
        exists = bool(profile_exists(canon))
        home = Path(get_profile_dir(canon))
        readable = home.is_dir()
    except Exception as exc:
        raise _unavailable(canon, f"its profile registry could not be read ({exc}).") from exc
    if not exists or not readable:
        raise _unavailable(
            canon,
            f"there is no live profile home at {home} to inspect (missing, deleted or unreadable).",
        )
    return canon, home


def _probe_env(home: Path) -> dict:
    """Child environment: the assignee's home, and nothing of this run's identity.

    ``HERMES_KANBAN_*`` is stripped so the probe is never mistaken for a
    dispatcher-owned worker, and ``HERMES_PROFILE*`` so nothing resolves back to
    the caller's profile.
    """
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("HERMES_KANBAN_", "HERMES_PROFILE"))
    }
    env["HERMES_HOME"] = str(home)
    # User plugin directories are mirrored into the shadow home. Importing one
    # must not write ``__pycache__`` through that directory symlink.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    repo_root = str(Path(__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{existing}" if existing else repo_root
    return env


@contextmanager
def _read_only_home(profile_home: Path):
    """A throwaway home that RESOLVES like *profile_home* but absorbs its writes.

    Loading a skill is not a read-only operation: it seeds the home skeleton and
    ``SOUL.md``, and bumps that skill's Curator usage counters in
    ``skills/.usage.json``. Filing a card must not do any of that to somebody
    else's profile — an inspected skill would look "used" to the Curator, which
    is what decides staleness and archival.

    So the child gets its own directory whose ``config.yaml`` is the profile's
    (skill dirs, ``external_dirs``, ``disabled`` all resolve identically), whose
    ``skills/`` is a real directory, and whose user plugin registry is visible
    to qualified ``plugin:skill`` lookups. Skill/plugin directories are
    symlinked for loader fidelity, while root metadata and legacy flat skill
    files are copied so writes such as ``bump_use`` land in the temp dir and
    disappear. Bytecode writes are disabled in :func:`_probe_env` so importing a
    symlinked plugin cannot create ``__pycache__`` in the real profile.
    ``.env`` is deliberately NOT copied: it holds secrets, and skill *readiness*
    does not affect whether a skill loads.
    """
    with tempfile.TemporaryDirectory(prefix="hermes-skill-probe-home-") as tmp:
        shadow = Path(tmp)
        config = profile_home / "config.yaml"
        if config.is_file():
            shutil.copy2(config, shadow / "config.yaml")
        shadow_skills = shadow / "skills"
        shadow_skills.mkdir()
        real_skills = profile_home / "skills"
        if real_skills.is_dir():
            for entry in real_skills.iterdir():
                target = shadow_skills / entry.name
                if entry.is_dir():
                    target.symlink_to(entry, target_is_directory=True)
                elif entry.is_file():
                    shutil.copy2(entry, target)

        # ``plugin:skill`` resolution imports the assignee's user plugins and
        # reads registrations from that profile-scoped registry. Keep the
        # registry visible without making its root metadata writable through.
        real_plugins = profile_home / "plugins"
        if real_plugins.is_dir():
            shadow_plugins = shadow / "plugins"
            shadow_plugins.mkdir()
            for entry in real_plugins.iterdir():
                target = shadow_plugins / entry.name
                if entry.is_dir():
                    target.symlink_to(entry, target_is_directory=True)
                elif entry.is_file():
                    shutil.copy2(entry, target)
        yield shadow


def _run_probe(canon: str, home: Path, names: list[str]) -> dict:
    """Load *names* under *home* in a subprocess; returns the loader's verdict.

    The child runs in an empty temp directory so trusted project-local skill
    dirs cannot make a card look satisfiable: those depend on the worker's
    working directory and trust config, not on the profile.
    """
    from hermes_cli.kanban_skill_probe import RESULT_PREFIX

    try:
        with tempfile.TemporaryDirectory(prefix="hermes-skill-probe-cwd-") as neutral_cwd, \
                _read_only_home(home) as shadow_home:
            proc = subprocess.run(
                [sys.executable, "-m", "hermes_cli.kanban_skill_probe", json.dumps(names)],
                capture_output=True, text=True, env=_probe_env(shadow_home),
                cwd=neutral_cwd, timeout=PROBE_TIMEOUT_SECONDS,
            )
    except subprocess.TimeoutExpired as exc:
        raise _unavailable(
            canon, f"inspecting its skill registry timed out after {PROBE_TIMEOUT_SECONDS}s.",
        ) from exc
    except Exception as exc:
        raise _unavailable(canon, f"its skill registry could not be inspected ({exc}).") from exc
    for line in proc.stdout.splitlines():
        if line.startswith(RESULT_PREFIX):
            try:
                return json.loads(line[len(RESULT_PREFIX):])
            except Exception as exc:
                raise _unavailable(canon, f"its skill registry returned unreadable output ({exc}).") from exc
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    raise _unavailable(
        canon,
        f"inspecting its skill registry failed (exit {proc.returncode}"
        + (f": {detail[-1][:200]}" if detail else "") + ").",
    )


def missing_skills_for_profile(profile: str, skills: Optional[Iterable[str]]) -> list[str]:
    """Requested skills *profile* cannot load, in request order.

    The verdict comes from the real loader running under that profile's home,
    so it covers every way a name fails to load — absent, disabled, ambiguous
    across skill dirs, gated to another platform, or an unresolvable
    ``namespace:skill`` — rather than only "no file with that name".
    """
    requested = [str(s).strip() for s in (skills or ()) if str(s).strip()]
    if not requested:
        return []
    canon, home = _profile_home(profile)
    verdict = _run_probe(canon, home, requested)
    missing = {str(name) for name in verdict.get("missing") or ()}
    return [name for name in requested if name in missing]


def preflight_task_skills(profile: Optional[str], skills: Optional[Iterable[str]]) -> None:
    """Raise :class:`KanbanSkillPreflightError` unless *profile* can load *skills*.

    Scope is the card's OWN forced skills. Skills the dispatcher injects itself
    (``sdlc-review`` on the review lane) are deliberately NOT checked here: they
    are a Hermes install-level default rather than card configuration, and
    blocking a card because a bundled skill is absent would convert an install
    problem into a stuck board. See ``REVIEW_LANE_SKILL``.

    A card that forces no skills is never inspected at all — so control-plane
    lanes, whose assignees are not Hermes profiles, are untouched. An unassigned
    card no-ops too: there is no profile to validate against yet, and the
    dispatcher re-runs this against whichever profile the card lands on.
    """
    wanted = [str(s).strip() for s in (skills or ()) if str(s).strip()]
    if not wanted:
        return
    if not profile or not str(profile).strip():
        return
    missing = missing_skills_for_profile(profile, wanted)
    if not missing:
        return
    from hermes_cli.profiles import normalize_profile_name

    canon = normalize_profile_name(str(profile))
    noun = "skill" if len(missing) == 1 else "skills"
    raise KanbanSkillPreflightError(
        f"Assignee profile {canon!r} cannot load forced {noun}: {', '.join(missing)}. "
        "Profiles have isolated skill registries, so a skill installed for another "
        f"profile is not available to {canon!r}. " + _how_to_fix(canon, missing),
        code=MISSING_CODE, profile=canon, missing=tuple(missing),
    )
