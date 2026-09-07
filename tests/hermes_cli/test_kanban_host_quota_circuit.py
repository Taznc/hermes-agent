"""Host-wide, account-scoped Kanban quota circuit contracts."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_quota_circuit as kqc


@pytest.fixture
def quota_home(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert kqc.quota_circuit_db_path().resolve().is_relative_to(tmp_path.resolve())
    kb.init_db(board="default")
    kb.init_db(board="second")
    return home


BUDGET_GROUPS = {
    "primary-wallet": {
        "providers": ["openai-codex"],
        "profiles": ["implementer", "reviewer"],
    },
    "backup-wallet": {
        "providers": ["anthropic"],
        "profiles": ["implementer"],
    },
}


def _task(board: str, *, profile: str, provider: str) -> str:
    with kbc.connect(board=board) as conn:
        return kb.create_task(
            conn,
            title=f"{board}-{profile}-{provider}",
            assignee=profile,
            model_override="test-model",
            provider_override=provider,
        )


def test_budget_group_resolution_is_explicit_and_diagnostics_are_opaque(quota_home):
    primary = _task("default", profile="implementer", provider="openai-codex")
    unknown = _task("default", profile="other", provider="openai-codex")
    automatic = _task("default", profile="implementer", provider="auto")

    with kbc.connect(board="default") as conn:
        assert kqc.resolve_task_budget_group(conn, primary, config=BUDGET_GROUPS) == "primary-wallet"
        assert kqc.resolve_task_budget_group(conn, unknown, config=BUDGET_GROUPS) is None
        assert kqc.resolve_task_budget_group(conn, automatic, config=BUDGET_GROUPS) is None

    label = kqc.sanitized_group_label("customer-actual-account-id@example.com")
    assert label.startswith("budget-")
    assert "customer" not in label
    assert "@" not in label


def test_one_board_quota_event_guards_matching_routes_on_every_board(quota_home, monkeypatch):
    monkeypatch.setattr(kqc, "configured_budget_groups", lambda: BUDGET_GROUPS)
    source = _task("default", profile="implementer", provider="openai-codex")
    matching = _task("second", profile="reviewer", provider="openai-codex")
    unrelated = _task("second", profile="implementer", provider="anthropic")

    with kbc.connect(board="default") as conn:
        group = kqc.resolve_task_budget_group(conn, source)
        assert group == "primary-wallet"
        state = kqc.register_quota_circuit(
            group,
            retry_after=120,
            board="default",
            task_id=source,
            reason="quota",
            max_seconds=3600,
            now=1_000,
        )
        assert state is not None and state["next_eligible_at"] == 1_120

    monkeypatch.setattr(kqc.time, "time", lambda: 1_001)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda: "unknown")
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    with kbc.connect(board="second") as conn:
        assert kbd.check_respawn_guard(conn, matching, board="second") == "host_quota_circuit"
        assert kbd.check_respawn_guard(conn, unrelated, board="second") is None
        result = kbd.dispatch_once(
            conn,
            board="second",
            dry_run=True,
            max_spawn=10,
            max_in_progress=10,
            reconcile_orphans=False,
        )
        assert [task_id for task_id, _who, _ws in result.spawned] == [unrelated]
        assert (matching, "host_quota_circuit") in result.respawn_guarded

    circuits = kqc.list_quota_circuits(now=1_001)
    assert len(circuits) == 1
    circuit = circuits[0]
    assert circuit["group"].startswith("budget-")
    assert circuit["reason"] == "quota"
    assert circuit["first_observed_at"] == 1_000
    assert circuit["last_observed_at"] == 1_000
    assert circuit["boards_deferred"] == 1
    assert circuit["cards_deferred"] == 1
    assert "primary-wallet" not in repr(circuit)


def test_expiry_allows_one_probe_then_spreads_remaining_starts(quota_home, monkeypatch):
    monkeypatch.setattr(kqc, "configured_budget_groups", lambda: BUDGET_GROUPS)
    first = _task("default", profile="implementer", provider="openai-codex")
    second = _task("second", profile="reviewer", provider="openai-codex")
    kqc.register_quota_circuit(
        "primary-wallet", retry_after=10, board="default", task_id=first,
        reason="quota", max_seconds=100, now=1_000,
    )

    monkeypatch.setattr(kqc.time, "time", lambda: 1_010)
    with kbc.connect(board="default") as conn:
        assert kbd.check_respawn_guard(
            conn, first, board="default", consume_host_probe=True,
        ) is None
    with kbc.connect(board="second") as conn:
        assert kbd.check_respawn_guard(
            conn, second, board="second", consume_host_probe=True,
        ) == "host_quota_resume_spread"

    monkeypatch.setattr(kqc.time, "time", lambda: 1_041)
    with kbc.connect(board="second") as conn:
        assert kbd.check_respawn_guard(
            conn, second, board="second", consume_host_probe=True,
        ) is None
    assert kqc.list_quota_circuits(now=1_041) == []
    assert kqc.clear_quota_circuit(kqc.sanitized_group_label("primary-wallet")) is False


def test_worker_publishes_structured_deadline_before_tempfail_reap(quota_home, monkeypatch):
    routes = {
        **BUDGET_GROUPS,
        "backup-wallet": {
            "providers": ["anthropic"],
            "profiles": ["implementer"],
        },
    }
    monkeypatch.setattr(kqc, "configured_budget_groups", lambda: routes)
    source = _task("default", profile="implementer", provider="auto")
    published = kqc.publish_worker_quota_result(
        {
            "failed": True,
            "failure_reason": "rate_limit",
            "reset_at": 5_120.0,
        },
        task_id=source,
        board="default",
        provider="openai-codex",
        now=5_000,
        max_seconds=3_600,
    )
    assert published is not None
    assert published["next_eligible_at"] == 5_120
    assert len(kqc.list_quota_circuits(now=5_001)) == 1


def test_quiet_cli_publishes_quota_before_tempfail_exit():
    import inspect
    import cli as cli_module

    source = inspect.getsource(cli_module._run_quiet_single_query)
    publish = source.index("publish_worker_quota_result")
    exit_call = source.index("sys.exit(_exit_code)")
    assert publish < exit_call


def test_machine_readable_tempfail_opens_host_circuit_without_failure_budget(
    quota_home, monkeypatch,
):
    monkeypatch.setattr(kqc, "configured_budget_groups", lambda: BUDGET_GROUPS)
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    with kbc.connect(board="default") as conn:
        task_id = kb.create_task(
            conn,
            title="tempfail source",
            assignee="implementer",
            model_override="test-model",
            provider_override="openai-codex",
        )
        task = kb.claim_task(conn, task_id)
        assert task is not None
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (91234, task_id))
        with kbd._open_worker_log(task, "default") as log:
            log.write(b"quota exhausted (429); retry after 60s.\n")
        kbd._record_worker_exit(91234, kb.KANBAN_RATE_LIMIT_EXIT_CODE << 8)
        assert kbd.detect_crashed_workers(conn, board="default") == []
        current = kb.get_task(conn, task_id)
        assert current is not None and current.consecutive_failures == 0
    assert len(kqc.list_quota_circuits()) == 1


def test_simultaneous_failures_register_one_circuit_without_card_failures(
    quota_home, monkeypatch,
):
    monkeypatch.setattr(kqc, "configured_budget_groups", lambda: BUDGET_GROUPS)
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    boards = ("default", "second")
    profiles = ("implementer", "reviewer")
    pids = (92340, 92341)
    source_ids: list[str] = []
    for board, profile, pid in zip(boards, profiles, pids):
        with kbc.connect(board=board) as conn:
            task_id = kb.create_task(
                conn,
                title=f"simultaneous-{board}",
                assignee=profile,
                model_override="test-model",
                provider_override="openai-codex",
            )
            task = kb.claim_task(conn, task_id)
            assert task is not None
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, task_id))
            with kbd._open_worker_log(task, board) as log:
                log.write(b"quota exhausted (429); retry after 60s.\n")
            source_ids.append(task_id)
        kbd._record_worker_exit(pid, 1 << 8)

    def reap(index: int):
        with kbc.connect(board=boards[index]) as conn:
            return kbd.detect_crashed_workers(conn, board=boards[index])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reap, range(2)))
    assert results == [[], []]

    circuits = kqc.list_quota_circuits()
    assert len(circuits) == 1
    assert circuits[0]["observations"] == 2
    for board, task_id in zip(boards, source_ids):
        with kbc.connect(board=board) as conn:
            task = kb.get_task(conn, task_id)
            assert task is not None and task.consecutive_failures == 0
            assert task.status == "scheduled"


def test_manual_clear_uses_opaque_group_handle(quota_home):
    source = _task("default", profile="implementer", provider="openai-codex")
    kqc.register_quota_circuit(
        "primary-wallet", retry_after=60, board="default", task_id=source,
        reason="quota", max_seconds=100, now=3_000,
    )
    handle = kqc.sanitized_group_label("primary-wallet")
    assert kqc.clear_quota_circuit(handle) is True
    assert kqc.list_quota_circuits(now=3_000) == []


def test_diagnostics_sweep_expired_probe_without_another_matching_task(quota_home, monkeypatch):
    monkeypatch.setattr(kqc, "configured_budget_groups", lambda: BUDGET_GROUPS)
    source = _task("default", profile="implementer", provider="openai-codex")
    kqc.register_quota_circuit(
        "primary-wallet", retry_after=1, board="default", task_id=source,
        reason="quota", max_seconds=100, now=4_000,
    )
    monkeypatch.setattr(kqc.time, "time", lambda: 4_001)
    with kbc.connect(board="default") as conn:
        assert kqc.task_quota_guard(
            conn, source, board="default", consume_probe=True,
        ) is None
    assert kqc.list_quota_circuits(now=4_032) == []
    assert kqc.clear_quota_circuit(kqc.sanitized_group_label("primary-wallet")) is False


def test_default_config_requires_explicit_groups_and_spreads_resume():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    kanban = DEFAULT_CONFIG["kanban"]
    assert kanban["quota_budget_groups"] == {}
    assert kanban["quota_resume_spread_seconds"] == 30
