"""Check the alleged stamper/worker PID mix-up using real surviving processes.

The payload is a bounded local script, never a model invocation. The optional
systemd launcher is disabled; this checks the direct spawn/restart path, not
systemd's own exec semantics.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest


SPAWNER = """
import json, os, sys
from pathlib import Path
from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
from tools import process_registry
from tests.hermes_cli.test_kanban_worker_log_timestamps import _task
root = Path(sys.argv[1])
assert kb.kanban_home().is_relative_to(root)
assert kb.kanban_db_path().is_relative_to(root)
dispatch._worker_launcher_prefix = lambda: []
dispatch._apply_worker_launcher = lambda task, command: (command, None)
dispatch._restart_safe_worker_argv = lambda task, command, *args: command
process_registry.restart_safe_supervised_child_argv = lambda command, **kwargs: command
stamper_pid = []
start = dispatch._start_worker_log_stamper
def track(*args):
    result = start(*args)
    assert result is not None
    stamper_pid.append(result[0].pid)
    return result
dispatch._start_worker_log_stamper = track
dispatch._worker_argv = lambda *args: [sys.executable, str(root/'payload.py'), str(root)]
worker_pid = dispatch._default_spawn(_task(), str(root))
print(json.dumps({'worker_pid':worker_pid, 'stamper_pid':stamper_pid[0],
                  'log':str(kb.worker_logs_dir()/'t_stamp01.log')}), flush=True)
"""

PAYLOAD = """
import os, sys, time
from pathlib import Path
root = Path(sys.argv[1])
print('worker_pid=' + str(os.getpid()), flush=True)
(root/'ready').write_text(str(os.getpid()), encoding='utf-8')
deadline = time.monotonic() + 15
while not (root/'release').exists() and time.monotonic() < deadline:
    time.sleep(0.02)
print('worker_finished', flush=True)
"""


def _wait_until(predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail("bounded identity probe did not reach its handoff")


@pytest.mark.linux_only
def test_worker_identity_survives_dispatcher_exit_without_becoming_stamper(tmp_path):
    (tmp_path / "spawn.py").write_text(SPAWNER, encoding="utf-8")
    (tmp_path / "payload.py").write_text(PAYLOAD, encoding="utf-8")
    home = tmp_path / ".hermes"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_KANBAN_")}
    env.update(HOME=str(tmp_path), HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home),
               PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    children = []
    try:
        # The dispatcher exits here; its actual worker and log filter survive.
        spawned = subprocess.run([sys.executable, str(tmp_path / "spawn.py"), str(tmp_path)],
                                 env=env, text=True, capture_output=True, timeout=10)
        assert spawned.returncode == 0, spawned.stderr
        identity = json.loads(spawned.stdout.strip().splitlines()[-1])
        children = [psutil.Process(identity[key]) for key in ("worker_pid", "stamper_pid")]
        _wait_until(lambda: (tmp_path / "ready").exists())
        actual_pid = int((tmp_path / "ready").read_text(encoding="utf-8"))
        assert identity["worker_pid"] == actual_pid
        assert identity["stamper_pid"] != actual_pid
        assert children[0].is_running() and children[1].is_running()
        assert str(tmp_path / "payload.py") in children[0].cmdline()
        assert any(Path(arg).name == "kanban_log_stamp.py" for arg in children[1].cmdline())
        log_path = Path(identity["log"])
        assert log_path.is_relative_to(home)
        (tmp_path / "release").touch()
        _wait_until(lambda: "worker_finished" in log_path.read_text(encoding="utf-8"))
        assert f"worker_pid={actual_pid}" in log_path.read_text(encoding="utf-8")
    finally:
        (tmp_path / "release").touch()
        # psutil keeps a create-time identity guard; never signal a recycled PID.
        _, alive = psutil.wait_procs(children, timeout=3)
        for child in alive:
            if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                child.kill()
        psutil.wait_procs(alive, timeout=3)
