"""Fork-owned pre-review receipt gate for ``kanban_db.request_review``.

Most reviewer rejections on a busy board are mechanical (a missing test, an
unchecked revision), and each one costs a full reviewer session plus a full
implementer session. With ``kanban.require_pre_review_gate: true`` a review
handoff is refused unless ``metadata.pre_review_gate`` carries a non-empty
``revision`` and ``tests``, so the worker fixes the receipt in the same turn
instead of burning a review round. Other receipt keys (lint, pushed,
mergeable, acceptance) are accepted but not enforced.

Called from exactly one site: the ``pre-review-gate`` FORK ANCHOR at the top
of ``hermes_cli.kanban_db.request_review``, before any row is read or written,
so every non-forced caller (``kanban_request_review`` tool, ``hermes kanban
request-review`` CLI) is covered and a refusal leaves the task untouched.
``force=True`` (the dashboard's human drag-to-review and CLI ``--force``) is an
explicit operator override and bypasses the gate, as it bypasses the live-claim
fence. Off by default, so upstream behaviour is unchanged.

Ported from the frozen ``dev`` branch (``tools/kanban_tools_review_gate.py``,
feb4981922), which gated the tool handler only.
"""

from __future__ import annotations

from typing import Any, Optional

REQUIRED_KEYS = ("revision", "tests")
_TRUE_STRINGS = frozenset({"1", "true", "yes", "on"})


def _filled(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_filled(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_filled(v) for v in value)
    return value is not None and value is not False


def missing_keys(metadata: Optional[dict]) -> list[str]:
    """Required ``pre_review_gate`` keys absent or empty in ``metadata``."""
    gate = metadata.get("pre_review_gate") if isinstance(metadata, dict) else None
    if not isinstance(gate, dict):
        return list(REQUIRED_KEYS)
    return [key for key in REQUIRED_KEYS if not _filled(gate.get(key))]


def gate_enabled() -> bool:
    """``kanban.require_pre_review_gate`` from the active HERMES_HOME config."""
    from hermes_cli.config import cfg_get, load_config_readonly

    value = cfg_get(load_config_readonly(), "kanban", "require_pre_review_gate", default=False)
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_STRINGS
    return value is True


def refusal(metadata: Optional[dict]) -> Optional[str]:
    """Actionable refusal reason when the gate is on and unmet, else ``None``."""
    if not gate_enabled():
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
        "mergeable, acceptance). Run the checks first (fork-dev-workflow skill, "
        "§4b Pre-review gate). The task is unchanged and still yours."
    )
