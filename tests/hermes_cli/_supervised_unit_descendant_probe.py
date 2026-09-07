"""Probe body for ``test_real_descendant_of_a_supervised_unit_leaves_the_unit_cgroup``.

Not a test module (the leading underscore keeps pytest from collecting it): this is
executed as a script *inside a throwaway transient systemd unit* by that test, because the
topology it reproduces cannot be created from within the test process itself.

What it establishes, in order:

1. **A supervising-unit cgroup.** The caller launches this script under a transient
   ``hermes-webdesktop-backend-probe-<hex>.service`` in ``app.slice`` — a genuine
   Hermes-named unit OUTSIDE ``hermes-workers.slice``, which is what
   ``_scope_needed_by_cgroup_placement`` reads as "a child spawned here lands in a
   supervised unit". Structurally identical to ``/system.slice/hermes-webdesktop-backend.service``
   for every predicate involved, and it touches no live unit.
2. **A genuine descendant.** We ``fork()``. The child inherits ``INVOCATION_ID`` and
   ``SYSTEMD_EXEC_PID`` (systemd set them for the unit's main process, which is our
   parent) but its own pid differs — exactly the process shape whose worker leaked. The
   child asserts ``_is_supervised_worker_dispatcher()`` is False so a future change that
   relaxes identity cannot make this test pass for the wrong reason.
3. **A real spawn through the production entry point.** ``kanban_db_dispatch._default_spawn``,
   not the wrapper helper, with only the worker's own argv stubbed to a payload that
   records ``/proc/self/cgroup``.

Results are written as JSON to ``$HERMES_PROBE_ROOT/result.json`` for the parent to assert
on; failures are recorded there too, so a crash inside the unit surfaces as a readable
message instead of a bare non-zero exit.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def cgroup_v2_path(pid: int | str = "self") -> str:
    """The ``0::`` line of ``/proc/<pid>/cgroup`` — this process's cgroup-v2 path."""
    text = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("0::"):
            return line.partition("::")[2].strip()
    raise RuntimeError(f"no cgroup-v2 line for pid {pid}")


TASK_ID = "t_descendant_probe"
RUN_ID = 25
EXPECTED_UNIT = f"hermes-worker-kanban-{TASK_ID}-run-{RUN_ID}.service"

root = Path(os.environ["HERMES_PROBE_ROOT"])
root.mkdir(parents=True, exist_ok=True)
result_path = root / "result.json"
receipt_path = root / "worker-receipt.txt"
payload_path = root / "payload.py"
payload_path.write_text(
    "from pathlib import Path\n"
    "import sys, time\n"
    "Path(sys.argv[1]).write_text(\n"
    "    Path('/proc/self/cgroup').read_text(encoding='utf-8'), encoding='utf-8')\n"
    "time.sleep(0.5)\n",
    encoding="utf-8",
)

unit_pid = os.getpid()
unit_cgroup = cgroup_v2_path()

child_pid = os.fork()
if child_pid:
    _, status = os.waitpid(child_pid, 0)
    raise SystemExit(0 if result_path.exists() else os.waitstatus_to_exitcode(status) or 1)

# ---- descendant of the unit's main process from here on -------------------------------
try:
    # Board isolation, per the dispatcher's env contract: HERMES_KANBAN_DB (and friends)
    # are inherited and are read BEFORE HERMES_KANBAN_HOME, so clearing only the home
    # would leave this probe writing into the live board.
    for key in [k for k in os.environ if k.startswith("HERMES_KANBAN_")]:
        os.environ.pop(key, None)
    sandbox = root / "kanban-home"
    sandbox.mkdir(parents=True, exist_ok=True)
    os.environ["HERMES_KANBAN_HOME"] = str(sandbox)
    hermes_home = root / "hermes-home"
    hermes_home.mkdir(parents=True, exist_ok=True)
    hermes_home.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    os.environ["HERMES_HOME"] = str(hermes_home)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd
    from tools import process_registry as pr

    resolved_db = kb.kanban_db_path(board="default")
    if root not in resolved_db.parents:
        raise RuntimeError(f"probe board is NOT isolated, refusing to spawn: {resolved_db}")

    descendant_pid = os.getpid()
    descendant_cgroup = cgroup_v2_path()
    identity = pr._is_supervised_worker_dispatcher()
    placement = pr._scope_needed_by_cgroup_placement()

    workspace = root / "worker-space"
    workspace.mkdir(exist_ok=True)
    task = kb.Task(
        id=TASK_ID,
        title="descendant cgroup probe",
        body=None,
        assignee="coder",
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=1,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=str(workspace),
        claim_lock="probe:descendant",
        claim_expires=9999999999,
        tenant=None,
        branch_name=None,
        current_run_id=RUN_ID,
    )
    # Only the worker's own argv is stubbed; the scope decision, argv wrapping, env
    # mutation and Popen are all the production path.
    kbd._worker_argv = lambda *_a, **_kw: [sys.executable, str(payload_path), str(receipt_path)]
    kbd._worker_launcher_prefix = lambda: []

    spawned_pid = kbd._default_spawn(task, str(workspace), board="default")
    deadline = time.monotonic() + 10
    while not receipt_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not receipt_path.exists():
        raise RuntimeError("the spawned worker never recorded its cgroup")
    worker_cgroup = next(
        line.partition("::")[2].strip()
        for line in receipt_path.read_text(encoding="utf-8").splitlines()
        if line.startswith("0::")
    )

    result_path.write_text(
        json.dumps(
            {
                "unit_pid": unit_pid,
                "descendant_pid": descendant_pid,
                "systemd_exec_pid": os.environ.get("SYSTEMD_EXEC_PID"),
                "invocation_id_present": bool(os.environ.get("INVOCATION_ID")),
                "identity": identity,
                "placement": placement,
                "unit_cgroup": unit_cgroup,
                "descendant_cgroup": descendant_cgroup,
                "spawned_pid": spawned_pid,
                "worker_cgroup": worker_cgroup,
                "worker_leaf": worker_cgroup.rsplit("/", 1)[-1],
                "expected_leaf": EXPECTED_UNIT,
                "worker_shares_parent_cgroup": worker_cgroup == descendant_cgroup,
                "resolved_db": str(resolved_db),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
except BaseException as exc:  # noqa: BLE001 - reported to the parent, not swallowed
    result_path.write_text(
        json.dumps({"error": f"{type(exc).__name__}: {exc}"}, sort_keys=True),
        encoding="utf-8",
    )
    os._exit(1)
os._exit(0)
