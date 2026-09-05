"""Systemd-gated integration test for ``kanban.worker_launcher``.

Real ``systemd-run --user --scope`` spawn — the one piece of the
hermes-workers.slice spec (t_cb47a946, docs/rfcs/hermes-workers-slice-spec.md
S6) that cannot be faked with a mock. Skips cleanly (not failing) when
``systemd-run --user --scope`` is unavailable — the same gating pattern
already used by ``tests/tools/test_process_registry.py``'s #70716 tests.

B5 history: a prior version of this file asserted
``_scope_exit_status()`` (a ``systemctl --user show -p ExecMain*``-based
exit-status query) returned a real verdict; when actually executed on live
systemd 255 that assertion FAILED (``ExecMainCode``/``ExecMainStatus`` are
never populated for a ``--scope`` unit -- systemd adopts, never forks, the
target process into it) even though the test had only ever been observed
skipping in prior handoffs, so a claimed "green" was really an unexecuted
skip. ``_scope_exit_status()`` is removed (see kanban_db_dispatch.py); this
file now proves the mechanism the fix actually relies on instead: a
``--scope`` unit is a *transparent exec*, so the spawned process remains a
real, direct, waitpid-able child of the spawning process, and the existing
``os.waitpid``-based ``_classify_worker_exit`` path classifies its exit with
full fidelity WITHOUT any systemd status query.

Guardrail: never restarts a real gateway/systemd unit. Every scope spawned
here is `--collect`ed and short-lived, torn down by the test itself.
"""

from __future__ import annotations

import os
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
    def test_scope_worker_is_real_waitpid_able_child(self, tmp_path: Path):
        """B1 proof: a ``--scope`` unit is a transparent exec, so the spawned
        process is a genuine, direct child of the spawning process and its
        real exit status is captured by ``os.waitpid`` -- no systemd status
        query needed. This is the actual mechanism the fix relies on instead
        of the removed (and provably non-functional) ``_scope_exit_status``.
        """
        if not _systemd_user_scope_available():
            pytest.skip("systemd-run --user --scope is unavailable on this host")

        unit_name = f"kanban-integration-waitpid-{int(time.time())}.scope"
        rate_limit_code = kb.KANBAN_RATE_LIMIT_EXIT_CODE
        script = f"import sys; sys.exit({rate_limit_code})"
        argv = [
            "systemd-run", "--user", "--scope", "--quiet",
            "--unit", unit_name, "--collect",
            "--", sys.executable, "-c", script,
        ]
        proc = subprocess.Popen(argv)
        pid, status = os.waitpid(proc.pid, 0)
        assert pid == proc.pid

        kbd._record_worker_exit(pid, status)
        kind, code = kbd._classify_worker_exit(pid)
        assert (kind, code) == ("rate_limited", rate_limit_code)

    def test_scope_survives_simulated_gateway_restart(self, tmp_path: Path):
        """Spawn a scope from a throwaway process standing in for the gateway,
        kill that throwaway process (never the real gateway/systemd), and
        assert the scope's PID is still alive and queryable from an unrelated
        process -- the structural mechanism restart-survival depends on."""
        if not _systemd_user_scope_available():
            pytest.skip("systemd-run --user --scope is unavailable on this host")

        unit_name = f"kanban-integration-survive-{int(time.time())}.scope"
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

    def test_unit_stop_requires_scope_suffix_to_resolve(self, tmp_path: Path):
        """B2 proof: ``systemctl --user stop <name>`` WITHOUT the ``.scope``
        suffix resolves to a same-named ``.service`` unit that was never
        created (reported "not loaded", rc=5) and does NOT touch the real
        scope -- the worker is left running. Stopping WITH the suffix
        actually reaps it. This is why ``_worker_launcher_unit_name`` always
        mints the id with ``.scope`` already appended."""
        if not _systemd_user_scope_available():
            pytest.skip("systemd-run --user --scope is unavailable on this host")

        from tools import process_registry

        base_name = f"kanban-integration-suffix-{int(time.time())}"
        unit_name = f"{base_name}.scope"
        argv = [
            "systemd-run", "--user", "--scope", "--quiet",
            "--unit", unit_name, "--collect",
            "--", sys.executable, "-c", "import time; time.sleep(20)",
        ]
        proc = subprocess.Popen(argv)
        try:
            deadline = time.monotonic() + 5
            while proc.poll() is not None and time.monotonic() < deadline:
                time.sleep(0.1)

            # Stopping WITHOUT the .scope suffix must not kill the real scope:
            # systemctl resolves the bare name to an unrelated, never-created
            # .service unit ("not loaded"), leaving the process alive.
            result_no_suffix = subprocess.run(
                ["systemctl", "--user", "stop", base_name],
                capture_output=True, timeout=15,
            )
            stderr = (result_no_suffix.stderr or b"").decode(errors="replace").lower()
            assert result_no_suffix.returncode != 0 or "not loaded" in stderr
            assert kb._pid_alive(proc.pid), (
                "process died from stopping the bare (suffix-less) unit name; "
                "expected it to resolve to an unrelated .service and be a no-op"
            )

            # Stopping WITH the .scope suffix (what _stop_systemd_unit is now
            # always handed, per B2) actually reaps it.
            assert process_registry._stop_systemd_unit(unit_name) is True
            deadline = time.monotonic() + 10
            while kb._pid_alive(proc.pid) and time.monotonic() < deadline:
                time.sleep(0.2)
            assert not kb._pid_alive(proc.pid), "real scope survived a correctly-suffixed stop"
        finally:
            subprocess.run(
                ["systemctl", "--user", "stop", unit_name], capture_output=True, timeout=15,
            )
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)

    def test_systemd_user_bus_reachable_on_live_host(self):
        """B3 proof: the reachability probe (whatever the current process's
        real environment/uid resolves to) actually finds the live bus socket
        on a host where ``systemd-run --user --scope`` genuinely works --
        the fail-closed guard must not also fail closed on a healthy host."""
        if not _systemd_user_scope_available():
            pytest.skip("systemd-run --user --scope is unavailable on this host")

        assert kbd._systemd_user_bus_reachable() is True
