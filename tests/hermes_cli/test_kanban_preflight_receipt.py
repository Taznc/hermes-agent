from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_db_receipt as kbr


_KANBAN_ENV = (
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_PIN_HOME",
)


@pytest.fixture
def isolated_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name in _KANBAN_ENV:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    db_path = home / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    assert kb.kanban_db_path() == db_path
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn, home


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo_with_unrelated_anchor(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    scripts = repo / "scripts"
    scripts.mkdir()
    runner = scripts / "run_tests.sh"
    runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runner.chmod(0o755)
    (repo / "uv.lock").write_text("lock-v1\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("declared base\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt", "scripts/run_tests.sh", "uv.lock")
    _git(repo, "commit", "-m", "declared base")
    declared = _git(repo, "rev-parse", "HEAD")
    (repo / "anchor-only.txt").write_text("wrong anchor\n", encoding="utf-8")
    _git(repo, "add", "anchor-only.txt")
    _git(repo, "commit", "-m", "unrelated anchor head")
    anchor = _git(repo, "rev-parse", "HEAD")
    return repo, declared, anchor


def test_new_worktree_starts_at_declared_base_not_anchor_head(
    isolated_board, tmp_path: Path
):
    conn, _ = isolated_board
    repo, declared, anchor = _repo_with_unrelated_anchor(tmp_path)
    task_id = kb.create_task(
        conn,
        title="correct base",
        assignee="worker",
        workspace_kind="worktree",
        workspace_path=str(repo),
    )
    task = kb.get_task(conn, task_id)
    assert task is not None

    workspace, branch = kbw._resolve_worktree_workspace(task, base_ref=declared)

    assert branch == f"wt/{task_id}"
    assert _git(workspace, "rev-parse", "HEAD") == declared
    assert _git(workspace, "rev-parse", "HEAD") != anchor
    assert not (workspace / "anchor-only.txt").exists()


def test_reviewer_binding_rejects_workspace_at_different_artifact(
    isolated_board, tmp_path: Path
):
    conn, _ = isolated_board
    repo, artifact, _ = _repo_with_unrelated_anchor(tmp_path)
    workspace = repo / ".worktrees" / "review"
    _git(repo, "worktree", "add", "-b", "wt/review", str(workspace), "HEAD")
    task_id = kb.create_task(
        conn,
        title="review exact artifact",
        assignee="reviewer",
        workspace_kind="worktree",
        workspace_path=str(workspace),
        branch_name="wt/review",
    )
    task = kb.get_task(conn, task_id)
    assert task is not None

    with pytest.raises(kbr.PreflightError, match="review artifact mismatch"):
        kbr.validate_workspace_binding(
            workspace,
            task,
            base_ref=artifact,
            artifact_ref=artifact,
            role="reviewer",
        )


def test_receipt_cache_reuses_identical_inputs_and_invalidates_changes(
    isolated_board, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    conn, _ = isolated_board
    repo, declared, _ = _repo_with_unrelated_anchor(tmp_path)
    task_id = kb.create_task(
        conn,
        title="cached preflight",
        body="Edit-Targets: hermes_cli/kanban_db.py, tests/hermes_cli/\n",
        assignee="worker",
        workspace_kind="worktree",
        workspace_path=str(repo),
        branch_name=f"wt/{task_id}" if False else None,
    )
    task = kb.get_task(conn, task_id)
    assert task is not None
    workspace, _ = kbw._resolve_worktree_workspace(task, base_ref=declared)
    task = kb.get_task(conn, task_id)
    assert task is not None
    task.workspace_path = str(workspace)
    task.branch_name = f"wt/{task_id}"
    task.claim_lock = "host:111"

    first = kbr.build_preflight_receipt(
        task,
        workspace,
        board=None,
        base_ref=declared,
        artifact_ref=None,
        role="implementer",
    )
    second = kbr.build_preflight_receipt(
        task,
        workspace,
        board=None,
        base_ref=declared,
        artifact_ref=None,
        role="implementer",
    )
    (workspace / "uv.lock").write_text("lock-v2\n", encoding="utf-8")
    changed = kbr.build_preflight_receipt(
        task,
        workspace,
        board=None,
        base_ref=declared,
        artifact_ref=None,
        role="implementer",
    )

    assert first.reused is False
    assert second.reused is True
    assert second.path == first.path
    assert changed.reused is False
    assert changed.path != first.path
    payload = json.loads(first.path.read_text(encoding="utf-8"))
    assert payload["workspace"]["repo"] == str(repo.resolve())
    assert payload["workspace"]["base_sha"] == declared
    assert payload["workspace"]["head_sha"] == declared
    assert payload["workspace"]["branch"] == f"wt/{task_id}"
    assert payload["claim_identity"] == {"lock": "host:111", "run_id": None}
    assert payload["lock_identity"][0]["path"] == "uv.lock"
    assert len(payload["lock_identity"][0]["sha256"]) == 64
    assert payload["interpreter"]["executable"]
    assert payload["commands"]["test"][0].endswith("scripts/run_tests.sh")
    assert payload["commands"]["test"][1:] == ["tests/hermes_cli/"]
    assert payload["commands"]["lint"][1:4] == ["-m", "ruff", "check"]
    assert payload["artifacts"]["full_log"].endswith(".log")
    assert payload["summary"]["exit_code"] == 0
    assert payload["summary"]["checks_failed"] == 0

    monkeypatch.setenv("HERMES_KANBAN_PREFLIGHT_RECEIPT", str(first.path))
    packet = kb.build_worker_task_packet(conn, task_id).to_dict()
    packet_preflight = packet["workspace"]["preflight"]
    assert packet_preflight["summary"]["exit_code"] == 0
    full_log = Path(payload["artifacts"]["full_log"]).read_text(encoding="utf-8")
    assert full_log not in json.dumps(packet)

    task.current_run_id = 42
    next_claim = kbr.build_preflight_receipt(
        task,
        workspace,
        board=None,
        base_ref=declared,
        artifact_ref=None,
        role="implementer",
    )
    assert next_claim.reused is False
    assert next_claim.path not in {first.path, changed.path}


def test_packet_reads_bounded_receipt_summary_not_full_log(
    isolated_board, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    conn, _ = isolated_board
    task_id = kb.create_task(conn, title="packet receipt", assignee="worker")
    receipt = tmp_path / "receipt.json"
    full_log = tmp_path / "preflight.log"
    secret_log_line = "FULL-LOG-ONLY-" + ("x" * 10_000)
    full_log.write_text(secret_log_line, encoding="utf-8")
    receipt.write_text(
        json.dumps({
            "receipt_version": 1,
            "task_id": task_id,
            "cache_key": "abc",
            "summary": {
                "exit_code": 0,
                "checks_run": 5,
                "checks_failed": 0,
                "excerpt": "ok",
            },
            "artifacts": {"receipt": str(receipt), "full_log": str(full_log)},
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_KANBAN_PREFLIGHT_RECEIPT", str(receipt))

    packet = kb.build_worker_task_packet(conn, task_id).to_dict()

    preflight = packet["workspace"]["preflight"]
    assert preflight["cache_key"] == "abc"
    assert preflight["summary"]["exit_code"] == 0
    assert preflight["summary"]["checks_failed"] == 0
    assert preflight["artifacts"]["full_log"] == str(full_log)
    assert secret_log_line not in json.dumps(packet)


def test_dispatch_materializes_receipt_and_reviewer_binds_same_artifact(
    isolated_board, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    conn, _ = isolated_board
    repo, declared, _ = _repo_with_unrelated_anchor(tmp_path)
    kb.write_board_metadata(None, default_workdir=str(repo), land_target=declared)
    task_id = kb.create_task(
        conn,
        title="dispatch receipt",
        body="Edit-Targets: hermes_cli/kanban_db.py, tests/hermes_cli/\n",
        assignee="worker",
        workspace_kind="worktree",
        workspace_path=str(repo),
    )
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    spawned: list[tuple[object, str]] = []

    def capture(task, workspace, *, board=None):
        spawned.append((task, workspace))
        return None

    result = kbd.dispatch_once(conn, spawn_fn=capture, board=None)

    assert result.preflight_blocked == []
    assert len(spawned) == 1
    implementation_task, implementation_workspace = spawned.pop()
    receipt_path = Path(implementation_task.preflight_receipt_path)
    assert receipt_path.is_file()
    assert _git(Path(implementation_workspace), "rev-parse", "HEAD") == declared
    assert kb.request_review(
        conn,
        task_id,
        reviewer="reviewer",
        summary="implementation ready",
        metadata={"commit": declared},
        expected_run_id=implementation_task.current_run_id,
    )

    review_result = kbd.dispatch_once(conn, spawn_fn=capture, board=None)

    assert review_result.preflight_blocked == []
    assert len(spawned) == 1
    review_task, review_workspace = spawned[0]
    review_receipt = json.loads(
        Path(review_task.preflight_receipt_path).read_text(encoding="utf-8")
    )
    assert review_workspace == implementation_workspace
    assert review_receipt["role"] == "reviewer"
    assert review_receipt["artifact_ref"] == declared
    assert review_receipt["workspace"]["head_sha"] == declared


def test_dispatch_ignores_cross_repository_parent_artifact(
    isolated_board, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    conn, _ = isolated_board
    target_root = tmp_path / "target"
    foreign_root = tmp_path / "foreign"
    target_root.mkdir()
    foreign_root.mkdir()
    target_repo, target_base, _ = _repo_with_unrelated_anchor(target_root)
    foreign_repo, _, _ = _repo_with_unrelated_anchor(foreign_root)
    (foreign_repo / "foreign-only.txt").write_text("foreign repository\n", encoding="utf-8")
    _git(foreign_repo, "add", "foreign-only.txt")
    _git(foreign_repo, "commit", "-m", "foreign artifact")
    foreign_artifact = _git(foreign_repo, "rev-parse", "HEAD")
    parent_id = kb.create_task(
        conn,
        title="dependency-only foreign parent",
        workspace_kind="worktree",
        workspace_path=str(foreign_repo),
    )
    assert kb.complete_task(
        conn,
        parent_id,
        summary="foreign dependency complete",
        metadata={"commit": foreign_artifact},
    )
    kb.write_board_metadata(None, default_workdir=str(target_repo), land_target=target_base)
    child_id = kb.create_task(
        conn,
        title="target repository child",
        assignee="worker",
        parents=[parent_id],
        workspace_kind="worktree",
        workspace_path=str(target_repo),
    )
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    spawned: list[tuple[Any, str]] = []

    result = kbd.dispatch_once(
        conn,
        spawn_fn=lambda task, workspace, **_kwargs: spawned.append((task, workspace)),
        board=None,
    )

    assert result.preflight_blocked == []
    assert len(spawned) == 1
    child_task, child_workspace = spawned[0]
    receipt = json.loads(
        Path(child_task.preflight_receipt_path).read_text(encoding="utf-8")
    )
    assert _git(Path(child_workspace), "rev-parse", "HEAD") == target_base
    assert receipt["workspace"]["base_sha"] == target_base
    assert receipt["workspace"]["base_sha"] != foreign_artifact


def test_dispatch_blocks_divergent_same_repository_parent_artifacts(
    isolated_board, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    conn, _ = isolated_board
    repo, _, common = _repo_with_unrelated_anchor(tmp_path)
    _git(repo, "checkout", "-b", "left", common)
    (repo / "left.txt").write_text("left\n", encoding="utf-8")
    _git(repo, "add", "left.txt")
    _git(repo, "commit", "-m", "left parent")
    left = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    (repo / "right.txt").write_text("right\n", encoding="utf-8")
    _git(repo, "add", "right.txt")
    _git(repo, "commit", "-m", "right parent")
    right = _git(repo, "rev-parse", "HEAD")

    parents = []
    for title, artifact in (("left parent", left), ("right parent", right)):
        parent_id = kb.create_task(
            conn,
            title=title,
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        assert kb.complete_task(
            conn,
            parent_id,
            summary=f"{title} complete",
            metadata={"commit": artifact},
        )
        parents.append(parent_id)
    kb.write_board_metadata(None, default_workdir=str(repo), land_target=right)
    child_id = kb.create_task(
        conn,
        title="must combine both parents",
        assignee="worker",
        parents=parents,
        workspace_kind="worktree",
        workspace_path=str(repo),
    )
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    spawned: list[str] = []

    result = kbd.dispatch_once(
        conn,
        spawn_fn=lambda *_args, **_kwargs: spawned.append("spawned"),
        board=None,
    )

    assert spawned == []
    assert result.preflight_blocked == [child_id]
    child = kb.get_task(conn, child_id)
    assert child is not None and child.status == "blocked"
    blocked = [event for event in kb.list_events(conn, child_id) if event.kind == "blocked"]
    assert "parent artifacts diverge" in (blocked[-1].payload or {})["reason"]


def test_dispatch_blocks_missing_declared_base_before_spawn(
    isolated_board, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    conn, _ = isolated_board
    repo, _, _ = _repo_with_unrelated_anchor(tmp_path)
    kb.write_board_metadata(None, default_workdir=str(repo), land_target="")
    task_id = kb.create_task(
        conn,
        title="missing base",
        assignee="worker",
        workspace_kind="worktree",
        workspace_path=str(repo),
    )
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    spawned: list[str] = []

    result = kbd.dispatch_once(
        conn,
        spawn_fn=lambda *_args, **_kwargs: spawned.append("spawned"),
        board=None,
    )

    assert spawned == []
    assert result.preflight_blocked == [task_id]
    task = kb.get_task(conn, task_id)
    assert task is not None and task.status == "blocked"
    assert task.consecutive_failures == 0
    blocked = [
        event for event in kb.list_events(conn, task_id) if event.kind == "blocked"
    ]
    assert "no declared base" in blocked[-1].payload["reason"]


def test_dispatch_blocks_missing_required_linter_before_spawn(
    isolated_board, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    conn, _ = isolated_board
    repo, declared, _ = _repo_with_unrelated_anchor(tmp_path)
    kb.write_board_metadata(None, default_workdir=str(repo), land_target=declared)
    task_id = kb.create_task(
        conn,
        title="missing linter",
        body="Edit-Targets: hermes_cli/kanban_db.py, tests/hermes_cli/\n",
        assignee="worker",
        workspace_kind="worktree",
        workspace_path=str(repo),
    )
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    real_capabilities = kbr._capabilities

    def without_ruff(repo_root, workspace, commands):
        capabilities = real_capabilities(repo_root, workspace, commands)
        capabilities["ruff"] = False
        return capabilities

    monkeypatch.setattr(kbr, "_capabilities", without_ruff)
    spawned: list[str] = []

    result = kbd.dispatch_once(
        conn,
        spawn_fn=lambda *_args, **_kwargs: spawned.append("spawned"),
        board=None,
    )

    assert spawned == []
    assert result.preflight_blocked == [task_id]
    task = kb.get_task(conn, task_id)
    assert task is not None and task.status == "blocked"
    assert task.consecutive_failures == 0
    blocked = [
        event for event in kb.list_events(conn, task_id) if event.kind == "blocked"
    ]
    assert (
        "required capability unavailable: ruff" in (blocked[-1].payload or {})["reason"]
    )


def test_scratch_worker_gets_explicit_non_git_receipt(
    isolated_board, monkeypatch: pytest.MonkeyPatch
):
    conn, _ = isolated_board
    task_id = kb.create_task(conn, title="scratch receipt", assignee="worker")
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    spawned: list[object] = []

    result = kbd.dispatch_once(
        conn,
        spawn_fn=lambda task, _workspace, **_kwargs: spawned.append(task),
    )

    assert result.preflight_blocked == []
    assert len(spawned) == 1
    payload = json.loads(
        Path(spawned[0].preflight_receipt_path).read_text(encoding="utf-8")
    )
    assert payload["workspace"]["kind"] == "scratch"
    assert payload["workspace"]["repo"] is None
    assert payload["workspace"]["base_sha"] is None
    assert payload["workspace"]["head_sha"] is None
    assert payload["commands"] == {"lint": [], "test": []}
    assert payload["environment"]["capabilities"]["git"] is True
