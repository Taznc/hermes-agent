"""Restart/reboot/service-lifecycle categorization for the approval system.

A command classified here must NEVER become auto-approvable: not via
``command_allowlist`` ("always"), not via prior session/permanent approval
memory, not via the smart-approval guardian LLM, and not via
``hermes approvals suggest`` mining it into a proposal. This module is a
classification LENS on top of the existing dangerous-command descriptions in
:mod:`tools.approval_detection` and :mod:`tools.hermes_service_guard` — never
a second copy of the patterns themselves. A new phrasing added to either of
those modules without a matching keyword here fails OPEN (not classified as
lifecycle), so keep the marker set broad and review it whenever a new
service-lifecycle pattern is added.

See kanban card evidence 2026-09-25: an unapproved
``systemctl --user restart hermes-webdesktop-dev`` took down the operator's
desktop after being silently auto-approved by smart approval.
"""

from __future__ import annotations

import re

# Deliberately broad: matches every current restart/stop/reboot/shutdown dangerous-command
# description (both the static ones in approval_detection.DANGEROUS_PATTERNS/HARDLINE_PATTERNS
# and the dynamic, unit-named ones hermes_service_guard.py builds for kill/pid indirection).
_LIFECYCLE_MARKERS_RE = re.compile(
    r"\brestart\b|\breboot\b|\bshutdown\b|\bpoweroff\b|\bhalt\b|\bkexec\b|"
    r"stop/restart|stop/delete|force stop service|"
    r"container lifecycle|"
    r"kills the running agent fleet|kills running agents",
    re.IGNORECASE,
)


def is_lifecycle_pattern(description: str | None) -> bool:
    """True when a dangerous-command DESCRIPTION denotes a service/system stop, restart,
    reboot, or shutdown — the class this card locks out of every auto-approve path."""
    return bool(description) and bool(_LIFECYCLE_MARKERS_RE.search(description))


def lifecycle_out_of_band_message(command: str, description: str | None) -> str:
    """Deny text for a lifecycle command reaching an unattended context with no human to ask.

    Names the flagged command verbatim (it already carries the unit name — e.g.
    ``systemctl --user restart hermes-hindsight-proxy``) and tells the operator exactly what
    to run themselves if the action is actually wanted. See ``approvals.protected_units`` in
    ``hermes_cli/config_defaults.py``.
    """
    desc = description or "service/system lifecycle command"
    return (
        f"BLOCKED: {desc}. This is a restart/stop/reboot action and can NEVER be "
        "auto-approved (not by --yolo, approvals.mode=off, command_allowlist, prior "
        "session/permanent approval, or the smart-approval guardian) — and no interactive "
        "operator is present to approve it right now. Do NOT retry. If this is genuinely "
        f"needed, ask the operator to run it themselves out-of-band: {command}"
    )
