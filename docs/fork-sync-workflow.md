# Fork sync workflow

How `Taznc/hermes-agent` (this fork) stays current with
`NousResearch/hermes-agent` (upstream) without losing custom work.

> **Fork-local doc.** Everything here describes the fork's own workflow. It is
> not upstream policy.

## Remotes — know which is which

| Remote     | URL                              | Push?                        |
| ---------- | -------------------------------- | ---------------------------- |
| `origin`   | `Taznc/hermes-agent`             | **Yes** — this is your fork  |
| `upstream` | `NousResearch/hermes-agent`      | **Never** — read-only        |

Always name the remote explicitly when discussing branches: "the fork's `main`"
vs "upstream's `main`". Bare "main" is ambiguous and has caused real confusion.

## Branch model

- **`main`** — a *pure mirror* of `upstream/main`. Never commit here directly.
  It only ever moves by fast-forward. If `git log upstream/main..main` prints
  anything, something is wrong; fix it before syncing.
- **`dev`** — the rolling branch carrying all fork-custom commits, and the
  branch the desktop app actually runs from. Upstream is **merged** into it.

### Why `dev` merges instead of rebasing

`dev` is a long-lived daily driver shared by two machines (laptop + dev VM),
not a pull request. Merging is what makes that safe:

| | rebase | **merge** (current) |
| --- | --- | --- |
| Push | `--force-with-lease` | plain `git push` |
| Two machines | must coordinate; a stale clone silently duplicates work | both fast-forward |
| Conflicts | re-resolved on every sync | resolved once, recorded in the merge commit |
| Backups | required before rewriting | unneeded — merges only add commits |
| History | linear | merge bubbles (irrelevant here) |

The linear history a rebase buys is only worth paying for on a branch someone
will review. **Upstream PRs are cut fresh off `upstream/main` and cherry-picked**,
so they stay clean regardless of what `dev` looks like:

```bash
git checkout -b fix/some-bug upstream/main
git cherry-pick <sha>
git push origin fix/some-bug
```

Rebasing `dev` is what produced the two-machine failure this workflow now
guards against: because a rebase rewrites history, a stale local `dev` read as
"8 ahead / 539 behind" while `origin/dev` already contained those same commits
replayed — and re-syncing from it silently redid ~500 commits of the other
machine's work.

## The routine

```bash
./scripts/fork-sync.sh            # the one command: sync, merge, verify, push
./scripts/fork-sync.sh --no-push  # ...stop before pushing
./scripts/fork-sync.sh --check    # report divergence, change nothing
```

Same command on both machines. It pulls the other machine's work first,
fast-forwards `main`, merges it into `dev`, and refuses to push unless the
desktop typecheck **and** the full vitest suite pass.

## Why each guard exists (learned the hard way)

### Shallow clones silently break everything
A shallow clone (`--depth`) reports nonsense ahead/behind numbers — one sync
showed "22,734 commits behind" with a single local commit — and merges blow up
across the graft boundary. The script auto-runs `git fetch --unshallow`.

```bash
git rev-parse --is-shallow-repository   # must print false
```

### `git log main..dev` overcounts your custom work
Commits already merged upstream (possibly rewritten there) still show up.
`git cherry` is the honest signal:

```bash
git cherry upstream/main dev
# + = genuinely unique to dev
# - = equivalent patch already upstream
```

One sync looked like 32 custom commits; only 7 were real.

### Undoing a sync
No backup branch is needed — a merge only *adds* commits, so the pre-merge tip
is still reachable:

```bash
git reset --hard ORIG_HEAD    # immediately after a merge
git reflog                    # or find the old tip
```

That is the practical payoff of dropping rebase: nothing that was ever pushed
gets rewritten, so there is nothing to back up.

### Conflicts are usually *semantic*, not textual
The common shape: upstream renamed, extracted, or restructured an API that a
fork commit also touched. Taking either side wholesale produces code that
merges cleanly and then fails to compile.

Rules that produced correct resolutions:

1. **Keep upstream's structure, re-inject the fork's feature into it.**
   Upstream rewrote a gateway swap into an atomic `batch()`; the fix was to
   move the fork's `$activeGatewayConnection` write *inside* that batch, not to
   keep the fork's old sequential version.
2. **Verify the API still exists before keeping your side.**
   ```bash
   git grep -n "ensureGatewayForProfile" -- apps/desktop/src
   ```
   Upstream had deleted it — the fork's side could not have compiled.
3. **"Both sides added different things" means keep both.** Merged parameter
   lists (`(baseUrl, headers, staticToken)`) keep every existing call site
   working while preserving the fork's feature.
4. **Update the call sites.** A merged signature is only half the change.

With merges each conflict is resolved **once** and recorded in the merge commit;
it never comes back on the next sync. `git config rerere.enabled true` (set by
the script) additionally replays a resolution if the same conflict recurs.

### Test doubles are the usual post-merge breakage
Code merges fine, then tests fail because a `vi.mock` factory still describes
the old upstream API. A mocked module must export **everything** the component
imports, or the test dies with
`No "<export>" export is defined on the "<module>" mock`.

After a sync, expect to update mocks for any upstream API that was renamed,
and remember to `mockClear()` newly added mocks in `beforeEach` — otherwise
call counts leak between cases and `not.toHaveBeenCalled()` sees a prior test's
call.

### A green typecheck is the real proof
Hand-merged semantic conflicts are exactly the class of change that looks right
and isn't. Always:

```bash
cd apps/desktop && npm run typecheck && npx vitest run
```

Pre-existing failures are not your problem — confirm by running the same check
on clean `main` before assuming the sync caused them. (A missing `blobatar`
module once looked like merge damage; it was a stale `node_modules`, fixed by
`npm install`.)

## Keeping the fork small

Every custom commit on `dev` is a commit that can conflict on a future sync, so
the cheapest fork is a small one. Two ways to shrink it:

- **Upstream your bug fixes.** Most of this fork's commits are fixes that affect
  every self-hosted user, not personal preferences — those belong upstream, and
  once merged they leave `dev` automatically.
- **Prefer the plugin seam for personal features.** Desktop ships a plugin SDK
  (`apps/desktop/src/sdk/`) with host state atoms, inline message components and
  notifications. A customization built as a plugin lives outside the upstream
  tree and **never conflicts**.

## Dev build on this VM

```bash
cd ~/projects/hermes/hermes-agent
source ~/.hermes/venvs/hermes-dev/bin/activate
hermes desktop --source
```

Uses the normal `~/.hermes` config/credentials and rebuilds on change.
`scripts/hermes-dev.sh` is macOS-oriented (`~/Library/Application Support`) and
is not the path used here.
