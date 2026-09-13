"""Detect commands that would stop a Hermes-managed service and kill the running fleet.

Restarting ``hermes-webdesktop-backend``/``hermes-gateway``/``hermes-webui`` tears down
every agent those units supervise, including the kanban worker issuing the command. The
flat ``DANGEROUS_PATTERNS`` rules in :mod:`tools.approval_detection` catch the obvious
spelling (``systemctl restart <unit>``), but a determined agent reaches the same outcome
through spellings those regexes never see:

* ``systemctl kill``/``try-restart``/``reload-or-restart`` — real lifecycle verbs absent
  from the ``stop|restart|disable|mask`` rule.
* ``kill -TERM <pid>`` where ``<pid>`` is a Hermes unit's ``MainPID``. A bare number
  carries no dangerous keyword at all, so no text pattern can classify it. This is the
  spelling that actually took the fleet down on 2026-09-05: blocked from
  ``systemctl restart hermes-webdesktop-backend``, a worker ran
  ``sudo -n kill -TERM 880`` against that unit's MainPID and systemd's ``Restart=``
  finished the job.

The guard therefore RESOLVES its target instead of matching its spelling: a numeric pid
is mapped through ``/proc/<pid>/cgroup`` to the systemd unit that owns it. Whatever number
the command names, the guard asks the kernel what that number is.

Findings are ordinary dangerous-command findings, not hardline blocks: an interactive
human still gets the normal approval prompt (restarting a service is legitimate work),
while a non-interactive session fails closed through the existing unattended-context path.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Callable, Iterable, Optional

from tools.approval_detection import (
    _deobfuscate_shell_word_for_detection, _iter_shell_command_starts,
    _literal_command_substitution_output, _read_shell_word)

logger = logging.getLogger("tools.approval")

# systemctl verbs that STOP the current main process of a unit. `start`, `status`, `show`,
# `cat`, `is-active` and `daemon-reload` are deliberately absent: they never kill an agent.
# `disable`/`mask` do not stop a running unit by themselves but remove its ability to come
# back, which is the same operational outcome one boot later.
_SERVICE_STOPPING_VERBS = frozenset({
    "stop", "restart", "kill", "try-restart", "reload-or-restart", "try-reload-or-restart",
    "force-reload", "disable", "mask",
})

# systemctl options that consume the FOLLOWING token, so a value is never mistaken for the
# verb (`systemctl --signal SIGKILL kill hermes-gateway`).
_SYSTEMCTL_VALUE_OPTIONS = frozenset({
    "-H", "--host", "-M", "--machine", "-t", "--type", "--state", "--property", "-p",
    "--signal", "-s", "--kill-whom", "--kill-who", "--job-mode", "--root", "--what",
})

# Wrappers that hand execution to their argument tail; without peeling them
# `sudo systemctl restart hermes-gateway` presents `sudo` as the executable.
#
# `then`/`do`/`else`/`elif` are shell COMPOUND-STATEMENT keywords, not programs, but they sit
# in the executable slot exactly like a wrapper does: `if true; then kill -9 <pid>; fi` puts
# `then` where `_command_parts` looks for the command word, and the walk stops there unless
# it is peeled the same way `sudo`/`env` are. `case`'s `WORD)` pattern-close is peeled
# separately by `_peel_case_pattern` (its syntax isn't a single leading word). `xargs` hands
# its stdin-derived argument list to the command in its own argument tail
# (`echo <pid> | xargs kill -9`), so it is peeled the same way; see `_preceding_pipe_source_pid`
# for how the piped-in pid itself is recovered.
_TRANSPARENT_PREFIXES = frozenset({
    "sudo", "doas", "env", "nohup", "setsid", "nice", "ionice", "stdbuf", "timeout",
    "exec", "command", "builtin", "eatmydata", "pkexec", "su", "runuser", "setpriv",
    "systemd-run", "nsenter", "unshare", "then", "do", "else", "elif", "xargs",
})

# Wrapper options consuming the next token (same rationale as _SYSTEMCTL_VALUE_OPTIONS).
_PREFIX_VALUE_OPTIONS = {
    "sudo": frozenset({"-u", "-g", "-U", "-C", "-p", "-r", "-t", "-T", "--user", "--group", "--prompt"}),
    "doas": frozenset({"-u", "-C"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "-n", "-p", "--class", "--classdata"}),
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "su": frozenset({"-s", "--shell", "-g", "--group"}),
    "runuser": frozenset({"-u", "--user", "-s", "--shell", "-g", "--group"}),
    "systemd-run": frozenset({"-u", "--unit", "-p", "--property", "-E", "--setenv", "--slice", "--uid", "--gid"}),
    "nsenter": frozenset({"-t", "--target", "-S", "--setuid", "-G", "--setgid", "-r", "--root", "-w", "--wd"}),
    "xargs": frozenset({
        "-I", "-L", "-l", "-n", "-P", "-s", "-a", "-d", "-E", "--replace", "--max-lines",
        "--max-args", "--max-procs", "--arg-file", "--delimiter", "--eof",
    }),
}
# Wrappers whose first non-option operand is a VALUE, not the command (`timeout 60 systemctl ...`).
_PREFIX_OPERANDS = {"timeout": 1}

_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# `kill -s KILL`, `kill --signal=TERM`; an attached `-9`/`-TERM` is handled positionally.
_KILL_SIGNAL_VALUE_OPTIONS = frozenset({"-s", "--signal", "-n"})
_NUMERIC_TARGET_RE = re.compile(r"^-?\d+$")
_MAX_PREFIX_PEELS = 8
# A pathological word run must not spin the walk.
_MAX_WORDS_PER_COMMAND = 64

_PidUnitResolver = Callable[[int], Optional[str]]


def _executable_name(token: str) -> str:
    """Command name of an executable token, case-folded and without a Windows suffix."""
    return Path(token.replace("\\", "/")).name.removesuffix(".exe").lower() or token.lower()


def is_hermes_unit(unit: str) -> bool:
    """True for a systemd unit/scope whose death takes Hermes agents with it.

    Covers the supervising services (``hermes-gateway.service``,
    ``hermes-webdesktop-backend.service``, ``hermes-webui.service``) and the transient
    ``hermes-worker-*.scope`` units kanban workers run in, so one worker cannot signal a
    sibling worker's scope either. Deliberately prefix-based rather than an enumerated
    unit list: the fleet grows new ``hermes-*`` units regularly and an allowlist of names
    would silently stop covering them.
    """
    return _executable_name(unit).startswith("hermes")


def _unit_for_pid(pid: int) -> Optional[str]:
    """systemd unit/scope owning ``pid``, read from ``/proc/<pid>/cgroup``; None if unknown.

    Reads the cgroup path rather than shelling out to ``systemctl``: no subprocess in the
    approval hot path, and it works for transient scopes systemd would not list as units.
    """
    try:
        with open(f"/proc/{int(pid)}/cgroup", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except (OSError, ValueError, OverflowError):
        return None
    for line in reversed(content.splitlines()):
        # cgroup v2: "0::/system.slice/hermes-gateway.service"; v1 lines share the shape.
        path = line.rpartition(":")[2]
        for component in reversed(path.split("/")):
            if component.endswith((".service", ".scope")):
                return component
    return None


def _shell_words_at(command: str, start: int) -> list[str]:
    """Deobfuscated words of the simple command at ``start`` (stops at a newline)."""
    words: list[str] = []
    cursor = start
    for _ in range(_MAX_WORDS_PER_COMMAND):
        word_start, word_end, raw_word = _read_shell_word(command, cursor)
        if word_start == word_end or (words and "\n" in command[cursor:word_start]):
            break
        words.append(_deobfuscate_shell_word_for_detection(raw_word))
        cursor = word_end
    return words


def _peel_prefixes(words: list[str], index: int) -> tuple[int, bool]:
    """Index of the command a wrapper chain actually executes, plus whether ``xargs`` was
    one of the peeled wrappers (its target arrives via stdin, not its own argument tail)."""
    saw_xargs = False
    for _ in range(_MAX_PREFIX_PEELS):
        if index >= len(words):
            return index, saw_xargs
        name = _executable_name(words[index])
        if name not in _TRANSPARENT_PREFIXES:
            return index, saw_xargs
        saw_xargs = saw_xargs or name == "xargs"
        value_options = _PREFIX_VALUE_OPTIONS.get(name, frozenset())
        index += 1
        while index < len(words):
            token = words[index]
            if token == "--":
                index += 1
                break
            if token in value_options:
                index += 2
                continue
            if token.startswith("-") or _ENV_ASSIGNMENT_RE.match(token):
                index += 1
                continue
            break
        for _ in range(_PREFIX_OPERANDS.get(name, 0)):
            if index < len(words) and not words[index].startswith("-"):
                index += 1
    return index, saw_xargs


def _command_parts(words: list[str]) -> tuple[Optional[str], list[str], bool]:
    """Split leading ``VAR=value`` assignments and wrappers off -> (executable, args, saw_xargs)."""
    index = 0
    while index < len(words) and _ENV_ASSIGNMENT_RE.match(words[index]):
        index += 1
    index, saw_xargs = _peel_prefixes(words, index)
    if index >= len(words):
        return None, [], saw_xargs
    return words[index], words[index + 1:], saw_xargs


def _systemctl_units(args: list[str]) -> tuple[Optional[str], list[str]]:
    """Parse ``systemctl`` args -> (verb, unit operands). Options are skipped, and the ones
    that carry a separate value consume it so a value never reads as the verb."""
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            index += 1
            break
        if not token.startswith("-"):
            break
        # `--signal=SIGKILL` carries its value inline; `--signal SIGKILL` consumes the next token.
        index += 2 if "=" not in token and token in _SYSTEMCTL_VALUE_OPTIONS else 1
    if index >= len(args):
        return None, []
    return args[index].lower(), [arg for arg in args[index + 1:] if not arg.startswith("-")]


def _kill_target_pids(args: list[str]) -> list[int]:
    """Numeric pids/pgids a ``kill`` invocation would signal.

    A leading ``-9``/``-TERM`` is the signal, not a target; every later ``-<digits>`` is a
    process GROUP id. A group is resolved through its leader pid, which is the group id
    itself — enough to identify the unit being signalled.
    """
    pids: list[int] = []
    index = 0
    signal_seen = False
    while index < len(args):
        token = args[index]
        if token == "--":
            index += 1
            continue
        if token in _KILL_SIGNAL_VALUE_OPTIONS:
            signal_seen = True
            index += 2
            continue
        if token.startswith("-") and not signal_seen and not _NUMERIC_TARGET_RE.match(token[1:]):
            signal_seen = True  # `-TERM`, `-SIGKILL`, `--verbose`
            index += 1
            continue
        if _NUMERIC_TARGET_RE.match(token):
            value = abs(int(token))
            # `kill -9 <pid>`: the FIRST bare `-<digits>` is the signal number.
            if token.startswith("-") and not signal_seen:
                signal_seen = True
            elif value:
                pids.append(value)
            index += 1
            continue
        if token.startswith("-"):
            signal_seen = True
        index += 1
    return pids


def _preceding_pipe_stage_pids(command: str, start: int) -> list[int]:
    """Numeric pid ``xargs`` would append to its command, from the pipe stage before ``start``.

    ``echo <pid> | xargs kill -9`` never puts the pid in ``kill``'s own argument tail — xargs
    reads it from stdin and appends it. Only a literal, non-executing producer (``echo``/
    ``printf`` with a single simple literal argument, mirroring
    ``_literal_command_substitution_output``) is resolved; anything more dynamic (a pipeline
    reading real process state) yields no pid rather than guessing.
    """
    pipe_index = command.rfind("|", 0, start)
    if pipe_index == -1 or (pipe_index > 0 and command[pipe_index - 1] == "|"):
        return []
    earlier_starts = [s for s in _iter_shell_command_starts(command) if s <= pipe_index]
    stage_start = max(earlier_starts) if earlier_starts else 0
    stage_text = command[stage_start:pipe_index].strip()
    literal = _literal_command_substitution_output(stage_text)
    return [abs(int(literal))] if literal is not None and _NUMERIC_TARGET_RE.match(literal) else []


def _human_instruction(unit: str) -> str:
    return f"a human must run `sudo systemctl restart {unit}` in a terminal outside the agent"


def _iter_findings(command: str, resolve_pid_unit: _PidUnitResolver) -> Iterable[str]:
    """Yield a description for each in-scope Hermes-service-stopping command found."""
    for start in sorted(set(_iter_shell_command_starts(command))):
        executable, args, saw_xargs = _command_parts(_shell_words_at(command, start))
        if executable is None:
            continue
        name = _executable_name(executable)
        if name == "systemctl":
            verb, units = _systemctl_units(args)
            if verb not in _SERVICE_STOPPING_VERBS:
                continue
            for unit in units:
                if is_hermes_unit(unit):
                    yield (f"systemctl {verb} of Hermes service {unit} "
                           f"(kills the running agent fleet; {_human_instruction(unit)})")
        elif name == "kill":
            target_args = args + [str(pid) for pid in _preceding_pipe_stage_pids(command, start)] \
                if saw_xargs else args
            for pid in _kill_target_pids(target_args):
                unit = resolve_pid_unit(pid)
                if unit and is_hermes_unit(unit):
                    yield (f"signalling pid {pid}, which is Hermes service {unit} "
                           f"(kills the running agent fleet; {_human_instruction(unit)})")


def detect_hermes_service_stop(
    command: str, resolve_pid_unit: Optional[_PidUnitResolver] = None,
) -> tuple[bool, Optional[str]]:
    """-> (is_dangerous, description) for a command that would stop a Hermes service.

    ``resolve_pid_unit`` maps a pid to its owning systemd unit; injectable so tests can
    describe a process topology without spawning one. Never raises: a guard that throws
    inside the approval path would fail OPEN, which is the outcome this module exists to
    prevent.
    """
    if not command or os.name == "nt":
        # systemd is Linux-only; on Windows there is no unit to stop this way.
        return False, None
    resolver = resolve_pid_unit if resolve_pid_unit is not None else _unit_for_pid
    try:
        for description in _iter_findings(command, resolver):
            return True, description
    except Exception:
        logger.debug("hermes service guard failed to parse command", exc_info=True)
    return False, None
