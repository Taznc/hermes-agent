"""Contract tests for the dedicated systemd worker-slice argv."""

import tools.process_registry as pr


def test_scope_argv_assigns_worker_slice_and_resource_guards(monkeypatch):
    """Every restart-safe worker scope joins the worker slice with its guards."""
    monkeypatch.setattr(pr, "_worker_memory_max_bytes", lambda: 4 * 1024**3)

    argv = pr._systemd_scope_argv(
        "/usr/bin/systemd-run",
        "hermes-worker-kanban-contract",
        "/bin/true",
    )

    assert "--slice=hermes-workers.slice" in argv
    assert "--property=MemoryHigh=3G" in argv
    assert "--property=TimeoutStopSec=30s" in argv
    assert "MemoryMax=4294967296" in argv
    assert argv[argv.index("--") + 1:] == ["/bin/true"]
