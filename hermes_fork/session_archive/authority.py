"""Owned-work archive admission on upstream's canonical metadata mutation seam (tier T1).

Upstream routes every session metadata edit -- REST ``PATCH /api/sessions/{id}``, the gateway
owner's ``session.mutate`` and the offline store -- through
``hermes_state_runtime.mutate_runtime_session``, which writes the ``archived`` column with
``_set_lineage_column_in_transaction`` and never reaches ``SessionDB.set_session_archived`` (the
``session-archive-storage-guard`` anchor). Without this hook an archive from any of those
transports would hide a session whose turn, subagents or processes are still running.

Called from exactly one site: the ``session-archive-authority-guard`` FORK ANCHOR around the
final ``db._execute_write(write)`` of ``mutate_runtime_session``. Producer locks are taken
BEFORE the writer lock (``owned_work_scope``'s order), and the check runs through upstream's own
``_authorize_write`` hook, i.e. after the receipt replay and revision/generation fences, so an
exact retry of a committed archive still replays its receipt.

A refusal is a ``RuntimeStoreError('session_archive_blocked')``: every transport already maps
that bounded reason (REST answers 409 with that detail; the gateway client forwards the code).
"""

from __future__ import annotations

from contextlib import contextmanager


def _archives(payload) -> bool:
    return isinstance(payload, dict) and payload.get("archived") is True


@contextmanager
def archive_admission(db, session_id, payload, authorize_write):
    """Yield the ``_authorize_write`` to use; guarded only when the edit archives."""
    if not _archives(payload):
        yield authorize_write
        return
    from hermes_state_runtime import RuntimeStoreError

    from . import SessionArchiveBlocked, assert_archivable
    from .activity import owned_work_scope

    with owned_work_scope(db, session_id) as work:

        def authorize(conn):
            try:
                assert_archivable(db, conn, session_id, work)
            except SessionArchiveBlocked as exc:
                raise RuntimeStoreError(
                    "session_archive_blocked", field="archived", detail=", ".join(exc.blockers)
                ) from exc
            if authorize_write is not None:
                authorize_write(conn)

        yield authorize
