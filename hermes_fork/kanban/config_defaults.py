"""Fork-owned Kanban dispatcher config defaults.

Extracted from ``hermes_cli/config_defaults.py`` (fork-budget.json cap on that
file): these are the fork's additions to upstream's ``DEFAULT_CONFIG["kanban"]``
dict and its ``HERMES_KANBAN_*`` ``OPTIONAL_ENV_VARS`` entries — infra-failure
classification, provider-quota backoff, review routing, priority reservation,
post-drain maintenance actions, review-round capping, and the worker-launcher
decoupling knob. Values and comments are unchanged from their original inline
position in ``DEFAULT_CONFIG["kanban"]``; only the storage location moved.

Pure-data leaf module, like ``hermes_cli/config_defaults.py`` itself — must not
import from ``hermes_cli.config`` or ``hermes_cli.config_defaults`` (the anchor
site in that file imports THIS module, not the other way around; an import back
would be circular).
"""

# Merged into DEFAULT_CONFIG["kanban"] via `.update()` at the
# `# >>> FORK ANCHOR: kanban-config-defaults <<<` site in
# hermes_cli/config_defaults.py, right after the DEFAULT_CONFIG literal closes.
FORK_KANBAN_DEFAULTS = {
    # When false (default), worker deaths classified as "infra" — an external
    # SIGTERM/SIGKILL the dispatcher did not send itself, a dead pid discovered
    # inside the dispatcher's own startup window (gateway restart / VM boot), or a
    # provider 429/quota signature — do NOT increment consecutive_failures and are
    # recorded as `interrupted` instead of `crashed`/`gave_up`, so a restart or a
    # multi-hour quota window can't burn the failure budget. Set true to restore
    # pre-classification behaviour (every such death counts like any other crash).
    # The exact signal allowlist: only SIGTERM and SIGKILL are neutral; SIGABRT,
    # SIGSEGV, SIGPIPE, and every other signal use the ordinary counted failure
    # path. See docs/kanban/infra-failure-classification.md.
    "count_infra_failures": False,
    # How long (seconds) after the dispatcher process itself started a "pid N not
    # alive" discovery is presumed to be a gateway restart / VM boot racing the
    # crash check (classified "infra") rather than a genuine mid-life crash
    # (classified "legit", counts as today). Only consulted when
    # count_infra_failures is false. Overridable via
    # HERMES_KANBAN_INFRA_STARTUP_WINDOW_SECONDS.
    "infra_startup_window_seconds": 120,
    # Provider-wide quota-backoff parking (default true). When a worker dies with a
    # provider 429/quota signature in its log, every same-provider task is parked in
    # "scheduled" until the retry-after deadline, rather than re-spawning immediately
    # and bouncing off the same quota wall. provider_override: false avoids parking
    # entirely. Pauses are provider-scoped and durable across dispatcher restarts.
    "provider_backoff": True,
    # Maximum seconds a provider backoff pause may last. retry-after values above
    # this cap are clamped to it and an operator-visible diagnostic is recorded. A
    # malformed/missing/nonpositive retry-after does NOT create a provider pause —
    # it follows the bounded interruption policy (max_infra_interruptions) instead.
    # Default 24h. Parse only positive base-10 integer retry-after values.
    "provider_backoff_max_seconds": 86400,
    # Optional host-wide account/budget quota circuits. Configure this only
    # in the shared/default Hermes home's config.yaml; dispatchers and
    # profile-scoped workers read that one authoritative host policy.
    # Empty by default:
    # provider names are not account identities, and credential selection
    # happens inside the worker. Operators explicitly map opaque, non-secret
    # group labels to provider/profile routes, for example:
    # quota_budget_groups:
    #   primary-wallet:
    #     providers: [openai-codex]
    #     profiles: [implementer, reviewer]
    # A route matches both lists; `*` is accepted only when written. A task
    # with provider `auto` is a candidate for every group mapped to its
    # profile: the dispatcher predicts the provider the worker's own
    # resolution ladder will choose for the explicit `provider=auto`
    # request it is spawned with, and starts it only when that provider
    # maps to an unpaused group. Unpredictable or unmapped resolution fails
    # closed while any candidate group is paused.
    "quota_budget_groups": {},
    # At a circuit deadline, admit one recovery probe host-wide, then admit
    # at most one further matching start per this many seconds until no
    # start has been admitted for four such windows. A renewed quota event
    # re-arms the circuit.
    "quota_resume_spread_seconds": 30,
    # Max consecutive infra interruptions (external SIGTERM/SIGKILL, startup-window
    # dead pid, quota signature including malformed/missing retry-after) before the
    # task is routed through normal counted failure accounting. Default 3; minimum
    # effective value 1. The streak is persistent per task, is incremented for every
    # otherwise-neutral infra path, and is reset only on a genuine non-interruption
    # terminal outcome or an explicit operator reset/unblock — never merely because
    # a task is redispatched. On exceeding the cap the task records an operator-
    # visible reason/event and the streak is preserved so repeated interruption
    # cannot evade the normal failure budget.
    "max_infra_interruptions": 3,
    # Profile that claims review-lane cards when the card is still assigned to the
    # implementer. "" = keep the card's own assignee (legacy behavior). Set this on
    # boards where review must never route back to the profile that did the work.
    "default_reviewer": "",
    # Opt-in create-time model routing for Kanban tasks. The built-in default
    # is deliberately inert: each profile must configure both its classifier
    # credentials/model selection and every eligible candidate route.
    "model_routing": {
        "enabled": False,
        "classifier": {
            # Keep only the bounded-input default; no provider/model means
            # enabled-but-unconfigured profiles fail closed without calling.
            "max_input_tokens": 8000,
        },
        "routes": {},
    },
    # Reserve a slice of each tick's spawn budget for high-priority cards, so a
    # Critical card is not stuck behind a pool saturated by Normal work. 0 (the
    # default) is today's behaviour: priority only orders rows inside a tick and
    # reserves no capacity. A positive int holds that many of the ready lane's
    # slots for cards at or above `priority_reserved_threshold` whenever such a
    # card actually wants one this tick (unclaimed in ready, with an assignee
    # that names a real profile). READY demand only: the review lane already
    # reserves a slot of its own, so a high-priority card in review does not
    # additionally draw on this one. Slots nobody is queued for fall through to
    # normal work in the SAME tick — no qualifying ready card, or fewer of them
    # than configured slots. A slot claimed by a queued high-priority card that
    # cannot spawn yet (per-profile cap, co-edit serialization, respawn guard) is
    # deliberately held idle rather than lent out, and reported as `unused`.
    # It grants EARLIER ACCESS to a slot, never preemption: a running worker is
    # never reclaimed to free one.
    "priority_reserved_slots": 0,
    # Priority at or above which a card draws on the reservation. Default 1 =
    # High and above on the documented tier scale (critical=2, high=1, normal=0,
    # low=-1). Inert while priority_reserved_slots is 0.
    "priority_reserved_threshold": 1,
    # Per-board worker-session rolling start rate limit. A positive integer
    # allows at most this many `spawned` events within
    # dispatch_start_window_seconds; queued work resumes automatically when
    # the earliest start leaves the window. None = off.
    "dispatch_start_budget": None,
    "dispatch_start_window_seconds": 600,
    # Maintenance actions an operator may queue to fire automatically once a
    # PAUSED board drains to zero running workers (dashboard "after drain"
    # selector / POST /dispatch/post-drain). The trigger is drain, never a
    # wall clock; expiry below is a safety bound, not a schedule.
    "post_drain": {
        # Units `service_restart` may restart, by exact name. EMPTY BY
        # DEFAULT: a queued action runs unattended, so which units may be
        # restarted is an explicit local decision rather than an inherited
        # one, and an empty list makes `service_restart` unqueueable. A
        # request may only NAME an entry from this list — it can never
        # supply a unit of its own. e.g. ["hermes-gateway.service"].
        "service_restart_allowlist": [],
        # "system" (systemctl) or "user" (systemctl --user).
        "service_restart_scope": "system",
        # Maintenance scripts `run_script` may run, as NAME -> ABSOLUTE
        # PATH. EMPTY BY DEFAULT for the same reason as the unit allowlist:
        # a request may only NAME an entry here, never supply a path or
        # arguments of its own, and an empty mapping makes `run_script`
        # unqueueable. Unlike `service_restart`, the name is always
        # required — a one-entry mapping does not resolve an unnamed
        # request. Scripts run as the gateway user with no privilege
        # escalation, and are bounded by a hard timeout.
        # e.g. {"fork-sync": "/home/me/projects/scripts/fork-sync.sh"}.
        "script_allowlist": {},
        # Expiry applied when the operator does not choose one. A pause that
        # never drains lets the action expire instead of firing hours later
        # into a state nobody expects.
        "default_expiry_seconds": 3600,
        # Hard ceiling on any requested expiry (24h).
        "max_expiry_seconds": 86400,
    },
    # After two reviewer changes-requested cycles, route the next rework run
    # to this specialist profile under that profile's own model defaults.
    # Empty preserves the original implementer loop.
    "review_rework_escalation_profile": "",
    # Worker preservation safety net. When a run ends (completion, review
    # request, block, archive) or is reclaimed (stale claim, timeout, dead
    # worker), Hermes commits any dirty work in that task's OWN git
    # worktree onto its existing task branch and pushes it to the
    # configured remote without force — so implementation work never
    # remains only on the machine that produced it.
    #
    # It is a PRESERVATION net, not merge automation: it never merges,
    # rebases, force-pushes, switches branches, or deletes a worktree or
    # branch, and it never touches another task's workspace. Ownership or
    # branch ambiguity fails closed before commit; unsafe content and git
    # failures record a redacted ``work_preservation_failed`` event. A
    # rejected push keeps the local commit and records ``pushed: false``.
    # In every case cleanup retains dirty or unpushed work for a human.
    # Only ``worktree`` workspaces are in scope; ``scratch``/``dir`` are
    # untouched. Gitignored files are excluded by git itself.
    "worker_preservation": {
        # Set false for non-Git workflows or hosts with custom remotes
        # where an automated push is unwanted. Preservation is skipped
        # entirely; nothing else changes.
        "enabled": True,
        # Refuse to snapshot when any single candidate file exceeds this,
        # or when the whole snapshot does. A safety net rescues
        # source-sized work; larger content is a build artifact or dataset
        # a human should place deliberately.
        "max_file_bytes": 5 * 1024 * 1024,
        "max_total_bytes": 20 * 1024 * 1024,
    },
    # Bound on the review<->changes_requested loop: once a card accumulates this many
    # changes_requested events since its last completion, the dispatcher hands it to
    # review_rework_escalation_profile for ONE terminal rework round (event
    # review_cap_escalated) and blocks it (kind="review_round_cap") only if that round also
    # comes back changes_requested — or immediately, when no escalation profile is set.
    # 0 = unlimited (legacy behavior). The reviewer-side round contract (sdlc-review
    # skill) is advisory; this is the hard stop that actually bounds a runaway rework loop.
    "max_review_rounds": 3,
    # Refuse a kanban_request_review handoff whose branch already conflicts with the board's
    # land_target — the conflict costs a full review round to report and one `git merge` to
    # fix. Skipped when the board sets no land_target or git cannot answer.
    "require_mergeable_for_review": True,
    # Refuse a kanban_request_review handoff (first review and re-review alike) whose
    # metadata lacks a usable `pre_review_gate` dict — non-empty `revision` (commit SHA or
    # `patch:<path>`) and `tests` (focused tests/gates run + result). Other keys (lint,
    # pushed, mergeable, acceptance) are accepted, not required. Off by default; the
    # refusal is an actionable tool error and the card stays with the implementer.
    "require_pre_review_gate": False,
    # Refuse a kanban_request_review handoff on a card with >= 1 changes_requested round
    # since its last completion unless metadata.rework_items=[{item, evidence}] maps each
    # reviewer item to its proof. An incomplete rework handoff otherwise burns the next
    # review round on "items 2 and 3 still not done" — with max_review_rounds at 2 that is
    # the main way a card hits the cap. First-time requests are never gated.
    "require_rework_items_for_review": True,
    # Argv PREFIX prepended to every spawned worker command. Empty list (default) = today's
    # plain `subprocess.Popen(argv, ...)` on every platform (Windows, macOS, non-systemd
    # Linux) — byte-identical behaviour, nothing to configure. When non-empty, the dispatcher
    # appends `--unit=kanban-<task_id>-run-<run_id>.scope` and the trailing `-- <argv>` itself
    # (the unit id always carries the explicit `.scope` suffix, since `systemctl --user`
    # resolves a bare name to a same-named `.service` that was never created); operators
    # supply only the launcher binary + its own flags, e.g.:
    #   worker_launcher: ["systemd-run", "--user", "--scope", "--slice=hermes-workers.slice",
    #                      "--collect", "--property", "MemoryAccounting=yes",
    #                      "--property", "MemoryHigh=1073741824", "--property", "MemoryMax=2147483648"]
    # This decouples a worker's lifetime/cgroup from the dispatching gateway process (a gateway
    # restart no longer kills in-flight kanban workers) on hosts that opt in. Applied
    # unconditionally to whatever argv is about to be Popen'd (after any restart-safe rewrap
    # has already happened), never gated on argv identity. The launcher binary is resolved
    # with `shutil.which()` at spawn time; if it can't be found the dispatcher logs a warning
    # and falls back to the `[]` (plain Popen) behaviour for that spawn rather than failing
    # the task. A `systemd-run --user` entry additionally requires a reachable user D-Bus
    # socket (XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS resolved from the process uid when
    # absent from the environment, since the gateway's own environment commonly lacks them);
    # when unreachable this also fails closed to plain Popen rather than letting `systemd-run`
    # fail at spawn time. Linux/systemd-specific in practice; never OS-conditioned in code —
    # an operator who sets this on Windows/macOS just gets a `FileNotFoundError`-driven
    # fallback.
    "worker_launcher": [],
}


def kanban_env_var_overrides(setting_factory):
    """Build the ``HERMES_KANBAN_*`` ``OPTIONAL_ENV_VARS`` entries.

    Takes the caller's ``_setting`` entry factory as a parameter instead of
    importing it, so this stays a pure-data leaf module reachable from
    ``hermes_cli/config_defaults.py`` without a circular import (``_setting``
    is defined further down in that same file, after ``DEFAULT_CONFIG``).
    """
    return {
        # Dispatcher infra-failure classification overrides (non-secret behavioral — allowed as
        # env bridges for the gateway-embedded dispatcher tick loop and the standalone daemon;
        # the config.yaml equivalents are the primary surface).
        "HERMES_KANBAN_COUNT_INFRA_FAILURES": setting_factory(
            "When set to true/false, overrides kanban.count_infra_failures (restores counted "
            "behaviour for infra deaths when true).", "Count infra failures", "false"),
        "HERMES_KANBAN_PROVIDER_BACKOFF": setting_factory(
            "When set to true/false, overrides kanban.provider_backoff (disables provider "
            "quota parking when false).", "Provider backoff", "true"),
        "HERMES_KANBAN_INFRA_STARTUP_WINDOW_SECONDS": setting_factory(
            "Overrides kanban.infra_startup_window_seconds (seconds after dispatcher start during "
            "which a dead-pid discovery is presumed to be a restart).", "Infra startup window (s)",
            "120"),
    }
