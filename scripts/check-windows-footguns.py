#!/usr/bin/env python3
"""
Grep-based checker for Windows cross-platform footguns.

Flags common patterns that break silently on Windows. Run before PRs —
cheap, fast, catches regressions in a codebase that runs on three OSes.

Covers Python (hermes_cli/, gateway/, tools/, cron/, agent/, plugins/,
scripts/, acp_adapter/) AND the Electron/TS desktop app (apps/desktop/{src,
electron,scripts}, *.ts/*.tsx/*.mjs) with a separate, smaller JS/TS
ruleset — the two rule sets never cross-apply (a Python-only rule never
scans a .ts file and vice versa).

Usage:
    # Scan staged changes (default when run from a git checkout)
    python scripts/check-windows-footguns.py

    # Scan the full tree (full-repo audit)
    python scripts/check-windows-footguns.py --all

    # Scan a specific file or directory
    python scripts/check-windows-footguns.py path/to/file.py path/to/dir/

    # Scan only modified files vs. main
    python scripts/check-windows-footguns.py --diff main

Exit status:
    0 — no Windows footguns found (or all matches suppressed)
    1 — at least one unsuppressed match

Suppress an intentional use (e.g. tests or platform-gated code) with:
    os.kill(pid, 0)  # windows-footgun: ok — only called on POSIX

The JS/TS ruleset uses the same marker in JS comment style:
    spawn('bash', args)  // windows-footgun: ok — guarded by isPosix at call site
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent

SUPPRESS_MARKER = re.compile(r"(?:#|//)\s*windows-footgun\s*:\s*ok\b", re.IGNORECASE)

# Line-level guard hints. If a line contains any of these tokens, we assume
# the programmer wrote the line in full awareness of the Windows pitfall —
# e.g. `if hasattr(os, 'setsid'): ... os.setsid()`, or the classic
# `getattr(signal, 'SIGKILL', signal.SIGTERM)`, or `shutil.which("wmic")`.
# False negatives are fine here — the inline `# windows-footgun: ok`
# suppression marker is still the authoritative suppression. This is just to
# reduce the noise floor on obviously-guarded lines so the signal-to-noise
# stays useful. Shared across the Python and JS/TS rulesets — the JS-side
# tokens (process.platform checks, IS_WINDOWS, isPosix) are additive and
# never match Python source.
GUARD_HINTS = (
    "hasattr(os,",
    "hasattr(signal,",
    "getattr(os,",
    "getattr(signal,",
    "shutil.which(",
    "if platform.system() != \"Windows\"",
    "if platform.system() != 'Windows'",
    "if sys.platform == \"win32\"",
    "if sys.platform != \"win32\"",
    "if sys.platform == 'win32'",
    "if sys.platform != 'win32'",
    "IS_WINDOWS",
    "is_windows",
    # JS/TS guard idioms.
    "process.platform === 'win32'",
    'process.platform === "win32"',
    "process.platform !== 'win32'",
    'process.platform !== "win32"',
    "isPosix",
    "isWindows",
)

# Dirs we never scan.
EXCLUDED_DIRS = {
    ".git",
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "site-packages",
    "website/build",
    "optional-skills",  # external skills
}

# File globs we never scan (beyond the dirs above).
EXCLUDED_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".so",
    ".dll",
    ".exe",
    ".png",
    ".jpg",
    ".gif",
    ".ico",
    ".svg",
    ".mp4",
    ".mp3",
    ".wav",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".whl",
    ".lock",
    ".min.js",
    ".min.css",
}

# Files we never scan (self-referential — this script mentions the
# patterns it detects — and the CONTRIBUTING docs that list them).
EXCLUDED_FILES = {
    "scripts/check-windows-footguns.py",
    "CONTRIBUTING.md",
}


@dataclass
class Footgun:
    """A Windows cross-platform footgun pattern."""

    name: str
    pattern: re.Pattern
    message: str
    fix: str
    # If set, matches in files/paths containing any of these substrings are
    # silently ignored (e.g. tests that legitimately exercise the footgun
    # behind a platform guard). Prefer `# windows-footgun: ok` inline
    # suppression over this list; only use path_allowlist for whole files
    # that are inherently tests of the footgun itself.
    path_allowlist: tuple[str, ...] = ()
    # Optional post-match predicate. Takes the re.Match and returns True
    # if the match is a REAL footgun (not a false positive). Use this when
    # the regex can't fully distinguish (e.g. open() where mode may contain
    # "b" for binary, or the line may have `encoding=` elsewhere).
    post_filter: "callable | None" = None
    # Optional multi-line context check for footguns where the mitigation
    # lives on a NEARBY line rather than the same one (e.g. an fs.watch()
    # error handler attached a few lines below the call, or a ctrlKey check
    # split across a multi-condition if-statement). Takes (all_lines,
    # match_line_idx) where all_lines is the full file split on '\n' and
    # match_line_idx is the 0-based index of the matched line. Return False
    # to suppress (mitigation found nearby), True to keep the match flagged.
    # Only used by the JS/TS ruleset so far — the line-based Python rules
    # haven't needed it.
    context_check: "callable | None" = None


FOOTGUNS: list[Footgun] = [
    Footgun(
        name="open() without encoding= on text mode",
        # Match builtins.open() specifically — NOT os.open(), .open()
        # method calls (Path.open, tarfile.open, zf.open, webbrowser.open,
        # Image.open, wave.open, etc), or `async def open()` method
        # definitions.  The pattern requires a start-of-identifier boundary
        # before `open(` so `os.open`, `.open`, `def open` are all skipped.
        # Note: Path.open() is ALSO affected by the encoding default, but
        # rather than flagging all `.open(` (huge noise), we require an
        # explicit builtins-style open() call.  Path.open() is rare in the
        # codebase compared to open() and can be audited separately.
        pattern=re.compile(
            r"""(?:^|[\s\(,;=])(?<![.\w])open\s*\(\s*[^,)]+\s*(?:,\s*['"](?P<mode>[^'"]*)['"])?"""
        ),
        message=(
            "open() without an explicit encoding= uses the platform default "
            "(UTF-8 on POSIX, cp1252/mbcs on Windows) — files round-tripped "
            "between hosts get mojibake. Always pass encoding='utf-8' for "
            "text files, or use open(path, 'rb')/'wb' for binary."
        ),
        fix=(
            "open(path, 'r', encoding='utf-8')  # or 'utf-8-sig' if the "
            "file may have a BOM"
        ),
        # Filter: only flag if mode is missing-or-text AND the line doesn't
        # already pass encoding=. Skip binary mode (contains "b").
        post_filter=lambda m, line: (
            "b" not in (m.group("mode") or "")
            and "encoding=" not in line
            and "encoding =" not in line
            # Skip `def open(` and `async def open(` (method definitions)
            and not line.lstrip().startswith("def ")
            and not line.lstrip().startswith("async def ")
            # Skip open(path, **kwargs) patterns — encoding may be in the dict.
            # Too expensive to trace; require the author to set encoding in
            # the dict and trust them (or they can add a # windows-footgun: ok).
            and "**" not in line
        ),
    ),
    Footgun(
        name="os.fdopen() without encoding= on text mode",
        # ruff PLW1514 covers builtins.open/Path.read_text/write_text/
        # Path.open but NOT os.fdopen — a bare text-mode fdopen still
        # decodes/encodes with the locale default (cp1252 on Windows).
        # This is the exact hole the July 2026 encoding sweep kept
        # re-fixing by hand (PRs #56033/#56940/#65565), so gate it here.
        pattern=re.compile(
            r"""(?:os\s*\.\s*)?\bfdopen\s*\(\s*[^,)]+\s*(?:,\s*['"](?P<mode>[^'"]*)['"])?"""
        ),
        message=(
            "os.fdopen() without an explicit encoding= uses the platform "
            "default (cp1252/mbcs on Windows) in text mode — the same "
            "mojibake class as bare open(). ruff PLW1514 does not cover "
            "fdopen, so this checker is the only gate."
        ),
        fix=(
            "os.fdopen(fd, 'w', encoding='utf-8')  # or mode 'wb' for binary"
        ),
        post_filter=lambda m, line: (
            "b" not in (m.group("mode") or "")
            and "encoding=" not in line
            and "encoding =" not in line
            and "**" not in line
        ),
    ),
    Footgun(
        name="os.kill(pid, 0)",
        pattern=re.compile(r"\bos\.kill\s*\(\s*[^,]+,\s*0\s*\)"),
        message=(
            "os.kill(pid, 0) is NOT a no-op on Windows — it sends "
            "CTRL_C_EVENT to the target's console process group, "
            "hard-killing the target and potentially unrelated siblings. "
            "See bpo-14484."
        ),
        fix=(
            "Use psutil.pid_exists(pid) (psutil is a core dependency). "
            "Or gateway.status._pid_exists(pid) for the hermes wrapper "
            "with a stdlib fallback."
        ),
    ),
    Footgun(
        name="bare os.setsid",
        pattern=re.compile(r"(?<!hasattr\()\bos\.setsid\b"),
        message=(
            "os.setsid does not exist on Windows and raises "
            "AttributeError. Subprocesses that need detachment on "
            "Windows use creationflags instead."
        ),
        fix=(
            "if platform.system() != 'Windows':\n"
            "    kwargs['preexec_fn'] = os.setsid\n"
            "else:\n"
            "    kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP"
        ),
    ),
    Footgun(
        name="bare os.killpg",
        pattern=re.compile(r"\bos\.killpg\b"),
        message="os.killpg does not exist on Windows.",
        fix=(
            "Use psutil for cross-platform process-tree kill:\n"
            "  p = psutil.Process(pid)\n"
            "  for c in p.children(recursive=True): c.kill()\n"
            "  p.kill()"
        ),
    ),
    Footgun(
        name="bare os.getuid / os.geteuid / os.getgid",
        pattern=re.compile(r"\bos\.(?:getuid|geteuid|getgid|getegid)\b"),
        message=(
            "os.getuid / os.geteuid / os.getgid do not exist on Windows "
            "and raise AttributeError at import time if referenced."
        ),
        fix=(
            "Use getpass.getuser() for the username, or gate with "
            "hasattr(os, 'getuid')."
        ),
    ),
    Footgun(
        name="bare os.fork",
        pattern=re.compile(r"(?<!hasattr\()\bos\.fork\s*\("),
        message="os.fork does not exist on Windows.",
        fix=(
            "Use subprocess.Popen for daemonization, or guard with "
            "hasattr(os, 'fork') and a Windows fallback path."
        ),
    ),
    Footgun(
        name="bare signal.SIGKILL",
        pattern=re.compile(r"\bsignal\.SIGKILL\b"),
        message=(
            "signal.SIGKILL does not exist on Windows and raises "
            "AttributeError at import time."
        ),
        fix="Use getattr(signal, 'SIGKILL', signal.SIGTERM).",
    ),
    Footgun(
        name="bare signal.SIGHUP / SIGUSR1 / SIGUSR2 / SIGALRM / SIGCHLD / SIGPIPE / SIGQUIT",
        pattern=re.compile(
            r"\bsignal\.(?:SIGHUP|SIGUSR1|SIGUSR2|SIGALRM|SIGCHLD|SIGPIPE|SIGQUIT)\b"
        ),
        message=(
            "These POSIX signals don't exist on Windows; referencing "
            "them raises AttributeError at import time."
        ),
        fix=(
            "Use getattr(signal, 'SIGXXX', None) and check for None "
            "before using, or gate the whole block behind a platform check."
        ),
    ),
    Footgun(
        name="subprocess shebang script invocation",
        pattern=re.compile(
            r"subprocess\.(?:run|Popen|call|check_output|check_call)\s*\(\s*\[\s*['\"]\./"
        ),
        message=(
            "Running a script via './scriptname' doesn't work on Windows — "
            "shebang lines aren't honored. CreateProcessW can't execute "
            "bash/python scripts without an explicit interpreter."
        ),
        fix="Use [sys.executable, 'scriptname.py', ...] explicitly.",
    ),
    Footgun(
        name="wmic invocation without shutil.which guard",
        # Match wmic appearing as a subprocess argument — NOT the
        # shutil.which("wmic") guard pattern itself. Looks for wmic in a
        # list or as first arg of subprocess.run/Popen.
        pattern=re.compile(
            r"""(?:subprocess\.\w+\s*\(\s*\[\s*['"]wmic['"]|['"]wmic\.exe['"])"""
        ),
        message=(
            "wmic was removed in Windows 10 21H1 and later. Always "
            "gate with shutil.which('wmic') and fall back to "
            "PowerShell (Get-CimInstance Win32_Process)."
        ),
        fix=(
            "if shutil.which('wmic'):\n"
            "    ... wmic path ...\n"
            "else:\n"
            "    subprocess.run(['powershell', '-NoProfile', '-Command',\n"
            "                    'Get-CimInstance Win32_Process | ...'])"
        ),
    ),
    Footgun(
        name="hardcoded ~/Desktop (OneDrive trap)",
        pattern=re.compile(
            r"""['"](?:~|~/|[A-Z]:[/\\]Users[/\\][^/\\'"]+[/\\])Desktop\b"""
        ),
        message=(
            "When OneDrive Backup is enabled on Windows, the real Desktop "
            "is at %USERPROFILE%\\OneDrive\\Desktop, not %USERPROFILE%\\"
            "Desktop (which exists as an empty husk)."
        ),
        fix=(
            "On Windows, resolve via ctypes + SHGetKnownFolderPath, or "
            "read the Shell Folders registry key, or run PowerShell "
            "[Environment]::GetFolderPath('Desktop')."
        ),
    ),
    Footgun(
        name="asyncio add_signal_handler without try/except",
        pattern=re.compile(r"\.add_signal_handler\s*\("),
        message=(
            "loop.add_signal_handler raises NotImplementedError on "
            "Windows — always wrap in try/except or gate with a "
            "platform check."
        ),
        fix=(
            "try:\n"
            "    loop.add_signal_handler(sig, handler, sig)\n"
            "except NotImplementedError:\n"
            "    pass  # Windows asyncio doesn't support signal handlers"
        ),
    ),
    Footgun(
        name="subprocess text=True without explicit encoding=",
        # Match ``text=True`` (or ``text = True``) anywhere on a line. We
        # rely on the post_filter to (a) skip lines that already pass
        # ``encoding=`` on the same line, and (b) skip false positives like
        # ``def text(self, ...)`` or string literals. ``text=True`` is
        # overwhelmingly a subprocess kwarg, so a bare match + filter has a
        # high signal-to-noise ratio and avoids the complexity of parsing
        # multi-line subprocess calls (which the line-based scanner can't
        # reliably attribute to a single line anyway).
        pattern=re.compile(r"\btext\s*=\s*True\b"),
        message=(
            "subprocess text=True without explicit encoding= decodes "
            "child output with locale.getpreferredencoding() — cp936 "
            "(GBK) on Chinese Windows, cp1252 on Western Windows — "
            "which crashes _readerthread with UnicodeDecodeError on "
            "non-default-codepage bytes. Always pass encoding='utf-8' "
            "(and errors='replace' for Windows-native CLIs that emit "
            "non-UTF-8). See issues #47939, #53428, #57238."
        ),
        fix=(
            "subprocess.run(..., text=True, encoding='utf-8', "
            "errors='replace')\n"
            "Both params are required: encoding alone still crashes on "
            "non-UTF-8 bytes from Windows-native CLIs (tasklist, "
            "schtasks)."
        ),
        post_filter=lambda m, line: (
            # Skip if the same line already specifies encoding=.
            "encoding=" not in line
            and "encoding =" not in line
            # Skip method definitions named ``text`` (def text(self, ...)).
            and not line.lstrip().startswith("def ")
            and not line.lstrip().startswith("async def ")
            # Skip ``text=True`` inside string literals (heuristic: the
            # substring appears between matching quotes that aren't part
            # of an f-string expression). This is imperfect but catches
            # the common case of docstrings mentioning text=True.
            and not _looks_like_string_literal(line, m)
            # Skip lines that are obviously not subprocess calls — e.g.
            # DataFrame.rename(text=True) or similar. We can't know for
            # sure without parsing, so we accept some false negatives by
            # only flagging when ``subprocess`` or a known subprocess-
            # shaped call (run/Popen/call/check_output/check_call/
            # check_output) appears on the same line. This keeps the
            # rule focused on the actual footgun.
            and _is_likely_subprocess_call(line)
        ),
    ),
    Footgun(
        name="bare Path.read_text()/write_text() without encoding=",
        # Match ``.read_text(`` / ``.write_text(`` when the same line does
        # not pass ``encoding=``. Multi-line calls where encoding= sits on
        # a later line are handled by the post_filter's lookahead-free
        # heuristic accepting a small false-negative rate — the AST guard
        # test in tests/gateway/test_gateway_utf8_encoding.py catches the
        # gateway/adapters exactly, and this rule catches the common
        # single-line form everywhere else.
        pattern=re.compile(r"\.(read_text|write_text)\s*\("),
        message=(
            "Path.read_text()/write_text() without encoding= uses "
            "locale.getpreferredencoding() — cp936/cp1252 on Windows — "
            "so UTF-8 content (config JSON, session state, skills) "
            "crashes with UnicodeDecodeError or writes mojibake. "
            "See issue #37423 and the #71014 / read_text campaign."
        ),
        fix='path.read_text(encoding="utf-8") / path.write_text(data, encoding="utf-8")',
        post_filter=lambda m, line: (
            "encoding=" not in line
            and "encoding =" not in line
            and not _looks_like_string_literal(line, m)
            # Skip calls that continue onto the next line — if the call's
            # own closing paren isn't on this line, encoding= may follow
            # on a later line. Balance parens from the call opener instead
            # of requiring the line to END with ``)`` so chained forms like
            # ``read_text()[:4000]`` / ``read_text().splitlines()`` are
            # still caught. AST-level enforcement for multi-line calls
            # lives in the gateway guard test.
            and _call_closes_on_line(line, m.end())
        ),
    ),
]


# ---------------------------------------------------------------------------
# JS/TS ruleset — apps/desktop/{src,electron,scripts} (Electron + renderer).
#
# The Python ruleset above only ever sees hermes_cli/gateway/tools/etc — the
# entire Electron/TS desktop app was a blind spot (should_scan_file only
# accepted .py/.pyw/.pyi). These rules target the JS-side footgun classes an
# audit found that the Python rules structurally cannot catch: fs.watch()
# without an error handler (unhandled 'error' on an EventEmitter throws —
# Windows raises EPERM on a deleted/renamed watched dir), '\n'-only line
# splitting on child process output (CRLF on Windows leaves a trailing '\r'),
# process.env.HOME (Windows sets USERPROFILE, not HOME), 'darwin' ternaries
# that silently assume "anything else is POSIX", template-literal path joins
# with a bare '/' (breaks on backslash-separated Windows paths — though NOT
# on URLs, which correctly always use '/'), spawn('bash'/'sh') without a
# platform guard, and metaKey-only keyboard shortcuts with no ctrlKey
# fallback (Windows/Linux have no Cmd key).
#
# Same architecture as the Python rules: regex + optional post_filter/
# context_check, `// windows-footgun: ok` suppression (SUPPRESS_MARKER
# accepts both # and // — see above), GUARD_HINTS shared with the Python
# rules (isPosix/isWindows/process.platform checks apply here too).
# ---------------------------------------------------------------------------


def _lines_in_window(all_lines: list[str], start: int, end: int) -> str:
    """Join all_lines[start:end] (both clamped to valid range) into one
    blob for a cheap substring/regex search across a multi-line window."""
    start = max(0, start)
    end = min(len(all_lines), end)
    return "\n".join(all_lines[start:end])


def _fs_watch_has_nearby_error_handler(all_lines: list[str], idx: int) -> bool:
    """True (keep flagged) unless a '.on('error'...)' or the repo's
    guardWatcherErrors() helper appears within the next 15 lines — the
    error handler is usually attached to the returned watcher a few
    statements after the fs.watch(...) call, never on the same line."""
    window = _lines_in_window(all_lines, idx, idx + 16)
    if re.search(r"\.on\(\s*['\"]error['\"]", window):
        return False
    if "guardWatcherErrors" in window:
        return False
    return True


def _metakey_has_nearby_ctrlkey(all_lines: list[str], idx: int) -> bool:
    """True (keep flagged) unless ctrlKey appears within 3 lines either
    side — catches the common case of a multi-condition if-statement
    where metaKey and ctrlKey land on different physical lines."""
    window = _lines_in_window(all_lines, idx - 3, idx + 4)
    return "ctrlKey" not in window


def _darwin_ternary_no_win32_guard_nearby(all_lines: list[str], idx: int) -> bool:
    """True (keep flagged) unless a 'win32' check appears shortly before
    this line — catches the common pattern where the actual Windows case
    is handled by an early return/branch a few lines above the ternary
    (e.g. `if (platform === 'win32') return X` followed by a plain
    `platform === 'darwin' ? Y : Z` that only needs to disambiguate
    mac/linux because win32 already left). A wider look-behind than
    look-ahead since the guard precedes the ternary in every real example
    found in this codebase."""
    window = _lines_in_window(all_lines, idx - 10, idx + 3)
    return "win32" not in window


JS_FOOTGUNS: list[Footgun] = [
    Footgun(
        name="fs.watch() without a nearby error handler",
        pattern=re.compile(r"\bfs\s*\.\s*watch\s*\("),
        message=(
            "fs.watch() with no '.on(\"error\", ...)' handler nearby: an "
            "unhandled 'error' event on an EventEmitter throws and crashes "
            "the whole Electron main process. Windows raises EPERM when the "
            "watched file/directory is deleted or renamed while watched — "
            "a routine user action (deleting a plugin folder, renaming a "
            "previewed file), not an edge case."
        ),
        fix=(
            "const watcher = fs.watch(dir, cb)\n"
            "watcher.on('error', (err) => { watcher.close(); /* log + \n"
            "  forget it instead of letting the throw take down main */ })"
        ),
        context_check=_fs_watch_has_nearby_error_handler,
    ),
    Footgun(
        name="split('\\n') on child process output",
        pattern=re.compile(r"""\.split\(\s*['"]\\n['"]\s*\)"""),
        message=(
            "Splitting child process stdout/stderr on a bare '\\n' leaves "
            "a trailing '\\r' on every line when the child writes "
            "Windows-native CRLF output (native tools like tasklist, "
            "reg.exe, schtasks, or any Windows batch/PowerShell script). "
            "Downstream string comparisons/parsing silently fail on the "
            "stray '\\r'."
        ),
        fix="text.split(/\\r?\\n/) — or .trimEnd() each line after split.",
        post_filter=lambda m, line: bool(
            re.search(r"\bstdout\b|\bstderr\b", line, re.IGNORECASE)
        ),
    ),
    Footgun(
        name="process.env.HOME",
        pattern=re.compile(r"\bprocess\.env\.HOME\b"),
        message=(
            "process.env.HOME is unset on Windows (Windows sets "
            "USERPROFILE, and HOMEDRIVE + HOMEPATH separately) — a bare "
            "read silently resolves to undefined instead of throwing, so "
            "the bug surfaces far from this line as a broken path."
        ),
        fix=(
            "Use app.getPath('home') (Electron, cross-platform) or "
            "os.homedir() (Node stdlib, cross-platform) instead of reading "
            "the env var directly."
        ),
    ),
    Footgun(
        name="'darwin' ternary lacking a win32 branch",
        pattern=re.compile(r"""\bplatform\s*===\s*['"]darwin['"]\s*\?"""),
        message=(
            "A `platform === 'darwin' ? X : Y` ternary treats every "
            "non-macOS platform as one bucket — Y silently has to be "
            "correct for BOTH Linux and Windows. That's often true for "
            "Linux and false for Windows (shell defaults, accelerator "
            "syntax, window-level APIs). Make the Windows case explicit."
        ),
        fix=(
            "platform === 'darwin' ? macValue\n"
            "  : platform === 'win32' ? winValue\n"
            "  : linuxValue"
        ),
        # Electron accelerator strings are the one legitimate exception:
        # 'Ctrl+...' is correct on BOTH win32 and Linux (that's what
        # CommandOrControl already encodes), so a darwin-only ternary
        # whose else-branch is an accelerator string isn't missing a case.
        post_filter=lambda m, line: "win32" not in line and "accelerator" not in line,
        context_check=_darwin_ternary_no_win32_guard_nearby,
    ),
    Footgun(
        name="template-literal filesystem path join with bare '/'",
        # Named group so the post_filter can inspect the interpolated
        # variable's name. Deliberately narrow to path-suggestive
        # identifiers (dir/path/home/root/cwd, case-insensitive, as a
        # substring) rather than every `${x}/` — the wider match would
        # flag URL joins (`${base}/api/...`) as often as real fs joins,
        # and '/' in a URL is correct on every platform.
        pattern=re.compile(r"\$\{(?P<var>[a-zA-Z_][a-zA-Z0-9_]*)\}/(?!/)"),
        message=(
            "Template-literal path join with a hardcoded '/' breaks when "
            "the interpolated value is a Windows path (backslash-"
            "separated) — string concatenation doesn't normalize "
            "separators the way path.join()/path.posix.join() do."
        ),
        fix="path.join(dir, rest) instead of `${dir}/${rest}`",
        path_allowlist=(
            ".test.",
            ".spec.",
            "/tests/",
            "/test/",
            # display-path.ts's entire job is producing DISPLAY strings that
            # are ALREADY forward-slash-normalized (normalizeDisplayPath
            # converts '\\' -> '/' first, per its own module docstring) —
            # every `${x}/` in that file operates on already-normalized
            # values for paint-only output, never a real fs join. Structural
            # exception for the whole file, not a one-off suppression.
            "src/lib/display-path.ts",
        ),
        # Skip URL/HTTP contexts: `request.pathname`/`.startsWith(...)` on
        # a `pathname`/`basePath`/`url` value is a URL path, which is
        # ALWAYS forward-slash per RFC 3986 regardless of host OS — not a
        # filesystem join, so path.join() would be the wrong fix there.
        post_filter=lambda m, line: (
            bool(re.search(r"(?i)dir|path|home|root|cwd", m.group("var")))
            and not re.search(r"(?i)pathname|basePath\b|\burl\b", line)
        ),
    ),
    Footgun(
        name="spawn('bash'|'sh') without a platform guard",
        pattern=re.compile(r"""\bspawn\(\s*['"](?:bash|sh)['"]"""),
        message=(
            "spawn('bash'/'sh', ...) assumes a POSIX shell is on PATH — "
            "Windows has neither by default (Git Bash is opt-in and not "
            "guaranteed). Gate on process.platform or dispatch to a "
            "PowerShell/cmd equivalent."
        ),
        fix=(
            "const isPosix = process.platform !== 'win32'\n"
            "await (isPosix ? spawnBash : spawnPowerShell)(scriptPath, args)"
        ),
    ),
    Footgun(
        name="metaKey without a ctrlKey fallback",
        pattern=re.compile(r"\.metaKey\b"),
        message=(
            "A keyboard/mouse shortcut gated on metaKey (Cmd) alone has no "
            "equivalent on Windows/Linux, which have no Cmd key — the "
            "shortcut is silently unreachable off macOS."
        ),
        fix="event.metaKey || event.ctrlKey  (or isMac ? event.metaKey : event.ctrlKey)",
        path_allowlist=(".test.", ".spec.", "/tests/", "/test/"),
        context_check=_metakey_has_nearby_ctrlkey,
    ),
]

JS_SUFFIXES = {".ts", ".tsx", ".mjs", ".mts", ".js", ".jsx"}


def should_scan_file(path: Path) -> bool:
    """Return True if this file is in scope for the checker."""
    # Skip the excluded dirs
    parts = set(path.parts)
    if parts & EXCLUDED_DIRS:
        return False
    # Skip excluded suffixes
    for suffix in EXCLUDED_SUFFIXES:
        if str(path).endswith(suffix):
            return False
    # Skip self and docs that intentionally mention the patterns
    rel = path.relative_to(REPO_ROOT).as_posix()
    if rel in EXCLUDED_FILES:
        return False
    # Python ruleset scope: unchanged from before this file's JS/TS rules.
    if path.suffix in {".py", ".pyw", ".pyi"}:
        return True
    # JS/TS ruleset scope: the Electron/TS desktop app only — NOT every
    # .ts file in the repo (apps/bootstrap-installer, apps/shared, and
    # tests-js/ are separate surfaces this checker doesn't own yet; adding
    # them is a follow-up, not scope creep on this one). Anchored on the
    # apps/desktop/{src,electron,scripts} prefix so a .ts file elsewhere
    # (e.g. a config script at the repo root) isn't silently pulled in.
    if path.suffix in JS_SUFFIXES and "apps/desktop" in rel:
        for prefix in ("apps/desktop/src/", "apps/desktop/electron/", "apps/desktop/scripts/"):
            if rel.startswith(prefix):
                return True
        return False
    # Other file types are read but no rule in either ruleset would match;
    # that's fine and cheap to skip.
    return False


def ruleset_for_file(path: Path) -> list[Footgun]:
    """Return the Footgun ruleset applicable to this file's language."""
    if path.suffix in JS_SUFFIXES:
        return JS_FOOTGUNS
    return FOOTGUNS


def iter_files(paths: Iterable[Path]) -> Iterable[Path]:
    for p in paths:
        if p.is_file():
            if should_scan_file(p):
                yield p
        elif p.is_dir():
            for root, dirs, files in os.walk(p):
                # prune excluded dirs in-place for speed
                dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
                for fname in files:
                    fpath = Path(root) / fname
                    if should_scan_file(fpath):
                        yield fpath


def _strip_code(line: str) -> str:
    """Return just the code portion of a line — strip trailing comments and
    skip lines that are entirely inside a string literal or comment.

    Heuristic only (we don't parse Python); good enough to avoid flagging
    our own `# ``os.kill(pid, 0)`` is NOT a no-op` docstring-style comments.
    """
    stripped = line.lstrip()
    # Line starts with # — entirely a comment.
    if stripped.startswith("#"):
        return ""
    # Remove trailing "# ..." inline comment. Naive — doesn't handle `#`
    # inside strings — but on balance reduces noise far more than it adds.
    hash_idx = _find_unquoted_hash(line)
    if hash_idx is not None:
        return line[:hash_idx]
    return line


def _find_unquoted_hash(line: str) -> int | None:
    """Index of the first `#` not inside a single/double/triple-quoted string.

    Simple state machine — good enough for the 99% case of "code, then
    optional trailing comment."
    """
    i = 0
    n = len(line)
    in_s = False  # single-quote string
    in_d = False  # double-quote string
    while i < n:
        c = line[i]
        if c == "\\" and (in_s or in_d) and i + 1 < n:
            i += 2
            continue
        if not in_d and c == "'":
            in_s = not in_s
        elif not in_s and c == '"':
            in_d = not in_d
        elif c == "#" and not in_s and not in_d:
            return i
        i += 1
    return None


def _find_unquoted_double_slash(line: str) -> int | None:
    """Index of the first `//` not inside a single/double/template-literal
    (backtick) string. JS/TS analog of ``_find_unquoted_hash`` — needed
    because ``_strip_code`` only knows Python's ``#`` comment syntax, and
    without this a JS comment merely MENTIONING ``fs.watch()`` in prose
    (e.g. "// main.ts owns the actual fs.watch() calls") gets scanned as
    if it were live code and false-positives the fs.watch rule.
    """
    i = 0
    n = len(line)
    in_s = False  # single-quote string
    in_d = False  # double-quote string
    in_t = False  # template-literal (backtick) string
    while i < n:
        c = line[i]
        if c == "\\" and (in_s or in_d or in_t) and i + 1 < n:
            i += 2
            continue
        if not in_d and not in_t and c == "'":
            in_s = not in_s
        elif not in_s and not in_t and c == '"':
            in_d = not in_d
        elif not in_s and not in_d and c == "`":
            in_t = not in_t
        elif c == "/" and i + 1 < n and line[i + 1] == "/" and not in_s and not in_d and not in_t:
            return i
        i += 1
    return None


def _strip_code_js(line: str) -> str:
    """JS/TS analog of ``_strip_code``: drop a whole-line ``//`` comment or
    a trailing ``// ...`` inline comment, respecting quote/template-literal
    state so a ``//`` inside a string (e.g. a URL) is never mistaken for a
    comment marker. Does not handle multi-line ``/* ... */`` block comments
    (rare in this codebase's call-site style) — an acceptable false-negative
    for a line-based scanner, same tradeoff the Python side makes for
    triple-quoted strings spanning awkward structures.
    """
    stripped = line.lstrip()
    if stripped.startswith("//"):
        return ""
    idx = _find_unquoted_double_slash(line)
    if idx is not None:
        return line[:idx]
    return line


# Subprocess method names that accept ``text=`` and are affected by the
# encoding-default footgun. Used by ``_is_likely_subprocess_call`` below to
# keep the ``text=True`` rule focused on subprocess calls (and avoid flagging
# unrelated APIs that happen to accept a ``text`` kwarg).
_SUBPROCESS_METHODS = (
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_output",
    "subprocess.check_call",
    "_sp.run",            # common alias
    "_sp.Popen",
    "_sp.check_output",
    "_sp.check_call",
    "_sp.call",
    ".run(",              # bare .run( — usually subprocess.run
    ".Popen(",
    ".check_output(",
    ".check_call(",
    ".call(",
)


def _is_likely_subprocess_call(line: str) -> bool:
    """Heuristic: does this line look like a subprocess invocation?

    The ``text=True`` footgun rule only fires when the matched line also
    contains a subprocess-shaped call site. This avoids false positives on
    unrelated APIs that accept a ``text`` kwarg (e.g. DataFrame.rename,
    custom library calls). Multi-line calls where the ``subprocess.X(``
    prefix is on a previous line won't be flagged — that's an acceptable
    false negative for a line-based scanner.
    """
    return any(token in line for token in _SUBPROCESS_METHODS)


def _call_closes_on_line(line: str, open_paren_end: int) -> bool:
    """True when the call whose ``(`` sits at ``open_paren_end - 1`` closes
    on this same line (paren-balance walk). Multi-line calls return False —
    the missing ``encoding=`` may sit on a continuation line, so the caller
    should skip them rather than false-positive."""
    depth = 1
    for ch in line[open_paren_end:]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return True
    return False


def _looks_like_string_literal(line: str, match: "re.Match") -> bool:
    """Heuristic: is the ``text=True`` match inside a string literal?

    Catches the common case of docstrings/comments that mention ``text=True``
    as prose. Walks the line tracking single/double quote state and returns
    True if the match start index falls inside a quoted region.
    """
    start = match.start()
    in_s = False
    in_d = False
    i = 0
    while i < start and i < len(line):
        c = line[i]
        if c == "\\" and (in_s or in_d) and i + 1 < len(line):
            i += 2
            continue
        if not in_d and c == "'":
            in_s = not in_s
        elif not in_s and c == '"':
            in_d = not in_d
        i += 1
    return in_s or in_d


def scan_file(path: Path, footguns: list[Footgun]) -> list[tuple[int, str, Footgun]]:
    """Return a list of (line_number, line, footgun) for unsuppressed matches."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    matches: list[tuple[int, str, Footgun]] = []
    all_lines = text.splitlines()
    is_js = path.suffix in JS_SUFFIXES

    # Track whether we're inside a triple-quoted string (docstring/raw block).
    # Simple state machine — handles both ''' and """, toggled by the FIRST
    # triple-quote we see; we don't try to handle nested or f-string cases.
    # Python-only: JS/TS has no triple-quote syntax, so this stays inert
    # there (skipped below) rather than risk a false match against JS
    # string literals that happen to contain the same characters.
    in_triple: str | None = None  # None, "'''", or '"""'

    for i, line in enumerate(all_lines, start=1):
        # Update triple-quote state based on this line's occurrences.
        code_for_scan = line
        if not is_js:
            if in_triple:
                # We're inside a docstring — skip the whole line's scan.
                # Check if it closes here.
                if in_triple in line:
                    # Find the closing delimiter; anything after it is real code.
                    after = line.split(in_triple, 1)[1]
                    in_triple = None
                    code_for_scan = after
                else:
                    continue
            # Now check for docstring-open in the (possibly after-triple) portion.
            # Scan for the first unescaped '''/""" in the current code_for_scan.
            for delim in ('"""', "'''"):
                if delim in code_for_scan:
                    # Count occurrences — even count means single-line docstring,
                    # odd means we've entered a multi-line one.
                    count = code_for_scan.count(delim)
                    if count % 2 == 1:
                        # Odd — we're now inside the triple-quoted block.
                        # Scan only the part BEFORE the opening delimiter.
                        before = code_for_scan.split(delim, 1)[0]
                        code_for_scan = before
                        in_triple = delim
                        break
                    else:
                        # Even — entire docstring fits on one line. Strip it
                        # from the scan text to avoid matching on prose.
                        parts = code_for_scan.split(delim)
                        # Keep the "outside" parts (every other chunk, starting
                        # with index 0) as code, drop the "inside" parts.
                        code_for_scan = "".join(parts[::2])
                        break

        if SUPPRESS_MARKER.search(line):
            continue
        # Skip if the line has an obvious guard — e.g. hasattr/getattr/
        # shutil.which or a platform check. False negatives are acceptable;
        # the inline suppression marker is the authoritative override.
        if any(hint in line for hint in GUARD_HINTS):
            continue
        code = _strip_code_js(code_for_scan) if is_js else _strip_code(code_for_scan)
        if not code.strip():
            continue
        for fg in footguns:
            if fg.path_allowlist and any(s in str(path) for s in fg.path_allowlist):
                continue
            match = fg.pattern.search(code)
            if not match:
                continue
            if fg.post_filter is not None:
                try:
                    if not fg.post_filter(match, line):
                        continue
                except (IndexError, AttributeError):
                    # Post-filter assumed a named group that isn't there — skip.
                    continue
            if fg.context_check is not None:
                # 0-based index of this line within all_lines (i is 1-based).
                if not fg.context_check(all_lines, i - 1):
                    continue
            matches.append((i, line.rstrip(), fg))
    return matches


def get_staged_files() -> list[Path]:
    """Return paths staged in the current git index. Empty on non-git trees."""
    try:
        out = subprocess.check_output(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', errors='replace',
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [REPO_ROOT / f for f in out.splitlines() if f.strip()]


def get_diff_files(ref: str) -> list[Path]:
    """Return paths modified vs. the given git ref."""
    try:
        out = subprocess.check_output(
            ["git", "diff", f"{ref}...HEAD", "--name-only", "--diff-filter=ACMR"],
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', errors='replace',
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [REPO_ROOT / f for f in out.splitlines() if f.strip()]


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Flag Windows cross-platform footguns in Python code and the "
            "Electron/TS desktop app."
        )
    )
    p.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Specific files/dirs to scan (default: staged changes).",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help=(
            "Scan the full repository (hermes_cli/, gateway/, tools/, "
            "cron/, etc. + apps/desktop/{src,electron,scripts})."
        ),
    )
    p.add_argument(
        "--diff",
        metavar="REF",
        help="Scan files changed vs. the given git ref (e.g. --diff main).",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="List all known footgun rules (both rulesets) and exit.",
    )
    return p.parse_args(argv)


def print_rules() -> None:
    print("Known Windows footguns checked by this script:\n")
    print("Python ruleset:\n")
    for i, fg in enumerate(FOOTGUNS, start=1):
        print(f"{i:2}. {fg.name}")
        print(f"    {fg.message}")
        print(f"    Fix: {fg.fix}")
        print()
    print("JS/TS ruleset (apps/desktop/{src,electron,scripts}):\n")
    for i, fg in enumerate(JS_FOOTGUNS, start=1):
        print(f"{i:2}. {fg.name}")
        print(f"    {fg.message}")
        print(f"    Fix: {fg.fix}")
        print()


def main(argv: list[str]) -> int:
    # Windows terminals default to cp1252, which can't encode the ✓/✗
    # characters used in the output. Reconfigure streams to UTF-8 so the
    # script works correctly on the very platform it is designed to help.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    args = parse_args(argv)

    if args.list:
        print_rules()
        return 0

    if args.all:
        # Scan main Python packages + scripts, plus the Electron/TS desktop
        # app (apps/desktop/{src,electron,scripts} — should_scan_file further
        # narrows this to those three subdirs so apps/desktop/dist,
        # apps/desktop/node_modules, apps/desktop/e2e etc. stay excluded).
        roots = [
            REPO_ROOT / "hermes_cli",
            REPO_ROOT / "gateway",
            REPO_ROOT / "tools",
            REPO_ROOT / "cron",
            REPO_ROOT / "agent",
            REPO_ROOT / "plugins",
            REPO_ROOT / "scripts",
            REPO_ROOT / "acp_adapter",
            REPO_ROOT / "apps" / "desktop" / "src",
            REPO_ROOT / "apps" / "desktop" / "electron",
            REPO_ROOT / "apps" / "desktop" / "scripts",
        ]
        roots = [r for r in roots if r.exists()]
    elif args.diff:
        roots = get_diff_files(args.diff)
    elif args.paths:
        roots = [p.resolve() for p in args.paths]
    else:
        # Default: staged changes
        roots = get_staged_files()
        if not roots:
            print(
                "No staged files to scan. Pass --all for a full-repo scan, "
                "--diff <ref> for a range diff, or paths explicitly.",
                file=sys.stderr,
            )
            return 0

    total_matches = 0
    files_scanned = 0
    for path in iter_files(roots):
        files_scanned += 1
        matches = scan_file(path, ruleset_for_file(path))
        for lineno, line, fg in matches:
            rel = path.relative_to(REPO_ROOT).as_posix()
            print(f"{rel}:{lineno}: [{fg.name}]")
            print(f"    {line.strip()}")
            print(f"    — {fg.message}")
            print(f"    Fix: {fg.fix.splitlines()[0]}")
            print()
            total_matches += 1

    if total_matches:
        print(
            f"\n✗ {total_matches} Windows footgun(s) found across "
            f"{files_scanned} file(s) scanned.",
            file=sys.stderr,
        )
        print(
            "  If an individual match is a false positive or intentionally "
            "platform-gated, suppress it with `# windows-footgun: ok` (or "
            "`// windows-footgun: ok` in JS/TS) on the same line.\n"
            "  Run with --list to see all rules.",
            file=sys.stderr,
        )
        return 1

    print(
        f"✓ No Windows footguns found ({files_scanned} file(s) scanned)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
