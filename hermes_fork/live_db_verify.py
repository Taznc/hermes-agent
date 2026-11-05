"""A live connection is not evidence of state.db corruption (tier T1, see FORK.md).

``hermes_cli.backup.verify_sqlite_integrity`` reads the file header through
``read_header_bytes_preopen``, which refuses while this process holds a connection to the file
(closing any descriptor on it would cancel the process's POSIX locks). The refusal comes back as
``None``, and the verifier reports that as ``cannot read header``, so ``hermes update`` calls a
healthy database corrupted and goes looking for a snapshot to restore.

While a connection is live this module drops the raw header read and makes SQLite itself deliver
the verdict through its own connection, which is safe to open beside a live one. Zeroed or
overwritten files still fail there. Offline files keep upstream's checks unchanged.

Wiring (anchor ``live-db-header-check``): ``verify_sqlite_integrity`` passes its check flags
through :func:`resolve_checks`.

Temporary: delete this module, its anchor and its tests when upstream lands
NousResearch/hermes-agent#122990, which fixes the same false alarm.
"""

from __future__ import annotations

from pathlib import Path

from hermes_cli.sqlite_safe_read import has_live_connection


def resolve_checks(path: Path, check_header: bool, run_pragma: bool) -> tuple[bool, bool]:
    """Return the ``(check_header, run_pragma)`` flags to verify *path* with.

    ``run_pragma`` is forced on when the header check is dropped. Otherwise the verifier would fall
    through to "header check passed" without having checked anything.
    """
    if check_header and has_live_connection(path):
        return False, True
    return check_header, run_pragma
