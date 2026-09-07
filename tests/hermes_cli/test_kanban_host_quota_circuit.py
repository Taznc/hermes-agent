"""Host-wide, account-scoped Kanban quota circuit contracts."""
from __future__ import annotations

import contextlib
import io
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

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


def _guard(board: str, task_id: str, *, consume: bool = True):
    with kbc.connect(board=board) as conn:
        return kqc.task_quota_guard(conn, task_id, board=board, consume_probe=consume)


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
    assert circuit["state"] == "paused"
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
    assert kqc.list_quota_circuits(now=1_010)[0]["state"] == "recovering"

    monkeypatch.setattr(kqc.time, "time", lambda: 1_041)
    with kbc.connect(board="second") as conn:
        assert kbd.check_respawn_guard(
            conn, second, board="second", consume_host_probe=True,
        ) is None
    # Metering stays armed for the recovery window after the last granted
    # slot, then the row clears on its own.
    window = kqc._recovery_window_seconds()
    assert len(kqc.list_quota_circuits(now=1_071 + window - 1)) == 1
    assert kqc.list_quota_circuits(now=1_071 + window) == []
    assert kqc.clear_quota_circuit(kqc.sanitized_group_label("primary-wallet")) is False


def test_post_probe_recovery_serializes_contending_cards_across_boards(quota_home, monkeypatch):
    """Five cards on two boards contend at the post-probe boundary: exactly one
    start per spread window, host-wide, until the contenders drain. The cards
    were never observed during the pause, so metering cannot depend on
    previously recorded demand."""
    monkeypatch.setattr(kqc, "configured_budget_groups", lambda: BUDGET_GROUPS)
    boards = ["default", "second", "default", "second", "default", "second"]
    tasks = [_task(b, profile="implementer", provider="openai-codex") for b in boards]
    kqc.register_quota_circuit(
        "primary-wallet", retry_after=10, board="default", task_id=tasks[0],
        reason="rate_limit", max_seconds=3600, now=2_000,
    )
    monkeypatch.setattr(kqc.time, "time", lambda: 2_005)
    assert _guard(boards[0], tasks[0]) == "host_quota_circuit"

    monkeypatch.setattr(kqc.time, "time", lambda: 2_010)
    assert _guard(boards[0], tasks[0]) is None  # the single recovery probe

    waiting = list(zip(boards[1:], tasks[1:]))
    admitted: list[str] = []

    def contend(now: int) -> list:
        monkeypatch.setattr(kqc.time, "time", lambda: now)
        outcomes = []
        for board, task_id in list(waiting):
            outcome = _guard(board, task_id)
            outcomes.append(outcome)
            if outcome is None:
                # A real dispatcher claims the admitted card; it stops contending.
                waiting.remove((board, task_id))
                admitted.append(task_id)
        return outcomes

    for now in (2_041, 2_041, 2_050, 2_071, 2_101, 2_131, 2_161):
        outcomes = contend(now)
        assert outcomes.count(None) <= 1, (now, outcomes)
        for outcome in outcomes:
            assert outcome in (None, "host_quota_resume_spread"), (now, outcome)
    # Every contender was admitted exactly once across the serialized slots.
    assert sorted(admitted) == sorted(tasks[1:])
    assert waiting == []
    assert kqc.list_quota_circuits(now=2_161)[0]["state"] == "recovering"
    # The last slot was granted at 2_161; the meter stays armed for one
    # recovery window past it, then the circuit clears and starts are
    # unmetered again.
    settle = 2_161 + kqc._resume_spread_seconds() + kqc._recovery_window_seconds()
    assert len(kqc.list_quota_circuits(now=settle - 1)) == 1
    assert kqc.list_quota_circuits(now=settle) == []
    monkeypatch.setattr(kqc.time, "time", lambda: settle)
    assert [_guard(b, t) for b, t in zip(boards, tasks)] == [None] * 6


def test_dry_run_guard_peeks_without_consuming_probe_or_slot(quota_home, monkeypatch):
    monkeypatch.setattr(kqc, "configured_budget_groups", lambda: BUDGET_GROUPS)
    task = _task("default", profile="implementer", provider="openai-codex")
    kqc.register_quota_circuit(
        "primary-wallet", retry_after=10, board="default", task_id=task,
        reason="quota", max_seconds=100, now=1_000,
    )
    monkeypatch.setattr(kqc.time, "time", lambda: 1_010)
    assert _guard("default", task, consume=False) is None
    assert _guard("default", task, consume=False) is None
    assert kqc.list_quota_circuits(now=1_010)[0]["state"] == "paused"


def test_auto_route_fails_closed_unless_resolution_avoids_paused_group(quota_home, monkeypatch):
    monkeypatch.setattr(kqc, "configured_budget_groups", lambda: BUDGET_GROUPS)
    auto_task = _task("default", profile="implementer", provider="auto")
    kqc.register_quota_circuit(
        "primary-wallet", retry_after=300, board="default", task_id=auto_task,
        reason="rate_limit", max_seconds=3600, now=1_000,
    )
    monkeypatch.setattr(kqc.time, "time", lambda: 1_001)

    # A merely configured backup does not admit auto while resolution keeps
    # choosing the paused provider — repeated starts cannot hammer it.
    monkeypatch.setattr(kqc, "predict_auto_provider", lambda _profile: "openai-codex")
    for _ in range(3):
        assert _guard("default", auto_task) == "host_quota_circuit"
    # Unprovable resolution fails closed.
    monkeypatch.setattr(kqc, "predict_auto_provider", lambda _profile: None)
    assert _guard("default", auto_task) == "host_quota_circuit"
    assert kqc.list_quota_circuits(now=1_001)[0]["cards_deferred"] == 1
    # Resolution to an unpaused mapped provider dispatches.
    monkeypatch.setattr(kqc, "predict_auto_provider", lambda _profile: "anthropic")
    assert _guard("default", auto_task) is None
    # No circuit at all means no resolver call is needed.
    assert kqc.clear_quota_circuit(kqc.sanitized_group_label("primary-wallet"))
    monkeypatch.setattr(kqc, "predict_auto_provider", lambda _profile: pytest.fail("unused"))
    assert _guard("default", auto_task) is None


def test_auto_route_ignores_same_provider_group_for_unrelated_profile(quota_home, monkeypatch):
    groups = {
        "exhausted-implementer": {
            "providers": ["openai-codex"],
            "profiles": ["implementer"],
        },
        "healthy-implementer": {
            "providers": ["anthropic"],
            "profiles": ["implementer"],
        },
        "healthy-reviewer": {
            "providers": ["anthropic"],
            "profiles": ["reviewer"],
        },
    }
    monkeypatch.setattr(kqc, "configured_budget_groups", lambda: groups)
    auto_task = _task("default", profile="implementer", provider="auto")
    kqc.register_quota_circuit(
        "exhausted-implementer", retry_after=300, board="default", task_id=auto_task,
        reason="rate_limit", max_seconds=3600, now=1_000,
    )
    monkeypatch.setattr(kqc.time, "time", lambda: 1_001)
    monkeypatch.setattr(kqc, "predict_auto_provider", lambda _profile: "anthropic")

    # The implementer's healthy Anthropic account is unambiguous within that
    # profile. A reviewer's independent Anthropic account must not block it.
    assert _guard("default", auto_task) is None
    circuit = kqc.list_quota_circuits(now=1_001)
    assert len(circuit) == 1
    assert circuit[0]["state"] == "paused"


def test_auto_route_matches_profile_scoped_wildcard_provider(quota_home, monkeypatch):
    groups = {
        "exhausted-implementer": {
            "providers": ["openai-codex"],
            "profiles": ["implementer"],
        },
        "healthy-implementer-wildcard": {
            "providers": ["*"],
            "profiles": ["implementer"],
        },
    }
    monkeypatch.setattr(kqc, "configured_budget_groups", lambda: groups)
    auto_task = _task("default", profile="implementer", provider="auto")
    kqc.register_quota_circuit(
        "exhausted-implementer", retry_after=300, board="default", task_id=auto_task,
        reason="rate_limit", max_seconds=3600, now=1_000,
    )
    monkeypatch.setattr(kqc.time, "time", lambda: 1_001)
    monkeypatch.setattr(kqc, "predict_auto_provider", lambda _profile: "anthropic")

    assert _guard("default", auto_task) is None
    circuit = kqc.list_quota_circuits(now=1_001)
    assert len(circuit) == 1
    assert circuit[0]["state"] == "paused"


def test_auto_route_fails_closed_when_exact_and_wildcard_groups_overlap(quota_home, monkeypatch):
    groups = {
        "exhausted-implementer": {
            "providers": ["openai-codex"],
            "profiles": ["implementer"],
        },
        "healthy-implementer-exact": {
            "providers": ["anthropic"],
            "profiles": ["implementer"],
        },
        "healthy-implementer-wildcard": {
            "providers": ["*"],
            "profiles": ["implementer"],
        },
    }
    monkeypatch.setattr(kqc, "configured_budget_groups", lambda: groups)
    auto_task = _task("default", profile="implementer", provider="auto")
    kqc.register_quota_circuit(
        "exhausted-implementer", retry_after=300, board="default", task_id=auto_task,
        reason="rate_limit", max_seconds=3600, now=1_000,
    )
    monkeypatch.setattr(kqc.time, "time", lambda: 1_001)
    monkeypatch.setattr(kqc, "predict_auto_provider", lambda _profile: "anthropic")

    assert _guard("default", auto_task) == "host_quota_circuit"


def test_repeated_dispatch_ticks_never_start_auto_task_on_paused_provider(quota_home, monkeypatch):
    """End to end through ``dispatch_once``: an auto card whose profile keeps
    resolving to the exhausted provider is never spawned across many ticks,
    while a sibling auto card whose profile resolves elsewhere spawns."""
    groups = {
        **BUDGET_GROUPS,
        "reviewer-backup-wallet": {
            "providers": ["anthropic"],
            "profiles": ["reviewer"],
        },
    }
    monkeypatch.setattr(kqc, "configured_budget_groups", lambda: groups)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda: "unknown")
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    stuck = _task("default", profile="implementer", provider="auto")
    healthy = _task("second", profile="reviewer", provider="auto")
    kqc.register_quota_circuit(
        "primary-wallet", retry_after=600, board="default", task_id=stuck,
        reason="rate_limit", max_seconds=3600, now=1_000,
    )
    monkeypatch.setattr(kqc.time, "time", lambda: 1_001)
    monkeypatch.setattr(
        kqc, "predict_auto_provider",
        lambda profile: "openai-codex" if profile == "implementer" else "anthropic",
    )
    spawned: list[str] = []
    for _tick in range(4):
        for board in ("default", "second"):
            with kbc.connect(board=board) as conn:
                result = kbd.dispatch_once(
                    conn, board=board, dry_run=True, max_spawn=10,
                    max_in_progress=10, reconcile_orphans=False,
                )
                spawned.extend(task_id for task_id, _who, _ws in result.spawned)
                if board == "default":
                    assert (stuck, "host_quota_circuit") in result.respawn_guarded
    assert stuck not in spawned
    # The reviewer card shares the paused candidate group but its profile's
    # resolution lands on a provider mapped to an unpaused group, so it is
    # admitted every tick.
    assert spawned.count(healthy) == 4


def test_auto_prediction_runs_resolver_under_profile_home(quota_home, monkeypatch):
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    (quota_home / "profiles" / "implementer").mkdir(parents=True)
    seen: list[str] = []

    def fake_resolve(requested=None, **_kw):
        from hermes_constants import get_hermes_home
        seen.append(str(get_hermes_home()))
        assert requested == "auto"
        return "anthropic"

    from hermes_cli import auth
    monkeypatch.setattr(auth, "resolve_provider", fake_resolve)
    assert kqc.predict_auto_provider("implementer") == "anthropic"
    assert seen == [str(quota_home / "profiles" / "implementer")]

    # A profile whose config pins model.provider is resolved from that config,
    # exactly as the worker's own startup does.
    pinned_home = quota_home / "profiles" / "pinned"
    pinned_home.mkdir(parents=True)
    (pinned_home / "config.yaml").write_text("model:\n  provider: openai-codex\n", encoding="utf-8")
    assert kqc.predict_auto_provider("pinned") == "openai-codex"
    assert len(seen) == 1

    def broken(requested=None, **_kw):
        raise RuntimeError("no provider configured")

    monkeypatch.setattr(auth, "resolve_provider", broken)
    assert kqc.predict_auto_provider("implementer") is None
    # A profile whose home does not exist cannot boot a worker either;
    # prediction reports it as unprovable rather than guessing.
    assert kqc.predict_auto_provider("missing-profile") is None


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


def test_worker_publication_reads_budget_groups_from_host_config(quota_home, monkeypatch):
    """A worker runs under its assignee profile home, but the account map is
    host policy and therefore lives only in the shared/default config."""
    (quota_home / "config.yaml").write_text(
        """kanban:
  quota_budget_groups:
    primary-wallet:
      providers: [openai-codex]
      profiles: [implementer]
    backup-wallet:
      providers: [anthropic]
      profiles: [implementer]
""",
        encoding="utf-8",
    )
    profile_home = quota_home / "profiles" / "implementer"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "model:\n  provider: openai-codex\n",
        encoding="utf-8",
    )
    source = _task("default", profile="implementer", provider="auto")

    # Reproduce _default_spawn's worker environment: HERMES_HOME is the
    # assignee profile, while HERMES_KANBAN_HOME remains host-wide.
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
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


def test_quiet_cli_publishes_quota_circuit_then_exits_tempfail(quota_home, monkeypatch, capsys):
    import cli as cli_module

    monkeypatch.setattr(kqc, "configured_budget_groups", lambda: BUDGET_GROUPS)
    task_id = _task("default", profile="implementer", provider="auto")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)
    monkeypatch.setattr(kqc.time, "time", lambda: 6_000)
    result = {
        "failed": True,
        "failure_reason": "rate_limit",
        "error": "usage_limit_reached",
        "final_response": "",
        "reset_at": 6_300.0,
    }
    agent = SimpleNamespace(
        run_conversation=lambda **_kw: result,
        session_id="s1",
        provider="openai-codex",
    )
    cli = SimpleNamespace(agent=agent, session_id="s1", conversation_history=[])

    with pytest.raises(SystemExit) as exc:
        cli_module._run_quiet_single_query(cli, "work kanban task")
    assert exc.value.code == kb.KANBAN_RATE_LIMIT_EXIT_CODE
    assert "usage_limit_reached" in capsys.readouterr().err
    circuits = kqc.list_quota_circuits(now=6_001)
    assert len(circuits) == 1
    assert circuits[0]["reason"] == "rate_limit"
    assert circuits[0]["next_eligible_at"] == 6_300


def test_goal_mode_turn_two_quota_publishes_and_exits_tempfail(
    quota_home, monkeypatch, capsys,
):
    import cli as cli_module
    from hermes_cli import goals

    monkeypatch.setattr(kqc, "configured_budget_groups", lambda: BUDGET_GROUPS)
    task_id = _task("default", profile="implementer", provider="auto")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_KANBAN_GOAL_MODE", "1")
    monkeypatch.setattr(kqc.time, "time", lambda: 7_000)
    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *_args, **_kwargs: ("continue", "more work", False, None, False),
    )
    results = iter(
        [
            {"failed": False, "final_response": "first turn succeeded"},
            {
                "failed": True,
                "failure_reason": "rate_limit",
                "error": "usage_limit_reached",
                "final_response": "",
                "reset_at": 7_300.0,
            },
        ]
    )
    calls = 0

    def run_conversation(**_kwargs):
        nonlocal calls
        calls += 1
        return next(results)

    agent = SimpleNamespace(
        run_conversation=run_conversation,
        session_id="s1",
        provider="openai-codex",
    )
    cli = SimpleNamespace(agent=agent, session_id="s1", conversation_history=[])

    with pytest.raises(SystemExit) as exc:
        cli_module._run_quiet_single_query(cli, "work kanban task")

    assert exc.value.code == kb.KANBAN_RATE_LIMIT_EXIT_CODE
    assert calls == 2
    captured = capsys.readouterr()
    assert "usage_limit_reached" in captured.err
    assert "session_id: s1" in captured.err
    circuits = kqc.list_quota_circuits(now=7_001)
    assert len(circuits) == 1
    assert circuits[0]["reason"] == "rate_limit"
    assert circuits[0]["next_eligible_at"] == 7_300


@pytest.mark.parametrize(
    "deadline_fields",
    [{}, {"reset_at": "not-a-deadline", "retry_after_seconds": "30"}],
    ids=("missing", "malformed"),
)
def test_machine_quota_without_valid_deadline_uses_bounded_interruption_policy(
    quota_home, monkeypatch, deadline_fields,
):
    """A structured quota failure without a usable deadline may interrupt a
    few runs, but it must eventually reach the existing infra-interruption
    breaker instead of remaining an indefinitely neutral EX_TEMPFAIL loop."""
    import cli as cli_module

    monkeypatch.setattr(kqc, "configured_budget_groups", lambda: BUDGET_GROUPS)
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setenv("HERMES_KANBAN_MAX_INFRA_INTERRUPTIONS", "2")
    task_id = _task("default", profile="implementer", provider="openai-codex")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    result = {
        "failed": True,
        "failure_reason": "rate_limit",
        "error": "usage_limit_reached",
        **deadline_fields,
    }
    agent = SimpleNamespace(provider="openai-codex")
    worker_cli = SimpleNamespace(agent=agent)

    with kbc.connect(board="default") as conn:
        host = kb._claimer_id().split(":", 1)[0]
        for attempt, pid in enumerate((93400, 93401, 93402), start=1):
            claimed = kb.claim_task(conn, task_id, claimer=f"{host}:w{attempt}")
            assert claimed is not None
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, task_id))

            # Drive the same machine-readable result path as a quiet Kanban
            # worker. stderr is captured because the real dispatcher redirects
            # it into the run-scoped worker log.
            worker_stderr = io.StringIO()
            with contextlib.redirect_stderr(worker_stderr):
                exit_code = cli_module._kanban_worker_result_exit_code(worker_cli, result)
            with kbd._open_worker_log(claimed, "default") as log:
                log.write(worker_stderr.getvalue().encode())
            kbd._record_worker_exit(pid, exit_code << 8)
            crashed = kbd.detect_crashed_workers(conn, board="default")

            task = kb.get_task(conn, task_id)
            assert task is not None
            if attempt <= 2:
                assert crashed == []
                assert task.status == "ready"
                assert task.consecutive_failures == 0
                assert kb.read_interruption_streak(conn, task_id=task_id) == attempt
            else:
                assert crashed == [task_id]
                assert task.status == "blocked"
                assert task.consecutive_failures == 1
                assert kb.read_interruption_streak(conn, task_id=task_id) == 3

    assert kqc.list_quota_circuits() == []


def test_machine_readable_tempfail_opens_host_circuit_without_failure_budget(
    quota_home, monkeypatch,
):
    import cli as cli_module

    monkeypatch.setattr(kqc, "configured_budget_groups", lambda: BUDGET_GROUPS)
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kqc.time, "time", lambda: 8_000)
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
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
        result = {
            "failed": True,
            "failure_reason": "rate_limit",
            "error": "usage_limit_reached",
            "reset_at": 8_060.0,
        }
        worker_cli = SimpleNamespace(agent=SimpleNamespace(provider="openai-codex"))
        worker_stderr = io.StringIO()
        with contextlib.redirect_stderr(worker_stderr):
            exit_code = cli_module._kanban_worker_result_exit_code(worker_cli, result)
        assert exit_code == kb.KANBAN_RATE_LIMIT_EXIT_CODE
        assert "quota exhausted (429); retry after 60s" in worker_stderr.getvalue()
        with kbd._open_worker_log(task, "default") as log:
            log.write(worker_stderr.getvalue().encode())
        kbd._record_worker_exit(91234, exit_code << 8)
        assert kbd.detect_crashed_workers(conn, board="default") == []
        current = kb.get_task(conn, task_id)
        assert current is not None and current.consecutive_failures == 0
        assert kb.read_interruption_streak(conn, task_id=task_id) == 0
    assert len(kqc.list_quota_circuits(now=8_001)) == 1


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
    settle = 4_001 + kqc._resume_spread_seconds() + kqc._recovery_window_seconds()
    assert kqc.list_quota_circuits(now=settle) == []
    assert kqc.clear_quota_circuit(kqc.sanitized_group_label("primary-wallet")) is False


def test_recovery_meter_disarms_after_idle_window_even_with_stale_deferrals(quota_home, monkeypatch):
    """A card deferred during the pause that then vanished (blocked/deleted)
    must not keep the group metered forever: after the recovery window with
    no further admissions the circuit clears on its own."""
    monkeypatch.setattr(kqc, "configured_budget_groups", lambda: BUDGET_GROUPS)
    source = _task("default", profile="implementer", provider="openai-codex")
    vanished = _task("second", profile="reviewer", provider="openai-codex")
    kqc.register_quota_circuit(
        "primary-wallet", retry_after=10, board="default", task_id=source,
        reason="quota", max_seconds=100, now=1_000,
    )
    monkeypatch.setattr(kqc.time, "time", lambda: 1_005)
    assert _guard("second", vanished) == "host_quota_circuit"
    monkeypatch.setattr(kqc.time, "time", lambda: 1_010)
    assert _guard("default", source) is None
    settle = 1_010 + kqc._resume_spread_seconds() + kqc._recovery_window_seconds()
    assert len(kqc.list_quota_circuits(now=settle - 1)) == 1
    assert kqc.list_quota_circuits(now=settle) == []


def test_default_config_requires_explicit_groups_and_spreads_resume():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    kanban = DEFAULT_CONFIG["kanban"]
    assert kanban["quota_budget_groups"] == {}
    assert kanban["quota_resume_spread_seconds"] == 30
