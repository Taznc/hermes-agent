"""Worker log lines carry a wall-clock timestamp.

The dispatcher used to wire the worker's stdout straight at the log file, so a
reader could see WHAT a worker printed but never WHEN. These tests pin the
contract end-to-end through the real spawn path (``Popen`` is NOT mocked) plus
the two properties that make the approach safe: old un-timestamped logs still
read, and a filter that cannot start degrades to the previous raw behaviour
rather than stalling the board.
"""

import os
import re
import sys
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_log_stamp as stamp

# ``[YYYY-MM-DD HH:MM:SS] `` — see kanban_log_stamp.TIMESTAMP_FORMAT.
STAMP_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")


def _task(**overrides):
    fields: dict = dict(
        id="t_stamp01",
        title="stamp me",
        body=None,
        assignee="default",
        status="in_progress",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
        current_run_id=7,
    )
    fields.update(overrides)
    return kb.Task(**fields)


@pytest.fixture()
def spawn_env(tmp_path, monkeypatch):
    """Point the dispatcher's log dir at tmp and neutralise the systemd wrapper.

    The scope wrapper is a no-op off supervised-systemd hosts anyway; pinning it
    keeps the test identical on a dev box and in CI.
    """
    logs = tmp_path / "logs"
    monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: logs)
    monkeypatch.setattr(kbd, "_worker_launcher_prefix", lambda: [])
    monkeypatch.setattr(kbd, "_apply_worker_launcher", lambda task, cmd: (cmd, None))
    monkeypatch.setattr(kbd, "_restart_safe_worker_argv", lambda *a, **k: a[1])
    monkeypatch.setattr(
        "tools.process_registry.restart_safe_supervised_child_argv",
        lambda command, **kwargs: command,
    )
    monkeypatch.setattr(kbd, "_retag_legacy_worker_sessions", lambda _root: None)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return logs, str(workspace)


def _wait_for_log(log_path, predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="replace")
            if predicate(text):
                return text
        time.sleep(0.05)
    return text


def test_real_spawn_writes_timestamped_lines(spawn_env, monkeypatch):
    """A real short-lived subprocess through ``_default_spawn`` lands stamped.

    This is the end-to-end assertion: no mocked ``Popen``, no hand-fed pipe —
    the child is spawned exactly the way a worker is and its bytes travel the
    production route (worker -> pipe -> filter process -> log file).
    """
    logs, workspace = spawn_env
    task = _task()
    monkeypatch.setattr(
        kbd,
        "_worker_argv",
        lambda *a, **k: [
            sys.executable,
            "-c",
            "import sys; print('hello from the worker'); "
            "print('to stderr', file=sys.stderr); sys.stderr.flush()",
        ],
    )

    pid = kbd._default_spawn(task, workspace)
    assert pid

    log_path = logs / f"{task.id}.log"
    text = _wait_for_log(log_path, lambda t: "hello from the worker" in t and "to stderr" in t)

    body = [line for line in text.splitlines() if not line.startswith("--- hermes-kanban-run")]
    assert body, f"worker produced no log lines; log was:\n{text}"
    assert all(STAMP_RE.match(line) for line in body), body
    # stderr is merged into the same stamped stream, not lost.
    assert any(line.endswith("to stderr") for line in body), body
    assert any(line.endswith("hello from the worker") for line in body), body


def test_run_marker_stays_unstamped_and_byte_identical(spawn_env, monkeypatch):
    """The per-run marker keeps its exact shape — one line, no timestamp.

    ``_detect_quota_exit_signal`` does ``marker in log_text`` and
    ``rsplit(marker, 1)``; a stamped or reformatted marker would silently break
    quota-death classification.
    """
    logs, workspace = spawn_env
    task = _task(current_run_id=99)
    monkeypatch.setattr(
        kbd, "_worker_argv", lambda *a, **k: [sys.executable, "-c", "print('work')"]
    )

    kbd._default_spawn(task, workspace)

    log_path = logs / f"{task.id}.log"
    text = _wait_for_log(log_path, lambda t: "work" in t)
    marker = kb.worker_log_run_marker(99)
    assert marker in text
    assert text.count(marker) == 1
    assert text.startswith(marker)


def test_stamper_failure_falls_back_to_raw_logging(spawn_env, monkeypatch):
    """A filter that cannot start must not stall or break the spawn.

    Logging is a refinement; the board keeps running without it.
    """
    logs, workspace = spawn_env
    task = _task(id="t_stamp02")
    monkeypatch.setattr(kbd, "_start_worker_log_stamper", lambda *a, **k: None)
    monkeypatch.setattr(
        kbd, "_worker_argv", lambda *a, **k: [sys.executable, "-c", "print('unstamped')"]
    )

    pid = kbd._default_spawn(task, workspace)
    assert pid

    log_path = logs / f"{task.id}.log"
    text = _wait_for_log(log_path, lambda t: "unstamped" in t)
    assert "unstamped" in text
    body = [line for line in text.splitlines() if not line.startswith("--- hermes-kanban-run")]
    assert body == ["unstamped"]


def test_no_stamper_without_a_run_id(spawn_env):
    """No run id means no traceable scope, so no filter — mirrors the worker rule."""
    _logs, _workspace = spawn_env
    task = _task(current_run_id=None)
    assert kbd._start_worker_log_stamper(task, "/dev/null") is None


def test_read_worker_log_handles_a_log_with_no_timestamps(tmp_path, monkeypatch):
    """Logs written before this change read back unchanged — no format assumed."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: logs)
    legacy = "--- hermes-kanban-run:1 ---\nplain old line\nanother\n"
    (logs / "t_old.log").write_text(legacy, encoding="utf-8")

    assert kb.read_worker_log("t_old") == legacy
    # Mixed old/new content in one file (a task re-run after upgrading) is just
    # text to every reader.
    (logs / "t_old.log").write_text(legacy + "[2026-09-07 20:14:03] new line\n", encoding="utf-8")
    mixed = kb.read_worker_log("t_old")
    assert "plain old line" in mixed and "[2026-09-07 20:14:03] new line" in mixed


# --- the filter itself -------------------------------------------------------

def _run_filter(tmp_path, payload: bytes) -> str:
    """Feed ``payload`` through the filter and return the resulting log text.

    The input is a regular file, not a pipe: a pipe's buffer is ~64 KiB, so
    writing a large payload before anything drains it would deadlock the test
    itself. ``stamp_stream`` reads with ``os.read`` and is agnostic to the fd's
    kind, so this exercises the same code the pipe path runs.
    """
    src = tmp_path / "in.bin"
    src.write_bytes(payload)
    out = tmp_path / "out.log"
    in_fd = os.open(src, os.O_RDONLY)
    out_fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        stamp.stamp_stream(in_fd, out_fd)
    finally:
        os.close(out_fd)
        os.close(in_fd)
    return out.read_text(encoding="utf-8")


def test_filter_stamps_each_line_once(tmp_path):
    text = _run_filter(tmp_path, b"alpha\nbeta\n")
    lines = text.splitlines()
    assert len(lines) == 2
    assert all(STAMP_RE.match(line) for line in lines)
    assert [STAMP_RE.sub("", line) for line in lines] == ["alpha", "beta"]


def test_filter_preserves_empty_lines_and_trailing_partial(tmp_path):
    """Blank lines still get a stamp; a partial final line is not given a newline."""
    text = _run_filter(tmp_path, b"a\n\nno trailing newline")
    lines = text.split("\n")
    assert STAMP_RE.match(lines[0]) and lines[0].endswith("a")
    assert STAMP_RE.match(lines[1]) and STAMP_RE.sub("", lines[1]) == ""
    assert STAMP_RE.match(lines[2]) and lines[2].endswith("no trailing newline")
    assert not text.endswith("\n")


def test_filter_does_not_invent_newlines_in_a_long_unbroken_run(tmp_path):
    """A huge newline-free blob is flushed as-is; only the first chunk is stamped."""
    blob = b"x" * (stamp._MAX_PENDING + 512)
    text = _run_filter(tmp_path, blob + b"\ntail\n")
    assert text.count("\n") == 2
    first = text.split("\n")[0]
    assert STAMP_RE.match(first)
    assert STAMP_RE.sub("", first) == "x" * len(blob)
