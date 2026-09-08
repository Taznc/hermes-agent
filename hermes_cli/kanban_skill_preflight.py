"""Profile-scoped preflight of a Kanban card's forced skills.

A card's ``skills`` list becomes ``hermes -p <assignee> --skills X ...`` on the
worker command line. Profiles are isolated homes, so a skill that exists for the
profile FILING the card may not exist for the profile that will RUN it; the
worker then dies during initialization with ``Unknown skill(s): X`` before doing
any work, and the dispatcher scores that as a crash — retry budget, start budget
and the failure breaker all charged for a configuration mistake.

So skills are resolved against the ASSIGNEE's home, never the caller's: this
module installs that profile's directory as the context-local Hermes home and
walks the same search roots the worker itself would use.

Import-light on purpose (``agent.skill_utils`` + ``hermes_cli.profiles``): it
runs inside ``create_task`` and on every dispatcher tick.
"""

from __future__ import annotations

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


class KanbanSkillPreflightError(ValueError):
    """A card's forced skills cannot be satisfied by its assignee profile.

    ``ValueError`` so every existing surface reports it correctly without new
    plumbing: the CLI prints it, ``tools/kanban_tools.py`` turns it into a
    ``tool_error`` without a traceback, and the dashboard router maps it to
    HTTP 400. ``code``/``profile``/``missing`` carry the structured form.
    """

    def __init__(self, message: str, *, code: str, profile: str, missing: tuple[str, ...] = ()):
        super().__init__(message)
        self.code = code
        self.profile = profile
        self.missing = tuple(missing)


def _how_to_fix(profile: str, missing: Iterable[str]) -> str:
    names = list(missing)
    first = names[0] if names else "<skill>"
    return (
        f"Inspect that profile's skills with `hermes -p {profile} skills list`, "
        f"install or enable the skill for it (`hermes -p {profile} skills install {first}`, "
        f"or copy it into that profile's skills/ directory, or remove it from "
        f"`skills.disabled` in its config.yaml), or drop the skill from the card."
    )


def _skill_config_names(key: str) -> set[str]:
    """A ``skills.<key>`` config list as a set of names (empty when absent)."""
    from agent.skill_utils import _skills_cfg, parse_config_string_list

    cfg = _skills_cfg()
    if cfg is None:
        return set()
    return {name.strip() for name in parse_config_string_list(cfg.get(key)) if name.strip()}


def _disabled_names() -> set[str]:
    """Operator-disabled skill names for a CLI worker under the CURRENT home.

    Read straight from config rather than via ``get_disabled_skill_names()``:
    that resolves the platform from the *calling* process's session env, and
    the answer we need is about a ``hermes --cli`` worker, not about us.
    """
    from agent.skill_utils import ESSENTIAL_SKILLS, _skills_cfg, parse_config_string_list

    cfg = _skills_cfg()
    if cfg is None:
        return set()
    disabled = _skill_config_names("disabled")
    platform_disabled = (cfg.get("platform_disabled") or {}) if isinstance(cfg.get("platform_disabled"), dict) else {}
    disabled |= {
        name.strip()
        for name in parse_config_string_list(platform_disabled.get("cli"))
        if name.strip()
    }
    return disabled - set(ESSENTIAL_SKILLS)


def _identifiers_for(skill_md: Path, root: Path, *, frontmatter_name: Optional[str]) -> set[str]:
    """Every identifier ``skill_view()`` would accept for one skill file."""
    identifiers: set[str] = set()
    target = skill_md.parent if skill_md.name == "SKILL.md" else skill_md.with_suffix("")
    try:
        identifiers.add(target.relative_to(root).as_posix())
    except ValueError:
        pass
    identifiers.add(target.name)
    if frontmatter_name:
        identifiers.add(str(frontmatter_name).strip())
    return {i for i in identifiers if i}


def _canonical_name(skill_md: Path, frontmatter_name: Optional[str]) -> str:
    """The name ``skills.disabled`` is matched against."""
    if frontmatter_name and str(frontmatter_name).strip():
        return str(frontmatter_name).strip()
    return (skill_md.parent if skill_md.name == "SKILL.md" else skill_md.with_suffix("")).name


def _available_identifiers_in_current_home() -> set[str]:
    """Identifiers resolvable under the CURRENT Hermes home, minus disabled ones.

    Trusted project-local skill dirs are deliberately excluded: they depend on
    the worker's working directory and trust config, so they are not a property
    of the profile and must not make a card look satisfiable.
    """
    from agent.skill_utils import (
        get_all_skills_dirs, is_excluded_skill_path, iter_skill_index_files, parse_frontmatter,
    )

    disabled = _disabled_names()
    available: set[str] = set()
    for root in get_all_skills_dirs():
        if not root.is_dir():
            continue
        for skill_md in iter_skill_index_files(root, "SKILL.md"):
            frontmatter = _safe_frontmatter(skill_md, parse_frontmatter)
            name = frontmatter.get("name") if isinstance(frontmatter, dict) else None
            if _canonical_name(skill_md, name) in disabled:
                continue
            available |= _identifiers_for(skill_md, root, frontmatter_name=name)
        # Legacy flat ``<name>.md`` skills, the fourth form skill_view accepts.
        for flat in root.rglob("*.md"):
            if flat.name == "SKILL.md" or is_excluded_skill_path(flat, root=root):
                continue
            if _canonical_name(flat, None) in disabled:
                continue
            available |= _identifiers_for(flat, root, frontmatter_name=None)
    return available


def _safe_frontmatter(skill_md: Path, parse_frontmatter) -> dict:
    try:
        return parse_frontmatter(skill_md.read_text(encoding="utf-8"))[0] or {}
    except Exception:
        return {}


def _lookup_forms(requested: str) -> tuple[str, ...]:
    """Identifier spellings to try for one requested name.

    ``category:name`` is the config/gateway spelling of the on-disk
    ``category/name`` and resolves to it (``_resolve_plugin_skill`` falls
    through that way when no plugin owns the namespace).
    """
    name = (requested or "").strip().lstrip("/")
    if not name:
        return ()
    forms = {name}
    namespace, _, bare = name.partition(":")
    if bare and namespace:
        forms.add(f"{namespace}/{bare}")
    return tuple(forms)


def _is_plugin_namespaced(requested: str) -> bool:
    """True for ``plugin:skill``, whose owner can only be enumerated by loading
    that profile's plugins — which this process must not do."""
    from agent.skill_utils import is_valid_namespace, parse_qualified_name

    namespace, bare = parse_qualified_name((requested or "").strip())
    return bool(bare) and is_valid_namespace(namespace)


def available_skill_identifiers(profile: str) -> set[str]:
    """Skill identifiers resolvable for *profile*, under ITS home.

    Raises :class:`KanbanSkillPreflightError` (``PROFILE_UNAVAILABLE_CODE``)
    when a profile that EXISTS cannot be authoritatively inspected — fail closed
    rather than assume the skill is there. A profile that does not exist at all
    is a different case; see :func:`assignee_is_inspectable`.
    """
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    from hermes_cli.profiles import get_profile_dir, normalize_profile_name

    try:
        canon = normalize_profile_name(profile)
        profile_home = get_profile_dir(canon)
    except Exception as exc:
        raise KanbanSkillPreflightError(
            f"Cannot verify skills for assignee profile {profile!r}: {exc}. "
            "Skill preflight fails closed rather than assuming the skill is installed.",
            code=PROFILE_UNAVAILABLE_CODE, profile=str(profile),
        ) from exc
    token = set_hermes_home_override(str(profile_home))
    try:
        return _available_identifiers_in_current_home()
    except Exception as exc:
        raise KanbanSkillPreflightError(
            f"Cannot verify skills for assignee profile {canon!r}: its skill registry "
            f"({profile_home}) could not be read ({exc}). Skill preflight fails closed rather "
            "than assuming the skill is installed.",
            code=PROFILE_UNAVAILABLE_CODE, profile=canon,
        ) from exc
    finally:
        reset_hermes_home_override(token)


def assignee_is_inspectable(profile: str) -> bool:
    """True when *profile* is a live Hermes profile with a real home on disk.

    A non-existent assignee is NOT a preflight failure. The board deliberately
    accepts assignees that are not (yet) Hermes profiles — control-plane lanes
    that pull work via ``claim_task``, and profiles created after the card — and
    the dispatcher already refuses to spawn them (``skipped_nonspawnable``). No
    worker starts, so there is no init crash for preflight to prevent, and
    rejecting the card here would break card-then-profile ordering.

    The home directory must exist too: without it there is no registry to read,
    and an absent home already fails the worker's own ``hermes -p`` startup for
    reasons that have nothing to do with skills. Fail-closed
    (``PROFILE_UNAVAILABLE_CODE``) is reserved for a home that EXISTS but cannot
    be enumerated — the case where a skill might really be there and we must not
    pretend either way.
    """
    try:
        from hermes_cli.profiles import get_profile_dir, normalize_profile_name, profile_exists

        canon = normalize_profile_name(profile)
        return bool(profile_exists(canon)) and Path(get_profile_dir(canon)).is_dir()
    except Exception:
        return False


def missing_skills_for_profile(profile: str, skills: Optional[Iterable[str]]) -> list[str]:
    """Requested skills *profile* cannot load, in request order.

    ``plugin:skill`` names are skipped: enumerating another profile's plugin
    skills means discovering and importing its plugins, which this process must
    not do on that profile's behalf.
    """
    requested = [str(s).strip() for s in (skills or ()) if str(s).strip()]
    checkable = [name for name in requested if not _is_plugin_namespaced(name)]
    if not checkable:
        return []
    available = available_skill_identifiers(profile)
    return [
        name for name in checkable
        if not any(form in available for form in _lookup_forms(name))
    ]


def preflight_task_skills(profile: Optional[str], skills: Optional[Iterable[str]]) -> None:
    """Raise :class:`KanbanSkillPreflightError` unless *profile* can load *skills*.

    Scope is the card's OWN forced skills. Skills the dispatcher injects itself
    (``sdlc-review`` on the review lane) are deliberately NOT checked here: they
    are a Hermes install-level default rather than card configuration, and
    blocking a card because a bundled skill is absent would convert an install
    problem into a stuck board. See ``REVIEW_LANE_SKILL``.

    No-ops for an unassigned card: there is no profile to validate against, and
    the dispatcher re-runs this against whichever profile the card lands on.
    """
    if not profile or not str(profile).strip():
        return
    wanted = [str(s).strip() for s in (skills or ()) if str(s).strip()]
    if not wanted:
        return
    if not assignee_is_inspectable(profile):
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
