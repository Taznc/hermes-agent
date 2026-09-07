"""Managed-gateway isolation for dispatcher-owned Kanban workers."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture(autouse=True)
def _placement_gives_no_answer(monkeypatch: pytest.MonkeyPatch):
    """State this file's PLACEMENT input: "the kernel says nothing".

    Every in-process test here simulates a topology through IDENTITY —
    ``INVOCATION_ID`` / ``SYSTEMD_EXEC_PID`` / ``_is_supervised_gateway_process`` — which
    is data a test can set. Placement is not: ``_scope_needed_by_cgroup_placement`` reads
    the TEST RUNNER's own real cgroup, so leaving it live would let the verdict be decided
    by wherever the suite happens to run (inside a ``hermes-worker-*`` scope it reads
    "already isolated"; inside a supervised ``hermes-*`` unit it reads "wrap this").
    Pinning it to ``None`` — a cgroup-v1 host, or a container that hides
    ``/proc/self/cgroup`` — makes these exercise the identity fallback deterministically
    on any host.

    ``test_real_descendant_of_a_supervised_unit_leaves_the_unit_cgroup`` is unaffected by
    design: it does its work in a subprocess inside its own transient unit, which reads
    its own real cgroup rather than this process's patched module.
    """
    monkeypatch.setattr(
        "tools.process_registry._scope_needed_by_cgroup_placement", lambda: None
    )


@pytest.fixture
def worker_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, kb.Task]:
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    profile.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])

    workspace = tmp_path / "candidate-worktree"
    workspace.mkdir()
    task = kb.Task(
        id="t_candidate_restart",
        title="activate candidate",
        body=None,
        assignee="coder",
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=1,
        completed_at=None,
        workspace_kind="worktree",
        workspace_path=str(workspace),
        claim_lock="host:dispatcher",
        claim_expires=999,
        tenant=None,
        branch_name="wt/t_candidate_restart",
        current_run_id=23,
    )
    return workspace, task


# ``_default_spawn`` issues TWO Popen calls: first the worker-log timestamp
# filter (``hermes_cli/kanban_log_stamp.py``), then the worker itself. These
# helpers keep every assertion aimed at the intended one — an argv-accumulating
# fake would otherwise silently mix the two.
def _is_stamper(argv: list[str]) -> bool:
    return any(arg.endswith("kanban_log_stamp.py") for arg in argv)


def _worker_call(calls: list[list[str]]) -> list[str]:
    workers = [argv for argv in calls if not _is_stamper(argv)]
    assert len(workers) == 1, calls
    return workers[0]


def _stamper_call(calls: list[list[str]]) -> list[str]:
    stampers = [argv for argv in calls if _is_stamper(argv)]
    assert len(stampers) == 1, calls
    return stampers[0]


@pytest.mark.linux_only
def test_managed_gateway_worker_is_spawned_in_restart_safe_scope(
    worker_setup: tuple[Path, kb.Task], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, task = worker_setup
    calls: list[list[str]] = []
    captured_env: dict[str, str] = {}
    captured_cwd: str | None = None

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        nonlocal captured_cwd
        calls.append(list(cmd))
        if not _is_stamper(list(cmd)):
            captured_env.update(kwargs.get("env") or {})
            captured_cwd = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setenv("INVOCATION_ID", "managed-gateway-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-cross-profile")
    monkeypatch.setattr("agent.secret_scope.is_multiplex_active", lambda: True)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process", lambda: True)
    monkeypatch.setattr("tools.process_registry._systemd_run_user_scope_available", lambda: True)
    monkeypatch.setattr("tools.process_registry._worker_memory_max_bytes", lambda: 536_870_912)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run")

    assert kbd._default_spawn(task, str(workspace)) == 4242
    captured_cmd = _worker_call(calls)
    assert captured_cmd[:3] == ["/usr/bin/systemd-run", "--user", "--quiet"]
    assert "--scope" not in captured_cmd
    assert "--pipe" in captured_cmd
    assert "--slice=hermes-workers.slice" in captured_cmd
    unit_index = captured_cmd.index("--unit")
    assert captured_cmd[unit_index + 1] == "hermes-worker-kanban-t_candidate_restart-run-23"
    assert "MemoryMax=536870912" in captured_cmd
    separator = captured_cmd.index("--")
    assert captured_cmd[separator + 1 : separator + 4] == ["hermes", "-p", "coder"]
    assert captured_cwd == str(workspace)
    assert captured_env["HERMES_KANBAN_TASK"] == task.id
    assert captured_env["HERMES_KANBAN_RUN_ID"] == "23"
    assert "ANTHROPIC_API_KEY" not in captured_env

    # The log-timestamp filter needs the SAME protection, under its own unit
    # name. Left in the gateway's cgroup it would die on `systemctl restart`,
    # closing the read end of the pipe and SIGPIPE-ing the very worker this
    # scope exists to keep alive.
    stamper_cmd = _stamper_call(calls)
    assert stamper_cmd[:3] == ["/usr/bin/systemd-run", "--user", "--quiet"]
    stamper_unit = stamper_cmd.index("--unit")
    assert stamper_cmd[stamper_unit + 1] == "hermes-worker-kanban-log-t_candidate_restart-run-23"


@pytest.mark.linux_only
def test_reclaim_stops_the_deterministic_worker_service_before_signalling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped: list[str] = []
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(kbd._kb, "_host_prefix", lambda: "host:")
    monkeypatch.setattr(
        "tools.process_registry._stop_systemd_unit",
        lambda unit: stopped.append(unit) or True,
    )

    result = kbd._terminate_reclaimed_worker(
        4242,
        "host:dispatcher",
        systemd_unit="hermes-worker-kanban-t_candidate_restart-run-23.service",
        signal_fn=lambda pid, sig: signals.append((pid, sig)) or (_ for _ in ()).throw(ProcessLookupError()),
    )

    assert stopped == ["hermes-worker-kanban-t_candidate_restart-run-23.service"]
    assert signals == [(4242, signal.SIGTERM)]
    assert result["systemd_unit_stopped"] is True
    assert result["terminated"] is True


@pytest.mark.linux_only
def test_managed_gateway_worker_spawn_fails_closed_without_scope(
    worker_setup: tuple[Path, kb.Task], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, task = worker_setup
    popen_calls: list[list[str]] = []
    monkeypatch.setenv("INVOCATION_ID", "managed-gateway-test")
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: popen_calls.append(list(cmd)))
    monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process", lambda: True)
    monkeypatch.setattr("tools.process_registry._systemd_run_user_scope_available", lambda: False)

    with pytest.raises(RuntimeError, match="restart-safe systemd scope"):
        kbd._default_spawn(task, str(workspace))
    assert popen_calls == []


@pytest.mark.linux_only
def test_managed_gateway_scope_builder_fails_closed_if_binary_disappears(
    worker_setup: tuple[Path, kb.Task], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, task = worker_setup
    monkeypatch.setenv("INVOCATION_ID", "managed-gateway-test")
    monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process", lambda: True)
    monkeypatch.setattr("tools.process_registry._systemd_run_user_scope_available", lambda: True)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("unsafe direct spawn"))

    with pytest.raises(RuntimeError, match="restart-safe systemd scope"):
        kbd._default_spawn(task, str(workspace))


def test_standalone_dispatcher_keeps_direct_worker_spawn(
    worker_setup: tuple[Path, kb.Task], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, task = worker_setup
    calls: list[list[str]] = []

    class FakeProc:
        pid = 4243

    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: calls.append(list(cmd)) or FakeProc())
    monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process", lambda: False)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv("SYSTEMD_EXEC_PID", raising=False)
    monkeypatch.setattr(
        "tools.process_registry._systemd_run_user_scope_available",
        lambda: pytest.fail("scope probe must not run outside managed gateway"),
    )

    assert kbd._default_spawn(task, str(workspace)) == 4243
    assert _worker_call(calls)[:3] == ["hermes", "-p", "coder"]
    # The log filter is likewise unwrapped here — nothing to be lifted out of.
    assert _stamper_call(calls)[0] == sys.executable


@pytest.mark.linux_only
def test_backend_supervised_worker_is_spawned_in_restart_safe_scope(
    worker_setup: tuple[Path, kb.Task], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatcher inside a NON-gateway systemd unit (``hermes serve``, the web-desktop
    backend) must still lift its worker into a private scope.

    Gating this on "am I the gateway" left every backend-spawned worker inside
    ``hermes-webdesktop-backend.service``'s own ``KillMode=control-group`` cgroup, so one
    unit stop SIGKILLed five in-flight workers at once.
    """
    workspace, task = worker_setup
    calls: list[list[str]] = []

    class FakeProc:
        pid = 4244

    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: calls.append(list(cmd)) or FakeProc())
    # Not the gateway — no _HERMES_GATEWAY, no gateway PID-file ownership.
    monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process", lambda: False)
    # ...but the MAIN process of a supervised systemd unit, which is what matters.
    monkeypatch.setenv("INVOCATION_ID", "webdesktop-backend-test")
    monkeypatch.setenv("SYSTEMD_EXEC_PID", str(os.getpid()))
    monkeypatch.setattr("tools.process_registry._systemd_run_user_scope_available", lambda: True)
    monkeypatch.setattr("tools.process_registry._worker_memory_max_bytes", lambda: 536_870_912)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run")

    assert kbd._default_spawn(task, str(workspace)) == 4244
    captured_cmd = _worker_call(calls)
    assert captured_cmd[:3] == ["/usr/bin/systemd-run", "--user", "--quiet"]
    assert "--scope" not in captured_cmd
    assert "--pipe" in captured_cmd
    assert "--slice=hermes-workers.slice" in captured_cmd
    assert f"--working-directory={workspace}" in captured_cmd
    unit_index = captured_cmd.index("--unit")
    assert captured_cmd[unit_index + 1] == "hermes-worker-kanban-t_candidate_restart-run-23"
    separator = captured_cmd.index("--")
    assert captured_cmd[separator + 1 : separator + 4] == ["hermes", "-p", "coder"]

    # Same reasoning as the gateway arm: the log filter must leave this unit's
    # cgroup too, or a backend restart kills it under a surviving worker.
    stamper_cmd = _stamper_call(calls)
    assert stamper_cmd[:3] == ["/usr/bin/systemd-run", "--user", "--quiet"]
    stamper_unit = stamper_cmd.index("--unit")
    assert stamper_cmd[stamper_unit + 1] == "hermes-worker-kanban-log-t_candidate_restart-run-23"


@pytest.mark.linux_only
def test_backend_supervised_worker_spawn_fails_closed_without_scope(
    worker_setup: tuple[Path, kb.Task], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backend path must refuse rather than silently spawn an unprotected worker.

    Before the fix it never reached the fail-closed check at all: the early return handed
    back the unwrapped command and the worker landed in the unit's cgroup.
    """
    workspace, task = worker_setup
    popen_calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: popen_calls.append(list(cmd)))
    monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process", lambda: False)
    monkeypatch.setenv("INVOCATION_ID", "webdesktop-backend-test")
    monkeypatch.setenv("SYSTEMD_EXEC_PID", str(os.getpid()))
    monkeypatch.setattr("tools.process_registry._systemd_run_user_scope_available", lambda: False)

    with pytest.raises(RuntimeError, match="restart-safe systemd scope"):
        kbd._default_spawn(task, str(workspace))
    assert popen_calls == []


def test_systemd_service_descendant_keeps_direct_worker_spawn(
    worker_setup: tuple[Path, kb.Task], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``INVOCATION_ID``/``SYSTEMD_EXEC_PID`` are inherited by every descendant.

    This pins the IDENTITY fallback, which is what decides on a host where cgroup
    placement gives no answer (cgroup v1, or a container that hides
    ``/proc/self/cgroup``): the markers alone are not evidence, so a nested CLI under
    an arbitrary unit must not mint scopes.

    On a cgroup-v2 host a descendant of a SUPERVISED HERMES unit is wrapped instead,
    by placement — that is the leak this fix closes, and it is asserted by
    ``test_real_descendant_of_a_supervised_unit_leaves_the_unit_cgroup`` below.
    """
    workspace, task = worker_setup
    calls: list[list[str]] = []

    class FakeProc:
        pid = 4245

    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: calls.append(list(cmd)) or FakeProc())
    monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process", lambda: False)
    monkeypatch.setenv("INVOCATION_ID", "inherited-from-the-unit")
    monkeypatch.setenv("SYSTEMD_EXEC_PID", str(os.getpid() + 1))  # the unit's main pid, not ours
    monkeypatch.setattr(
        "tools.process_registry._systemd_run_user_scope_available",
        lambda: pytest.fail("scope probe must not run for a service DESCENDANT"),
    )

    assert kbd._default_spawn(task, str(workspace)) == 4245
    assert _worker_call(calls)[:3] == ["hermes", "-p", "coder"]
    assert _stamper_call(calls)[0] == sys.executable


@pytest.mark.linux_only
def test_real_user_systemd_scope_preserves_worker_context(
    worker_setup: tuple[Path, kb.Task], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tools import process_registry

    if not process_registry._systemd_run_user_scope_available():
        pytest.skip("systemd-run --user --scope is unavailable on this host")

    workspace, task = worker_setup
    receipt = workspace / "worker-receipt.json"
    script = (
        "import json, os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
        "'pid': os.getpid(), 'cwd': os.getcwd(), "
        "'task': os.environ.get('HERMES_KANBAN_TASK'), "
        "'run': os.environ.get('HERMES_KANBAN_RUN_ID'), "
        "'cgroup': pathlib.Path('/proc/self/cgroup').read_text()})); time.sleep(0.5)"
    )
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: [sys.executable, "-c", script, str(receipt)])
    monkeypatch.setenv("INVOCATION_ID", "managed-gateway-test")
    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: True)

    pid = kbd._default_spawn(task, str(workspace))
    deadline = time.monotonic() + 5
    while not receipt.exists() and time.monotonic() < deadline:
        time.sleep(0.05)

    assert receipt.exists()
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["pid"] != pid  # systemd-run stays attached with --pipe; service owns the worker.
    assert payload["cwd"] == str(workspace)
    assert payload["task"] == task.id
    assert payload["run"] == "23"
    assert (
        "/hermes.slice/hermes-workers.slice/"
        "hermes-worker-kanban-t_candidate_restart-run-23.service"
    ) in payload["cgroup"]
    assert "hermes-gateway.service" not in payload["cgroup"]


@pytest.mark.linux_only
def test_real_backend_supervised_worker_leaves_the_unit_cgroup(
    worker_setup: tuple[Path, kb.Task], monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end proof for the web-desktop backend topology: a real worker spawned by a
    supervised NON-gateway unit lands in its own transient service.

    The gateway arm above cannot catch this regression — it forces
    ``_is_supervised_gateway_process`` True, which is exactly the predicate that was wrong.
    """
    from tools import process_registry

    if not process_registry._systemd_run_user_scope_available():
        pytest.skip("systemd-run --user --scope is unavailable on this host")

    workspace, task = worker_setup
    # A distinct task/run id: the gateway E2E above mints
    # `hermes-worker-kanban-t_candidate_restart-run-23.service`, and `--collect` reaps a
    # transient service only shortly after exit — reusing the name races that teardown and
    # systemd-run fails with "unit already exists".
    task.id = "t_backend_restart"
    task.current_run_id = 24
    receipt = workspace / "backend-worker-receipt.json"
    script = (
        "import json, os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
        "'pid': os.getpid(), "
        "'cgroup': pathlib.Path('/proc/self/cgroup').read_text()})); time.sleep(0.5)"
    )
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: [sys.executable, "-c", script, str(receipt)])
    # The backend's real markers: a supervised unit whose main process we are, and NOT the gateway.
    monkeypatch.setenv("INVOCATION_ID", "webdesktop-backend-e2e")
    monkeypatch.setenv("SYSTEMD_EXEC_PID", str(os.getpid()))
    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: False)

    pid = kbd._default_spawn(task, str(workspace))
    deadline = time.monotonic() + 5
    while not receipt.exists() and time.monotonic() < deadline:
        time.sleep(0.05)

    assert receipt.exists()
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["pid"] != pid
    cgroup = payload["cgroup"].strip()
    # The LEAF cgroup is the worker's own transient service; the incident had the leaf be
    # the dispatching unit's own service cgroup (`/system.slice/hermes-*.service`).
    leaf = cgroup.rsplit("/", 1)[-1]
    assert leaf == "hermes-worker-kanban-t_backend_restart-run-24.service", cgroup
    assert "/hermes.slice/hermes-workers.slice/" in cgroup, cgroup


@pytest.mark.linux_only
def test_real_descendant_of_a_supervised_unit_leaves_the_unit_cgroup(
    tmp_path: Path,
) -> None:
    """The card's core regression, reproduced by genuinely creating the topology.

    A dispatch tick that runs in a DESCENDANT of a supervised unit's main process sees
    ``INVOCATION_ID`` and ``SYSTEMD_EXEC_PID`` (both inherited) but its own pid differs,
    so identity answered False and ``_default_spawn`` ``Popen``'d the worker straight into
    the unit's cgroup with no log line. One such leaked worker measured 1853 MiB against
    ``hermes-webdesktop-backend.service``'s 3G ``MemoryHigh``.

    Nothing here is simulated. The probe body runs inside a THROWAWAY transient unit
    (``hermes-webdesktop-backend-probe-<hex>.service`` in ``app.slice`` — a real
    Hermes-named unit cgroup outside ``hermes-workers.slice``, so placement reads it
    exactly as it reads the live backend) and ``fork()``s, which is the only way to obtain
    a true descendant: cgroup membership is kernel-maintained, so a test process cannot
    talk itself into another unit's cgroup. No live unit is touched, and the probe asserts
    its kanban board is sandboxed before it spawns anything.

    The assertion compares the worker's cgroup LEAF against the unit this run should have
    minted, not a substring — a child that merely inherited a scoped dispatcher's cgroup
    would still match ``hermes-worker-*`` — and separately against the dispatching
    process's own cgroup, which is what the leak made them share.
    """
    from tools import process_registry

    if not process_registry._systemd_run_user_scope_available():
        pytest.skip("systemd-run --user --scope is unavailable on this host")

    repo_root = Path(__file__).resolve().parents[2]
    probe = Path(__file__).with_name("_supervised_unit_descendant_probe.py")
    probe_root = tmp_path / "probe"
    # A supervised-unit NAME (`is_hermes_unit` is prefix-based) placed in app.slice, i.e.
    # outside the worker slice — the shape of a unit that dispatches workers.
    unit = f"hermes-webdesktop-backend-probe-{uuid.uuid4().hex[:8]}"
    completed = subprocess.run(
        [
            "systemd-run", "--user", "--quiet", "--collect", "--pipe",
            f"--unit={unit}", "--slice=app.slice",
            f"--working-directory={repo_root}",
            f"--setenv=HERMES_PROBE_ROOT={probe_root}",
            f"--setenv=PYTHONPATH={repo_root}",
            sys.executable, str(probe),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    result_file = probe_root / "result.json"
    assert result_file.exists(), (
        f"probe produced no result (rc={completed.returncode})\n"
        f"stdout: {completed.stdout}\nstderr: {completed.stderr}"
    )
    result = json.loads(result_file.read_text(encoding="utf-8"))
    assert "error" not in result, result["error"]

    # The topology is the real one, not an assumption: a Hermes unit's cgroup, and a
    # process that is a descendant of its main pid rather than the main pid itself.
    assert result["invocation_id_present"] is True
    assert result["systemd_exec_pid"] == str(result["unit_pid"])
    assert result["descendant_pid"] != result["unit_pid"]
    assert result["identity"] is False, (
        "the identity predicate must be False here — that is what made this leak silent"
    )
    assert result["placement"] is True, result["unit_cgroup"]

    # The worker landed in the unit this run should have minted, NOT in the cgroup of the
    # process that dispatched it (which is precisely what the leak looked like).
    assert result["worker_leaf"] == result["expected_leaf"], result["worker_cgroup"]
    assert "/hermes-workers.slice/" in result["worker_cgroup"], result["worker_cgroup"]
    assert result["worker_shares_parent_cgroup"] is False
    assert unit not in result["worker_cgroup"], result["worker_cgroup"]
