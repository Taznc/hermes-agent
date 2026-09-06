"""Live re-read of kanban dispatcher concurrency caps.

The dispatcher used to resolve ``kanban.max_in_progress`` /
``max_in_progress_per_profile`` ONCE, before its ``while`` loop, so retuning
concurrency required a gateway restart. On a busy host that restart SIGKILLs
every in-flight worker and discards its uncommitted worktree — the retune
destroyed exactly the work it was meant to schedule. ``_reload_dispatcher_settings``
is now called every tick, mirroring ``kanban.auto_decompose`` (#49638).

These are invariants, not snapshots: they assert how a config change relates to
the settings the next tick uses, never that a particular number is the default.
"""

from __future__ import annotations

from gateway.kanban_watchers_dispatcher import (
    _paused_board_slugs,
    _reload_dispatcher_settings,
    _resolve_dispatcher_settings,
)


class _KB:
    """Minimal stand-in for hermes_cli.kanban_db (only DEFAULT_FAILURE_LIMIT is read)."""

    DEFAULT_FAILURE_LIMIT = 2


def _settings(**kanban):
    return _resolve_dispatcher_settings(kanban, _KB())


def test_raising_the_global_cap_applies_without_a_restart():
    before = _settings(max_in_progress=4)
    assert before.max_in_progress == 4

    after = _reload_dispatcher_settings(lambda: {"kanban": {"max_in_progress": 8}}, _KB(), before)
    assert after.max_in_progress == 8, "a raised cap must reach the next tick"


def test_lowering_the_per_profile_cap_applies_without_a_restart():
    before = _settings(max_in_progress_per_profile=4)
    after = _reload_dispatcher_settings(
        lambda: {"kanban": {"max_in_progress_per_profile": 1}}, _KB(), before
    )
    assert after.max_in_progress_per_profile == 1


def test_unreadable_config_keeps_the_current_caps():
    """Fail safe: a transient read error must not widen a deliberate cap."""

    def _boom():
        raise OSError("config temporarily unreadable")

    before = _settings(max_in_progress=2, max_in_progress_per_profile=1)
    after = _reload_dispatcher_settings(_boom, _KB(), before)

    assert after.max_in_progress == before.max_in_progress
    assert after.max_in_progress_per_profile == before.max_in_progress_per_profile


def test_malformed_config_shape_keeps_the_current_caps():
    before = _settings(max_in_progress=3)
    after = _reload_dispatcher_settings(lambda: "not-a-mapping", _KB(), before)
    assert after.max_in_progress == before.max_in_progress


def test_interval_is_not_changed_by_a_reload():
    """The loop sleeps on the boot interval; reloading must not desync it."""
    before = _settings(dispatch_interval_seconds=30)
    after = _reload_dispatcher_settings(
        lambda: {"kanban": {"dispatch_interval_seconds": 600}}, _KB(), before
    )
    assert after.interval == before.interval


def test_other_dispatch_settings_also_track_config():
    before = _settings(failure_limit=2, default_assignee="alice")
    after = _reload_dispatcher_settings(
        lambda: {"kanban": {"failure_limit": 5, "default_assignee": "bob"}}, _KB(), before
    )
    assert after.failure_limit == 5
    assert after.default_assignee == "bob"


def test_reload_with_unchanged_config_is_a_no_op():
    cfg = {"kanban": {"max_in_progress": 6, "max_in_progress_per_profile": 2}}
    before = _resolve_dispatcher_settings(cfg["kanban"], _KB())
    after = _reload_dispatcher_settings(lambda: cfg, _KB(), before)
    assert after == before


def test_quiet_reload_does_not_emit_steady_state_info(caplog):
    """A per-tick re-read must not log the same cap once a minute forever."""
    import logging

    before = _settings(max_in_progress=4, max_in_progress_per_profile=2)
    with caplog.at_level(logging.INFO):
        _reload_dispatcher_settings(
            lambda: {"kanban": {"max_in_progress": 4, "max_in_progress_per_profile": 2}},
            _KB(),
            before,
        )
    assert not [r for r in caplog.records if r.levelno == logging.INFO], (
        "an unchanged reload should be silent"
    )


def test_a_changed_cap_is_logged(caplog):
    import logging

    before = _settings(max_in_progress=4)
    with caplog.at_level(logging.INFO):
        _reload_dispatcher_settings(lambda: {"kanban": {"max_in_progress": 9}}, _KB(), before)
    assert any("max_in_progress" in r.getMessage() for r in caplog.records), (
        "an operator retuning concurrency should see it land in the log"
    )


def test_paused_boards_are_identified_for_health_probe_exclusion():
    class _Result:
        def __init__(self, paused):
            self.dispatch_paused = paused

    results = [
        ("paused", _Result({"reason": "start_budget_exceeded"})),
        ("running", _Result(None)),
        ("failed", None),
    ]

    assert _paused_board_slugs(results) == {"paused"}
