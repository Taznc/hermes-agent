"""Systemd-gated integration test for ``kanban.worker_launcher``.

Real ``systemd-run --user --scope`` spawn + ``systemctl --user show`` status
read — the one piece of the hermes-workers.slice spec (t_cb47a946,
docs/rfcs/hermes-workers-slice-spec.md §6) that cannot be faked with a mock,
because it exercises real ``systemctl --user show -p ExecMainStatus``
semantics and the ``--collect`` self-cleanup window. Skips cleanly (not
failing) when ``systemd-run --user --scope`` is unavailable — the same
gating pattern already used by ``tests/tools/test_process_registry.py``'s
#70716 tests.

Guardrail: never restarts a real gateway/systemd unit. Every scope spawned
here is `--collect`ed and short-lived, torn down by the test itself.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


def _systemd_user_scope_available() -> bool:
    from tools import process_registry

    return process_registry._systemd_run_user_scope_available()


@pytest.mark.linux_only
class TestWorkerLauncherSystemdIntegration:
    def test_scope_exit_status_reads_real_unit_rate_limited(self, tmp_path: Path):
        if not _systemd_user_scope_available():
            pytest.skip("systemd-run --user --scope is unavailable on this host")

        from tools import process_registry

        unit_name = f"kanban-integration-test-{int(time.time())}"
        rate_limit_code = kb.KANBAN_RATE_LIMIT_EXIT_CODE
        script = f"import sys; sys.exit({rate_limit_code})"
        argv = [
            "systemd-run", "--user", "--scope", "--quiet",
            "--unit", unit_name, "--collect",
            "--", sys.executable, "-c", script,
        ]
        proc = subprocess.run(argv, capture_output=True, timeout=15)
        assert proc.returncode == 0, proc.stderr.decode(errors="replace")

        deadline = time.monotonic() + 5
        result = None
        while time.monotonic() < deadline:
            result = kbd._scope_exit_status(unit_name)
            if result is not None:
                break
            time.sleep(0.1)

        assert result == ("rate_limited", rate_limit_code)

        # --collect behavior: eventually the unit disappears and a second read
        # must return None gracefully, not raise. Poll briefly for cleanup.
        cleanup_deadline = time.monotonic() + 10
        collected = False
        while time.monotonic() < cleanup_deadline:
            props = process_registry.scope_unit_show_properties(unit_name)
            if props is None:
                collected = True
                break
            time.sleep(0.2)
        assert collected, "expected --collect to eventually remove the transient unit"
        assert kbd._scope_exit_status(unit_name) is None

    def test_scope_survives_simulated_gateway_restart(self, tmp_path: Path):
        """Spawn a scope from a throwaway process standing in for the gateway,
        kill that throwaway process (never the real gateway/systemd), and
        assert the scope's PID is still alive and queryable from an unrelated
        process — the structural mechanism restart-survival depends on."""
        if not _systemd_user_scope_available():
            pytest.skip("systemd-run --user --scope is unavailable on this host")

        unit_name = f"kanban-integration-survive-{int(time.time())}"
        receipt = tmp_path / "receipt.json"
        worker_script = (
            "import json, os, pathlib, sys, time; "
            f"pathlib.Path({str(receipt)!r}).write_text(json.dumps({{'pid': os.getpid()}})); "
            "time.sleep(10)"
        )
        launcher_argv = [
            "systemd-run", "--user", "--scope", "--quiet",
            "--unit", unit_name, "--collect",
            "--", sys.executable, "-c", worker_script,
        ]

        # A throwaway "gateway stand-in" process performs the spawn, then gets
        # killed — never the real gateway/systemd unit.
        standin_script = (
            "import subprocess, sys, time; "
            f"subprocess.run({launcher_argv!r}); "
            "time.sleep(30)"
        )
        standin = subprocess.Popen([sys.executable, "-c", standin_script])
        try:
            deadline = time.monotonic() + 10
            while not receipt.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            assert receipt.exists(), "worker did not start inside the scope in time"
            import json

            worker_pid = json.loads(receipt.read_text())["pid"]

            # Kill the stand-in "gateway" — the scope must survive (it's not
            # in the stand-in's cgroup/process-tree ownership).
            standin.terminate()
            standin.wait(timeout=5)

            assert kb._pid_alive(worker_pid), (
                "worker PID died when the throwaway gateway stand-in was killed; "
                "the systemd --scope isolation is not working as expected"
            )
        finally:
            subprocess.run(
                ["systemctl", "--user", "stop", unit_name], capture_output=True, timeout=15,
            )
            if standin.poll() is None:
                standin.kill()
                standin.wait(timeout=5)
