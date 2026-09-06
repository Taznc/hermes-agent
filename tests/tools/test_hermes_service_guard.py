"""Behavior contracts for the Hermes-service stop guard (``tools/hermes_service_guard.py``).

The incident this guards: on 2026-09-05 a headless kanban worker was correctly BLOCKED from
``sudo -n systemctl restart hermes-webdesktop-backend.service``, then reached the identical
outcome with ``sudo -n kill -TERM 880`` — 880 being that unit's MainPID. Five workers died in
the same second. A bare pid carries no dangerous keyword, so no text pattern can classify it;
the guard resolves the target instead of matching its spelling.

These tests assert relationships (in-scope vs out-of-scope, interactive vs non-interactive),
never the pattern list or its contents.
"""

import json
from unittest.mock import patch

import pytest

from tools.approval_detection import detect_dangerous_command
from tools.hermes_service_guard import detect_hermes_service_stop, is_hermes_unit

# A fake process topology: pid -> owning systemd unit. Injected so the tests describe a
# fleet without spawning one, and so they behave identically on a runner with no systemd.
FLEET = {
    880: "hermes-webdesktop-backend.service",
    901: "hermes-gateway.service",
    902: "hermes-webui.service",
    903: "hermes-worker-kanban-t_abc123-run-7.scope",
}
UNRELATED = {5669: "ssh.service", 872: "cron.service"}
TOPOLOGY = {**FLEET, **UNRELATED}


def resolve(pid):
    return TOPOLOGY.get(pid)


def detect(command):
    return detect_hermes_service_stop(command, resolve_pid_unit=resolve)


# --- the incident itself -------------------------------------------------------------------

def test_signalling_a_hermes_unit_pid_is_dangerous():
    """The exact command that killed the fleet: a bare pid, no dangerous keyword."""
    is_dangerous, description = detect("sudo -n kill -TERM 880")
    assert is_dangerous
    assert "hermes-webdesktop-backend.service" in description


def test_refusal_names_the_command_a_human_should_run():
    """Acceptance: the worker must learn what a human must do, not merely that it was refused."""
    _, description = detect("sudo -n kill -TERM 880")
    assert "systemctl restart hermes-webdesktop-backend.service" in description
    assert "human" in description.lower()


# --- in-scope vs out-of-scope --------------------------------------------------------------

@pytest.mark.parametrize("pid", sorted(FLEET))
def test_every_fleet_unit_pid_is_in_scope(pid):
    assert detect(f"kill -9 {pid}")[0]


@pytest.mark.parametrize("pid", sorted(UNRELATED))
def test_unrelated_service_pids_stay_allowed(pid):
    """Scope is Hermes lifecycle only — signalling an unrelated service must keep working."""
    assert not detect(f"kill -TERM {pid}")[0]


@pytest.mark.parametrize("verb", ["stop", "restart", "kill", "try-restart", "reload-or-restart"])
def test_service_stopping_verbs_on_a_hermes_unit_are_dangerous(verb):
    assert detect(f"systemctl {verb} hermes-gateway")[0]


@pytest.mark.parametrize("verb", ["stop", "restart", "kill", "try-restart", "reload-or-restart"])
def test_the_same_verbs_on_a_non_hermes_unit_are_allowed(verb):
    """The in-scope/out-of-scope split is the unit, not the verb."""
    assert not detect(f"systemctl {verb} nginx")[0]


@pytest.mark.parametrize("command", [
    "systemctl status hermes-gateway",
    "systemctl show hermes-webui -p MainPID",
    "systemctl start hermes-gateway",
    "systemctl daemon-reload",
    "journalctl -u hermes-webdesktop-backend -n 50",
])
def test_commands_that_do_not_stop_a_unit_are_untouched(command):
    """Read-only inspection and `start` never kill a running agent."""
    assert not detect(command)[0]


# --- spelling variants ---------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "sudo systemctl restart hermes-gateway",
    "systemctl --user stop hermes-gateway",
    "env FOO=1 systemctl restart hermes-gateway",
    "timeout 60 sudo systemctl stop hermes-gateway",
    "HERMES_HOME=/tmp systemctl restart hermes-gateway",
    "systemctl --signal SIGKILL kill hermes-gateway",
    "systemctl restart 'hermes-gateway'",
    "echo ok && systemctl restart hermes-gateway",
])
def test_spelling_variants_reach_the_same_verdict(command):
    """A differently-spelled command must not walk past the guard."""
    assert detect(command)[0]


def test_a_value_option_is_not_mistaken_for_the_verb():
    """`--type service` must not read as the verb and mask the real one."""
    assert detect("systemctl --type service restart hermes-gateway")[0]
    assert not detect("systemctl --type service list-units hermes-gateway")[0]


def test_kill_signal_operand_is_not_treated_as_a_target():
    """In `kill -9 <pid>` the 9 is the signal; treating it as a pid would misclassify."""
    assert not detect("kill -9 5669")[0]      # 5669 = ssh, out of scope
    assert detect("kill -9 880")[0]           # 880 = hermes backend, in scope


# --- integration with the real detector ----------------------------------------------------

def test_guard_is_reachable_through_the_real_dangerous_command_detector():
    """The guard must be wired into the path terminal execution actually consults."""
    with patch("tools.hermes_service_guard._unit_for_pid", resolve):
        is_dangerous, key, _ = detect_dangerous_command("sudo -n kill -TERM 880")
    assert is_dangerous
    assert "hermes-webdesktop-backend.service" in key


def test_guard_never_raises_on_malformed_input():
    """Throwing inside the approval path would fail OPEN — the opposite of this guard's job."""
    for command in ("", "kill -", "systemctl", "kill 'unterminated", "systemctl --signal"):
        assert detect(command)[0] is False


def test_worker_scopes_count_as_hermes_units():
    """A worker must not be able to signal a sibling worker's transient scope either."""
    assert is_hermes_unit("hermes-worker-kanban-t_abc123-run-7.scope")
    assert not is_hermes_unit("ssh.service")


# --- interactive vs non-interactive divergence ---------------------------------------------
# The whole point of the card: the same command must reach OPPOSITE outcomes depending on
# whether a human can answer. These drive the real approval gate, not the detector alone.

KILL_THE_FLEET = "sudo -n kill -TERM 880"


@pytest.fixture
def approval_env(monkeypatch):
    """Neutralize host leakage so the gate's own branching is what the test observes."""
    import tools.approval as approval_mod
    from tools import approval_context

    monkeypatch.setattr(approval_mod, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "smart")
    monkeypatch.setenv("HERMES_SESSION_KEY", "test-service-guard")
    for var in ("HERMES_CRON_SESSION", "HERMES_GATEWAY_SESSION", "HERMES_INTERACTIVE",
                "HERMES_EXEC_ASK", "HERMES_SINGLE_QUERY_SESSION", "HERMES_SESSION_PLATFORM"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_non_interactive_worker_is_refused_and_never_auto_approved(approval_env):
    """A headless worker must NOT execute and must NOT be auto-approved.

    This is the regression the incident produced: the worker reached the fleet-killing
    outcome because nothing stopped an unanswerable-prompt session from proceeding.
    """
    from tools.approval import check_all_command_guards

    approval_env.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    with patch("tools.hermes_service_guard._unit_for_pid", resolve):
        result = check_all_command_guards(KILL_THE_FLEET, "local")

    assert result["approved"] is False
    # Actionable enough to hand to kanban_block(kind='capability').
    assert "hermes-webdesktop-backend.service" in result["message"]
    assert not result.get("smart_approved")


def test_interactive_session_still_gets_a_prompt_and_can_proceed(approval_env):
    """No regression for human-driven restarts: the prompt is still offered and honored."""
    from tools.approval import check_all_command_guards

    approval_env.setenv("HERMES_INTERACTIVE", "1")
    asked = []

    def approve(command, description, **kwargs):
        asked.append(description)
        return "once"

    with patch("tools.hermes_service_guard._unit_for_pid", resolve), \
            patch("tools.approval.prompt_dangerous_approval", side_effect=approve):
        result = check_all_command_guards(KILL_THE_FLEET, "local")

    assert asked, "an interactive session must still be ASKED, not silently refused"
    assert result["approved"] is True


def test_out_of_scope_service_command_is_unaffected_by_interactivity(approval_env):
    """A non-Hermes target must not be dragged into the fail-closed path by this guard."""
    from tools.approval import check_all_command_guards

    approval_env.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    with patch("tools.hermes_service_guard._unit_for_pid", resolve):
        result = check_all_command_guards("kill -TERM 5669", "local")

    assert result["approved"] is True
