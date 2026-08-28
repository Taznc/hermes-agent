"""Full-repo self-scan wrapper for scripts/check-windows-footguns.py.

scripts/check_subprocess_stdin.py has had a pytest wrapper (see
tests/tools/test_subprocess_stdin_guard.py's test_all_tui_subprocess_calls_
have_stdin) that runs the checker with its default full-scan behavior and
asserts a clean exit — so a normal pytest run of that file catches
regressions even when no one remembers to run the standalone script by hand.
check-windows-footguns.py had no equivalent: only a narrow rule-level test
(tests/scripts/test_footgun_subprocess_encoding.py, scoped to the
text=True/encoding= rule) existed, so a bare ``os.killpg``/``signal.SIGKILL``
regression (caught by CI running the real script with --all, not by any
local pytest run) shipped in the T1-T3 npx-agent-browser hardening commit
before anyone ran the script directly. This closes that gap the same way
the stdin guard already closes its equivalent one.

The Python ruleset scans clean on this tree (asserted unconditionally
below). The newer JS/TS ruleset (apps/desktop/{src,electron,scripts}) is a
genuine tooling gap fix: it surfaces real, pre-existing Windows footguns
in the current tree that predate this checker's JS/TS coverage and are
each individually triaged (see the card thread / commit message for the
full list — six fs.watch() sites needing error handlers, template-literal
path joins, CRLF-unsafe splits, etc.). Those are fixed by sibling cards,
not by this tooling card, so this test allowlists the exact known sites
by ``file:rule`` pair rather than suppressing them inline (suppression
markers would silence the checker for the callers who ARE about to fix
them). Any NEW match — a different file, a different rule, or a genuinely
new site — still fails the test, which is the regression-guard property
that matters.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check-windows-footguns.py"

# (relative file path, rule name) pairs that are KNOWN, TRIAGED true
# positives on the current tree as of the JS/TS ruleset's introduction.
# Each is a real pre-existing Windows footgun slated for a sibling fix
# card, not a checker false positive — do not add an entry here to
# silence a NEW finding; use an inline `// windows-footgun: ok` suppression
# instead if it's genuinely a false positive, or fix the underlying bug.
KNOWN_JS_TRUE_POSITIVES = {
    ("apps/desktop/src/lib/chat-runtime.ts", "template-literal filesystem path join with bare '/'"),
    ("apps/desktop/src/app/settings/plugins-settings.tsx", "template-literal filesystem path join with bare '/'"),
    # NOTE: right-sidebar/review/file-tree.tsx used to be listed here. The
    # Windows path-correctness card (fcc6e84153) routed that join through
    # path-compare.ts's cleanPath/comparisonPath, so the checker no longer
    # flags it. Both cards were in flight at once; this allowlist was written
    # against the pre-fix tree.
    ("apps/desktop/src/app/right-sidebar/files/ipc.ts", "template-literal filesystem path join with bare '/'"),
    ("apps/desktop/src/app/chat/composer/index.tsx", "metaKey without a ctrlKey fallback"),
    ("apps/desktop/src/store/coding-status.ts", "template-literal filesystem path join with bare '/'"),
    ("apps/desktop/electron/main.ts", "fs.watch() without a nearby error handler"),
    ("apps/desktop/electron/main.ts", "split('\\n') on child process output"),
    ("apps/desktop/electron/dev-backend-watch.real-loop.test.ts", "fs.watch() without a nearby error handler"),
    ("apps/desktop/electron/git-review-ops.ts", "split('\\n') on child process output"),
    ("apps/desktop/electron/bootstrap-runner.ts", "spawn('bash'|'sh') without a platform guard"),
    ("apps/desktop/scripts/perf/gateway_attach_bench.py", "bare Path.read_text()/write_text() without encoding="),
    ("apps/desktop/scripts/perf/scenarios/right-pane.mjs", "template-literal filesystem path join with bare '/'"),
}

MATCH_HEADER_RE = re.compile(r"^(\S+):\d+: \[(.+)\]$", re.MULTILINE)


def _run_checker() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--all"],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


def test_full_repo_scan_has_no_unsuppressed_windows_footguns():
    """Mirrors check_subprocess_stdin.py's wrapper: run the real checker
    against the whole repo (--all) and require every match to be one of
    the KNOWN_JS_TRUE_POSITIVES above — so this test file, not just
    institutional memory, is what catches the next bare os.killpg/
    signal.SIGKILL-style Python regression AND any new JS/TS footgun."""
    result = _run_checker()

    matches = {(file, rule) for file, rule in MATCH_HEADER_RE.findall(result.stdout)}
    unexpected = matches - KNOWN_JS_TRUE_POSITIVES
    missing = KNOWN_JS_TRUE_POSITIVES - matches

    assert not unexpected, (
        f"New/unexpected Windows footgun matches found (not in the known-"
        f"triaged allowlist): {sorted(unexpected)}\n"
        f"Full output:\n{result.stdout}\n{result.stderr}"
    )
    assert not missing, (
        f"Expected known true-positive matches are MISSING (fixed already? "
        f"update KNOWN_JS_TRUE_POSITIVES): {sorted(missing)}"
    )

