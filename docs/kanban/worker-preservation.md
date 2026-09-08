# Kanban worker preservation safety net

**This is a preservation safety net, not merge automation.**

A Kanban worker does its work inside its own git worktree
(`<repo>/.worktrees/<task-id>` on branch `wt/<task-id>`). When a run ends —
normally or because the dispatcher reclaimed it — that work has historically
been able to stay dirty and unpushed on the machine that produced it. Audits
repeatedly found task worktrees carrying real uncommitted changes, or local
commits that had never reached a remote. `repo-sync.timer` does not help: it
only covers allowlisted primary checkouts, not arbitrary task worktrees.

The safety net closes that gap by doing exactly two things, and nothing else:

1. **Commit** the worktree's dirty changes onto the branch it is already on.
2. **Push** that branch to its configured remote, without force.

## What it will never do

These are hard guarantees, not defaults:

- Never **merges** or **rebases** anything.
- Never **force-pushes** (no `--force`, no `--force-with-lease`).
- Never **switches or creates** a branch — it commits on the branch the
  worktree already has checked out, or refuses.
- Never **deletes** a worktree, a branch, or a file.
- Never touches a **different task's** workspace, or any workspace that is not
  `workspace_kind='worktree'`.

Landing work on `dev`/`main` remains a human/reviewer decision. Preservation
only ensures the work still exists to be landed.

## Where it fires

Preservation runs on every path that ends or reclaims a run, always *before*
the workspace can be cleaned or a retry can be spawned onto it:

| Path | Function |
|---|---|
| Normal completion | `kanban_db.complete_task` |
| Review handoff | `kanban_db.request_review` |
| Block | `kanban_db.block_task` |
| Archive | `kanban_db.archive_task` |
| Operator reclaim | `kanban_db.reclaim_task` |
| Stale-claim reclaim | `kanban_db.release_stale_claims` |
| Per-task timeout | `kanban_db_dispatch.enforce_max_runtime` |
| Dead-worker sweep | `kanban_db_dispatch._reclaim_dead_workers` |

Ordering matters in two directions. `_cleanup_workspace` removes a worktree
only when it is clean **and** fully pushed, so a successful snapshot is
precisely what makes cleanup safe — and a refused one keeps the worktree.
On the reclaim paths, preservation happens before the claim is released, so a
retry can never be spawned onto a worktree whose previous run's output has not
been saved.

## Fail-closed rules

Any ambiguity produces **no commit**, a `work_preservation_failed` event on the
card, and a preserved worktree. The full list of refusals:

| Verdict | Meaning |
|---|---|
| `skipped/not_a_worktree_workspace` | `scratch`/`dir` workspaces are out of scope |
| `skipped/workspace_missing` | The worktree directory is gone |
| `skipped/not_a_git_worktree` | The path is not inside a git working tree |
| `skipped/detached_head` | No branch to preserve onto |
| `skipped/branch_mismatch` | The worktree is on a different branch than the task's — our ownership belief is wrong |
| `skipped/stale_run` | The caller's `expected_run_id` is not the task's current run: a newer worker owns this worktree |
| `skipped/worker_alive` | The owning PID is still running and is not this process |
| `skipped/concurrent` | Another preserver holds the per-worktree lock |
| `skipped/disabled` | `kanban.worker_preservation.enabled: false` |
| `unsafe/suspected_secret` | A credential-bearing filename, or content the redactor flags as a credential |
| `unsafe/generated_artifact` | A candidate path inside `node_modules/`, `dist/`, `build/`, `.venv/`, … |
| `unsafe/oversized` | A file, or the whole snapshot, over the configured byte budget |
| `failed/*` | git itself failed; the reason is recorded |

Gitignored files are never candidates — they do not appear in
`git status --porcelain` at all. The artifact guard exists for the tree whose
`.gitignore` simply forgot them.

Content safety is **all-or-nothing**: one unsafe candidate refuses the whole
snapshot rather than committing a partial one, so a human sees exactly the
tree the worker left.

## Push failures never lose work

A push can fail for reasons preservation must not try to "fix": the remote is
offline, credentials are absent, or the branch has diverged. In every case the
commit is already made locally, `pushed: false` and the git error are recorded,
and cleanup still refuses the worktree because it has unpushed commits. A
non-fast-forward rejection specifically means the remote holds work this
snapshot does not — overwriting that would be strictly worse than leaving these
commits local, which is why force-push is never attempted.

A repo with no usable remote records `push_error: no_remote_configured` and
keeps the commit; committed work is already safer than dirty work.

## Concurrency

Completion and reclaim can fire on one worktree at the same instant. Both would
otherwise run `git add`/`git commit` against the same index and produce either
two snapshot commits or a corrupt index. An `fcntl` lock file in the worktree's
own git dir (`hermes-kanban-preserve-<task-id>.lock`) serializes them
non-blockingly: the loser returns `skipped/concurrent` and does nothing. The
lock is per-worktree — unlike `refs/stash`, it is never shared between tasks.

On platforms without `fcntl` (Windows) the lock degrades to "always acquired",
matching the pre-existing single-preserver behaviour rather than disabling
preservation.

## What lands on the card

A successful snapshot appends `work_preserved`:

```json
{"commit_sha": "…", "pushed": true, "push_error": null, "branch": "wt/t_abc123"}
```

A refusal appends `work_preservation_failed`:

```json
{"status": "unsafe", "reason": "suspected_secret",
 "detail": "config/.env has a credential-bearing filename", "branch": "wt/t_abc123"}
```

`detail` names the offending **path**, never its contents — the whole point of
the guard is to keep credential material out of durable records.

No-ops and ordinary skips record nothing; they are the common case and would be
pure event-log noise.

## Configuration

```yaml
kanban:
  worker_preservation:
    enabled: true            # false = skip entirely (non-Git / custom remotes)
    max_file_bytes: 5242880  # refuse any single candidate above this
    max_total_bytes: 20971520
```

Config-read errors fail **open** (preservation enabled): losing a worker's work
because `config.yaml` was momentarily unparseable is the worse outcome.

## Tests

- `tests/hermes_cli/test_kanban_worker_preservation.py` — the mechanism against
  real repos and local bare remotes.
- `tests/hermes_cli/test_kanban_worker_preservation_lifecycle.py` — ownership
  gating and the board record.
- `tests/hermes_cli/test_kanban_worker_preservation_wiring.py` — every
  lifecycle call site, plus proof that cleanup still refuses a dirty, unpushed,
  failed-preservation workspace.
