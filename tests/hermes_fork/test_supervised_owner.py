"""``hermes_fork.supervised_owner``: the host-owner reclaim rung spares systemd-supervised backends.

Regression for 2026-10-08 00:57 UTC: a restarted ``hermes-dashboard-next`` reaped the ~2 h old
``hermes-webdesktop-next-backend`` (null spawner, ppid 1, no SSH lock) through
``reap_orphaned_backend_owner``, and the backend stayed down.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

from hermes_cli import dashboard_procs, main_dashboard
from hermes_cli import process_identity as pi
from hermes_fork import supervised_owner


class _NoSuchProcess(Exception):
    pass


def _psutil(procs: dict[int, float]):
    def _process(pid):
        if pid not in procs:
            raise _NoSuchProcess(pid)
        proc = MagicMock()
        proc.pid = pid
        proc.create_time.return_value = procs[pid]
        return proc

    return types.SimpleNamespace(Process=_process, NoSuchProcess=_NoSuchProcess)


def _entry(pid, create, spawner_pid=None, spawner_create=None):
    return {"pid": pid, "create_time": create, "purpose": "serve",
            "install": pi.install_id(Path("/x/install")), "spawner_pid": spawner_pid,
            "spawner_create": spawner_create, "registered_at": 0.0, "argv": ""}


def _reap(entry, *, unit):
    """Run the reclaim rung on an ancient ppid-1 owner; *unit* is what the systemd probe says."""
    kills = []
    probe = (MagicMock(side_effect=unit) if isinstance(unit, BaseException)
             else MagicMock(return_value=unit))
    with patch.dict(sys.modules, {"psutil": _psutil({555: 1000.0})}), \
         patch.object(pi, "ledger_entries", return_value=[entry]), \
         patch.object(dashboard_procs, "_process_ppid", return_value=1), \
         patch.object(dashboard_procs, "_lock_owned_serve_pids", return_value=set()), \
         patch.object(main_dashboard, "_get_systemd_service_for_pid", probe):
        result = pi.reap_orphaned_backend_owner(555, 1000.0, kill_fn=kills.append)
    return result, kills


def test_spares_null_spawner_owner_supervised_by_systemd():
    result, kills = _reap(_entry(555, 1000.0), unit="hermes-webdesktop-next-backend.service")
    assert (result, kills) == (None, [])


def test_spares_dead_spawner_owner_supervised_by_systemd():
    # A dead recorded spawner doesn't matter: systemd still owns the process.
    result, kills = _reap(_entry(555, 1000.0, spawner_pid=700, spawner_create=7.0),
                          unit="hermes-dashboard-next.service")
    assert (result, kills) == (None, [])


def test_still_reaps_unsupervised_orphan():
    result, kills = _reap(_entry(555, 1000.0), unit=None)
    assert (result, kills) == (555, [555])


def test_failed_probe_spares():
    result, kills = _reap(_entry(555, 1000.0), unit=OSError("systemctl missing"))
    assert (result, kills) == (None, [])


def test_predicate_reads_upstream_mainpid_probe():
    with patch.object(main_dashboard, "_get_systemd_service_for_pid",
                      return_value="x.service") as probe:
        assert supervised_owner.is_service_supervised(42) is True
    probe.assert_called_once_with(42)
    with patch.object(main_dashboard, "_get_systemd_service_for_pid", return_value=None):
        assert supervised_owner.is_service_supervised(42) is False
