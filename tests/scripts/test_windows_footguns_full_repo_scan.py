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
below). The JS/TS ruleset (apps/desktop/{src,electron,scripts}) previously
carried a KNOWN_JS_TRUE_POSITIVES allowlist of 12 real, pre-existing
Windows footguns that predated this checker's JS/TS coverage. All 12 were
fixed in commit 3016a2c1ca5cadb78423f57269d6e99fe94aa91c ("fix(desktop):
resolve Windows-footgun findings and map contributor emails", already on
dev): inline suppressions for the two already-normalized template-literal
joins, error handlers wired for the fs.watch() sites, CRLF-safe
split(/\r?\n/) in place of split('\n'), a ctrlKey fallback added alongside
metaKey, and a shared joinPath() helper replacing the remaining bare
${x}/ joins. --all now exits 0 on the full repo, so the allowlist is
empty. Any NEW match — a different file, a different rule, or a genuinely
new site — still fails the test, which is the regression-guard property
that matters. Do not repopulate this allowlist as a parking lot; fix the
footgun or suppress it inline with a comment naming the guard.
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
# Each entry here is a real pre-existing Windows footgun awaiting a
# sibling fix card, not a checker false positive — do not add an entry
# here to silence a NEW finding; use an inline `// windows-footgun: ok`
# suppression instead if it's genuinely a false positive, or fix the
# underlying bug.
#
# Empty as of commit 3016a2c1ca5cadb78423f57269d6e99fe94aa91c
# ("fix(desktop): resolve Windows-footgun findings and map contributor
# emails", already on origin/dev), which fixed the 12 entries that used
# to live here (apps/desktop/electron/bootstrap-runner.ts,
# dev-backend-watch.real-loop.test.ts, git-review-ops.ts, main.ts (x2),
# apps/desktop/scripts/perf/gateway_attach_bench.py,
# scripts/perf/scenarios/right-pane.mjs, apps/desktop/src/app/chat/
# composer/index.tsx, right-sidebar/files/ipc.ts, settings/
# plugins-settings.tsx, apps/desktop/src/lib/chat-runtime.ts,
# apps/desktop/src/store/coding-status.ts) via inline suppressions,
# nearby error handlers, CRLF-safe splits, a ctrlKey fallback, and the
# shared joinPath() helper. `--all` scans clean, so this stays empty
# until a genuinely new triaged true positive appears.
KNOWN_JS_TRUE_POSITIVES: set[tuple[str, str]] = set()

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

