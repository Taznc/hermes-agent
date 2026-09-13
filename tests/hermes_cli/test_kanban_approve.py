"""``approve_review_task`` — the explicit reviewer verdict that PRESERVES the card.

The round-1 design read approval off ``complete_task`` from the review column.
That is unsafe for landing: ``complete_task`` sets ``done`` and immediately runs
``_cleanup_workspace()``, so the reviewed worktree is reaped before landing can
verify anything. Approval must therefore be its own terminal verdict that leaves
the card and its worktree exactly where the reviewer found them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_approve as ka
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _hand_to_review(conn, task_id: str) -> None:
    task = kb.claim_task(conn, task_id, claimer=f"lock-impl-{task_id}")
    assert task is not None
    assert kb.request_review(
        conn, task_id, summary="impl done", reviewer="reviewer",
        expected_run_id=task.current_run_id,
    )


def _reviewed(conn, *, title: str = "impl") -> str:
    """A card sitting in an active reviewer run, ready for a verdict."""
    task_id = kb.create_task(conn, title=title, assignee="dev-a")
    _hand_to_review(conn, task_id)
    assert kb.claim_review_task(conn, task_id, claimer="lock-rev") is not None
    return task_id


def test_approval_leaves_the_card_in_review_and_never_cleans_the_workspace(kanban_home, tmp_path):
    """The whole point: an approved card is NOT done. Its workspace must still be
    on disk when the attended landing runs, because that tree is the evidence."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with kbc.connect() as conn:
        task_id = _reviewed(conn)
        conn.execute(
            "UPDATE tasks SET workspace_kind = 'worktree', workspace_path = ? WHERE id = ?",
            (str(workspace), task_id),
        )
        conn.commit()

        ok, reason = ka.approve_review_task(conn, task_id, source_sha="a" * 40)
        task = kb.get_task(conn, task_id)

    assert (ok, reason) == (True, None)
    assert task.status == "review", "approval must not close the card"
    assert task.completed_at is None
    assert workspace.is_dir(), "approval must not reap the reviewed worktree"


def test_approval_records_the_reviewed_sha_on_its_run_and_event(kanban_home):
    """Approval is bound to an exact commit: what landed must be what was read."""
    with kbc.connect() as conn:
        task_id = _reviewed(conn)
        assert ka.approve_review_task(conn, task_id, source_sha="b" * 40)[0]

        run = conn.execute(
            "SELECT id, outcome, metadata FROM task_runs WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
        event = conn.execute(
            "SELECT payload, run_id FROM task_events WHERE task_id = ? AND kind = 'approved' "
            "ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()

    assert run["outcome"] == "approved"
    assert kb._json_dict(run["metadata"])["approved_sha"] == "b" * 40
    assert kb._json_dict(event["payload"])["approved_sha"] == "b" * 40
    assert event["run_id"] == run["id"], "the event must belong to the approving run"


def test_approval_releases_the_reviewer_claim_so_nothing_looks_live(kanban_home):
    with kbc.connect() as conn:
        task_id = _reviewed(conn)
        assert ka.approve_review_task(conn, task_id, source_sha="c" * 40)[0]
        task = kb.get_task(conn, task_id)
    assert task.claim_lock is None
    assert task.current_run_id is None


def test_the_dispatcher_will_not_respawn_an_approved_review_card(kanban_home):
    """An approved card sits in ``review`` waiting for an attended landing. The
    review lane must not hand it to another reviewer — that would open a second
    review cycle on a verdict that already exists."""
    from hermes_cli import kanban_db_dispatch as kd

    with kbc.connect() as conn:
        task_id = _reviewed(conn)
        assert kd.check_respawn_guard(conn, task_id, lane="review") is None
        assert ka.approve_review_task(conn, task_id, source_sha="e" * 40)[0]

        assert kd.check_respawn_guard(conn, task_id, lane="review") == "approved_awaiting_land"


def test_requesting_changes_after_an_approval_reopens_the_review_lane(kanban_home):
    """The guard is not a one-way door: a card sent back for changes and
    re-reviewed is spawnable again, so an approval can never wedge a card."""
    from hermes_cli import kanban_db_dispatch as kd

    with kbc.connect() as conn:
        task_id = _reviewed(conn)
        assert ka.approve_review_task(conn, task_id, source_sha="f" * 40)[0]
        # A human pulls it back for another round.
        assert kb.reopen_review_task(conn, task_id)
        _hand_to_review(conn, task_id)

        assert kd.check_respawn_guard(conn, task_id, lane="review") is None


def test_approval_requires_an_active_run_claimed_from_review(kanban_home):
    """An implementer cannot approve their own card: the verdict is only
    reachable from a run the review column handed out."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="self", assignee="dev-a")
        kb.claim_task(conn, task_id, claimer="lock-impl")
        ok, reason = ka.approve_review_task(conn, task_id, source_sha="d" * 40)
    assert ok is False
    assert reason == "active run was not claimed from review"


# ---------------------------------------------------------------------------
# CLI surface — `hermes kanban approve`
# ---------------------------------------------------------------------------


def _run_kanban(*tokens) -> tuple[str, int]:
    """Drive the real CLI entry point; returns (stdout, exit code)."""
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


def test_cli_approve_resolves_the_reviewed_sha_from_the_task_worktree(kanban_home, tmp_path):
    """The reviewer approves a card, not a commit id they had to look up: the
    command reads the worktree's own HEAD so the binding cannot be mistyped."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args, cwd=repo):
        env = {
            "HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    git("init", "-b", "main", ".")
    (repo / "f.txt").write_text("work\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "work")
    head = git("rev-parse", "HEAD")

    with kbc.connect() as conn:
        task_id = _reviewed(conn)
        conn.execute(
            "UPDATE tasks SET workspace_kind = 'worktree', workspace_path = ? WHERE id = ?",
            (str(repo), task_id),
        )
        conn.commit()

    out, rc = _run_kanban("approve", task_id)
    assert rc == 0, out
    assert head[:12] in out, "the approved commit must be reported to the reviewer"

    with kbc.connect() as conn:
        approval = ka.latest_approval(conn, task_id)
        assert kb.get_task(conn, task_id).status == "review"
    assert approval is not None and approval.approved_sha == head


def test_cli_approve_refuses_a_card_that_is_not_in_an_active_review_run(kanban_home):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="not reviewed", assignee="dev-a")
    out, rc = _run_kanban("approve", task_id)
    assert rc != 0
    assert "review" in out.lower()
