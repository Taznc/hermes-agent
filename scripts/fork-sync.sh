#!/usr/bin/env bash
# Sync the fork with upstream and merge it into the custom `dev` branch.
#
#   origin   = Taznc/hermes-agent        (YOUR fork — safe to push)
#   upstream = NousResearch/hermes-agent (READ ONLY — never pushed to)
#
# `dev` is MERGE-based, not rebase-based. That is the whole reason this script
# is short and safe to run from two machines:
#   * merging only ever ADDS commits, so `dev` fast-forwards on the other box
#     and a plain `git push` is enough — no --force-with-lease, no pre-push
#     backup branch, no "adopt the other machine's rewritten history" dance
#   * a conflict is resolved ONCE, recorded in the merge commit, and never
#     replayed again; rebasing re-resolves the same conflicts every sync
#   * history grows merge bubbles. This is a daily-driver branch, not a PR —
#     upstream PRs are cut fresh off `upstream/main` and cherry-picked, so a
#     tidy `dev` history buys nothing.
#
# Other lessons this encodes:
#   * shallow clones silently break merges and fake huge "behind" counts
#   * fork main must be a pure upstream mirror before fast-forwarding
#   * conflicts are usually semantic (upstream renamed/restructured an API),
#     so a green typecheck + vitest run is the only real proof of a good merge
#
# Usage:
#   ./scripts/fork-sync.sh              # the one command: sync, merge, verify, push
#   ./scripts/fork-sync.sh --no-push    # ...stop before pushing dev
#   ./scripts/fork-sync.sh --check      # report divergence only, change nothing
set -euo pipefail

REPO="${FORK_SYNC_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DEV_BRANCH="${FORK_SYNC_DEV_BRANCH:-dev}"
MAIN_BRANCH="${FORK_SYNC_MAIN_BRANCH:-main}"
# Push is the default: the whole point is one command. --no-push is the escape
# hatch for inspecting the merge before it becomes public.
DO_PUSH=1
CHECK_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --push) DO_PUSH=1 ;;
    --no-push) DO_PUSH=0 ;;
    --check) CHECK_ONLY=1 ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mXX  %s\033[0m\n' "$*" >&2; exit 1; }

cd "$REPO" || die "repo not found: $REPO"

# Remember where the user was standing. This script moves between main and dev
# to do its work; landing them somewhere they didn't ask for is a footgun.
START_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"

restore_branch() {
  if [[ -n "$START_BRANCH" && "$START_BRANCH" != "HEAD" ]]; then
    local now
    now="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"

    if [[ "$now" != "$START_BRANCH" ]]; then
      git checkout -q "$START_BRANCH" 2>/dev/null || true
    fi
  fi
}

# --- 0. Preconditions -------------------------------------------------------
say "Preconditions"

# A shallow clone makes merges explode across the graft boundary and reports
# nonsense ahead/behind numbers. This cost a full debugging detour once.
if [[ "$(git rev-parse --is-shallow-repository)" == "true" ]]; then
  warn "shallow clone detected — fetching full history (this is slow, once)"
  git fetch --unshallow
fi

git remote get-url upstream >/dev/null 2>&1 || die "no 'upstream' remote configured"

# A dirty tree only matters once we start moving branches around; --check is
# read-only and must stay usable mid-work.
if [[ "$CHECK_ONLY" != "1" && -n "$(git status --porcelain)" ]]; then
  die "working tree is dirty — commit or stash first"
fi

# --- 1. Fetch ---------------------------------------------------------------
say "Fetching origin + upstream"
git fetch origin --prune --quiet
git fetch upstream --prune --quiet

# --- 1b. Take the other machine's work first --------------------------------
# Both machines push `dev`. Because dev is merge-based, origin/dev is always a
# descendant of what we have, so this is a plain fast-forward — no history
# rewriting, nothing to reconcile. If it is NOT a fast-forward, this machine
# has local commits that were never pushed; merge them rather than guessing.
if git rev-parse --verify --quiet "origin/$DEV_BRANCH" >/dev/null; then
  BEHIND_ORIGIN="$(git rev-list --count "$DEV_BRANCH..origin/$DEV_BRANCH")"
  AHEAD_ORIGIN="$(git rev-list --count "origin/$DEV_BRANCH..$DEV_BRANCH")"

  if [[ "$BEHIND_ORIGIN" != "0" ]]; then
    say "origin/$DEV_BRANCH has $BEHIND_ORIGIN new commit(s) from the other machine"

    if [[ "$CHECK_ONLY" == "1" ]]; then
      echo "  (--check) would pull them into local $DEV_BRANCH"
    else
      git checkout -q "$DEV_BRANCH"

      if [[ "$AHEAD_ORIGIN" == "0" ]]; then
        git merge --ff-only "origin/$DEV_BRANCH"
        echo "  fast-forwarded"
      else
        # Both sides moved. A merge keeps both; conflicts here are the same
        # semantic kind as an upstream merge and get resolved once.
        say "Local $DEV_BRANCH also has $AHEAD_ORIGIN commit(s) — merging both sides"
        git merge --no-edit "origin/$DEV_BRANCH" \
          || die "merge with origin/$DEV_BRANCH conflicted — resolve, commit, then re-run"
      fi
    fi
  fi
fi

# --- 2. Report divergence ---------------------------------------------------
say "Divergence"

# `git cherry` is the honest count: '+' = genuinely unique to dev,
# '-' = an equivalent patch already exists upstream.
UNIQUE="$(git cherry "upstream/$MAIN_BRANCH" "$DEV_BRANCH" 2>/dev/null | grep -c '^+' || true)"
BEHIND="$(git rev-list --count "$DEV_BRANCH".."upstream/$MAIN_BRANCH")"
MAIN_BEHIND="$(git rev-list --count "$MAIN_BRANCH".."upstream/$MAIN_BRANCH")"

echo "  fork $MAIN_BRANCH is behind upstream by : $MAIN_BEHIND commit(s)"
echo "  $DEV_BRANCH is behind upstream by        : $BEHIND commit(s)"
echo "  $DEV_BRANCH commits not upstream         : $UNIQUE"

if [[ "$CHECK_ONLY" == "1" ]]; then
  say "--check requested; nothing modified."
  exit 0
fi

if [[ "$BEHIND" == "0" && "$MAIN_BEHIND" == "0" ]]; then
  say "Already up to date with upstream."

  # An earlier fast-forward from origin may still be unpushed.
  if [[ "$DO_PUSH" == "1" ]] && [[ "$(git rev-list --count "origin/$DEV_BRANCH..$DEV_BRANCH")" != "0" ]]; then
    git push origin "$DEV_BRANCH"
  fi

  restore_branch
  exit 0
fi

# --- 3. Verify fork main is a clean mirror ----------------------------------
say "Verifying fork $MAIN_BRANCH is a pure upstream mirror"

# Anything here means someone committed to the fork's main directly. Merging on
# top of that silently buries the work, so refuse instead.
STRAY="$(git log --oneline "upstream/$MAIN_BRANCH..$MAIN_BRANCH" | head -20)"
if [[ -n "$STRAY" ]]; then
  echo "$STRAY"
  die "fork $MAIN_BRANCH has commits NOT in upstream (above). Resolve before syncing."
fi

# --- 4. Fast-forward fork main ---------------------------------------------
say "Fast-forwarding fork $MAIN_BRANCH -> upstream/$MAIN_BRANCH"
git checkout -q "$MAIN_BRANCH"
git merge --ff-only "upstream/$MAIN_BRANCH"
git push -q origin "$MAIN_BRANCH"

# --- 5. Merge upstream into dev ---------------------------------------------
# No backup branch needed: a merge only adds commits, so the pre-merge state is
# still reachable (`git reset --hard ORIG_HEAD` / the reflog) and nothing that
# was pushed is ever rewritten.
say "Merging $MAIN_BRANCH into $DEV_BRANCH ($UNIQUE custom commit(s) preserved)"
git checkout -q "$DEV_BRANCH"

# rerere records conflict resolutions so a repeat of the same conflict replays
# automatically instead of asking twice.
git config rerere.enabled true

if ! git merge --no-edit "$MAIN_BRANCH"; then
  cat <<'EOF'

  Merge stopped on a conflict. This is normal and usually SEMANTIC:
  upstream renamed or restructured an API your commit also touched.

  Rules that made the last sync correct:
    * Prefer upstream's STRUCTURE, re-inject your FEATURE into it.
    * Check the API still exists before keeping your side:
        git grep -n "theFunctionName" -- apps/desktop/src
      If upstream deleted it, your side cannot compile — adapt, don't keep.
    * "Both added different things" = keep BOTH, not one.
    * After resolving: git add -A && git commit    (finishes the merge)

  Unlike a rebase this is resolved ONCE — the merge commit records it.
  Abort at any point with: git merge --abort
  Then re-run this script.
EOF
  exit 1
fi

# --- 6. Verify -------------------------------------------------------------
say "Verifying (typecheck + tests) — the only real proof the merge is correct"
cd apps/desktop

if ! npm run typecheck; then
  die "typecheck FAILED — fix and commit before pushing. Your merge is intact; nothing was pushed.
    undo the merge with: git reset --hard ORIG_HEAD"
fi

if ! npx vitest run; then
  die "tests FAILED — fix and commit before pushing. Your merge is intact; nothing was pushed.
    undo the merge with: git reset --hard ORIG_HEAD"
fi

cd "$REPO"

# --- 7. Push ---------------------------------------------------------------
if [[ "$DO_PUSH" == "1" ]]; then
  say "Pushing $DEV_BRANCH to YOUR FORK (origin)"
  # A plain push: merges never rewrite history, so no force is involved.
  git push origin "$DEV_BRANCH"
  restore_branch
  say "Done."
  echo "  the other machine picks this up with: ./scripts/fork-sync.sh"
else
  restore_branch
  say "Merge verified and GREEN. Not pushed (--no-push)."
  echo "  push with: git push origin $DEV_BRANCH"
  echo "  undo with: git reset --hard ORIG_HEAD"
fi
