"""Fork CI must not queue forever on NousResearch-only paid runners."""
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CHECKS_SCRIPT = ROOT / ".github" / "scripts" / "run-workspace-checks.mjs"
OWNER = "github.repository_owner == 'NousResearch'"
_NODE_ON_PATH = shutil.which("node") is not None
_requires_node = pytest.mark.skipif(not _NODE_ON_PATH, reason="requires node on PATH")


@pytest.mark.parametrize(
    "filename,large,standard",
    [
        ("tests.yml", "ubuntu-latest-96-core", "ubuntu-latest"),
        ("tests-os.yml", "windows-latest-32-core", "windows-latest"),
        ("js-tests.yml", "ubuntu-latest-32-core", "ubuntu-latest"),
        ("rust-tests.yml", "ubuntu-latest-32-core", "ubuntu-latest"),
        ("e2e-desktop.yml", "ubuntu-latest-32-core", "ubuntu-latest"),
        ("nix.yml", "ubuntu-latest-32-core", "ubuntu-latest"),
        ("docker.yml", "ubuntu-latest-32-core", "ubuntu-latest"),
        ("docker.yml", "ubuntu-latest-32-arm-core", "ubuntu-24.04-arm"),
    ],
)
def test_large_runner_labels_have_a_standard_fork_fallback(filename, large, standard):
    document = yaml.safe_load((WORKFLOWS / filename).read_text())
    expected = "${{ " + OWNER + " && '" + large + "' || '" + standard + "' }}"
    def values(value):
        if isinstance(value, dict):
            for child in value.values():
                yield from values(child)
        elif isinstance(value, list):
            for child in value:
                yield from values(child)
        elif isinstance(value, str):
            yield value
    runners = [value for value in values(document) if large in value]
    assert runners, f"No runner covered in {filename}"
    assert all(value == expected for value in runners)


def test_fork_python_parallelism_fits_standard_runner():
    workflow = yaml.safe_load((WORKFLOWS / "tests.yml").read_text())
    step = next(step for step in workflow["jobs"]["test"]["steps"] if step.get("name") == "Run tests")
    assert step["env"]["HERMES_TEST_WORKERS"] == "${{ " + OWNER + " && 96 || 2 }}"
    assert workflow["jobs"]["test"]["timeout-minutes"] == "${{ " + OWNER + " && 30 || 120 }}"
    # This is a runner-availability fix, not permission to skip or narrow tests.
    assert "scripts/run_tests.sh" in step["run"]
    assert "--files" not in step["run"]


def test_no_other_workflow_uses_an_unconditional_paid_runner():
    import re
    for path in WORKFLOWS.glob("*.yml"):
        for line in path.read_text().splitlines():
            if re.match(r"\s*(runs-on|runner):.*-\d+-(?:arm-)?core", line):
                assert OWNER in line, f"Unconditional paid runner: {path.name}: {line}"


# ---------------------------------------------------------------------------
# .github/scripts/run-workspace-checks.mjs — fork concurrency + failure output
# ---------------------------------------------------------------------------
#
# Standard (non-paid) fork runners have 4 cores. vitest/tsc/eslint each size
# their own worker pool from availableParallelism() independently, so running
# several check units at once multiplies that oversubscription — the exact
# contention PR14 hit for web and TUI. These tests build a tiny synthetic npm
# workspace (no real vitest/tsc involved) and run the actual script against
# it as a subprocess, asserting on its real stdout/exit behavior rather than
# re-deriving the logic by inspecting source text.


def _make_workspace(tmp_path, *, failing_lines=0):
    """A 3-unit npm workspace: a plain `check`, and `check:lint`/`check:unit`
    on a second package. Optionally a `packages/c` unit that prints
    `failing_lines` lines then exits non-zero, for the drain/truncation test.
    """
    (tmp_path / "packages" / "a").mkdir(parents=True)
    (tmp_path / "packages" / "b").mkdir(parents=True)
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "root", "private": True, "workspaces": ["packages/*"]})
    )
    (tmp_path / "packages" / "a" / "package.json").write_text(
        json.dumps({"name": "a", "scripts": {"check": "node -e \"console.log('a ok')\""}})
    )
    (tmp_path / "packages" / "b" / "package.json").write_text(
        json.dumps(
            {
                "name": "b",
                "scripts": {
                    "check:lint": "node -e \"console.log('b lint')\"",
                    "check:unit": "node -e \"console.log('b unit')\"",
                },
            }
        )
    )
    if failing_lines:
        (tmp_path / "packages" / "c").mkdir(parents=True)
        script = (
            "for(let i=0;i<%d;i++){process.stdout.write('line '+i+'\\n')} process.exitCode=1"
            % failing_lines
        )
        (tmp_path / "packages" / "c" / "package.json").write_text(
            json.dumps({"name": "c", "scripts": {"check": f'node -e "{script}"'}})
        )
    subprocess.run(
        ["npm", "install", "--no-audit", "--no-fund"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )


def _run_checks(tmp_path, *, env_overrides=None, extra_args=()):
    env = dict(os.environ)
    env.pop("GITHUB_ACTIONS", None)
    env.pop("GITHUB_REPOSITORY_OWNER", None)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["node", str(CHECKS_SCRIPT), *extra_args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@_requires_node
def test_fork_ci_defaults_workspace_checks_to_serial(tmp_path):
    """Standard (non-Nous) fork CI must not run checks concurrently by
    default: that oversubscribes vitest/tsc/eslint's own worker pools on a
    4-core runner. Explicit --concurrency still overrides."""
    _make_workspace(tmp_path)
    result = _run_checks(tmp_path, env_overrides={"GITHUB_ACTIONS": "true"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "up to 1 at a time" in result.stdout

    result = _run_checks(
        tmp_path, env_overrides={"GITHUB_ACTIONS": "true"}, extra_args=["--concurrency", "2"]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "up to 2 at a time" in result.stdout


@_requires_node
def test_nous_ci_and_local_runs_keep_full_parallelism(tmp_path):
    """The paid NousResearch 32-core runner (and a local/laptop run outside
    CI) must not be bounded to 1 — the whole point of the big runner is
    concurrent units."""
    _make_workspace(tmp_path)

    result = _run_checks(
        tmp_path, env_overrides={"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY_OWNER": "NousResearch"}
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "up to 1 at a time" not in result.stdout

    result = _run_checks(tmp_path)  # no GITHUB_ACTIONS at all: local/laptop
    assert result.returncode == 0, result.stdout + result.stderr
    assert "up to 1 at a time" not in result.stdout


@_requires_node
def test_failure_output_and_summary_footer_survive_backpressure(tmp_path):
    """A failing check's buffered output must reach stdout in full, and the
    scheduler summary/footer after it must still print, even when the
    consuming pipe applies backpressure (large writes return False and
    finish flushing asynchronously). Forcing process.exit() before that
    flush completes silently truncates the log — this is exactly what
    happened to a large Desktop UI failure on PR14."""
    failing_lines = 200_000
    _make_workspace(tmp_path, failing_lines=failing_lines)

    # Pipe through `python -c "import sys,time; ..."` style slow consumer to
    # reliably trigger backpressure regardless of host pipe buffer size.
    slow_consumer = textwrap.dedent(
        """
        import sys, time
        while True:
            chunk = sys.stdin.buffer.read(4096)
            if not chunk:
                break
            time.sleep(0.0005)
        """
    )
    env = dict(os.environ)
    env.pop("GITHUB_ACTIONS", None)
    env.pop("GITHUB_REPOSITORY_OWNER", None)
    proc = subprocess.Popen(
        ["node", str(CHECKS_SCRIPT), "--concurrency", "1"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
    )
    assert proc.stdout is not None
    subprocess.run(
        [sys.executable, "-c", slow_consumer],
        stdin=proc.stdout,
        capture_output=True,
        timeout=120,
    )
    proc.stdout.close()
    proc.wait(timeout=120)

    # The script's own exit must reflect the failed check (via exitCode, not
    # a forced process.exit that could race the pending flush).
    assert proc.returncode == 1

    # None of this is captured by us (it went straight to the slow
    # consumer's stdin) — the assertion is that the *scheduler* did not
    # crash and the consumer's read loop terminated cleanly, which only
    # happens once the writer side has flushed and closed. Re-run without
    # a slow consumer and grep the real captured text for both the last
    # line of the failing unit's output and the trailing summary footer,
    # which is the actual regression signal.
    result = _run_checks(tmp_path, extra_args=["--concurrency", "1"])
    assert result.returncode == 1
    assert f"line {failing_lines - 1}" in result.stdout
    assert "=== summary ===" in result.stdout
    assert "all 4 checks passed" not in result.stdout
    assert "1 of 4 checks failed" in result.stderr
