"""Regression contracts for restart/stop/reboot approval enforcement.

Kanban card evidence 2026-09-25: a ``systemctl --user restart hermes-hindsight-proxy`` was
executed and auto-approved by smart approval even though the danger classifier correctly
tagged it "stop/restart system service". Separately, ``command_allowlist`` entries and
``kanban.post_drain.service_restart_allowlist`` granted standing restart/reboot permission.

This class of command (``tools.approval_lifecycle.is_lifecycle_pattern``) must be TERMINAL:
no ``--yolo``, ``approvals.mode: off``, ``command_allowlist`` entry, prior session/permanent
approval, smart-approval guardian verdict, or unattended approve-mode config can turn it into
an unconditional grant. A present human still gets asked (once/deny only — no session/always
persistence, so the SAME restart re-prompts later in the same session); an absent human gets
an immediate, specific deny.

These tests assert the BEHAVIOR CONTRACT (a lifecycle command always reaches this floor),
never a specific unit name or the current pattern list — a new phrasing added to the
detectors should trip these tests via ``is_lifecycle_pattern``, not require editing them.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

import tools.approval as approval_module
from tools import approval_context
from tools.approval import check_all_command_guards, check_dangerous_command
from tools.approval_lifecycle import is_lifecycle_pattern

_TIRITH_PATCH = "tools.tirith_security.check_command_security"
_RESTART_CMD = "systemctl --user restart hermes-hindsight-proxy"
_REBOOT_CMD = "reboot"


def _tirith_allow():
    return {"action": "allow", "findings": [], "summary": ""}


@pytest.fixture(autouse=True)
def _mode_manual(monkeypatch):
    """Pin approvals.mode to 'manual' unless a test explicitly wants 'smart'."""
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")


@pytest.fixture(autouse=True)
def _clean_state():
    approval_module._session_approved.clear()
    approval_module._pending.clear()
    approval_module._permanent_approved.clear()
    saved = {}
    for k in ("HERMES_INTERACTIVE", "HERMES_GATEWAY_SESSION", "HERMES_EXEC_ASK",
              "HERMES_YOLO_MODE", "HERMES_SINGLE_QUERY_SESSION", "HERMES_CRON_SESSION"):
        if k in os.environ:
            saved[k] = os.environ.pop(k)
    yield
    approval_module._session_approved.clear()
    approval_module._pending.clear()
    approval_module._permanent_approved.clear()
    for k in ("HERMES_INTERACTIVE", "HERMES_GATEWAY_SESSION", "HERMES_EXEC_ASK",
              "HERMES_YOLO_MODE", "HERMES_SINGLE_QUERY_SESSION", "HERMES_CRON_SESSION"):
        os.environ.pop(k, None)
    for k, v in saved.items():
        os.environ[k] = v


def test_lifecycle_marker_covers_the_incident_pattern():
    """Sanity: the actual pattern from the incident is classified as lifecycle."""
    assert is_lifecycle_pattern("stop/restart system service")
    assert is_lifecycle_pattern("system shutdown/reboot")
    assert is_lifecycle_pattern("stop/restart hermes gateway (kills running agents)")
    assert not is_lifecycle_pattern("recursive delete")


# ---------------------------------------------------------------------------
# --yolo / approvals.mode: off never bypass a lifecycle command
# ---------------------------------------------------------------------------

class TestYoloNeverBypassesLifecycle:
    @patch(_TIRITH_PATCH, return_value=_tirith_allow())
    def test_yolo_active_still_prompts_interactively(self, _mock_tirith):
        os.environ["HERMES_INTERACTIVE"] = "1"
        with patch.object(approval_module, "_YOLO_MODE_FROZEN", True):
            cb = MagicMock(return_value="deny")
            result = check_all_command_guards(_RESTART_CMD, "local", approval_callback=cb)
        cb.assert_called_once()
        assert result["approved"] is False

    def test_yolo_active_headless_fails_closed_not_approved(self):
        """No human present + yolo: an ordinary dangerous command would auto-approve;
        a lifecycle command must still BLOCK."""
        with patch.object(approval_module, "_YOLO_MODE_FROZEN", True):
            result = check_all_command_guards(_RESTART_CMD, "local")
        assert result["approved"] is False
        assert "restart" in result["message"].lower() or "stop" in result["message"].lower()

    def test_approvals_mode_off_headless_fails_closed(self, monkeypatch):
        monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "off")
        result = check_all_command_guards(_RESTART_CMD, "local")
        assert result["approved"] is False


# ---------------------------------------------------------------------------
# command_allowlist ("always") never covers a lifecycle command
# ---------------------------------------------------------------------------

class TestAllowlistNeverCoversLifecycle:
    @patch(_TIRITH_PATCH, return_value=_tirith_allow())
    def test_permanent_allowlist_entry_for_exact_command_ignored(self, _mock_tirith):
        os.environ["HERMES_INTERACTIVE"] = "1"
        with approval_module._lock:
            approval_module._permanent_approved.add(_RESTART_CMD)
        cb = MagicMock(return_value="deny")
        result = check_all_command_guards(_RESTART_CMD, "local", approval_callback=cb)
        cb.assert_called_once()
        assert result["approved"] is False

    @patch(_TIRITH_PATCH, return_value=_tirith_allow())
    def test_broad_glob_allowlist_entry_does_not_cover_restart(self, _mock_tirith):
        """A broad glob like 'systemctl *' allowlisted for an unrelated reason must not
        also grant a restart it happens to match."""
        os.environ["HERMES_INTERACTIVE"] = "1"
        with approval_module._lock:
            approval_module._permanent_approved.add("systemctl *")
        cb = MagicMock(return_value="deny")
        result = check_all_command_guards(_RESTART_CMD, "local", approval_callback=cb)
        cb.assert_called_once()
        assert result["approved"] is False

    def test_pattern_key_permanent_approval_ignored_headless(self):
        """The dangerous-pattern KEY itself (what an interactive 'always' answer persists)
        must not silently grant a later headless restart."""
        with approval_module._lock:
            approval_module._permanent_approved.add("stop/restart system service")
        result = check_all_command_guards(_RESTART_CMD, "local")
        assert result["approved"] is False


# ---------------------------------------------------------------------------
# Prior session approval never covers a LATER lifecycle command (every occurrence re-prompts)
# ---------------------------------------------------------------------------

class TestSessionApprovalNeverPersistsForLifecycle:
    @patch(_TIRITH_PATCH, return_value=_tirith_allow())
    def test_session_approve_then_same_command_reprompts(self, _mock_tirith):
        os.environ["HERMES_INTERACTIVE"] = "1"
        cb = MagicMock(return_value="session")
        first = check_all_command_guards(_RESTART_CMD, "local", approval_callback=cb)
        assert first["approved"] is True
        assert cb.call_count == 1

        # A second identical restart must ask again — 'session' never actually persisted.
        cb2 = MagicMock(return_value="deny")
        second = check_all_command_guards(_RESTART_CMD, "local", approval_callback=cb2)
        cb2.assert_called_once()
        assert second["approved"] is False

    @patch(_TIRITH_PATCH, return_value=_tirith_allow())
    def test_cli_prompt_hides_session_and_always_for_lifecycle(self, _mock_tirith):
        """The approval_callback contract: allow_session/allow_permanent both False."""
        os.environ["HERMES_INTERACTIVE"] = "1"
        cb = MagicMock(return_value="once")
        check_all_command_guards(_RESTART_CMD, "local", approval_callback=cb)
        cb.assert_called_once()
        _, kwargs = cb.call_args[0], cb.call_args[1]
        assert kwargs.get("allow_permanent") is False
        assert kwargs.get("allow_session") is False

    @patch(_TIRITH_PATCH, return_value=_tirith_allow())
    def test_ordinary_dangerous_command_still_offers_session_and_always(self, _mock_tirith):
        """Non-regression: this class-wide restriction must not leak onto ordinary commands."""
        os.environ["HERMES_INTERACTIVE"] = "1"
        cb = MagicMock(return_value="once")
        check_all_command_guards("rm -rf /tmp/somedir", "local", approval_callback=cb)
        cb.assert_called_once()
        kwargs = cb.call_args[1]
        assert kwargs.get("allow_permanent") is True


# ---------------------------------------------------------------------------
# The smart-approval guardian LLM is never consulted for a lifecycle command
# ---------------------------------------------------------------------------

class TestSmartApprovalNeverAutoApprovesLifecycle:
    def test_smart_verdict_not_called_when_human_present(self, monkeypatch):
        monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "smart")
        os.environ["HERMES_INTERACTIVE"] = "1"
        smart_calls = []

        def fake_smart_verdict(*a, **kw):
            smart_calls.append(1)
            return "approve"

        with patch("tools.approval_smart._smart_verdict", side_effect=fake_smart_verdict), \
             patch(_TIRITH_PATCH, return_value=_tirith_allow()), \
             patch.object(approval_module, "_present_with_selected_transport",
                          return_value={"selected": False}):
            cb = MagicMock(return_value="once")
            result = check_all_command_guards(_RESTART_CMD, "local", approval_callback=cb)
        assert smart_calls == [], "smart-approval guardian must never be asked about a restart"
        cb.assert_called_once()
        assert result["approved"] is True  # the HUMAN said once, not the guardian

    def test_smart_verdict_not_called_headless(self, monkeypatch):
        """This is the literal incident: no human present, smart mode active."""
        monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "smart")
        smart_calls = []

        def fake_smart_verdict(*a, **kw):
            smart_calls.append(1)
            return "approve"

        with patch("tools.approval_smart._smart_verdict", side_effect=fake_smart_verdict):
            result = check_all_command_guards(_RESTART_CMD, "local")
        assert smart_calls == [], "smart-approval guardian must never run for a headless restart"
        assert result["approved"] is False


# ---------------------------------------------------------------------------
# Unattended approve-mode (cron_mode/single_query_mode/unattended_mode: approve) fails closed
# ---------------------------------------------------------------------------

class TestUnattendedApproveModeNeverCoversLifecycle:
    def test_single_query_approve_mode_still_blocks(self, monkeypatch):
        monkeypatch.setattr(approval_context, "_is_single_query_approval_context", lambda: True)
        monkeypatch.setattr(approval_context, "_get_single_query_approval_mode", lambda: "approve")
        result = check_all_command_guards(_RESTART_CMD, "local")
        assert result["approved"] is False
        # Non-regression: an ordinary dangerous command still honors the operator's opt-in.
        result_ordinary = check_all_command_guards("rm -rf /tmp/x", "local")
        assert result_ordinary["approved"] is True

    def test_cron_approve_mode_still_blocks(self, monkeypatch):
        monkeypatch.setattr(approval_context, "_is_cron_approval_context", lambda: True)
        monkeypatch.setattr(approval_context, "_get_cron_approval_mode", lambda: "approve")
        result = check_all_command_guards(_RESTART_CMD, "local")
        assert result["approved"] is False
        result_ordinary = check_all_command_guards("rm -rf /tmp/x", "local")
        assert result_ordinary["approved"] is True

    def test_denial_names_unit_and_out_of_band_command(self):
        """AC6: the denial message names the unit/command and tells the operator what to run."""
        result = check_all_command_guards(_RESTART_CMD, "local")
        assert result["approved"] is False
        assert _RESTART_CMD in result["message"]


# ---------------------------------------------------------------------------
# check_dangerous_command (the pattern-only legacy entry point) has the same contract
# ---------------------------------------------------------------------------

class TestCheckDangerousCommandLifecycleParity:
    def test_yolo_does_not_bypass(self):
        with patch.object(approval_module, "_YOLO_MODE_FROZEN", True):
            result = check_dangerous_command(_RESTART_CMD, "local")
        assert result["approved"] is False

    def test_permanent_allowlist_does_not_bypass(self):
        with approval_module._lock:
            approval_module._permanent_approved.add(_RESTART_CMD)
        result = check_dangerous_command(_RESTART_CMD, "local")
        assert result["approved"] is False


# ---------------------------------------------------------------------------
# hardline reboot/shutdown stays on the pre-existing hardline floor (never bypassable)
# ---------------------------------------------------------------------------

class TestHardlineLifecycleUnaffected:
    def test_reboot_is_hardline_blocked_even_under_yolo(self):
        with patch.object(approval_module, "_YOLO_MODE_FROZEN", True):
            result = check_all_command_guards(_REBOOT_CMD, "local")
        assert result["approved"] is False
        assert result.get("hardline") is True
