#!/usr/bin/env python3
"""Mutation-test every guard in kanban_land.py that was written before its test.

The round-2 rewrite landed the B4-B9 fixes as one edit, so their tests were
written against working code. A test that never failed proves nothing, so each
guard is reverted here — one at a time, on a copy — and the test that is
supposed to catch it must go RED. A mutation that stays green is a vacuous test
and gets rewritten.

Usage: python3 mutate_land_guards.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] if (Path(__file__).resolve().parents[1] / "hermes_cli").is_dir() else Path.cwd()
IMPL = ROOT / "hermes_cli" / "kanban_land.py"
BACKUP = Path("/tmp/kanban_land.orig.py")
RUNNER = ROOT / "scripts" / "run_tests.sh"
TEST = "tests/hermes_cli/test_kanban_land.py"

# (label, [(old, new), ...], test selector that MUST fail)
MUTATIONS: list[tuple[str, list[tuple[str, str]], str]] = [
    (
        "B4: address the remote NAME instead of the resolved push URL",
        [
            (
                '    address = endpoint.url if endpoint is not None else remote',
                '    address = remote',
            ),
            (
                '    target_head = _remote_branch_sha(source.repo_root, endpoint.url, branch)',
                '    target_head = _remote_branch_sha(source.repo_root, remote, branch)',
            ),
            (
                '    landed_sha = _remote_branch_sha(source.repo_root, endpoint.url, branch)',
                '    landed_sha = _remote_branch_sha(source.repo_root, remote, branch)',
            ),
        ],
        "verifies_the_repository_it_actually_pushed_to",
    ),
    (
        "B4: accept any number of push URLs",
        [
            (
                '    if len(urls) > 1:\n        raise LandRefusal(\n            "remote_push_ambiguous",',
                '    if False:\n        raise LandRefusal(\n            "remote_push_ambiguous",',
            ),
        ],
        "several_push_urls",
    ),
    (
        "B5: stage from the ls-remote sha instead of a fetched ref",
        [
            (
                '    with fetched_ref(source.repo_root, endpoint.url, branch, purpose="target") as (\n'
                '        target_ref, fetched_head,\n'
                '    ):\n'
                '        with staged_checkout(source.repo_root, target_ref) as tree:',
                '    if True:\n'
                '        target_ref = target_head\n'
                '        with staged_checkout(source.repo_root, target_ref) as tree:',
            ),
        ],
        "foreign_clone",
    ),
    (
        "B6: run the configured verification command before the dry-run branch",
        [
            (
                '    if dry_run:\n'
                '        # Zero mutation',
                '    receipt = verify(conn, task_id, source, board=board, verdict=verdict)\n'
                '    if dry_run:\n'
                '        # Zero mutation',
            ),
            (
                '    receipt = verify(conn, task_id, source, board=board, verdict=verdict)\n\n'
                '    with fetched_ref(',
                '\n    with fetched_ref(',
            ),
        ],
        "does_not_execute_the_configured_verification_command",
    ),
    (
        "B7: use `git cherry` history equivalence instead of the current-tip tree",
        [
            (
                '        merged_tree = git(cwd, "merge-tree", "--write-tree", target_ref, source_sha, timeout=300)\n'
                '        target_tree = git(cwd, "rev-parse", f"{target_ref}^{{tree}}", timeout=60)\n'
                '    except GitError:',
                '        out = git(cwd, "cherry", target_ref, source_sha, timeout=120)\n'
                '        lines = [ln for ln in out.splitlines() if ln.strip()]\n'
                '        return bool(lines) and not any(ln.startswith("+") for ln in lines)\n'
                '    except GitError:',
            ),
        ],
        "applied_and_then_reverted",
    ),
    (
        "B8: rebuild the refusal target from the raw --target flag only",
        [
            (
                '    remote, branch = target if target else (None, None)',
                '    target = None\n    remote, branch = (None, None)',
            ),
        ],
        "board_configured_target_is_named_in_refusal_output",
    ),
    (
        "B9: record the ls-remote sha without proving it is the object fetched back",
        [
            (
                '        if readback_sha != landed_sha:',
                '        readback_sha = landed_sha\n        if False:',
            ),
        ],
        "receipt_records_one_coherent_landing_identity or moving_during_readback",
    ),
    (
        "B2: land without comparing the approved sha to what the branch publishes",
        [
            (
                '    if not _same_commit(verdict.approved_sha, source.sha):',
                '    if False:',
            ),
        ],
        "branch_moved_since_it_was_approved",
    ),
    (
        "B3: let a superseded approval stay live",
        [
            (
                '    if resubmitted is not None:\n        return None',
                '    if False:\n        return None',
            ),
        ],
        "stale_approval_cannot_authorize",
    ),
]

APPROVE_IMPL = ROOT / "hermes_cli" / "kanban_db_approve.py"
APPROVE_BACKUP = Path("/tmp/kanban_db_approve.orig.py")


def run_test(selector: str) -> bool:
    """True when the selected tests PASS."""
    proc = subprocess.run(
        [str(RUNNER), TEST, "-k", selector],
        cwd=ROOT, capture_output=True, text=True, timeout=1800,
        env={**__import__("os").environ,
             "HERMES_PYTHON": "/home/hermes/projects/hermes/hermes-agent-dev/.venv/bin/python",
             "HERMES_TEST_FILE_RETRIES": "0"},
    )
    return " 0 failed" in proc.stdout or "0 failed (100% complete)" in proc.stdout


def target_file(label: str) -> tuple[Path, Path]:
    if label.startswith("B3"):
        return APPROVE_IMPL, APPROVE_BACKUP
    return IMPL, BACKUP


def main() -> int:
    shutil.copy2(IMPL, BACKUP)
    shutil.copy2(APPROVE_IMPL, APPROVE_BACKUP)
    failures = []
    try:
        for label, edits, selector in MUTATIONS:
            path, backup = target_file(label)
            source = backup.read_text(encoding="utf-8")
            mutated = source
            for old, new in edits:
                if old not in mutated:
                    failures.append(f"{label}: PATTERN NOT FOUND -> {old[:70]!r}")
                    mutated = None
                    break
                mutated = mutated.replace(old, new, 1)
            if mutated is None:
                continue
            path.write_text(mutated, encoding="utf-8")
            passed = run_test(selector)
            path.write_text(source, encoding="utf-8")
            status = "VACUOUS (still green)" if passed else "ok (went RED)"
            print(f"[{status}] {label}  -k {selector}", flush=True)
            if passed:
                failures.append(f"{label}: test stayed green under the reversion")
    finally:
        shutil.copy2(BACKUP, IMPL)
        shutil.copy2(APPROVE_BACKUP, APPROVE_IMPL)

    print()
    if failures:
        print("MUTATION FAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"All {len(MUTATIONS)} guards proven non-vacuous.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
