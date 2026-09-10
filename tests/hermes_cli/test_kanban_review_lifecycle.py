"""Review-lifecycle tests: the first-class ``running -> review`` transition.

``request_review`` is the "implementation complete, awaiting review"
transition used by executor workers instead of encoding ``review-required:``
prose into a ``kanban_block`` call. The critical contract these tests pin
down:

* It transitions ``running``/``ready`` -> ``review`` and closes the active
  run with ``outcome="review_requested"``.
* It emits exactly one ``review_requested`` event carrying the handoff
  summary + implementer.
* Crucially, it is NOT a blocker: repeated review requests on the same task
  (a review -> rerun -> review follow-up cycle) never touch
  ``block_recurrences`` and never route to ``triage`` — the false
  ``block_loop_detected`` escalation that plagued the block-reason approach
  cannot happen.
* ``expected_run_id`` is honoured as a CAS guard so a stale/superseded
  worker cannot move the task.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _row(conn, tid):
    return conn.execute(
        "SELECT status, block_kind, block_recurrences, current_run_id "
        "FROM tasks WHERE id = ?",
        (tid,),
    ).fetchone()


def _events(conn, tid, kind=None):
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
        (tid,),
    ).fetchall()
    out = [
        (r["kind"], json.loads(r["payload"]) if r["payload"] else None)
        for r in rows
    ]
    if kind is not None:
        out = [e for e in out if e[0] == kind]
    return out


def _last_run(conn, tid):
    return conn.execute(
        "SELECT status, outcome, summary FROM task_runs "
        "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Happy path: running -> review
# ---------------------------------------------------------------------------


def test_request_review_transitions_running_to_review(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="impl a feature", assignee="worker")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert run_id is not None

        ok = kb.request_review(
            conn, tid,
            summary="Implementation complete\nfull details below",
            reviewer="reviewer",
            expected_run_id=run_id,
        )
        assert ok is True

        row = _row(conn, tid)
        assert row["status"] == "review"
        # The active run is closed and the pointer cleared.
        assert row["current_run_id"] is None
        # Not a block: recurrence machinery is untouched.
        assert (row["block_recurrences"] or 0) == 0
        assert row["block_kind"] is None

        run = _last_run(conn, tid)
        assert run["outcome"] == "review_requested"
        assert run["status"] == "review"

        # Exactly one review_requested event, with the handoff payload.
        rr = _events(conn, tid, kind="review_requested")
        assert len(rr) == 1
        payload = rr[0][1]
        assert payload["implementer"] == "worker"
        assert payload["reviewer"] == "reviewer"
        # First line of the summary rides the event payload.
        assert payload["summary"] == "Implementation complete"
        # No block / triage events were emitted.
        assert _events(conn, tid, kind="blocked") == []
        assert _events(conn, tid, kind="block_loop_detected") == []


# ---------------------------------------------------------------------------
# Core regression: repeated review requests never escalate to triage
# ---------------------------------------------------------------------------


def test_repeated_review_requests_never_triage(kanban_home: Path) -> None:
    """A task that goes review -> rerun -> review again (the executor
    follow-up cycle) must stay in ``review`` every time. Under the old
    ``kanban_block(review-required:)`` approach the second pass hit
    ``block_recurrences >= 2`` and was wrongly routed to ``triage`` with a
    ``block_loop_detected`` event. ``request_review`` must never do that."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="cycle me", assignee="worker")

        for _ in range(4):
            # Executor claims (ready->running or review->running) and finishes
            # with a review request. claim_review_task handles review->running.
            task = kb.get_task(conn, tid)
            if task.status == "ready":
                kb.claim_task(conn, tid)
            else:
                assert task.status == "review"
                claimed = kb.claim_review_task(conn, tid)
                assert claimed is not None

            run_id = kb.get_task(conn, tid).current_run_id
            ok = kb.request_review(
                conn, tid,
                summary="pass complete",
                expected_run_id=run_id,
            )
            assert ok is True
            row = _row(conn, tid)
            assert row["status"] == "review", "must never leave the review lane"
            assert (row["block_recurrences"] or 0) == 0

        # After several cycles: never triaged, never a false loop.
        assert _row(conn, tid)["status"] == "review"
        assert _events(conn, tid, kind="block_loop_detected") == []
        assert len(_events(conn, tid, kind="review_requested")) == 4


# ---------------------------------------------------------------------------
# CAS guard + bad-input behaviour
# ---------------------------------------------------------------------------


def test_request_review_expected_run_id_mismatch_is_noop(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="stale worker", assignee="worker")
        kb.claim_task(conn, tid)
        real_run = kb.get_task(conn, tid).current_run_id

        # A superseded worker passes a run id that is not the current one.
        ok = kb.request_review(conn, tid, expected_run_id=(real_run or 0) + 999)
        assert ok is False
        # Task is untouched — still running under the real run.
        row = _row(conn, tid)
        assert row["status"] == "running"
        assert row["current_run_id"] == real_run
        assert _events(conn, tid, kind="review_requested") == []


def test_request_review_unknown_task_returns_false(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        assert kb.request_review(conn, "t_deadbeefcafe") is False


def test_request_review_refuses_to_clear_live_claim_without_ownership(
    kanban_home: Path,
) -> None:
    """M1 regression: a run-id-less caller must not steal a live worker's claim.

    ``request_review`` on a running+claimed task without ``expected_run_id``
    fails with a distinct reason instead of silently NULLing claim_lock /
    worker_pid. ``force=True`` (explicit human override) and the worker path
    (``expected_run_id=<own run>``) both still work.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="live claim", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None

        # 1) No run id, no force -> refused with a distinct reason.
        ok, reason = kb.request_review(conn, tid, with_reason=True)
        assert ok is False
        assert reason is not None and "live claim" in reason
        row = conn.execute(
            "SELECT status, claim_lock, current_run_id FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
        assert row["status"] == "running"
        assert row["claim_lock"] is not None  # live claim untouched
        # bool-mode caller sees plain False.
        assert kb.request_review(conn, tid) is False

        # 2) Worker path: proving ownership via expected_run_id works.
        assert kb.request_review(
            conn, tid, summary="done", expected_run_id=claimed.current_run_id,
        ) is True
        assert kb.get_task(conn, tid).status == "review"

    # 3) force=True: explicit human override on a fresh live-claimed task.
    with kbc.connect() as conn:
        tid2 = kb.create_task(conn, title="forced", assignee="worker")
        assert kb.claim_task(conn, tid2) is not None
        assert kb.request_review(conn, tid2, summary="override", force=True) is True
        assert kb.get_task(conn, tid2).status == "review"


def test_request_review_malformed_provenance_gets_distinct_reason(
    kanban_home: Path,
) -> None:
    """M1 regression: malformed re-review provenance is a named failure, not
    the generic 'unknown id or not in running/ready'."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="provenance", assignee="builder")
        claimed = kb.claim_task(conn, tid)
        assert kb.request_review(
            conn, tid, summary="v1", reviewer="reviewer",
            expected_run_id=claimed.current_run_id,
        )
        review = kb.claim_review_task(conn, tid)
        assert review is not None
        assert kb.request_changes(
            conn, tid, reason="fix", expected_run_id=review.current_run_id,
        ) == (True, "builder")
        # Corrupt the changes_requested payload so re-review cannot recover
        # the prior reviewer.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET payload = '{\"reviewer\": 42}' "
                "WHERE task_id = ? AND kind = 'changes_requested'",
                (tid,),
            )
        retry = kb.claim_task(conn, tid, claimer="builder:retry")
        assert retry is not None
        ok, reason = kb.request_review(
            conn, tid, summary="v2",
            expected_run_id=retry.current_run_id, with_reason=True,
        )
        assert ok is False
        assert reason is not None and "provenance" in reason
        # Passing reviewer explicitly recovers, as the reason instructs.
        assert kb.request_review(
            conn, tid, summary="v2", reviewer="reviewer",
            expected_run_id=retry.current_run_id,
        ) is True


@pytest.mark.parametrize("blank", ["   ", "\n", "\t\n  "])
def test_request_review_whitespace_only_summary_does_not_crash(
    kanban_home: Path, blank: str
) -> None:
    """A whitespace-only handoff summary must not crash the review transition.

    Regression: the event-summary extraction tested the truthiness of the
    *pre-strip* value while indexing the *post-strip* (empty) list, so a
    summary like ``"   "`` is truthy, ``.strip()`` collapses it to ``""``,
    ``"".splitlines()`` is ``[]`` and ``[][0]`` raised ``IndexError`` inside
    ``write_txn`` — a 500 on the dashboard PATCH/bulk path, which forwards
    ``summary`` unstripped (the tool/CLI paths pre-strip to ``None`` and were
    never exposed). The transition must still succeed and the event must
    carry ``summary=None`` (whitespace collapses to no summary).
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="blank summary", assignee="worker")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id

        ok = kb.request_review(conn, tid, summary=blank, expected_run_id=run_id)
        assert ok is True
        assert kb.get_task(conn, tid).status == "review"

        rr = _events(conn, tid, kind="review_requested")
        assert len(rr) == 1
        # Whitespace collapses to no summary on the event payload.
        assert rr[0][1]["summary"] is None


# ---------------------------------------------------------------------------
# review -> done: a human can approve/close a task parked in review
# ---------------------------------------------------------------------------


def test_complete_task_closes_review_to_done(kanban_home: Path) -> None:
    """A task parked in ``review`` (with no active run — request_review
    closed it, so ``current_run_id IS NULL``, the #54823 shape) must be
    completable by a human approval via ``complete_task``."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="approve me", assignee="worker")
        kb.claim_task(conn, tid)
        kb.request_review(
            conn, tid, summary="ready",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "review"
        # The review lane has no active run — the exact state that used to
        # make `hermes kanban complete` a no-op (#54823).
        assert kb.get_task(conn, tid).current_run_id is None

        ok = kb.complete_task(conn, tid, summary="LGTM — merged", result="approved")
        assert ok is True
        assert kb.get_task(conn, tid).status == "done"
        assert _events(conn, tid, kind="completed")


# ---------------------------------------------------------------------------
# Wake plumbing: review_requested is a claimable terminal event for a sub
# ---------------------------------------------------------------------------


def test_review_requested_event_is_claimable_for_wake(kanban_home: Path) -> None:
    """The gateway kanban-notifier wakes an origin subscription by claiming
    unseen events whose kind is in its terminal set. ``review_requested`` is
    now in that set, so a wake subscription must see the event — and the
    subscription is NOT torn down (task is in ``review``, not done/archived),
    so later review cycles keep notifying."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="wake me", assignee="worker")
        kbn.add_notify_sub(
            conn,
            task_id=tid,
            platform="slack",
            chat_id="C123",
            thread_id="T1",
        )
        kb.claim_task(conn, tid)
        kb.request_review(
            conn, tid, summary="please review",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )

        # Same terminal set the notifier now uses (incl. review_requested).
        terminal_kinds = (
            "completed", "blocked", "gave_up", "crashed", "timed_out",
            "review_requested",
        )
        _old, _new, events = kbn.claim_unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="slack",
            chat_id="C123",
            thread_id="T1",
            kinds=terminal_kinds,
        )
        kinds_seen = [e.kind for e in events]
        assert "review_requested" in kinds_seen
        # Task is parked in review — the subscription must survive (only
        # done/archived tears it down), so subsequent cycles still wake.
        assert kb.get_task(conn, tid).status == "review"


# ---------------------------------------------------------------------------
# Dispatcher gate: operators may opt out of autonomous review dispatch
# ---------------------------------------------------------------------------


def test_review_dispatch_gate_prevents_phantom_reviewer(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``kanban.review_dispatch=false`` the dispatcher must NOT claim a
    task parked in ``review`` (this deployment explicitly waits for a human).
    Flipping the knob back on proves the gate, not
    something else, is what suppressed the claim."""
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="park", assignee="worker")
        kb.claim_task(conn, tid)
        kb.request_review(
            conn, tid, summary="done",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "review"

        # The assignee profile is spawnable — so ONLY the gate can stop the
        # review-column dispatch from claiming it.
        monkeypatch.setattr(profmod, "profile_exists", lambda name: True)

        # Gate OFF -> review task is left alone.
        monkeypatch.setattr(
            cfgmod, "load_config",
            lambda *a, **k: {"kanban": {"review_dispatch": False}},
        )
        res_off = kbd.dispatch_once(conn, dry_run=True)
        assert tid not in [s[0] for s in res_off.spawned]
        assert kb.get_task(conn, tid).status == "review"

        # Gate ON (the default; sdlc-review is bundled) -> the review task is
        # picked up by the dispatcher.
        monkeypatch.setattr(
            cfgmod, "load_config",
            lambda *a, **k: {"kanban": {"review_dispatch": True}},
        )
        res_on = kbd.dispatch_once(conn, dry_run=True)
        assert tid in [s[0] for s in res_on.spawned]


def test_active_pr_guard_skipped_for_review_lane_but_defers_ready_lane(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2 regression: a fresh PR-URL comment must not block reviewer spawns.

    A task parked in ``review`` with a PR link younger than 24h is the
    CANONICAL review handoff (worker opened a PR then requested review) —
    the review-lane dispatch must still claim/spawn it. The same comment on
    a ready-lane task is a duplicate-work signal and stays deferred.
    Rate-limit cooldown still applies in the review lane.
    """
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda *a, **k: {"kanban": {"review_dispatch": True}},
    )
    pr_comment = "Opened https://github.com/example/repo/pull/123 for review."

    with kbc.connect() as conn:
        # Review-lane task with a fresh PR comment.
        review_id = kb.create_task(conn, title="review me", assignee="reviewer")
        claimed = kb.claim_task(conn, review_id)
        assert claimed is not None
        kb.add_comment(conn, review_id, author="worker", body=pr_comment)
        assert kb.request_review(
            conn, review_id, summary="PR ready",
            expected_run_id=claimed.current_run_id,
        )
        # Ready-lane task with the same fresh PR comment.
        ready_id = kb.create_task(conn, title="already PRed", assignee="worker")
        kb.add_comment(conn, ready_id, author="worker", body=pr_comment)

        assert kbd.check_respawn_guard(conn, ready_id) == "active_pr"
        assert kbd.check_respawn_guard(conn, review_id, lane="review") is None

        res = kbd.dispatch_once(conn, dry_run=True)
        spawned_ids = [s[0] for s in res.spawned]
        guarded = dict(res.respawn_guarded)
        assert review_id in spawned_ids
        assert ready_id not in spawned_ids
        assert guarded.get(ready_id) == "active_pr"

        # Rate-limit cooldown still defers the review lane.
        _now = int(__import__("time").time())
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, "
                "started_at, ended_at) VALUES (?, 'reviewer', 'rate_limited', "
                "'rate_limited', ?, ?)",
                # ended_at strictly after the review-handoff run so the
                # "latest run" query deterministically picks this one.
                (review_id, _now, _now + 5),
            )
        assert kbd.check_respawn_guard(
            conn, review_id, lane="review"
        ) == "rate_limit_cooldown"


def test_active_pr_guard_ignores_unrelated_repo_and_non_worker_comments(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Regression for t_535b7818: any GitHub PR URL in any comment used to
    guard the task, so a research note quoting an upstream/unrelated repo's
    PR (or a human/orchestrator comment) permanently blocked dispatch even
    though this task never opened a PR of its own.

    Both scopes must independently fail to guard:
    * A PR URL for a DIFFERENT repo than this task's own worktree remote,
      posted by the task's own assignee -> no guard.
    * A PR URL for THIS task's own repo, but posted by someone other than
      the task's own assignee (a human/orchestrator "see also" note) ->
      no guard.
    * A PR URL for THIS task's own repo, posted by the task's own assignee
      -> guard fires (control, proves the scoping isn't simply disabled).
    """
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin",
         "https://github.com/acme-corp/widgets.git"],
        check=True, capture_output=True,
    )

    with kbc.connect() as conn:
        # Unrelated-repo PR cited by the task's OWN assignee.
        unrelated_id = kb.create_task(
            conn, title="unrelated repo PR cited", assignee="worker",
            workspace_kind="worktree", workspace_path=str(repo), branch_name="wt/unrelated",
        )
        kb.add_comment(
            conn, unrelated_id, author="worker",
            body="See prior art: https://github.com/NousResearch/hermes-agent/pull/79523",
        )
        assert kbd.check_respawn_guard(conn, unrelated_id) is None

        # Own-repo PR cited by someone who is NOT this task's assignee.
        wrong_author_id = kb.create_task(
            conn, title="own repo PR but not by assignee", assignee="worker",
            workspace_kind="worktree", workspace_path=str(repo), branch_name="wt/wrongauth",
        )
        kb.add_comment(
            conn, wrong_author_id, author="reviewer",
            body="For context, see https://github.com/acme-corp/widgets/pull/9",
        )
        assert kbd.check_respawn_guard(conn, wrong_author_id) is None

        # Control: own-repo PR cited BY this task's own assignee still guards.
        own_id = kb.create_task(
            conn, title="own repo PR by own assignee", assignee="worker",
            workspace_kind="worktree", workspace_path=str(repo), branch_name="wt/own",
        )
        kb.add_comment(
            conn, own_id, author="worker",
            body="Opened https://github.com/acme-corp/widgets/pull/42 for review.",
        )
        assert kbd.check_respawn_guard(conn, own_id) == "active_pr"


def test_review_dispatch_preserves_task_skills_and_adds_reviewer_skill(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(
        cfgmod,
        "load_config",
        lambda *args, **kwargs: {"kanban": {"review_dispatch": True}},
    )
    # The card forces `domain-specific-review`, and a forced skill is
    # preflighted against the assignee's own profile home — so the reviewer
    # profile has to really exist and really have it, or this card would be
    # building a worker that dies during initialization.
    reviewer_skill = kanban_home / "profiles" / "reviewer" / "skills" / "domain-specific-review"
    reviewer_skill.mkdir(parents=True)
    (reviewer_skill / "SKILL.md").write_text(
        '---\nname: domain-specific-review\ndescription: "Test skill."\n---\n\n# review\n',
        encoding="utf-8",
    )
    captured: list[list[str]] = []

    def spawn(task, workspace):
        captured.append(list(task.skills or []))
        return None

    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="domain review",
            assignee="reviewer",
            skills=["domain-specific-review"],
        )
        implementation = kb.claim_task(conn, task_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="ready",
            expected_run_id=implementation.current_run_id,
        )
        monkeypatch.setattr(
            kbd,
            "check_respawn_guard",
            lambda _conn, _task_id, **_kw: "rate_limit_cooldown",
        )
        guarded = kbd.dispatch_once(conn, spawn_fn=spawn)
        assert guarded.respawn_guarded == [(task_id, "rate_limit_cooldown")]
        assert not guarded.spawned
        guarded_task = kb.get_task(conn, task_id)
        assert guarded_task is not None
        assert guarded_task.status == "review"

        monkeypatch.setattr(kbd, "check_respawn_guard", lambda _conn, _task_id, **_kw: None)
        result = kbd.dispatch_once(conn, spawn_fn=spawn)

    assert task_id in [task[0] for task in result.spawned]
    assert captured == [["domain-specific-review", "sdlc-review"]]


def test_review_dispatch_honors_global_and_per_profile_caps(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda _name: True)
    monkeypatch.setattr(
        cfgmod,
        "load_config",
        lambda *args, **kwargs: {"kanban": {"review_dispatch": True}},
    )

    with kbc.connect() as conn:
        running_id = kb.create_task(conn, title="already running", assignee="builder")
        running = kb.claim_task(conn, running_id)
        assert running is not None

        review_ids: list[str] = []
        for title in ("review one", "review two"):
            task_id = kb.create_task(conn, title=title, assignee="reviewer")
            implementation = kb.claim_task(conn, task_id)
            assert implementation is not None
            assert kb.request_review(
                conn,
                task_id,
                summary="ready",
                expected_run_id=implementation.current_run_id,
            )
            review_ids.append(task_id)

        globally_capped = kbd.dispatch_once(
            conn,
            dry_run=True,
            max_in_progress=1,
        )
        assert not [
            task for task in globally_capped.spawned if task[0] in review_ids
        ]

        assert kb.complete_task(
            conn,
            running_id,
            expected_run_id=running.current_run_id,
        )
        global_dry_run = kbd.dispatch_once(
            conn,
            dry_run=True,
            max_in_progress=1,
        )
        assert len([
            task for task in global_dry_run.spawned if task[0] in review_ids
        ]) == 1

        per_profile_capped = kbd.dispatch_once(
            conn,
            dry_run=True,
            max_in_progress=10,
            max_in_progress_per_profile=1,
        )
        spawned_reviews = [
            task for task in per_profile_capped.spawned if task[0] in review_ids
        ]
        assert len(spawned_reviews) == 1
        assert len(per_profile_capped.skipped_per_profile_capped) == 1
        assert per_profile_capped.skipped_per_profile_capped[0][0] in review_ids


# ---------------------------------------------------------------------------
# reopen: a follow-up sends a review task back out for another pass
# ---------------------------------------------------------------------------


def test_reopen_review_task_returns_to_ready(kanban_home: Path) -> None:
    """The "changes requested" / follow-up path: a task parked in ``review``
    goes back to ``ready`` so the dispatcher re-runs the implementer. It must
    NOT touch ``block_recurrences`` (review was never a block)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="reopen me", assignee="worker")
        kb.claim_task(conn, tid)
        kb.request_review(
            conn, tid, summary="v1", reviewer="reviewer",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        reviewing = kb.get_task(conn, tid)
        assert reviewing is not None
        assert reviewing.status == "review"
        assert reviewing.assignee == "reviewer"

        ok = kb.reopen_review_task(conn, tid)
        assert ok is True
        row = _row(conn, tid)
        assert row["status"] == "ready"
        reopened = kb.get_task(conn, tid)
        assert reopened is not None
        assert reopened.assignee == "worker"
        assert row["current_run_id"] is None
        assert (row["block_recurrences"] or 0) == 0
        assert _events(conn, tid, kind="review_reopened")

        # Idempotent: not in review anymore -> reopening again is a no-op.
        assert kb.reopen_review_task(conn, tid) is False


def test_review_cycle_end_to_end(kanban_home: Path) -> None:
    """Full loop: run -> review -> follow-up reopen -> re-run -> review ->
    approve -> done. Never blocks, never triages, and stays wake-subscribed
    until done."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="cycle", assignee="worker")

        # Pass 1: implement -> review.
        kb.claim_task(conn, tid)
        kb.request_review(
            conn, tid, summary="v1",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "review"

        # Human asks for changes -> reopen -> re-run.
        assert kb.reopen_review_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "ready"
        kb.claim_task(conn, tid)
        kb.request_review(
            conn, tid, summary="v2",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "review"

        # Human approves.
        assert kb.complete_task(conn, tid, summary="approved") is True
        row = _row(conn, tid)
        assert row["status"] == "done"
        assert (row["block_recurrences"] or 0) == 0
        assert _events(conn, tid, kind="block_loop_detected") == []


# ---------------------------------------------------------------------------
# never-claimed 'ready' task: handoff must survive via a synthesized run
# ---------------------------------------------------------------------------


def test_request_review_on_unclaimed_ready_synthesizes_run(kanban_home: Path) -> None:
    """A manual/CLI request-review on a never-claimed ``ready`` task has no
    active run to close. The handoff summary must still be preserved on a
    synthesized run so the reviewer keeps the context."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ready then review", assignee="worker")
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.get_task(conn, tid).current_run_id is None

        ok = kb.request_review(conn, tid, summary="done without a claim")
        assert ok is True
        assert kb.get_task(conn, tid).status == "review"

        run = _last_run(conn, tid)
        assert run is not None
        assert run["outcome"] == "review_requested"
        assert run["summary"] == "done without a claim"
        # Exactly one review_requested event, carrying the handoff summary.
        evs = _events(conn, tid, kind="review_requested")
        assert len(evs) == 1
        assert evs[0][1]["summary"] == "done without a claim"


def test_reviewer_reassigns_for_autonomous_dispatch(kanban_home: Path) -> None:
    """An explicit reviewer routes the review run while preserving implementer provenance."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="route reviewer", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        ok = kb.request_review(
            conn, tid, summary="v1", reviewer="lead-reviewer",
            expected_run_id=claimed.current_run_id,
        )
        assert ok is True
        assert kb.get_task(conn, tid).assignee == "lead-reviewer"
        ev = _events(conn, tid, kind="review_requested")[0][1]
        assert ev["reviewer"] == "lead-reviewer"
        assert ev["implementer"] == "worker"


# ---------------------------------------------------------------------------
# Mergeability preflight on `hermes kanban request-review` (task t_11421628).
#
# The preflight landed on the tool handler only (t_3e83300c), leaving the CLI as
# a second, ungated door into the review lane — and the CLI is exactly what a
# worker refused by the tool would reach for next, which would make the
# tool-side gate advisory rather than real. These tests pin the two doors to one
# verdict.
#
# Real git repositories throughout, mirroring tests/tools/test_kanban_tools.py:
# git's own merge machinery is what decides, so a mocked `git` would assert
# nothing. They drive the real CLI entry point (build_parser -> kanban_command)
# rather than the handler, so the argparse wiring is covered too.
# ---------------------------------------------------------------------------


def _git(repo, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _make_origin(root):
    """A repo with ``main`` (base) and ``dev`` (base + an edit to f.txt)."""
    origin = root / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@example.invalid")
    _git(origin, "config", "user.name", "t")
    (origin / "f.txt").write_text("line1\nline2\n", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "base")
    base = _git(origin, "rev-parse", "HEAD")
    _git(origin, "checkout", "-q", "-b", "dev")
    (origin / "f.txt").write_text("line1-FROM-DEV\nline2\n", encoding="utf-8")
    _git(origin, "commit", "-qam", "dev moves f.txt")
    _git(origin, "checkout", "-q", "main")
    return origin, base


def _make_workspace(root, origin, base, *, conflicting: bool):
    """A clone branched off ``base``; ``conflicting`` decides whether its edit
    collides with what ``origin/dev`` did to the same line."""
    ws = root / "ws"
    _git(root, "-c", "init.defaultBranch=main", "clone", "-q", str(origin), str(ws))
    _git(ws, "config", "user.email", "t@example.invalid")
    _git(ws, "config", "user.name", "t")
    _git(ws, "checkout", "-q", "-b", "feature", base)
    if conflicting:
        (ws / "f.txt").write_text("line1-FROM-FEATURE\nline2\n", encoding="utf-8")
    else:
        (ws / "untouched-by-dev.txt").write_text("safe\n", encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "feature work")
    return ws


def _run_kanban(*tokens) -> tuple[str, int]:
    """Drive the real CLI entry point; returns (combined output, exit code)."""
    import argparse
    import contextlib
    import io

    from hermes_cli import kanban as kc

    buf = io.StringIO()
    parser = argparse.ArgumentParser()
    kanban_parser = kc.build_parser(parser.add_subparsers(dest="_top"))
    args = kanban_parser.parse_args(list(tokens))
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = kc.kanban_command(args)
    return buf.getvalue(), rc


@pytest.fixture
def cli_mergeability_env(monkeypatch, tmp_path):
    """Factory: a worker task whose workspace is a real git clone, on a board
    with a real ``land_target``. Returns ``make(conflicting=...)`` ->
    ``(task_id, workspace_path)``."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    repos = tmp_path / "repos"
    repos.mkdir()
    origin, base = _make_origin(repos)

    def make(*, conflicting: bool, land_target: str = "origin/dev",
             workspace_path=None, status: str = "running"):
        """``status`` selects the card state under test: ``running`` (claimed,
        the ordinary worker case), ``ready`` (never claimed), ``todo`` (held by
        an unfinished parent), or ``done`` (claimed then completed)."""
        ws = _make_workspace(repos, origin, base, conflicting=conflicting)
        kb._INITIALIZED_PATHS.clear()
        kb.init_db()
        if land_target:
            kb.write_board_metadata(None, land_target=land_target)
        with kbc.connect_closing() as conn:
            parents = ()
            if status == "todo":
                parents = (kb.create_task(
                    conn, title="unfinished parent", assignee="test-worker"),)
            tid = kb.create_task(
                conn, title="cli mergeability", assignee="test-worker",
                workspace_kind="worktree", parents=parents,
                workspace_path=str(ws if workspace_path is None else workspace_path))
            claimed = None
            if status in ("running", "done"):
                claimed = kb.claim_task(conn, tid)
                assert claimed is not None
            if status == "done":
                assert kb.complete_task(
                    conn, tid, summary="done", expected_run_id=claimed.current_run_id)
            assert kb.get_task(conn, tid).status == status
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        # request_review only clears a live claim with proof of ownership, which
        # the real dispatcher supplies through this env var at spawn time.
        if claimed is not None:
            monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
        else:
            monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
        return tid, ws

    return make


def _task_events(tid, kind=None):
    with kbc.connect_closing() as conn:
        evs = kb.list_events(conn, tid)
    return [e for e in evs if kind is None or e.kind == kind]


def _assert_untouched_cli_handoff(tid):
    """The pre-gate contract: the handoff succeeded, stamped nothing, and
    recorded nothing — byte-identical to the CLI's behavior before the gate."""
    requested = _task_events(tid, "review_requested")
    assert len(requested) == 1
    with kbc.connect_closing() as conn:
        run = kb.get_run(conn, requested[0].run_id)
    assert run is not None
    assert "mergeable_against" not in (run.metadata or {})
    assert not _task_events(tid, "review_preflight_conflict")


def test_cli_request_review_refuses_a_branch_conflicting_with_the_land_target(
    cli_mergeability_env,
) -> None:
    """A handoff made through the CLI on a worktree that conflicts with the
    board's ``land_target`` is refused with the same message and writes the
    same ``review_preflight_conflict`` event as the tool path."""
    tid, _ws = cli_mergeability_env(conflicting=True)

    out, rc = _run_kanban("request-review", tid, "--summary", "implemented the thing")

    assert rc != 0, out
    assert "f.txt" in out, out
    assert "git merge origin/dev" in out, out

    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "running"
    assert not _task_events(tid, "review_requested")

    conflicts = _task_events(tid, "review_preflight_conflict")
    assert len(conflicts) == 1
    assert conflicts[0].payload["target"] == "origin/dev"
    assert conflicts[0].payload["paths"] == ["f.txt"]


def test_cli_request_review_stamps_the_target_it_verified_when_mergeable(
    cli_mergeability_env,
) -> None:
    """A clean-merging worktree is handed off exactly as before, plus the proof
    of what it was checked against — the same stamp the tool path writes."""
    tid, ws = cli_mergeability_env(conflicting=False)

    out, rc = _run_kanban("request-review", tid, "--summary", "implemented the thing")
    assert rc == 0, out

    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "review"

    requested = _task_events(tid, "review_requested")
    assert len(requested) == 1
    with kbc.connect_closing() as conn:
        run = kb.get_run(conn, requested[0].run_id)
    assert run is not None
    target, _, sha = run.metadata["mergeable_against"].partition("@")
    assert target == "origin/dev"
    assert sha == _git(ws, "rev-parse", "origin/dev")

    assert not _task_events(tid, "review_preflight_conflict")


def test_cli_request_review_ignores_the_conflict_when_the_preflight_is_disabled(
    cli_mergeability_env,
) -> None:
    """``kanban.require_mergeable_for_review: false`` is a real off switch on
    the CLI too — written into a real config.yaml rather than patched onto a
    reader, so the resolution chain the operator actually edits is what is
    proven. This is the documented escape hatch for a human override."""
    tid, _ws = cli_mergeability_env(conflicting=True)
    (Path.home() / ".hermes" / "config.yaml").write_text(
        json.dumps({"kanban": {"require_mergeable_for_review": False}}), encoding="utf-8")

    out, rc = _run_kanban("request-review", tid, "--summary", "implemented the thing")
    assert rc == 0, out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "review"
    _assert_untouched_cli_handoff(tid)


def test_cli_request_review_skips_the_preflight_without_a_land_target(
    cli_mergeability_env,
) -> None:
    """With no ``land_target`` there is nothing to merge against and the
    preflight must not invent one; the same conflicting branch is handed off."""
    tid, _ws = cli_mergeability_env(conflicting=True, land_target="")

    out, rc = _run_kanban("request-review", tid, "--summary", "implemented the thing")
    assert rc == 0, out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "review"
    _assert_untouched_cli_handoff(tid)


def test_cli_request_review_skips_the_preflight_on_a_non_git_workspace(
    cli_mergeability_env, tmp_path: Path,
) -> None:
    """A scratch (non-git) workspace has no HEAD to merge, so the preflight
    fails open rather than refusing work it cannot judge."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    tid, _ws = cli_mergeability_env(conflicting=True, workspace_path=plain)

    out, rc = _run_kanban("request-review", tid, "--summary", "implemented the thing")
    assert rc == 0, out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "review"
    _assert_untouched_cli_handoff(tid)


def test_cli_request_review_fails_open_when_the_land_target_is_unfetchable(
    cli_mergeability_env, tmp_path: Path,
) -> None:
    """An unreachable remote is an infrastructure problem, not a verdict on the
    branch — the CLI inherits the tool path's fail-open contract unchanged."""
    tid, ws = cli_mergeability_env(conflicting=True)
    _git(ws, "remote", "set-url", "origin", str(tmp_path / "gone"))

    out, rc = _run_kanban("request-review", tid, "--summary", "implemented the thing")
    assert rc == 0, out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "review"
    _assert_untouched_cli_handoff(tid)


# ---------------------------------------------------------------------------
# Gate ordering: status before mergeability (task t_fd4e3978).
#
# The preflight used to run in front of the status check, so a card that could
# not enter the review lane at all was answered with a merge-conflict refusal
# that additionally asserted it was "still running". The ordering lives in the
# shared helper, so these mirror the tool-side cases exactly — same conflicting
# worktree, same gate ON, only the card's status varies.
# ---------------------------------------------------------------------------


def _assert_status_answer_not_merge_refusal(out: str) -> None:
    """The refusal must be about the card's state, not about git. Asserted
    negatively too: naming the conflicting path or the fix command would mean
    the merge gate answered a question it has no business answering."""
    assert "f.txt" not in out, out
    assert "git merge origin/dev" not in out, out
    assert "still running" not in out, out


def test_cli_request_review_on_a_done_card_answers_status_not_mergeability(
    cli_mergeability_env,
) -> None:
    """A completed card is not the worker's to hand off; the CLI must say so
    rather than hand back the merge-conflict refusal (the regression the
    reviewer measured on this exact path)."""
    tid, _ws = cli_mergeability_env(conflicting=True, status="done")

    out, rc = _run_kanban("request-review", tid, "--summary", "implemented the thing")

    assert rc != 0, out
    _assert_status_answer_not_merge_refusal(out)
    assert "running/ready" in out, out

    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "done"
    assert not _task_events(tid, "review_preflight_conflict")


def test_cli_request_review_on_a_todo_card_answers_status_not_mergeability(
    cli_mergeability_env,
) -> None:
    """A never-claimed card held in ``todo`` by an unfinished parent is gated
    on that parent, not on git."""
    tid, _ws = cli_mergeability_env(conflicting=True, status="todo")

    out, rc = _run_kanban("request-review", tid, "--summary", "implemented the thing")

    assert rc != 0, out
    _assert_status_answer_not_merge_refusal(out)
    assert "parent" in out, out

    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "todo"
    assert not _task_events(tid, "review_preflight_conflict")


def test_cli_request_review_refusal_states_the_status_the_card_is_actually_in(
    cli_mergeability_env,
) -> None:
    """``ready`` is reviewable, so a conflicting ``ready`` card is still
    refused by the merge gate — but the refusal describes the card it is
    holding instead of asserting it is "still running"."""
    tid, _ws = cli_mergeability_env(conflicting=True, status="ready")

    out, rc = _run_kanban("request-review", tid, "--summary", "implemented the thing")

    assert rc != 0, out
    assert "f.txt" in out, out
    assert "still ready" in out, out
    assert "still running" not in out, out

    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"
    assert len(_task_events(tid, "review_preflight_conflict")) == 1


def test_both_doors_refuse_a_conflicting_branch_with_the_identical_message(
    cli_mergeability_env,
) -> None:
    """Parity is the constraint that keeps the gate real: a worker refused by
    the tool must not get a different (or differently-worded) answer by
    shelling out to the CLI. Both doors are driven against ONE card — a
    refusal mutates nothing but the event log — and their text must match,
    including the status sentence."""
    from tools import kanban_tools as kt

    tid, _ws = cli_mergeability_env(conflicting=True, status="ready")

    tool_error = json.loads(
        kt._handle_request_review({"task_id": tid, "summary": "implemented the thing"})
    ).get("error", "")
    cli_out, rc = _run_kanban("request-review", tid, "--summary", "implemented the thing")

    assert rc != 0, cli_out
    assert tool_error, tool_error
    # The CLI prints the same message through _err(); compare the refusal body
    # line-for-line rather than assuming identical framing.
    for line in tool_error.splitlines():
        assert line in cli_out, (line, cli_out)
    assert "still ready" in tool_error and "still ready" in cli_out

    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"
    assert len(_task_events(tid, "review_preflight_conflict")) == 2
