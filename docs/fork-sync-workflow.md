# Fork sync & rebase workflow

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
- **`dev`** — the rolling branch carrying all fork-custom commits. Rebased on
  top of `main` after every upstream sync, so custom work always sits as a
  clean set of commits on top of a known-good upstream base.

## The routine

```bash
./scripts/fork-sync.sh --check   # report divergence, change nothing
./scripts/fork-sync.sh           # sync + rebase + verify, stop before pushing
./scripts/fork-sync.sh --push    # ...and force-with-lease push dev
```

The script refuses to proceed when the fork's `main` has drifted, backs `dev`
up to GitHub before rewriting anything, and will not push unless the desktop
typecheck **and** the full vitest suite pass.

## Why each guard exists (learned the hard way)

### Shallow clones silently break everything
A shallow clone (`--depth`) reports nonsense ahead/behind numbers — one sync
showed "22,734 commits behind" with a single local commit — and rebases blow up
across the graft boundary. The script auto-runs `git fetch --unshallow`.

```bash
git rev-parse --is-shallow-repository   # must print false
```

### `git log main..dev` overcounts your custom work
Commits already merged upstream (possibly rewritten there) still show up.
`git cherry` is the honest signal:

```bash
git cherry main dev
# + = genuinely unique to dev, will be replayed
# - = equivalent patch already upstream, will be dropped (expected, not data loss)
```

One sync looked like 32 custom commits; only 7 were real.

### Back up to GitHub *before* rewriting history
Rebase rewrites `dev`, so the push needs `--force-with-lease`. A local-only
backup does not survive the machine. The script pushes both a
`backup/dev-<stamp>` branch and a `presync-dev-<stamp>` tag to the fork first.

```bash
git reset --hard presync-dev-<stamp>    # full recovery
```

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

`git config rerere.enabled true` (set by the script) replays resolutions if the
same rebase is repeated.

### Test doubles are the usual post-rebase breakage
Code merges fine, then tests fail because a `vi.mock` factory still describes
the old upstream API. A mocked module must export **everything** the component
imports, or the test dies with
`No "<export>" export is defined on the "<module>" mock`.

After a rebase, expect to update mocks for any upstream API that was renamed,
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
on clean `main` before assuming a rebase caused them. (A missing `blobatar`
module once looked like rebase damage; it was a stale `node_modules`, fixed by
`npm install`.)

## Dev build on this VM

```bash
cd ~/projects/hermes/hermes-agent
source ~/.hermes/venvs/hermes-dev/bin/activate
hermes desktop --source
```

Uses the normal `~/.hermes` config/credentials and rebuilds on change.
`scripts/hermes-dev.sh` is macOS-oriented (`~/Library/Application Support`) and
is not the path used here.
