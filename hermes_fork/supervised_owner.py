"""The host-owner reclaim rung never kills a backend that a systemd unit supervises.

Upstream's ``reap_orphaned_backend_owner`` (#121964, ``hermes_cli/process_identity.py``) kills the
process holding the ``serve`` host lock when it looks like a dead session's orphan: a ledger entry
with no recorded spawner, reparented to init, older than 180 s and claimed by no Desktop SSH lock.
Every system-level service matches that shape: systemd is PID 1, so its MainPID has ppid 1 and was
never spawned by a Hermes session. On this fork's host two units share ``HERMES_HOME``
(``hermes-dashboard-next`` and ``hermes-webdesktop-next-backend``), so whichever restarts second
SIGTERMs the other. The victim exits cleanly, ``Restart=on-failure`` ignores it, and the web
Desktop stays down (2026-10-07 02:05 and 2026-10-08 00:57 UTC).

A process that is a unit's live ``MainPID`` has a supervisor, so it is by definition not an orphan.
``reap-spare-systemd-owner`` FORK ANCHOR (``process_identity.reap_orphaned_backend_owner``, after the
ledger lookup) calls :func:`is_service_supervised` and spares it. Ownership is proved with
upstream's ``main_dashboard._get_systemd_service_for_pid``, which matches ``MainPID``, not just
cgroup membership. That way a real orphan left over in some unrelated unit's cgroup can still be
reaped. Off Linux the probe returns None and upstream behaviour is unchanged.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def is_service_supervised(pid: int) -> bool:
    """True when *pid* is the live ``MainPID`` of a systemd service (or the probe itself failed).

    A failed probe answers True: the reaper's rule is "unprovable → never touch".
    """
    try:
        from hermes_cli.main_dashboard import _get_systemd_service_for_pid

        unit = _get_systemd_service_for_pid(int(pid))
    except Exception:
        logger.debug("systemd supervision probe failed for pid %s; sparing it", pid, exc_info=True)
        return True
    if unit:
        logger.info("not reaping host owner pid %s: supervised by %s", pid, unit)
        return True
    return False
