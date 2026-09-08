#!/usr/bin/env python3
"""End-to-end smoke of `hermes kanban approve` + `land` through the real CLI.

Not a substitute for the test suite — this exists to prove the wiring works
outside pytest: real argv parsing, a real sandboxed board, a real bare remote.

ISOLATION: every HERMES_KANBAN_* var is cleared and the resolved DB path is
ASSERTED to be inside the temp dir before anything writes. A dispatcher-spawned
worker inherits HERMES_KANBAN_DB, and _board_path() honours it over
kanban_home(), so a probe that sets only HERMES_KANBAN_HOME writes to the LIVE
board.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="land-smoke-")

for var in (
    "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_WORKSPACE", "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_PIN_HOME",
):
    os.environ.pop(var, None)
os.environ["HERMES_HOME"] = str(Path(TMP) / "hermes")
os.environ["HERMES_KANBAN_HOME"] = str(Path(TMP) / "hermes")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_cli import kanban_db as kb  # noqa: E402

db = str(kb.kanban_db_path(board="default"))
assert db.startswith(TMP), f"NOT ISOLATED — would write {db}"
print(f"isolated board: {db}")

from hermes_cli import kanban_db_approve as ka  # noqa: E402
from hermes_cli import kanban_db_connect as kbc  # noqa: E402


def git(cwd, *args, check=True):
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        env={
            "HOME": TMP, "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )
    if check and proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout.strip()


root = Path(TMP) / "git"
root.mkdir()
remote, clone = root / "remote.git", root / "clone"
git(root, "init", "--bare", "-b", "dev", str(remote))
git(root, "clone", str(remote), str(clone))
git(clone, "config", "user.name", "T")
git(clone, "config", "user.email", "t@example.invalid")
(clone / "README.md").write_text("base\n", encoding="utf-8")
git(clone, "add", "-A")
git(clone, "commit", "-m", "base")
git(clone, "push", "origin", "HEAD:refs/heads/dev")

kb.init_db()
with kbc.connect() as conn:
    task_id = kb.create_task(conn, title="smoke", assignee="dev-a")
    wt = clone / ".worktrees" / task_id
    git(clone, "worktree", "add", "-b", f"wt/{task_id}", str(wt), "origin/dev")
    (wt / "feature.txt").write_text("shipped\n", encoding="utf-8")
    git(wt, "add", "-A")
    git(wt, "commit", "-m", "feature")
    git(wt, "push", "origin", f"HEAD:refs/heads/wt/{task_id}")
    conn.execute(
        "UPDATE tasks SET workspace_kind='worktree', workspace_path=?, branch_name=? "
        "WHERE id=?", (str(wt), f"wt/{task_id}", task_id),
    )
    conn.commit()
    t = kb.claim_task(conn, task_id, claimer="impl")
    kb.request_review(
        conn, task_id, summary="done", reviewer="reviewer",
        expected_run_id=t.current_run_id,
        metadata={"pre_review_gate": {"pushed": git(wt, "rev-parse", "HEAD")}},
    )
    kb.claim_review_task(conn, task_id, claimer="rev")

kb.write_board_metadata(None, land_target="origin/dev")


def cli(*argv) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", *argv],
        capture_output=True, text=True, env={**os.environ}, timeout=300,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    return proc.returncode, (proc.stdout + proc.stderr)


print("\n--- land BEFORE approval (must refuse, naming the board target) ---")
rc, out = cli("land", task_id, "--json")
print(f"rc={rc}\n{out.strip()[:400]}")

print("\n--- approve ---")
rc, out = cli("approve", task_id)
print(f"rc={rc}\n{out.strip()[:400]}")
with kbc.connect() as conn:
    print(f"status after approve: {kb.get_task(conn, task_id).status}")
print(f"worktree still present: {wt.is_dir()}")

print("\n--- land --dry-run ---")
rc, out = cli("land", task_id, "--dry-run")
print(f"rc={rc}\n{out.strip()[:400]}")

print("\n--- land ---")
rc, out = cli("land", task_id)
print(f"rc={rc}\n{out.strip()[:500]}")

print("\n--- land again (idempotent) ---")
rc, out = cli("land", task_id)
print(f"rc={rc}\n{out.strip()[:300]}")

git(clone, "fetch", "origin", "dev")
print("\n--- remote dev contents ---")
print(git(clone, "ls-tree", "--name-only", "origin/dev"))
with kbc.connect() as conn:
    print(f"final status: {kb.get_task(conn, task_id).status}")
print(f"worktree removed: {not wt.exists()}")
print(f"\nsandbox: {TMP}")
