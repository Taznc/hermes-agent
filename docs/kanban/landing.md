# `hermes kanban approve` / `hermes kanban land` — safety model and recovery

Attended landing of an **explicitly approved** Kanban card: a verified merge to
a configured target, a non-force push, a receipt on the card, closure, and the
existing safe worktree cleanup.

It is user-triggered only. Nothing in the dispatcher, the worker preservation
path, or any scheduled job calls it — automatic preservation of a worker's work
remains commit-and-push, and never merges.

```bash
# Reviewer approves; the card STAYS in review, tree and branch intact
hermes kanban approve t_1a2b3c4d

# One card, target named explicitly
hermes kanban land t_1a2b3c4d --target origin/dev

# Set the board default once, then omit --target
hermes kanban boards set-land-target default origin/dev
hermes kanban land t_1a2b3c4d

# Look before you leap; changes nothing at all
hermes kanban land t_1a2b3c4d t_5e6f7a8b --dry-run --json
```

## Approval is not completion

`hermes kanban complete` ends a card's life: it sets `done` and immediately runs
`_cleanup_workspace()`, which reaps a clean, fully-pushed task worktree and its
`wt/` branch. That is correct for a card whose work is finished at review — and
fatal for one that still has to be landed, because the worktree, the branch, and
the open card are exactly the evidence landing re-verifies before it merges
anything.

So a card destined for landing is approved with `hermes kanban approve`, which:

- ends the reviewer's own run with `outcome='approved'` and appends an
  `approved` event;
- records the **exact commit** that was reviewed (read from the task worktree's
  HEAD, or `--sha`);
- releases the claim and leaves the card **in `review`**.

`review` is a locked column — a card can be dragged out of it but never into it
— so an approved card cannot drift into another lane while it waits. The
dispatcher will not re-spawn it either: the review lane's respawn guard returns
`approved_awaiting_land` for a card carrying a live approval.

An approved card leaves `review` in exactly three explicit ways: `land` closes
it after a proven remote read-back, `reopen-review` sends it back to
implementation, or a human moves it. `request-changes` is the verdict from an
active reviewer run; approval has already ended that run.

**A card approved the old way — a reviewer calling `complete` from the review
column — is not landable.** Its worktree was already reaped at completion, so
there is nothing left to verify. `land` reports `no_approval`. Re-open the card
and approve it explicitly, or land it by hand.

## The target is configuration, never inference

A repository that has both a fork and an upstream has **no safe default
remote**, so landing refuses to pick one. The target comes from exactly two
places, in this order:

1. `--target <remote>/<branch>`
2. the board's `land_target` (`hermes kanban boards set-land-target`)

There is no fallback to "the only remote", to `origin`, to the branch's
upstream, or to an environment variable. With neither configured the verdict is
`no_target`. Every line of output and every JSON record — including refusals —
names the remote and branch it acted on.

This is also why the fork-specific policy for an installation lives in
`board.json` rather than in the command: the product logic knows nothing about
any particular hosting provider or organisation.

## One repository, not two: the push endpoint

Git treats fetching and pushing as separate configuration. With
`remote.origin.pushurl` set, `git push origin` writes to a repository that
`git ls-remote origin` never looks at. A landing that preflights and reads back
through the fetch URL while pushing through the push URL verifies a repository
it did not write, and reports success for content that landed somewhere else.

Landing therefore resolves the **effective push URL** once, up front, and
addresses that URL for every read, the push, and the read-back. A remote with no
usable push URL refuses `remote_push_disabled`; one with several refuses
`remote_push_ambiguous`, because a single `git push` would write to more than
one repository and reading back only one of them proves nothing about the rest.

## What must be proven before anything is merged

Every gate fails **closed** — the verdict is a refusal with a machine-readable
reason code, and there is deliberately **no override flag**.

| Reason code | What was not proven |
|---|---|
| `task_not_found` | No such task on this board |
| `no_approval` | No explicit `hermes kanban approve` verdict from a run claimed out of the `review` column. A `done` status set by the implementer is not an approval |
| `approval_superseded` | The card was approved, then re-submitted for review; that newer cycle has not been adjudicated |
| `changes_requested_unresolved` | The newest review verdict is `changes_requested`, so an older approval is stale |
| `approval_sha_drift` | The branch publishes a different commit than the one that was approved — the difference was never reviewed |
| `live_worker` | The card is `running` or holds a claim lock |
| `deps_unsatisfied` | A parent dependency is not done |
| `workspace_unusable` | No git worktree workspace, no repository above it, or no named branch |
| `dirty_worktree` | The task worktree still has uncommitted changes, so the pushed sha is not the whole change |
| `branch_unpushed` | The branch is not published on the landing endpoint (or its local HEAD differs from what that endpoint publishes) |
| `wrong_remote` | The branch is published on some *other* remote but not the one being landed to — the fork/upstream mistake |
| `remote_push_disabled` | The remote resolves no usable push URL |
| `remote_push_ambiguous` | The remote has several push URLs, so one push writes several repositories |
| `verification_missing` | No `land_verify` command configured and no verification receipt on the approval run |
| `verification_stale` | The receipt vouches for a different commit than the one that would land |
| `verification_failed` | The board's `land_verify` command exited non-zero |
| `target_unresolvable` | The target is malformed, or the remote does not publish the target branch |
| `merge_conflict` | The source does not merge cleanly into the current target |
| `target_advanced` | The target moved between the plan and the push; the non-force push was correctly rejected |
| `push_rejected` | The remote refused the push for another reason (branch protection, credentials) |
| `readback_failed` | After the push, the remote does not serve content containing the reviewed work — or it moved during the read-back, so the proof and the recorded commit would disagree |

### Approval binds to a commit — verification is not review

`land_verify` re-runs a test suite. That answers "does this commit pass?", which
is a different question from "did a human read this commit?", and it cannot
stand in for the second. A branch that advanced after approval refuses with
`approval_sha_drift` **even when verification passes**, because the added
commits were never reviewed. Re-request review for the new commit.

### Verification evidence

Landing will not merge work it cannot show was verified **at the exact commit
being landed**:

- If the board has a `land_verify` command
  (`hermes kanban boards set-land-verify default 'scripts/run_tests.sh'`), it is
  re-run now, in a throwaway detached checkout of that commit. Non-zero exit →
  `verification_failed`.
- Otherwise a verification receipt must name the commit being landed, in one of
  `pushed`, `sha`, `commit`, or `head`, under a `pre_review_gate` or
  `verification` key. It is looked for on the approval run first, then on the
  review handoff — which is where it normally lives, since the pre-review gate
  is run and recorded by the **implementer** and a reviewer does not retype it.
  Missing → `verification_missing`; naming a different commit →
  `verification_stale`.

Widening where the receipt may live does not widen what it proves: it must
still name the exact commit being landed, and that commit is already pinned to
the reviewed one, so a receipt from an earlier round is refused as stale.

## Order of operations

1. Board gates: live approval verdict, no live worker, dependencies satisfied.
2. Resolve the single push endpoint that every later step addresses.
3. Git gates: clean worktree, branch published on that endpoint at exactly the
   local HEAD, and publishing exactly the approved commit.
4. **A dry run stops here**, having mutated nothing at all.
5. Verification evidence for that commit.
6. **Fetch** the target from the endpoint right now (not merely `ls-remote` — a
   target advanced from another clone is not in the local object database at
   all), merge the source into a throwaway detached worktree (`--no-ff`), and
   `git push` — **never** with `--force`.
7. Re-read the endpoint and prove the source commit is reachable from what it
   now serves, or already fully present in it. The fetched object must be the
   same commit the read-back reported, or the proof and the record would
   describe different commits.
8. Only then: complete the card with explicitly preliminary
   `bookkeeping.state = pending` metadata, letting the existing `_cleanup_workspace`
   seam reap the worktree and branch (it independently re-proves the tree is
   clean and pushed first), then archive it.
9. Observe the actual workspace and archived-card state. Atomically replace the
   preliminary run metadata with the final receipt, append a `landing_receipt`
   event, and write the human receipt comment. These final surfaces therefore
   never claim cleanup or archival before those operations have succeeded.

Steps 1–5 mutate nothing on the remote. Steps 8–9 never run without a
successful step 7.

The merge happens in a temporary detached worktree, so the operator's own
checkout is never moved to another branch and a served worktree can never be
left mid-merge.

### What `--dry-run` guarantees

Zero mutation, and that is meant literally. A dry run performs no fetch, creates
no staging worktree, writes no ref, makes no board write — and does **not run
the board's `land_verify` command**, since that is arbitrary operator shell and
is exactly the kind of external state a dry run promises not to touch. It
reports the *planned* verification instead:

```json
{"kind": "command", "planned": true, "command": "scripts/run_tests.sh"}
```

Because it never fetches, a dry run judges "already landed" only from objects
already present locally; a genuine landing re-checks that against the fetched
target.

## Idempotent re-runs and squash merges

Before merging, landing asks whether the work is *already there* — a question
about the target's **current tip**, not about its history:

- **`ancestor`** — the source commit is reachable from the target.
- **`patch_equivalent`** — merging the source into the target tip would produce
  the target's existing tree, i.e. the reviewed content is already present in
  full. This is how a **squash-merged** card reads afterwards, whether the
  branch was one commit or twenty.

The tip-and-tree test is deliberate. `git cherry`, the obvious alternative,
answers "did an equivalent patch ever appear upstream" — so work that was
applied and then **reverted** still looks landed to it, and a multi-commit
branch squashed into one commit does **not**, because per-commit patch ids do
not survive a squash. Both cases are wrong in the direction that matters:
the first archives a card whose work is gone.

When the work is already present the verdict is `already_landed`: no second
merge, no second push, and the bookkeeping (receipt, closure, cleanup) is
finished if it had not been. Re-running a completed landing is therefore safe
and is the supported way to finish a landing interrupted after the push.

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
| `approval_sha_drift` | Nothing changed | The branch moved after review. Re-request review for the new commit and approve it; there is no override |
| `approval_superseded` | Nothing changed | A newer review cycle is open. Adjudicate it (`approve` or `request-changes`) |
| `remote_push_disabled` / `remote_push_ambiguous` | Nothing changed | Fix `remote.<name>.pushurl` so the remote resolves exactly one push destination |
| `merge_conflict` | Nothing pushed; the staging tree is already gone | Merge the target into the card branch, re-verify, re-review (the merge changes the approved commit), land again |
| `target_advanced` | Nothing pushed; the target moved under you | Just re-run. Landing re-fetches the new target and merges onto it. Never force-push to work around this |
| `push_rejected` | Nothing on the remote; card still open, worktree intact | Resolve the rejection (branch protection, permissions), then re-run |
| Crash after the push, before the receipt | Content **is** on the remote; card still open | Re-run: the read-back reports `already_landed` and finishes the bookkeeping without a second merge |
| Crash after completion or archive, before the final receipt | Card is done/archived with `bookkeeping.state = pending` completion metadata; no final comment or event claims cleanup | Re-run: landing recognizes the remote content and finishes archival plus the coherent final receipt without a second merge |
| `readback_failed` — target moved during read-back | The content landed, but a competing push arrived before the proof | Re-run. The second run reads back cleanly and reports `already_landed` |
| `readback_failed` — content genuinely absent | The push reported success but the remote does not serve the content | Do **not** re-push. Inspect the remote branch directly; this means the remote rewrote or rejected the ref silently |
| Card closed, worktree still present | Landed; cleanup declined | Expected when the tree is dirty or holds unpushed commits. The receipt's `cleanup.workspace_removed` says so. Inspect it, then `hermes worktree prune` |

The completed-run metadata, `landing_receipt` event, and receipt comment record
the source branch and sha, the remote, branch and resolved push URL, the target
sha before, the exact sha read back from the remote afterwards, whether a push
occurred, the read-back result, the reviewer and approval run with the commit
they approved, the verification kind, the landing timestamp, closure reason,
and the observed workspace-removal/card-archive outcome — so a landing can be
audited after the fact without re-deriving any of it.
