# `hermes kanban land` — safety model and recovery

Attended landing of an **explicitly approved** Kanban card: a verified merge to
a configured target, a non-force push, a receipt on the card, closure, and the
existing safe worktree cleanup.

It is user-triggered only. Nothing in the dispatcher, the worker preservation
path, or any scheduled job calls it — automatic preservation of a worker's work
remains commit-and-push, and never merges.

```bash
# One card, target named explicitly
hermes kanban land t_1a2b3c4d --target origin/dev

# Set the board default once, then omit --target
hermes kanban boards set-land-target default origin/dev
hermes kanban land t_1a2b3c4d

# Look before you leap; changes nothing at all
hermes kanban land t_1a2b3c4d t_5e6f7a8b --dry-run --json
```

## The target is configuration, never inference

A repository that has both a fork and an upstream has **no safe default
remote**, so landing refuses to pick one. The target comes from exactly two
places, in this order:

1. `--target <remote>/<branch>`
2. the board's `land_target` (`hermes kanban boards set-land-target`)

There is no fallback to "the only remote", to `origin`, to the branch's
upstream, or to an environment variable. With neither configured the verdict is
`no_target`. Every line of output and every JSON record names the remote and
branch it acted on.

This is also why the fork-specific policy for an installation lives in
`board.json` rather than in the command: the product logic knows nothing about
any particular hosting provider or organisation.

## What must be proven before anything is merged

Every gate fails **closed** — the verdict is a refusal with a machine-readable
reason code, and there is deliberately **no override flag**.

| Reason code | What was not proven |
|---|---|
| `task_not_found` | No such task on this board |
| `no_approval` | No run claimed from the `review` column completed the card. A `done` status set by the implementer is not an approval |
| `changes_requested_unresolved` | The newest review verdict is `changes_requested`, so an older approval is stale |
| `live_worker` | The card is `running` or holds a claim lock |
| `deps_unsatisfied` | A parent dependency is not done |
| `workspace_unusable` | No git worktree workspace, no repository above it, or no named branch |
| `dirty_worktree` | The task worktree still has uncommitted changes, so the pushed sha is not the whole change |
| `branch_unpushed` | The branch is not published on the landing remote (or its local HEAD differs from what the remote publishes) |
| `wrong_remote` | The branch is published on some *other* remote but not the one being landed to — the fork/upstream mistake |
| `verification_missing` | No `land_verify` command configured and no verification receipt on the approval run |
| `verification_stale` | The receipt vouches for a different commit than the one that would land |
| `verification_failed` | The board's `land_verify` command exited non-zero |
| `target_unresolvable` | The target is malformed, or the remote does not publish the target branch |
| `merge_conflict` | The source does not merge cleanly into the current target |
| `push_rejected` | The remote refused the push (branch protection, a race, credentials) |
| `readback_failed` | After the push, the remote does not serve content containing the reviewed work |

### Verification evidence

Landing will not merge work it cannot show was verified **at the exact commit
being landed**:

- If the board has a `land_verify` command
  (`hermes kanban boards set-land-verify default 'scripts/run_tests.sh'`), it is
  re-run now, in a throwaway detached checkout of that commit. Non-zero exit →
  `verification_failed`.
- Otherwise the approval run's metadata must carry a receipt under
  `pre_review_gate` or `verification`, naming the commit in one of `pushed`,
  `sha`, `commit`, or `head`. Missing → `verification_missing`; naming a
  different commit → `verification_stale`.

## Order of operations

1. Board gates: approval verdict, no live worker, dependencies satisfied.
2. Git gates: clean worktree, branch published on the landing remote at exactly
   the local HEAD.
3. Verification evidence for that commit.
4. Re-read the target **from the remote** right now, merge the source into a
   throwaway detached worktree (`--no-ff`), and `git push` — **never** with
   `--force`.
5. Re-read the remote again and prove the source commit is reachable from what
   the remote now serves, or patch-equivalent to it.
6. Only then: write the receipt comment, complete the card, archive it, and let
   the existing `_cleanup_workspace` seam reap the worktree and branch (it
   independently re-proves the tree is clean and pushed first).

Steps 1–3 mutate nothing. Step 6 never runs without a successful step 5.

The merge happens in a temporary detached worktree, so the operator's own
checkout is never moved to another branch and a served worktree can never be
left mid-merge.

## Idempotent re-runs and squash merges

Before merging, landing asks whether the work is *already there*:

- **`ancestor`** — the source commit is reachable from the target.
- **`patch_equivalent`** — every commit unique to the branch already exists
  upstream as an equivalent patch (`git cherry`), which is how a
  **squash-merged** card reads afterwards.

Either way the verdict is `already_landed`: no second merge, no second push,
and the bookkeeping (receipt, closure, cleanup) is finished if it had not been.
Re-running a completed landing is therefore safe and is the supported way to
finish a landing that was interrupted after the push.

## Batch mode

`hermes kanban land t_a t_b t_c` evaluates and executes each card in isolation.
A refusal on one never aborts the others and never appears in another card's
record. The exit status is non-zero if **any** card refused, and `--json`
returns one object per card in the order given.

## Recovery from a partially completed landing

Because the steps are ordered so that the irreversible one (the push) happens
before any bookkeeping, every interruption leaves a state you can resolve by
re-running the same command.

| Where it stopped | State | Recovery |
|---|---|---|
| Any gate refused | Nothing changed | Fix what the reason code names, re-run |
| `merge_conflict` | Nothing pushed; the staging tree is already gone | Merge the target into the card branch, re-verify, re-review if the merge was substantive, land again |
| `push_rejected` | Nothing on the remote; card still open | Resolve the rejection (branch protection, permissions, or a target that advanced), then re-run. Never force-push to work around this |
| Crash after the push, before the receipt | Content **is** on the remote; card still open | Re-run: the read-back reports `already_landed` and finishes the bookkeeping without a second merge |
| Crash after the receipt, before closure | Receipt comment present; card still open | Re-run: same as above |
| Card closed, worktree still present | Landed; cleanup declined | Expected when the tree is dirty or holds unpushed commits. Inspect it, then `hermes worktree prune` |
| `readback_failed` | The push reported success but the remote does not serve the content | Do **not** re-push. Inspect the remote branch directly; this means the remote rewrote or rejected the ref silently |

The receipt comment on the card records the source branch and sha, the remote
and branch, the target sha before and after, whether a push occurred, the
read-back result, the reviewer and approval run, the verification kind, and the
closure reason — so a landing can be audited after the fact without re-deriving
any of it.
