"""``hermes_fork.terminal_scope``: terminal commands of a systemd-service backend leave its cgroup.

Regression for the 2026-10-24 web-desktop wedge (t_5efd7328, repro t_393a599c). A vitest run
started by an agent turn sat in ``hermes-webdesktop-backend.service``'s own cgroup. It pushed
that cgroup past ``memory.high`` with swap exhausted, and the kernel throttled the backend's
event-loop thread with it. Health probes then timed out until the unit was restarted.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from hermes_fork import terminal_scope as ts
from tools import process_registry as pr


SERVICE_CG = "0::/system.slice/hermes-webdesktop-backend.service\n"


@pytest.fixture
def placed(tmp_path, monkeypatch):
    """Pretend this process sits directly in a system service's cgroup."""
    def _place(text: str = SERVICE_CG):
        cg = tmp_path / "cgroup"
        cg.write_text(text)
        monkeypatch.setattr(ts, "_CGROUP_FILE", cg)
        monkeypatch.setattr(ts, "_placement", None)
        monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv(ts.DISABLE_ENV, raising=False)
    monkeypatch.delenv(ts.MEMORY_MAX_ENV, raising=False)
    monkeypatch.setenv("INVOCATION_ID", "5be2739fa0fb47ab8dbfa4293a18c763")
    monkeypatch.setattr(pr, "_is_supervised_gateway_process", lambda: False)
    monkeypatch.setattr(pr, "_systemd_run_user_scope_available", lambda: True)
    monkeypatch.setattr(ts.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setattr(ts, "_installed_pid", os.getpid())  # install() is a no-op unless a test resets it
    monkeypatch.setattr(ts, "_warned", set(), raising=False)
    _place()
    return _place


GATEWAY_CG = "0::/system.slice/hermes-gateway.service\n"


class TestPlacement:
    def test_system_service_backend_qualifies(self, placed):
        assert ts.host_unit() == "hermes-webdesktop-backend.service"

    def test_user_service_backend_qualifies(self, placed):
        placed("0::/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-webdesktop-next-backend.service\n")
        assert ts.host_unit() == "hermes-webdesktop-next-backend.service"

    @pytest.mark.parametrize("cgroup", [
        # already isolated: a kanban worker, or a command this module already scoped
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-worker-kanban-t_1-run-2.scope\n",
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-term-x-5be2739fa0fb-fg-ab.scope\n",
        "0::/user.slice/user-1000.slice/session-3.scope\n",       # an interactive login shell
        "0::/user.slice/user-1000.slice/user@1000.service\n",     # the user manager itself
        "0::/\n",
    ])
    def test_anything_else_is_left_alone(self, placed, cgroup):
        placed(cgroup)
        assert ts.host_unit() is None
        assert ts.scope_foreground(["bash", "-c", "true"]) == ["bash", "-c", "true"]

    def test_supervised_gateway_keeps_upstream_policy(self, placed, monkeypatch):
        monkeypatch.setattr(pr, "_is_supervised_gateway_process", lambda: True)
        assert ts.host_unit() is None

    @pytest.mark.parametrize("supervised, exec_pid_is_me", [(True, True), (False, True), (False, False)],
                             ids=["gateway-main-pid", "gateway-main-pid-unrecognised", "gateway-worker"])
    def test_gateway_unit_is_left_alone_main_pid_or_worker(self, placed, monkeypatch, supervised, exec_pid_is_me):
        """t_059acb3a: a kanban worker shell sits directly in hermes-gateway.service without being
        its supervised main pid. It must keep the unit's cgroup (no 4 GiB scope cap), and a gateway
        restart must not sweep in-flight scopes, even when the main pid's supervision markers are
        missing."""
        placed(GATEWAY_CG)
        monkeypatch.setattr(pr, "_is_supervised_gateway_process", lambda: supervised)
        monkeypatch.setenv("SYSTEMD_EXEC_PID", str(os.getpid() if exec_pid_is_me else 1))
        monkeypatch.setattr(pr, "systemd_user_bus_env", lambda base=None: {})
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            out = "hermes-term-hermes-gateway-aaaaaaaaaaaa-fg-1.scope loaded active running x\n"
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
        monkeypatch.setattr(ts.subprocess, "run", run)
        assert ts.host_unit() is None
        assert ts.scope_foreground(["bash", "-c", "npx vitest"]) == ["bash", "-c", "npx vitest"]
        assert ts.scope_background(types.SimpleNamespace(systemd_unit="", persist_on_release=False),
                                   ["bash"], "proc_abc") is None
        assert ts.reap_stale_scopes() == [] and calls == []

    @pytest.mark.parametrize("unit", ["hermes-dashboard.service", "hermes-webdesktop.service",
                                      "hermes-webdesktop-dev.service", "postgresql.service"])
    def test_other_services_are_left_alone(self, placed, unit):
        placed(f"0::/system.slice/{unit}\n")
        assert ts.host_unit() is None

    @pytest.mark.parametrize("value", ["0", "off", "FALSE", "no"])
    def test_kill_switch(self, placed, monkeypatch, value):
        monkeypatch.setenv(ts.DISABLE_ENV, value)
        assert ts.host_unit() is None

    def test_non_linux_is_inert(self, placed, monkeypatch):
        monkeypatch.setattr(ts, "_placement", None)
        monkeypatch.setattr(sys, "platform", "darwin")
        assert ts.host_unit() is None


class TestForeground:
    def test_wraps_in_a_named_scope_with_upstream_argv(self, placed):
        argv = ts.scope_foreground(["/bin/bash", "-c", "echo hi"])
        assert argv[:4] == ["/usr/bin/systemd-run", "--user", "--scope", "--quiet"]
        unit = argv[argv.index("--unit") + 1]
        assert unit.startswith("hermes-term-hermes-webdesktop-backend-5be2739fa0fb-fg-")
        assert any(a.startswith("MemoryMax=") for a in argv)
        assert argv[argv.index("--") + 1:] == ["/bin/bash", "-c", "echo hi"]

    def test_each_command_gets_its_own_scope(self, placed):
        def unit_of(a):
            return a[a.index("--unit") + 1]
        assert unit_of(ts.scope_foreground(["bash"])) != unit_of(ts.scope_foreground(["bash"]))

    def test_unusable_user_scope_falls_back_unchanged(self, placed, monkeypatch):
        monkeypatch.setattr(pr, "_systemd_run_user_scope_available", lambda: False)
        assert ts.scope_foreground(["bash", "-c", "x"]) == ["bash", "-c", "x"]

    def test_wrap_failure_never_breaks_the_command(self, placed, monkeypatch):
        def boom():
            raise RuntimeError("probe exploded")
        monkeypatch.setattr(pr, "_systemd_run_user_scope_available", boom)
        assert ts.scope_foreground(["bash", "-c", "x"]) == ["bash", "-c", "x"]

    def test_memory_max_override(self, placed, monkeypatch):
        monkeypatch.setenv(ts.MEMORY_MAX_ENV, "12G")
        argv = ts.scope_foreground(["bash", "-c", "MemoryMax=1"])
        sep = argv.index("--")
        assert [a for a in argv[:sep] if a.startswith("MemoryMax=")] == ["MemoryMax=12G"]
        assert argv[sep + 1:] == ["bash", "-c", "MemoryMax=1"]  # the command is never rewritten

    def test_malformed_memory_max_override_is_ignored(self, placed, monkeypatch):
        monkeypatch.setenv(ts.MEMORY_MAX_ENV, "12G; rm -rf /")
        argv = ts.scope_foreground(["bash"])
        assert f"MemoryMax={pr._worker_memory_max_bytes()}" in argv

    def test_missing_runtime_dir_is_supplied_to_systemd_run(self, placed, monkeypatch):
        monkeypatch.delenv("XDG_RUNTIME_DIR")
        monkeypatch.setattr(pr, "systemd_user_bus_env", lambda base=None: {"XDG_RUNTIME_DIR": "/run/user/1000"})
        argv = ts.scope_foreground(["bash"])
        assert argv[:2] == ["/usr/bin/env", "XDG_RUNTIME_DIR=/run/user/1000"]
        assert argv[2] == "/usr/bin/systemd-run"


class TestBackground:
    def _session(self, persist=False):
        return types.SimpleNamespace(systemd_unit="", persist_on_release=persist, id="proc_abc")

    def test_install_wraps_registry_scope_argv(self, placed, monkeypatch):
        monkeypatch.setattr(ts, "_installed_pid", None)
        monkeypatch.setattr(ts, "_reap_in_background", lambda: None)
        monkeypatch.setattr(pr.ProcessRegistry, "_scope_argv", pr.ProcessRegistry._scope_argv)
        ts.install()
        ts.install()  # idempotent: one wrap, not two
        wrapped = pr.ProcessRegistry._scope_argv
        assert wrapped._fork_terminal_scope and not getattr(wrapped.__wrapped__, "_fork_terminal_scope", False)

        session = self._session()
        argv = wrapped(object(), session, "npx vitest", "proc_abc", "Local")
        assert session.systemd_unit == "hermes-term-hermes-webdesktop-backend-5be2739fa0fb-bg-proc_abc.scope"
        assert argv[argv.index("--") + 1:][-1] == "set +m; npx vitest"

    def test_persist_on_release_gets_a_keep_scope(self, placed):
        session = self._session(persist=True)
        ts.scope_background(session, ["bash", "-lic", "x"], "proc_abc")
        assert "-keep-proc_abc.scope" in session.systemd_unit

    def test_upstream_scoped_session_is_untouched(self, placed, monkeypatch):
        monkeypatch.setattr(ts, "_installed_pid", None)
        monkeypatch.setattr(ts, "_reap_in_background", lambda: None)

        def upstream(self, session, safe_command, unit_suffix, label):
            session.systemd_unit = f"hermes-worker-{unit_suffix}.scope"
            return ["systemd-run", "upstream"]
        monkeypatch.setattr(pr.ProcessRegistry, "_scope_argv", upstream)
        ts.install()
        session = self._session()
        assert pr.ProcessRegistry._scope_argv(object(), session, "x", "proc_abc", "Local") == ["systemd-run", "upstream"]
        assert session.systemd_unit == "hermes-worker-proc_abc.scope"

    def test_inactive_host_leaves_upstream_argv(self, placed):
        placed("0::/user.slice/user-1000.slice/session-3.scope\n")
        session = self._session()
        assert ts.scope_background(session, ["bash"], "proc_abc") is None
        assert session.systemd_unit == ""


class TestStaleSweep:
    UNIT = "hermes-webdesktop-backend.service"
    CUR = "5be2739fa0fb"

    def test_only_this_services_older_fg_bg_scopes_are_stale(self):
        listed = [
            "hermes-term-hermes-webdesktop-backend-aaaaaaaaaaaa-fg-1a2b.scope",     # old fg: stale
            "hermes-term-hermes-webdesktop-backend-aaaaaaaaaaaa-bg-proc_1.scope",   # old bg: stale
            "hermes-term-hermes-webdesktop-backend-aaaaaaaaaaaa-keep-proc_2.scope", # persist_on_release
            "hermes-term-hermes-webdesktop-backend-5be2739fa0fb-fg-3c4d.scope",     # this invocation
            "hermes-term-hermes-webdesktop-next-backend-aaaaaaaaaaaa-fg-1.scope",   # another service
            "hermes-worker-kanban-t_1-run-2.scope",
        ]
        assert ts.stale_scope_names(listed, self.UNIT, self.CUR) == listed[:2]

    def test_sweep_requires_being_the_units_main_pid(self, placed, monkeypatch):
        calls = []
        monkeypatch.setattr(ts.subprocess, "run", lambda *a, **k: calls.append(a) or None)
        monkeypatch.setenv("SYSTEMD_EXEC_PID", str(os.getpid() + 1))  # an orphan of an old incarnation
        assert ts.reap_stale_scopes() == []
        assert calls == []

    def test_sweep_stops_stale_scopes(self, placed, monkeypatch):
        monkeypatch.setenv("SYSTEMD_EXEC_PID", str(os.getpid()))
        monkeypatch.setattr(pr, "systemd_user_bus_env", lambda base=None: {})
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            out = ("hermes-term-hermes-webdesktop-backend-aaaaaaaaaaaa-bg-proc_1.scope loaded active running x\n"
                   "hermes-term-hermes-webdesktop-backend-5be2739fa0fb-fg-1.scope loaded active running y\n")
            return subprocess.CompletedProcess(argv, 0, stdout=out if "list-units" in argv else "", stderr="")
        monkeypatch.setattr(ts.subprocess, "run", run)
        assert ts.reap_stale_scopes() == ["hermes-term-hermes-webdesktop-backend-aaaaaaaaaaaa-bg-proc_1.scope"]
        assert calls[-1][-2:] == ["--no-block", "hermes-term-hermes-webdesktop-backend-aaaaaaaaaaaa-bg-proc_1.scope"]

    def test_sweep_runs_off_the_callers_thread(self, monkeypatch):
        done = threading.Event()
        seen = []

        def reap():
            seen.append(threading.current_thread().name)
            done.set()
        monkeypatch.setattr(ts, "reap_stale_scopes", reap)
        ts._reap_in_background()
        assert done.wait(5)
        assert seen == ["fork-terminal-scope-sweep"]

    def test_sweep_failure_is_logged(self, monkeypatch, caplog):
        done = threading.Event()

        def reap():
            try:
                raise RuntimeError("systemctl exploded")
            finally:
                done.set()
        monkeypatch.setattr(ts, "reap_stale_scopes", reap)
        with caplog.at_level(logging.WARNING, logger=ts.__name__):
            ts._reap_in_background()
            assert done.wait(5)
            for _ in range(50):  # the warning is logged just after the event fires
                if "stale terminal scope sweep failed" in caplog.text:
                    break
                time.sleep(0.05)
        assert "stale terminal scope sweep failed" in caplog.text


class TestInstall:
    """``install()``: the wrap and the boot sweep, from the call and from the gateway seam."""

    @pytest.fixture
    def fresh(self, placed, monkeypatch):
        monkeypatch.setattr(ts, "_installed_pid", None)
        monkeypatch.setattr(pr.ProcessRegistry, "_scope_argv", pr.ProcessRegistry._scope_argv)
        if getattr(pr.ProcessRegistry._scope_argv, "_fork_terminal_scope", False):  # wrapped by an earlier boot
            monkeypatch.setattr(pr.ProcessRegistry, "_scope_argv", pr.ProcessRegistry._scope_argv.__wrapped__)  # type: ignore[attr-defined]
        sweeps = []
        monkeypatch.setattr(ts, "_reap_in_background", lambda: sweeps.append(os.getpid()))
        return sweeps

    def test_sweeps_once_per_process_on_a_backend(self, fresh):
        ts.install()
        ts.install()
        assert fresh == [os.getpid()]
        assert getattr(pr.ProcessRegistry._scope_argv, "_fork_terminal_scope", False)

    def test_gateway_wraps_but_never_sweeps(self, fresh, placed):
        placed(GATEWAY_CG)
        ts.install()
        assert fresh == []

    def test_failed_install_is_retried(self, fresh, monkeypatch):
        """t_059acb3a: the pid used to be recorded before the wrap, so one failure (here: the
        import of a renamed upstream class) left this process unwrapped for good."""
        registry_cls = pr.ProcessRegistry
        monkeypatch.delattr(pr, "ProcessRegistry")
        with pytest.raises(ImportError):
            ts.install()
        assert ts._installed_pid is None and fresh == []
        monkeypatch.setattr(pr, "ProcessRegistry", registry_cls, raising=False)
        ts.install()
        assert getattr(registry_cls._scope_argv, "_fork_terminal_scope", False)
        assert ts._installed_pid == os.getpid() and fresh == [os.getpid()]

    def test_concurrent_first_caller_waits_for_the_wrap(self, fresh, monkeypatch):
        """A second thread must not return from install() (and spawn unwrapped) while the first is
        still installing."""
        entered, release = threading.Event(), threading.Event()
        real_wrap = ts._wrap_registry

        def slow_wrap():
            entered.set()
            assert release.wait(5)
            real_wrap()
        monkeypatch.setattr(ts, "_wrap_registry", slow_wrap)
        first = threading.Thread(target=ts.install)
        first.start()
        assert entered.wait(5)
        returned = []
        second = threading.Thread(target=lambda: (ts.install(), returned.append(
            getattr(pr.ProcessRegistry._scope_argv, "_fork_terminal_scope", False))))
        second.start()
        second.join(0.3)
        assert second.is_alive() and returned == []  # blocked on the lock, not returned unwrapped
        release.set()
        first.join(5)
        second.join(5)
        assert returned == [True] and fresh == [os.getpid()]

    def test_gateway_seam_starts_the_sweep(self, fresh, monkeypatch):
        """The fork gateway seam (``hermes_fork.gateway.registry``) is what runs the boot sweep for a
        backend that never spawns a command."""
        from hermes_fork.gateway import config_cache, registry, session_surface
        from hermes_fork.ui import gateway as plugin_ui

        monkeypatch.setattr(registry._contracts, "STRICT", True)
        for module in (plugin_ui, config_cache, session_surface):
            monkeypatch.setattr(module, "install", lambda server: None)
        registry._install_gateway_seams(types.SimpleNamespace())
        assert fresh == [os.getpid()]
        assert getattr(pr.ProcessRegistry._scope_argv, "_fork_terminal_scope", False)


class TestDegradedIsVisible:
    """A fall-back to the unscoped path logs one WARNING per site (t_059acb3a), then DEBUG."""

    def _warnings(self, caplog, text):
        return [r for r in caplog.records if r.levelno == logging.WARNING and text in r.getMessage()]

    def test_renamed_upstream_builder_warns_once(self, placed, monkeypatch, caplog):
        monkeypatch.delattr(pr, "_systemd_scope_argv")
        with caplog.at_level(logging.DEBUG, logger=ts.__name__):
            assert ts.scope_foreground(["bash"]) == ["bash"]
            assert ts.scope_foreground(["bash"]) == ["bash"]
        assert len(self._warnings(caplog, "foreground wrap failed")) == 1

    def test_failed_install_keeps_foreground_scoped(self, placed, monkeypatch, caplog):
        """A renamed ``ProcessRegistry._scope_argv`` breaks only the background wrap. Foreground
        commands still get their scope on every call, install() is retried, and it warns once."""
        monkeypatch.setattr(ts, "_installed_pid", None)
        monkeypatch.delattr(pr.ProcessRegistry, "_scope_argv")
        with caplog.at_level(logging.DEBUG, logger=ts.__name__):
            first = ts.scope_foreground(["bash", "-c", "true"])
            second = ts.scope_foreground(["bash", "-c", "true"])
        for argv in (first, second):
            assert argv[0] == "/usr/bin/systemd-run" and argv[-3:] == ["bash", "-c", "true"]
            unit = argv[argv.index("--unit") + 1]
            assert unit.startswith("hermes-term-hermes-webdesktop-backend-5be2739fa0fb-fg-")
        assert ts._installed_pid is None  # still unwrapped, so the next call retries
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and "install failed" in warnings[0].getMessage()

    def test_background_failure_warns_once(self, placed, monkeypatch, caplog):
        monkeypatch.delattr(pr, "_systemd_scope_argv")
        session = types.SimpleNamespace(systemd_unit="", persist_on_release=False)
        with caplog.at_level(logging.DEBUG, logger=ts.__name__):
            assert ts.scope_background(session, ["bash"], "proc_abc") is None
            assert ts.scope_background(session, ["bash"], "proc_abc") is None
        assert len(self._warnings(caplog, "background wrap failed")) == 1

    def test_gateway_probe_failure_warns(self, placed, monkeypatch, caplog):
        monkeypatch.delattr(pr, "_is_supervised_gateway_process")
        with caplog.at_level(logging.DEBUG, logger=ts.__name__):
            assert ts.host_unit() is None
        assert len(self._warnings(caplog, "gateway identity probe failed")) == 1


def _user_scope_usable() -> bool:
    if not sys.platform.startswith("linux") or not shutil.which("systemd-run"):
        return False
    try:
        return subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "--collect", "--", "true"],
                              capture_output=True, timeout=20, env=pr.systemd_user_bus_env(), check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _user_scope_usable(), reason="needs a usable `systemd-run --user --scope`")
def test_live_foreground_command_leaves_the_host_cgroup(tmp_path, monkeypatch):
    """Real spawn through LocalEnvironment.execute. The command's own /proc/self/cgroup must be a
    fresh hermes-term-*-fg-* scope, not this process's cgroup. Its pid and process group must be
    the ones upstream's kill path signals."""
    host = Path("/proc/self/cgroup").read_text().strip()
    cg = tmp_path / "cgroup"
    cg.write_text(SERVICE_CG)
    monkeypatch.setattr(ts, "_CGROUP_FILE", cg)
    monkeypatch.setattr(ts, "_placement", None)
    monkeypatch.delenv(ts.DISABLE_ENV, raising=False)
    monkeypatch.setattr(pr, "_is_supervised_gateway_process", lambda: False)
    monkeypatch.setattr(pr, "_SYSTEMD_SCOPE_AVAILABLE", None)
    from tools.environments.local import LocalEnvironment

    env = LocalEnvironment(cwd=str(tmp_path), timeout=60)
    try:
        out = env.execute('echo "pgid=$(ps -o pgid= $$ | tr -d " ") pid=$$"; cat /proc/self/cgroup')
    finally:
        env.cleanup()
    text = out["output"]
    assert out["returncode"] == 0, text
    line = next(ln for ln in text.splitlines() if ln.startswith("0::"))
    assert line != host
    assert "/hermes-term-hermes-webdesktop-backend-" in line and "-fg-" in line and line.endswith(".scope")
    # systemd-run --scope execs the shell, so the Popen pid is still the group leader upstream kills.
    assert re.search(r"pgid=(\d+) pid=\1\b", text), text
