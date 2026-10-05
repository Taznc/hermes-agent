"""Service and node lifecycle consent. This is fork-owned T1 policy for t_3c25b726.

A LIFECYCLE command stops, restarts or reloads a service unit, or reboots or shuts down the node.
Examples: ``systemctl [--user] stop|restart|reload|kill|disable|mask ...``, ``service X restart``,
``hermes gateway stop|restart``, ``reboot``, ``shutdown``, ``init 0|6``. Such a command:

* is never auto-approved. ``command_allowlist`` ("always"), session approvals, a smart-approval
  APPROVE, ``--yolo``, ``approvals.mode: off`` and a prepared Desktop batch decision cannot bypass it;
* ASKS for reboot and shutdown instead of hitting the hardline deny. Every other hardline rule is
  unchanged and still wins when the same command also matches it;
* runs only after a ONE-SHOT approval from a trusted operator channel. A ``session`` or ``always``
  answer is clamped to ``once``, nothing is persisted, and prompts are never coalesced;
* fails CLOSED when no trusted channel is attached. The deny names the command so the operator can
  run it themselves.

A trusted channel is an approval notifier registered in THIS process for the current session through
``tools.approval.register_gateway_notify``. Two callers register one: the messaging gateway
(``/approve`` or buttons, behind the adapter's user authorisation) and the TUI/Desktop gateway
(``approval.respond``). For a TUI/Desktop session every attached client must be an authenticated
WebSocket. A stdio client, such as a standalone ``hermes --tui`` that any process can drive under a
PTY, is not trusted. The registry lives only in process memory.
No argv, env var, PTY or typed "yes" can create an entry for a CLI, ``-q``, cron or Kanban worker
process. For that reason the CLI ``input()`` prompt is never accepted as consent. Sessions on
unattended platforms (api_server, webhook, msgraph_webhook), single-query sessions and cron sessions
are untrusted even when a notifier exists.

Post-drain restarts: the dev-era Kanban post-drain service restart
(``kanban.post_drain.service_restart_allowlist`` and its PTY consent prompt, t_f58cc7f8) is absent on
``next`` and is deliberately not ported, because its consent could be forged (``env -u
HERMES_KANBAN_TASK`` + a PTY + typing "yes"). A dispatcher-driven restart has no trusted channel, so
this gate would deny it anyway.

Scope: this is a command-approval rule, not a sandbox. Actions that never reach the terminal guard
are out of scope, just as they are for every other approval rule: ``execute_code`` subprocesses,
the body of a script file, D-Bus clients, and ``kill`` of a unit's PID.

Anchors (``lifecycle-consent``): ``tools.approval.check_all_command_guards`` (runtime) and
``hermes_cli.approvals_test.evaluate_command`` (the dry-run verdict).
"""

from __future__ import annotations

import logging
import re
import uuid

logger = logging.getLogger("hermes_fork.lifecycle_consent")

PATTERN_KEY = "lifecycle:service-or-node"

# Upstream classifications reused by description so the patterns are not copied: the systemctl
# stop/restart rule, hermes gateway/launchd/update rules, Windows service stops (dangerous list),
# and the reboot/shutdown family (hardline list, which this policy turns into an ASK).
_UPSTREAM_DANGEROUS = frozenset({
    "stop/restart system service",
    "stop/restart hermes gateway (kills running agents)",
    "stop/restart hermes launchd service (kills running agents)",
    "hermes update (restarts gateway, kills running agents)",
    "force stop service (Stop-Service -Force)",
    "stop/delete service (sc)",
})
REBOOT_HARDLINE = frozenset({
    "system shutdown/reboot",
    "init 0/6 (shutdown/reboot)",
    "systemctl poweroff/reboot",
    "telinit 0/6 (shutdown/reboot)",
})
_SPLICED_GATEWAY = "stop/restart hermes gateway via shell-spliced verb (kills running agents)"

# Verbs the upstream rule misses (reload, try-restart, kill, isolate, ...). Option groups may carry
# one operand (``-M host``, ``--signal KILL``).
_OPTS = r"(?:-{1,2}[^\s]+\s+(?:[^-\s][^\s]*\s+)?)*"
_EXTRA = [
    (re.compile(r"\bsystemctl\s+" + _OPTS + r"(?:reload|force-reload|try-restart|condrestart|reload-or-restart"
                r"|try-reload-or-restart|kill|isolate|soft-reboot|rescue|emergency|default)\b", re.I | re.S),
     "stop/restart/reload system service (systemctl)"),
    (re.compile(r"(?:^|[\n;&|(`{]|\$\()\s*(?:sudo\s+" + _OPTS + r")?(?:\S*/)?(?:service|invoke-rc\.d|rc-service)\s+"
                r"[^\s;&|]+\s+(?:stop|restart|reload|force-reload|try-restart|condrestart)\b", re.I | re.S),
     "stop/restart/reload system service (service)"),
]


def _det():
    from tools import approval_detection
    return approval_detection


def classify(command: str) -> str | None:
    """Return the lifecycle description of *command*, or None.

    The scan covers the same de-obfuscated variants the upstream detectors evaluate, so ``bash -c``
    payloads, ``$(...)`` and quote/escape splicing are covered and quoted prose is not."""
    det = _det()
    if not command or det._command_parser_limit_exceeded(command):
        return None  # the upstream floor already fails closed on parser-limit input
    for variant in det._command_detection_variants(command):
        variant = variant or ""
        # Quoted prose (`git commit -m "systemctl restart x"`) is data unless a shell carrier
        # (sh -c, eval, source) executes it; this is the upstream rule for positionless hardline patterns.
        if not det._contains_shell_carrier(variant):
            variant = det._mask_quoted_prose(variant)
        low, flags_low = variant.lower(), det._lower_preserving_flags(variant)
        for rx, desc, _quote_masked in det.HARDLINE_PATTERNS_COMPILED:
            if desc in REBOOT_HARDLINE and rx.search(low):
                return desc
        for rx, desc in det.DANGEROUS_PATTERNS_COMPILED:
            if desc in _UPSTREAM_DANGEROUS and rx.search(flags_low):
                return desc
        for rx, desc in _EXTRA:
            if rx.search(low):
                return desc
    if det._is_shell_token_spliced_gateway_lifecycle(command):
        return _SPLICED_GATEWAY
    return _classify_tokens(command)


_WRAPPERS = frozenset({"sudo", "doas", "env", "exec", "nohup", "setsid", "time", "nice", "ionice", "command",
                       "builtin", "timeout", "stdbuf", "chroot", "runuser", "su", "xargs"})
_NODE_CMDS = frozenset({"reboot", "shutdown", "halt", "poweroff"})
_SYSTEMCTL_VERBS = frozenset({"stop", "restart", "reload", "force-reload", "try-restart", "condrestart",
                              "reload-or-restart", "try-reload-or-restart", "kill", "disable", "mask", "isolate",
                              "reboot", "poweroff", "halt", "kexec", "soft-reboot", "rescue", "emergency",
                              "default"})
_SERVICE_VERBS = frozenset({"stop", "restart", "reload", "force-reload", "try-restart", "condrestart"})


def _classify_tokens(command: str) -> str | None:
    """Shell-token pass that removes quote splicing: ``systemctl re"start" x`` and ``'reboot'``.

    The regex pass reads raw text, so a verb split by quotes slips past it. This pass splits each
    simple command with shlex (which removes the quotes), skips wrappers such as sudo and env, and
    checks argv[0] and the arguments. Unparsable input returns None and is left to the upstream rules."""
    import os
    import shlex
    try:
        lex = shlex.shlex(_det()._normalize_command_for_detection(command), posix=True, punctuation_chars=";&|()`")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return None
    segment: list[str] = []
    for tok in tokens + [";"]:
        if tok and set(tok) <= set(";&|()`"):
            if (desc := _classify_argv(segment, os.path.basename)) is not None:
                return desc
            segment = []
        else:
            segment.append(tok)
    return None


def _classify_argv(argv: list[str], basename) -> str | None:
    # A `-c` / `--command` payload (su -c, sh -c, runuser -c) is a command line of its own.
    for flag, payload in zip(argv, argv[1:]):
        if flag in ("-c", "--command") and (desc := _classify_tokens(payload)) is not None:
            return desc
    i, wrapper = 0, None
    while i < len(argv):
        tok, base = argv[i], basename(argv[i])
        if base in _WRAPPERS:
            wrapper, i = base, i + 1
        elif tok.startswith("-") or "=" in tok:
            # Wrapper options with a separate operand: sudo -u root, nice -n 5, runuser -u x.
            i += 2 if (wrapper and tok in _WRAPPER_VALUE_OPTS.get(wrapper, ())) else 1
        elif wrapper == "timeout" and tok[:1].isdigit():
            i += 1  # the timeout duration
        else:
            break
    if i >= len(argv):
        return None
    name, args = basename(argv[i]).lower(), [a.lower() for a in argv[i + 1:] if not a.startswith("-")]
    if name in _NODE_CMDS or (name in {"init", "telinit"} and args[:1] in (["0"], ["6"])):
        return "system shutdown/reboot"
    if name == "systemctl" and any(a in _SYSTEMCTL_VERBS for a in args):
        return "stop/restart/reload system service (systemctl)"
    if name in {"service", "invoke-rc.d", "rc-service"} and any(a in _SERVICE_VERBS for a in args[1:]):
        return "stop/restart/reload system service (service)"
    return None


_WRAPPER_VALUE_OPTS = {
    "sudo": ("-u", "-g", "-h", "-p", "-r", "-t", "-U", "-C", "-D", "-R", "-T"),
    "doas": ("-u", "-C"), "runuser": ("-u", "-g", "-G"), "su": ("-g", "-G", "-s"),
    "nice": ("-n",), "ionice": ("-c", "-n", "-p"), "timeout": ("-s", "-k"), "env": ("-u", "-C", "-S"),
}


def _other_hardline(command: str) -> str | None:
    """Run ``detect_hardline_command`` without the reboot/shutdown rules. Any hit means the upstream
    floor must keep blocking the command."""
    det = _det()
    if det._command_parser_limit_exceeded(command):
        return det._PARSER_LIMIT_DESCRIPTION
    if det._grep_safe_detection_variant(det._mask_quoted_newlines(command))[1]:
        return det._MALFORMED_EXEC_DESCRIPTION
    for variant in det._command_detection_variants(command):
        variant = variant or ""
        low, masked = variant.lower(), None
        for rx, desc, quote_masked in det.HARDLINE_PATTERNS_COMPILED:
            if desc in REBOOT_HARDLINE:
                continue
            if quote_masked and masked is None:
                masked = low if det._contains_shell_carrier(variant) else det._mask_quoted_prose(variant).lower()
            if rx.search(masked if quote_masked else low):
                return desc
    return None


def _floor_owned(command: str) -> bool:
    """True when an unconditional upstream floor other than reboot applies. Upstream then blocks."""
    from agent.runtime_self_protection import command_deletes_runtime
    return bool(_other_hardline(command) or command_deletes_runtime(command)
                or _det()._check_sudo_stdin_guard(command)[0])


_CONSENT = ("requires a one-shot operator approval over a trusted channel (gateway chat or Hermes "
            "Desktop). command_allowlist, session/always approvals, smart approval, --yolo and "
            "approvals.mode=off cannot bypass it.")


def dry_run_verdict(command: str):
    """``(verdict, rule, detail)`` for ``hermes approvals test``. Returns None to defer to upstream:
    the command is not a lifecycle command, or a non-reboot floor rule applies."""
    desc = classify(command)
    if desc is None or _floor_owned(command):
        return None
    from tools import approval_floors
    if (deny := approval_floors._match_user_deny_rule(command)) is not None:
        return ("user-deny", deny, "matches a user-defined approvals.deny rule in config.yaml "
                                   "(blocked even under --yolo / mode=off)")
    return ("ask-approval", desc, f"service/node lifecycle action: {_CONSENT} Execution is DENIED when no "
                                  "trusted channel is attached (CLI prompt, -q, cron, Kanban worker).")


def _untrusted_reason(notify_cb, session_key: str) -> str | None:
    from tools import approval_context as ctx
    if ctx._is_single_query_approval_context():
        return "this is a single-query (-q) session (approvals.single_query_mode does not apply to lifecycle actions)"
    if ctx._is_cron_approval_context():
        return "this is a cron session (approvals.cron_mode does not apply to lifecycle actions)"
    if ctx._is_unattended_platform_approval_context():
        return (f"this session runs on an unattended platform ({ctx._get_session_platform()}); "
                "approvals.unattended_mode does not apply to lifecycle actions")
    if notify_cb is None:
        return ("no gateway or Desktop approval channel is attached to this session; CLI prompts, "
                "Kanban workers and env flags cannot approve lifecycle actions")
    return _tui_transport_problem(session_key)


def _tui_transport_problem(session_key: str) -> str | None:
    """TUI-gateway sessions: the approval must come back over an authenticated WebSocket.

    The TUI gateway also serves stdio clients (standalone ``hermes --tui``). Any process can start one
    under a PTY and answer its prompt, so stdio is not proof of an operator. Every attached transport
    must be a ``WSTransport``; ``_ws_auth_reason`` admitted those with a dashboard credential. The check
    is skipped when the notifier belongs to the messaging gateway or api_server (no TUI session for the
    key). Under ``dashboard.turn_isolation`` the turn's transport is the compute-host pipe, so lifecycle
    commands there fail closed."""
    import sys
    server = sys.modules.get("tui_gateway.server")
    if server is None:
        return None
    try:
        session = server._session_for_key(session_key)
    except Exception:
        return "the TUI session could not be inspected"
    if session is None:
        return None
    transport = session.get("transport")
    from tui_gateway.transport import FanoutTransport
    from tui_gateway.ws import WSTransport
    if isinstance(transport, FanoutTransport):
        with transport._lock:
            peers = [p.transport for p in transport._peers if p.attached]
    else:
        peers = [transport]
    if peers and all(isinstance(p, WSTransport) for p in peers):
        return None
    return ("this session's client is attached over stdio or a pipe (terminal TUI or compute host), "
            "and that cannot prove an operator")


def guard(command: str, env_type: str, has_host_access: bool = False) -> dict | None:
    """Runtime decision for ``check_all_command_guards``. Returns None when *command* is not a
    lifecycle command, or when an unconditional non-reboot floor owns it. Otherwise returns the
    final approval dict."""
    desc = classify(command)
    if desc is None or _floor_owned(command):
        return None
    from tools import approval as A
    if (deny := A._user_deny_block(command)) is not None:
        return deny
    # A Desktop batch may have prepared a decision for this exact call. It is accepted only when this
    # gate produced it (a one-shot operator answer). Any other prepared decision may come from a bypass
    # that is valid for other commands, so it is discarded and the command prompts live.
    from agent.terminal_approval_batch import consume_prepared_guard
    prepared = consume_prepared_guard(command, env_type, has_host_access)
    if prepared is not None and prepared.get(_MARK) is True:
        return prepared
    result = _decide(command, desc)
    result[_MARK] = True
    return result


_MARK = "lifecycle_consent"


def _decide(command: str, desc: str) -> dict:
    from tools import approval as A
    from tools import approval_context as ctx
    session_key = ctx.get_current_session_key()
    notify_cb = A._gateway_notify_cb(session_key)
    why = _untrusted_reason(notify_cb, session_key)
    if why is not None:
        logger.warning("Lifecycle command denied (%s): %s", why, command[:200])
        return A._denied(
            f"BLOCKED: {desc}. Lifecycle actions (stop/restart/reload a service, reboot/shutdown) "
            f"{_CONSENT} No trusted channel is available because {why}. Do NOT retry, rephrase or "
            f"work around this. If the action is genuinely needed, ask the operator to run it: {command}",
            pattern_key=PATTERN_KEY, description=desc, outcome="blocked")

    findings = [desc]
    is_dangerous, _key, other = _det().detect_dangerous_command(command)
    if is_dangerous and other != desc:
        findings.append(other)
    tirith = A._tirith_scan(command)
    if tirith.get("action") in {"block", "warn"}:
        findings.append(A._format_tirith_description(tirith))
    description = "; ".join(findings) + " [lifecycle: one-shot approval only]"
    # The per-request nonce key keeps two identical prompts from coalescing, so each run needs its
    # own approval.
    data = {"command": command, "description": description, "pattern_key": PATTERN_KEY,
            "pattern_keys": [PATTERN_KEY, f"{PATTERN_KEY}:{uuid.uuid4().hex}"],
            "allow_session": False, "allow_permanent": False}
    from tools.approval_gateway_wait import _await_gateway_decision
    decision = _await_gateway_decision(session_key, notify_cb, data)
    choice = decision.get("choice")
    common = {"pattern_key": PATTERN_KEY, "description": description}
    if decision.get("notify_failed"):
        return A._denied("BLOCKED: the lifecycle approval request could not be delivered. Do NOT retry.",
                         outcome="notify_failed", **common)
    if decision.get("cancelled"):
        return A._denied(f"BLOCKED: the lifecycle approval prompt was withdrawn ({decision['cancelled']}). "
                         "Do NOT retry.", outcome="cancelled", **common)
    if not decision.get("resolved"):
        return A._denied("BLOCKED: lifecycle approval timed out without operator response. Silence is not "
                         "consent. Do NOT retry.", outcome="timeout", **common)
    if choice not in ("once", "session", "always"):
        reason = f" Reason: {decision['reason']}" if decision.get("reason") else ""
        return A._denied(f"BLOCKED: the operator denied this lifecycle action.{reason} Do NOT retry, rephrase "
                         "or work around it.", outcome="denied", **common)
    if choice != "once":
        logger.info("Lifecycle approval scope %r clamped to once (nothing persisted): %s", choice, command[:200])
    return A._user_approved(session_key, description)
