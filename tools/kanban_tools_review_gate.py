"""Enforced pre-review gate for ``kanban_request_review``.

Most reviewer rejections on a busy board are mechanical — a missing test, a red
lint gate, an unpushed commit — and each one costs a full reviewer session plus
a full implementer session. The handoff convention (fork-dev-workflow §4b) asks
implementers to attach a ``pre_review_gate`` receipt, but an advisory
convention is mostly skipped. With ``kanban.require_pre_review_gate: true`` the
tool refuses a handoff without a usable receipt, so the worker fixes it in the
same turn instead of burning a review round.

Only the minimal receipt is required: which revision was checked and which
focused tests/gates ran. Anything else (lint, pushed, mergeable, acceptance) is
accepted but not enforced. Off by default, so upstream behaviour is unchanged.
"""
from __future__ import annotations

from typing import Any, Optional

from hermes_cli.config import cfg_get, load_config

REQUIRED_KEYS = ("revision", "tests")


def _filled(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return any(_filled(v) for v in (value.values() if isinstance(value, dict) else value))
    return value is not None and value is not False


def missing_keys(metadata: Optional[dict]) -> list[str]:
    """Required ``pre_review_gate`` keys absent or empty in ``metadata``."""
    gate = (metadata or {}).get("pre_review_gate")
    if not isinstance(gate, dict):
        return list(REQUIRED_KEYS)
    return [key for key in REQUIRED_KEYS if not _filled(gate.get(key))]


def refusal(metadata: Optional[dict]) -> Optional[str]:
    """Actionable refusal message when the gate is on and unmet, else None."""
    if not cfg_get(load_config(), "kanban", "require_pre_review_gate", default=False):
        return None
    missing = missing_keys(metadata)
    if not missing:
        return None
    names = ", ".join(f"pre_review_gate.{key}" for key in missing)
    return (
        f"review handoff refused: metadata is missing {names}. This board requires a "
        "pre-review receipt (kanban.require_pre_review_gate). Re-call kanban_request_review "
        "with metadata.pre_review_gate = {\"revision\": \"<commit sha or patch:<path>>\", "
        "\"tests\": \"<focused tests/gates run and their result>\"} (optional: lint, pushed, "
        "mergeable, acceptance). Run the checks first — see the fork-dev-workflow skill, "
        "§4b Pre-review gate. The card stays with you until then."
    )
