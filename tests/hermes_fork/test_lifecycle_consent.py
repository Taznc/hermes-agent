"""Lifecycle consent (``hermes_fork.lifecycle_consent``) through the ``lifecycle-consent`` FORK ANCHORs.

Every runtime case calls the REAL guard, ``tools.approval.check_all_command_guards``, and the real
dry-run, ``hermes approvals test``. Only the human side is stubbed: the notifier the gateway would
register, and the poll that would wait for the operator's click.
"""

from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli import approvals_test as at
from hermes_fork import lifecycle_consent as lc
from tools import approval as A
from tools import approval_context as ctx
from tools import approval_gateway_wait as wait_mod

SK = "lifecycle-test"
RESTART = "systemctl --user restart hermes-gateway-next"
LIFECYCLE = [
    RESTART, "sudo systemctl stop nginx", "systemctl reload nginx", "systemctl --user kill hermes-gateway-next",
    "service nginx restart", "hermes gateway restart", "reboot", "sudo reboot", "shutdown -h now",
    "systemctl reboot", "init 6", "bash -c 'systemctl --user stop x'", "systemctl re\"start\" x",
    "sudo -u root reboot", "su -c 'systemctl restart x'", "echo $(systemctl restart x)",
]
NOT_LIFECYCLE = ["ls -la", "systemctl status x", "systemctl --user start x", "echo 'please reboot later'",
                 "grep reboot /var/log/syslog", 'gh pr create --title "block (reboot) spellings"']
_ENV = ("HERMES_YOLO_MODE", "HERMES_INTERACTIVE", "HERMES_GATEWAY_SESSION", "HERMES_EXEC_ASK",
        "HERMES_CRON_SESSION", "HERMES_SINGLE_QUERY_SESSION", "HERMES_SESSION_PLATFORM", "HERMES_KANBAN_TASK")


@pytest.fixture
def env(monkeypatch):
    """Isolated approval state: manual mode, no yolo, empty allowlist, no persistence."""
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ctx, "_get_approval_config", lambda: {"mode": "manual"})
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(A, "load_permanent_allowlist", lambda: set())
    saved = set(A._permanent_set())
    A._permanent_set().clear()
    saves: list = []
    monkeypatch.setattr(A, "save_permanent_allowlist", lambda s: saves.append(set(s)))
    A._gateway_queues.clear()
    A._gateway_notify_cbs.clear()
    A._session_approved.pop(SK, None)
    A.disable_session_yolo(SK)
    token = ctx.set_current_session_key(SK)
    yield saves
    ctx.reset_current_session_key(token)
    A.disable_session_yolo(SK)
    A._session_approved.pop(SK, None)
    A._gateway_queues.clear()
    A._gateway_notify_cbs.clear()
    A._permanent_set().clear()
    A._permanent_set().update(saved)


@pytest.fixture
def operator(monkeypatch):
    """Register a trusted notifier and answer each prompt with the next scripted choice."""
    answers: list[str] = []
    prompts: list[dict] = []

    def notify(data):
        prompts.append(data)

    def poll(event, session_key, *, interrupt_log):
        choice = answers.pop(0)
        if choice == "timeout":
            return "timeout"
        assert A.resolve_gateway_approval(session_key, choice) == 1
        return "set"

    monkeypatch.setattr(wait_mod, "_poll_event", poll)
    monkeypatch.setattr(wait_mod._ctx, "_fire_approval_hook", lambda *a, **k: None)
    A.register_gateway_notify(SK, notify)
    return answers, prompts


def _guard(cmd=RESTART):
    return A.check_all_command_guards(cmd, "local")


def _dry(cmd):
    return at.evaluate_command(cmd)


# --- classification --------------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", LIFECYCLE)
def test_lifecycle_commands_are_classified(cmd):
    assert lc.classify(cmd)


@pytest.mark.parametrize("cmd", NOT_LIFECYCLE)
def test_non_lifecycle_commands_are_not(cmd):
    assert lc.classify(cmd) is None


# --- done criterion 1 + 2: dry-run says ASK, never allow and never hardline for reboot ----------------

@pytest.mark.parametrize("cmd", LIFECYCLE)
def test_dry_run_reports_ask(env, cmd):
    v = _dry(cmd)
    assert (v["verdict"], v["exit_code"]) == ("ask-approval", at.EXIT_ASK), v


def test_dry_run_ask_even_when_category_is_allowlisted(env, monkeypatch):
    A.load_permanent({"stop/restart system service", RESTART, "systemctl *"})
    monkeypatch.setattr(ctx, "_get_approval_config", lambda: {"mode": "smart"})
    A.enable_session_yolo(SK)
    v = _dry(RESTART)
    assert v["verdict"] == "ask-approval" and "one-shot" in v["detail"]


def test_cli_json_output(env, capsys):
    ns = argparse.Namespace(command_words=["--", RESTART], env_type="local", json=True)
    assert at.approvals_test_command(ns) == at.EXIT_ASK
    assert json.loads(capsys.readouterr().out)["verdict"] == "ask-approval"


@pytest.mark.parametrize("cmd", ["rm -rf /", "reboot; rm -rf /", "mkfs.ext4 /dev/sda1", ":(){ :|:& };:",
                                 "kill -9 -1", "dd if=/dev/zero of=/dev/sda"])
def test_other_hardlines_unchanged(env, cmd):
    assert _dry(cmd)["verdict"] == "hardline-deny"
    r = _guard(cmd)
    assert r["approved"] is False and r.get("hardline") is True


def test_user_deny_rule_still_wins(env, monkeypatch):
    from tools import approval_floors
    monkeypatch.setattr(approval_floors, "_match_user_deny_rule", lambda c: "systemctl *")
    assert _dry(RESTART)["verdict"] == "user-deny"
    assert _guard()["approved"] is False


# --- fail closed without a trusted channel ------------------------------------------------------------

def test_no_channel_denies(env):
    r = _guard()
    assert r["approved"] is False and r["pattern_key"] == lc.PATTERN_KEY
    assert RESTART in r["message"] and "Do NOT retry" in r["message"]


def test_reboot_without_channel_denies_not_hardline(env):
    r = _guard("sudo reboot")
    assert r["approved"] is False and not r.get("hardline")


def test_cli_prompt_is_not_consent(env, monkeypatch):
    """A forged PTY plus a typed 'yes' (HERMES_INTERACTIVE + a CLI callback answering always) is refused."""
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    calls = []
    r = A.check_all_command_guards(RESTART, "local", approval_callback=lambda *a, **k: calls.append(a) or "always")
    assert r["approved"] is False and calls == []


@pytest.mark.parametrize("var,val", [("HERMES_EXEC_ASK", "1"), ("HERMES_GATEWAY_SESSION", "1"),
                                     ("HERMES_SESSION_PLATFORM", "telegram"), ("HERMES_KANBAN_TASK", "")])
def test_forged_env_flags_are_not_a_channel(env, monkeypatch, var, val):
    monkeypatch.setenv(var, val)
    assert _guard()["approved"] is False


@pytest.mark.parametrize("var,val", [("HERMES_SINGLE_QUERY_SESSION", "1"), ("HERMES_CRON_SESSION", "1"),
                                     ("HERMES_SESSION_PLATFORM", "api_server")])
def test_unattended_contexts_deny_even_with_a_notifier(env, operator, monkeypatch, var, val):
    monkeypatch.setenv(var, val)
    answers, prompts = operator
    assert _guard()["approved"] is False and prompts == []


# --- bypasses that work for other dangerous commands must not work here --------------------------------

def test_allowlist_category_does_not_bypass(env):
    A.load_permanent({"stop/restart system service"})
    assert _guard()["approved"] is False


def test_allowlist_exact_and_glob_do_not_bypass(env):
    A.load_permanent({RESTART, "systemctl *", "reboot"})
    assert _guard()["approved"] is False
    assert _guard("reboot")["approved"] is False


def test_session_approval_does_not_bypass(env):
    A.approve_session(SK, "stop/restart system service")
    A.approve_session(SK, lc.PATTERN_KEY)
    assert _guard()["approved"] is False


def test_yolo_and_mode_off_do_not_bypass(env, monkeypatch):
    A.enable_session_yolo(SK)
    assert _guard()["approved"] is False
    A.disable_session_yolo(SK)
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", True)
    assert _guard()["approved"] is False
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(ctx, "_get_approval_config", lambda: {"mode": "off"})
    assert _guard()["approved"] is False


def test_smart_approve_does_not_bypass(env, operator, monkeypatch):
    import tools.approval_smart as smart
    monkeypatch.setattr(ctx, "_get_approval_config", lambda: {"mode": "smart"})
    monkeypatch.setattr(smart, "_smart_approve", lambda c, d: "approve")
    monkeypatch.setattr(A, "_smart_verdict", lambda *a, **k: "approve")
    answers, prompts = operator
    answers.append("deny")
    r = _guard()
    assert r["approved"] is False and len(prompts) == 1


def test_prepared_batch_decision_from_another_path_is_discarded(env, operator, monkeypatch):
    import agent.terminal_approval_batch as batch
    monkeypatch.setattr(batch, "consume_prepared_guard", lambda *a: {"approved": True, "message": None})
    answers, prompts = operator
    answers.append("deny")
    assert _guard()["approved"] is False and len(prompts) == 1


def test_prepared_decision_from_this_gate_is_used_once(env, monkeypatch):
    import agent.terminal_approval_batch as batch
    marked = {"approved": True, "message": None, "user_approved": True, lc._MARK: True}
    monkeypatch.setattr(batch, "consume_prepared_guard", lambda *a: marked)
    assert _guard() is marked


# --- done criterion 3: a trusted approval runs once and nothing persists --------------------------------

@pytest.mark.parametrize("choice", ["once", "session", "always"])
def test_trusted_approval_is_one_shot(env, operator, choice):
    saves = env
    answers, prompts = operator
    answers.extend([choice, "deny"])
    first = _guard()
    assert first["approved"] is True and first.get("user_approved") is True
    assert prompts[0]["allow_session"] is False and prompts[0]["allow_permanent"] is False
    assert saves == [] and not A._permanent_set() and not A._session_approved.get(SK)
    second = _guard()
    assert second["approved"] is False and len(prompts) == 2


def test_identical_prompts_never_coalesce(env, operator):
    answers, prompts = operator
    answers.extend(["once", "once"])
    assert _guard()["approved"] and _guard()["approved"]
    keys = [p["pattern_keys"][1] for p in prompts]
    assert len(set(keys)) == 2


@pytest.mark.parametrize("outcome", ["deny", "timeout"])
def test_deny_and_timeout_fail_closed(env, operator, outcome):
    answers, _ = operator
    answers.append(outcome)
    assert _guard()["approved"] is False


def test_reboot_asks_over_trusted_channel(env, operator):
    answers, prompts = operator
    answers.append("once")
    assert _guard("sudo reboot")["approved"] is True and "shutdown/reboot" in prompts[0]["description"]


def test_notify_failure_fails_closed(env, monkeypatch):
    def boom(_data):
        raise RuntimeError("socket gone")
    A.register_gateway_notify(SK, boom)
    assert _guard()["approved"] is False


# --- TUI transport trust ---------------------------------------------------------------------------

def test_tui_stdio_session_is_untrusted(env, operator, monkeypatch):
    import tui_gateway.server as server
    monkeypatch.setattr(server, "_session_for_key", lambda k: {"transport": server._stdio_transport})
    answers, prompts = operator
    assert _guard()["approved"] is False and prompts == []


def test_tui_websocket_session_is_trusted(env, operator, monkeypatch):
    import tui_gateway.server as server
    from tui_gateway.ws import WSTransport
    ws = WSTransport.__new__(WSTransport)
    monkeypatch.setattr(server, "_session_for_key", lambda k: {"transport": ws})
    answers, _ = operator
    answers.append("once")
    assert _guard()["approved"] is True


def test_tui_fanout_with_a_stdio_peer_is_untrusted(env, operator, monkeypatch):
    import tui_gateway.server as server
    from tui_gateway.transport import FanoutTransport
    from tui_gateway.ws import WSTransport
    ws = WSTransport.__new__(WSTransport)
    fan = FanoutTransport.__new__(FanoutTransport)
    import threading
    from tui_gateway.transport import _FanoutPeer
    fan._lock = threading.Lock()
    fan._peers = [_FanoutPeer(ws), _FanoutPeer(server._stdio_transport)]
    monkeypatch.setattr(server, "_session_for_key", lambda k: {"transport": fan})
    answers, prompts = operator
    assert _guard()["approved"] is False and prompts == []


# --- non-lifecycle behaviour is unchanged -------------------------------------------------------------

def test_other_dangerous_commands_still_honour_allowlist(env):
    A.load_permanent({"docker restart/stop/kill (container lifecycle)"})
    assert _guard("docker restart web")["approved"] is True


def test_container_backend_skip_unchanged(env):
    assert A.check_all_command_guards(RESTART, "docker")["approved"] is True
