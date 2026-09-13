"""Standalone line-timestamp filter for kanban worker logs.

Reads a worker's raw stdout/stderr from stdin and appends it to the worker's
log file with a wall-clock prefix on every line::

    [2026-09-07 20:14:03] the worker's original line

The format is fixed-width local time, ``[%Y-%m-%d %H:%M:%S] `` (a trailing
single space separates the stamp from the original bytes). It is deliberately
locale-independent so a log is readable regardless of the dispatcher's locale.

Why a separate process instead of a reader thread in the dispatcher: the
dispatcher is short-lived and a supervised gateway restart kills it mid-run,
while workers are spawned ``start_new_session=True`` specifically so they
survive that. A reader thread owned by the dispatcher would die with it and the
timestamp stream would go dark on every restart — worse, the pipe would close
under a live worker. This filter is spawned the same restart-safe way the
worker is (its own transient systemd scope when the dispatcher is supervised),
so it outlives the dispatcher exactly as the worker does.

Stdlib only, and run by absolute script path, so it needs no package import and
no external binary (no moreutils ``ts``).
"""

from __future__ import annotations

import os
import sys
import time

#: Prefix format; ``[<local time>] `` — see the module docstring.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

_READ_CHUNK = 65536

#: A worker that emits a very long run of bytes with no newline (a progress
#: spinner, a binary blob) must not grow this process's memory without bound.
#: Past this many buffered bytes the partial line is flushed as-is; the
#: continuation stays unstamped until the next newline, so no newline is ever
#: invented inside the worker's output.
_MAX_PENDING = 1 << 20


def _stamp(now: float | None = None) -> bytes:
    return (
        "[" + time.strftime(TIMESTAMP_FORMAT, time.localtime(now)) + "] "
    ).encode("utf-8")


def _write_all(fd: int, data: bytes | bytearray) -> None:
    """Write every byte, tolerating short writes and EINTR."""
    view = memoryview(bytes(data))
    while view:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        if written <= 0:
            return
        view = view[written:]


def stamp_stream(in_fd: int, out_fd: int) -> None:
    """Copy ``in_fd`` to ``out_fd``, prefixing each line with a timestamp.

    Line-oriented and streaming: a complete line is stamped and written as soon
    as its newline arrives, so ``tail -f`` on the log stays live.
    """
    pending = bytearray()
    at_line_start = True
    while True:
        try:
            chunk = os.read(in_fd, _READ_CHUNK)
        except InterruptedError:
            continue
        except OSError:
            break
        if not chunk:
            break
        pending += chunk
        out = bytearray()
        while True:
            newline = pending.find(b"\n")
            if newline < 0:
                break
            line = bytes(pending[: newline + 1])
            del pending[: newline + 1]
            if at_line_start:
                out += _stamp()
            out += line
            at_line_start = True
        if len(pending) >= _MAX_PENDING:
            if at_line_start:
                out += _stamp()
            out += pending
            pending.clear()
            at_line_start = False
        if out:
            _write_all(out_fd, out)
    if pending:
        # Trailing partial line at EOF: stamped, but no newline is appended —
        # the log stays byte-faithful to what the worker actually wrote.
        _write_all(out_fd, (_stamp() if at_line_start else b"") + bytes(pending))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        sys.stderr.write("usage: kanban_log_stamp.py <log-path>\n")
        return 2
    out_fd = os.open(args[0], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        stamp_stream(0, out_fd)
    finally:
        os.close(out_fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
