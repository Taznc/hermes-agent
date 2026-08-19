#!/usr/bin/env bash
# Sync the fork with upstream and rebase the custom `dev` branch onto it.
#
#   origin   = Taznc/hermes-agent        (YOUR fork — safe to push)
#   upstream = NousResearch/hermes-agent (READ ONLY — never pushed to)
#
# Encodes the lessons from the 2026-08-19 sync:
#   * shallow clones silently break rebases and fake huge "behind" counts
#   * fork main must be a pure upstream mirror before fast-forwarding
#   * a GitHub-side backup must exist BEFORE history is rewritten
#   * `git log main..dev` overcounts: commits already merged upstream still
#     appear. `git cherry` marks those with '-' and is the honest count.
#   * conflicts are usually semantic (upstream renamed/restructured an API),
#     so a green typecheck + vitest run is the only real proof of a good merge
#
# Usage:
#   ./scripts/fork-sync.sh              # the one command: adopt, sync, rebase, verify, push
#   ./scripts/fork-sync.sh --no-push    # ...stop before pushing dev
#   ./scripts/fork-sync.sh --check      # report divergence only, change nothing
set -euo pipefail

REPO="${FORK_SYNC_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DEV_BRANCH="${FORK_SYNC_DEV_BRANCH:-dev}"
MAIN_BRANCH="${FORK_SYNC_MAIN_BRANCH:-main}"
# Push is the default: the whole point is one command. --no-push is the escape
# hatch for inspecting a rebase before it becomes public.
DO_PUSH=1
CHECK_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --push) DO_PUSH=1 ;;
    --no-push) DO_PUSH=0 ;;
    --check) CHECK_ONLY=1 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
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

# A shallow clone makes rebases explode across the graft boundary and reports
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

# --- 1b. Reconcile with the OTHER machine -----------------------------------
# Two machines (laptop + dev VM) both push this branch, and dev gets REBASED,
# so it moves non-fast-forward. A stale local dev therefore looks "N ahead" of
# upstream while origin/dev already contains those same commits rebased — and
# working from it silently duplicates the other machine's sync. (This exact
# trap cost a ~500-commit redundant merge once.)
#
# `git cherry` is what makes the adopt decision safe: '+' is genuinely unique
# to local, '-' means an equivalent patch already exists on origin/dev.
if git rev-parse --verify --quiet "origin/$DEV_BRANCH" >/dev/null; then
  BEHIND_ORIGIN="$(git rev-list --count "$DEV_BRANCH..origin/$DEV_BRANCH")"

  if [[ "$BEHIND_ORIGIN" != "0" ]]; then
    say "origin/$DEV_BRANCH is ahead by $BEHIND_ORIGIN commit(s) — the other machine synced"

    # `git cherry` alone is too strict here: when the other machine rebased, it
    # RESOLVED CONFLICTS, so the replayed commit is no longer patch-identical
    # and shows as '+' even though the work is present. Fall back to comparing
    # subjects — a commit whose subject already exists on origin/dev is
    # accounted for; anything else is genuinely unpushed local work.
    MISSING=""
    while IFS= read -r sha; do
      [[ -z "$sha" ]] && continue
      subject="$(git log -1 --format='%s' "$sha")"

      if ! git log --format='%s' "upstream/$MAIN_BRANCH..origin/$DEV_BRANCH" | grep -qxF "$subject"; then
        MISSING+="  $(git log -1 --format='%h %s' "$sha")"$'\n'
      fi
    done < <(git cherry "origin/$DEV_BRANCH" "$DEV_BRANCH" | awk '/^\+/ {print $2}')

    if [[ -n "$MISSING" ]]; then
      printf '%s' "$MISSING"
      die "local $DEV_BRANCH has commit(s) NOT on origin (above).
    Rebase them yourself first — this script will not choose which side wins:
      git rebase origin/$DEV_BRANCH"
    fi

    if [[ "$CHECK_ONLY" == "1" ]]; then
      echo "  (--check) would adopt origin/$DEV_BRANCH; every local commit already exists there"
    else
      # Safe: every local subject is present on origin/dev (rebased there, with
      # conflicts already resolved), so adopting loses nothing. The tag makes
      # the pre-adopt state recoverable regardless.
      git tag -f "preadopt-$DEV_BRANCH-$(date +%Y%m%d-%H%M%S)" "$DEV_BRANCH" >/dev/null
      say "Adopting origin/$DEV_BRANCH (all local work already present there)"
      git checkout -q "$DEV_BRANCH"
      git reset --hard -q "origin/$DEV_BRANCH"
    fi
  fi
fi

UPSTREAM_HEAD="$(git rev-parse upstream/$MAIN_BRANCH)"
MAIN_HEAD="$(git rev-parse $MAIN_BRANCH)"
DEV_HEAD="$(git rev-parse $DEV_BRANCH)"

# --- 2. Report divergence ---------------------------------------------------
say "Divergence"

# `git cherry` is the honest count: '+' = genuinely unique to dev,
# '-' = an equivalent patch already exists upstream (would be dropped).
UNIQUE="$(git cherry "$MAIN_BRANCH" "$DEV_BRANCH" | grep -c '^+' || true)"
DUPES="$(git cherry "$MAIN_BRANCH" "$DEV_BRANCH" | grep -c '^-' || true)"
BEHIND="$(git rev-list --count "$MAIN_BRANCH".."upstream/$MAIN_BRANCH")"

echo "  fork $MAIN_BRANCH is behind upstream by : $BEHIND commit(s)"
echo "  $DEV_BRANCH commits genuinely unique     : $UNIQUE"
echo "  $DEV_BRANCH commits already upstream     : $DUPES (will be dropped by rebase — expected)"

if [[ "$CHECK_ONLY" == "1" ]]; then
  say "--check requested; nothing modified."
  exit 0
fi

if [[ "$BEHIND" == "0" ]]; then
  # Upstream is caught up, but an adopt above may still have moved dev, and the
  # working branch must be restored either way.
  say "Already up to date with upstream."
  restore_branch
  exit 0
fi

# --- 3. Verify fork main is a clean mirror ----------------------------------
say "Verifying fork $MAIN_BRANCH is a pure upstream mirror"

# Anything here means someone committed to the fork's main directly. Rebasing
# on top of that silently buries the work, so refuse instead.
STRAY="$(git log --oneline "upstream/$MAIN_BRANCH..$MAIN_BRANCH" | head -20)"
if [[ -n "$STRAY" ]]; then
  echo "$STRAY"
  die "fork $MAIN_BRANCH has commits NOT in upstream (above). Resolve before syncing."
fi

# --- 4. Backup BEFORE rewriting anything ------------------------------------
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="backup/$DEV_BRANCH-$STAMP"

say "Pushing safety backup to your fork: $BACKUP"
git branch -f "$BACKUP" "$DEV_BRANCH"
git tag -f "presync-$DEV_BRANCH-$STAMP" "$DEV_BRANCH" >/dev/null
# Backup must live on GitHub, not just locally — a local-only backup does not
# survive the machine it was made on.
git push -q origin "$BACKUP" --force
git push -q origin "presync-$DEV_BRANCH-$STAMP" --force
echo "  recover with: git reset --hard presync-$DEV_BRANCH-$STAMP"

# --- 5. Fast-forward fork main ---------------------------------------------
say "Fast-forwarding fork $MAIN_BRANCH -> upstream/$MAIN_BRANCH"
git checkout -q "$MAIN_BRANCH"
git merge --ff-only "upstream/$MAIN_BRANCH"
git push -q origin "$MAIN_BRANCH"

# --- 6. Rebase dev ----------------------------------------------------------
say "Rebasing $DEV_BRANCH onto $MAIN_BRANCH ($UNIQUE commit(s) to replay)"
git checkout -q "$DEV_BRANCH"

# rerere records conflict resolutions so a re-run of the same rebase replays
# them automatically instead of asking twice.
git config rerere.enabled true

if ! git rebase "$MAIN_BRANCH"; then
  cat <<'EOF'

  Rebase stopped on a conflict. This is normal and usually SEMANTIC:
  upstream renamed or restructured an API your commit also touched.

  Rules that made the last sync correct:
    * Prefer upstream's STRUCTURE, re-inject your FEATURE into it.
    * Check the API still exists before keeping your side:
        git grep -n "theFunctionName" -- apps/desktop/src
      If upstream deleted it, your side cannot compile — adapt, don't keep.
    * "Both added different things" = keep BOTH, not one.
    * After resolving: git add -A && git rebase --continue

  When finished, re-run this script with --push.
EOF
  exit 1
fi

# --- 7. Verify -------------------------------------------------------------
say "Verifying (typecheck + tests) — the only real proof the merge is correct"
cd apps/desktop

if ! npm run typecheck; then
  die "typecheck FAILED — fix before pushing. Your rebase is intact; nothing was pushed."
fi

if ! npx vitest run; then
  die "tests FAILED — fix before pushing. Your rebase is intact; nothing was pushed."
fi

cd "$REPO"

# --- 8. Push ---------------------------------------------------------------
if [[ "$DO_PUSH" == "1" ]]; then
  say "Force-pushing rebased $DEV_BRANCH to YOUR FORK (origin)"
  # --force-with-lease refuses to clobber commits fetched after our last fetch.
  git push --force-with-lease origin "$DEV_BRANCH"
  restore_branch
  say "Done. Backup retained at origin/$BACKUP"
  echo "  the other machine picks this up with: ./scripts/fork-sync.sh"
else
  restore_branch
  say "Rebase verified and GREEN. Not pushed (--no-push)."
  echo "  push with: git push --force-with-lease origin $DEV_BRANCH"
  echo "  backup at: origin/$BACKUP"
fi
