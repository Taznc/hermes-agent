# hermes-workers.slice — decoupling kanban worker lifetime/memory from hermes-gateway

Status: spec only, no implementation in this document/card.
Card: t_cb47a946 (child of t_44ca59a3). Evidence: t_f44be004 (`worker-slice-evidence.md`).

## 0. Chosen mechanism (up front)

**Keep `subprocess.Popen`, add `start_new_session=True` (already present) plus a
configurable launcher-prefix hook: `kanban.worker_launcher: [...]`, default `[]`
(today's plain Popen).** When set, the dispatcher prepends the configured argv
to the worker command before `Popen`, exactly as `_default_spawn` already does
internally for `_restart_safe_worker_argv`/`restart_safe_gateway_child_argv`
(option (a)-flavored: `systemd-run --user --scope --slice=hermes-workers.slice
--unit=kanban-<task_id>-run-<run_id> --property MemoryAccounting=yes
--property MemoryHigh=<...> --property MemoryMax=<...> -- <argv>` is the
infra-shipped default value of that launcher on this VM, not a hardcoded
mechanism in core).

This is option (d) in the card body, generalized to also cover the slice
(`--slice=hermes-workers.slice`) rather than only the ad hoc
`hermes-worker-<pid>` unit name `tools/process_registry.py` mints today.

### Why (d), rejecting (a)/(b)/(c) as the *hardcoded* mechanism

- **(a) `systemd-run --scope --slice=... ` as a *system* scope, unconditionally.**
  Evidence: unprivileged system-scope creation returned `Failed to start
  transient scope unit: Interactive authentication required.` `sudo -n
  systemd-run` worked only because this dev VM grants `hermes` blanket
  passwordless sudo (`(ALL) NOPASSWD: ALL`) — not a property any real
  deployment should assume, and shelling to `sudo` from the gateway process
  is itself a privilege-escalation surface we don't want baked into core.
  Rejected as the *only, hardcoded* path — it becomes a legitimate *value* of
  `worker_launcher` for an operator who has set up narrow polkit rules, which
  the launcher-prefix design permits without core caring.
- **(b) `systemd-run --user --scope` unconditionally, relying on
  `loginctl enable-linger`.** Evidence: this is exactly what
  `tools/process_registry.py`'s existing `_systemd_run_user_scope_available()`
  / `_build_systemd_scope_argv()` already do for background terminal
  executors (#70716), and `kanban_db_dispatch.py`'s
  `_restart_safe_worker_argv()`/`restart_safe_gateway_child_argv()` already
  wraps kanban workers the same way when the process is a supervised systemd
  gateway. It is unprivileged and portable in principle, but two evidence
  gaps disqualify it as *the sole hardcoded mechanism*: (1) the user scope
  landed under `user-1000.slice`, which has `MemoryHigh=infinity` /
  `MemoryMax=infinity` by default — the ceiling is not free, it requires a
  second privileged `systemctl set-property user-<uid>.slice ...` step
  outside the gateway's own permissions; (2) it only fired for the evidence
  probe after manually supplying `XDG_RUNTIME_DIR`/`DBUS_SESSION_BUS_ADDRESS`
  — the bare gateway environment failed with `Failed to connect to bus: No
  medium found` despite `Linger=yes`. Both are solvable, which is exactly why
  they belong in the *launcher script* infra ships (see §6), not hardcoded
  into `kanban_db_dispatch.py`.
- **(c) `Delegate=yes` + dispatcher-managed child cgroups.** Evidence proves
  this only reassigns memory accounting *within* the existing service cgroup
  subtree — `systemd.kill(5)`'s `KillMode=mixed` still SIGTERMs-then-SIGKILLs
  every process in the *unit's* cgroup, delegated subtree included, on `stop`/
  restart. A live probe (`kanban-evidence-delegate.scope`, `Delegate=yes`)
  landed at `/system.slice/kanban-evidence-delegate.scope` — still reachable
  by the parent's kill fan-out. This solves memory *attribution* only; it
  does not solve the restart-survival problem that is half of this card's
  motivation (18/25 worker crashes were gateway restarts). Rejected outright
  as insufficient on its own; nothing in (c) is even worth keeping as a
  `worker_launcher` value since it changes nothing about spawn topology from
  the dispatcher's Popen call.

### Why (d) over hardcoding (a) or (b) as the one true mechanism

The evidence report ends with: *"the evidence favors preserving a launcher
abstraction/fallback and using user scopes only when their D-Bus environment
is positively probed."* Three independent reasons converge on the same
answer:

1. **Precedent already exists twice in this codebase** for exactly this
   pattern — `tools/process_registry.py` (background terminal executors) and
   `kanban_db_dispatch.py`'s `_restart_safe_worker_argv()` (kanban workers on
   a supervised systemd gateway) both already wrap `Popen` argv with a
   systemd-run prefix *conditionally*, probed once and cached, with a plain
   argv fallback everywhere else. A `kanban.worker_launcher` config key
   generalizes that existing internal pattern into an operator-facing knob
   instead of adding a third bespoke internal implementation.
2. **Contribution rubric ("Footprint Ladder")**: extend existing code /
   config over new mechanism. `kanban.worker_launcher: [...]` is a
   `config.yaml` list (rung 1 — extend existing code, zero new model-tool
   surface), not a new `HERMES_*` env var and not a new core tool. It is also
   the *only* option of the four that stays correct with zero config on
   Windows/macOS/non-systemd Linux (§5) — the default value is `[]`, meaning
   "run `Popen` exactly like today."
3. **Evidence explicitly shows environment-dependence that must be handled
   by infra, not baked into core**: sudo availability, D-Bus session
   reachability, and slice-vs-unprivileged-cgroup existence are all
   host/deployment properties. A single hardcoded core mechanism is
   guaranteed to be wrong on some supported platform (Windows/macOS have no
   systemd at all). The launcher-prefix design pushes exactly that
   variability to the thing that already varies per host: infra
   configuration.

**What `hermes-workers.slice` + `--user --scope` need to actually work in
production** (the infra-shipped default `worker_launcher` value, delivered by
the separate hermes-dev-infrastructure PR referenced in the parent card, not
by this repo):

- `loginctl enable-linger hermes` (already true per evidence: `Linger=yes`).
- A `systemctl set-property user-1000.slice Slice=hermes-workers.slice ...`
  equivalent is NOT how user scopes attach to a *system* slice — user scopes
  under `--user` always nest under `user-<uid>.slice` regardless of
  `--slice=`; `--slice=hermes-workers.slice` under `--user` creates
  `hermes-workers.slice` as a *sub-slice of the user manager*
  (`user-1000.slice/hermes-workers.slice`), which is allowed and is the
  correct wiring for this design — the ceiling lives on that sub-slice, not
  on `user-1000.slice` itself, so infra does not need to touch
  `user-1000.slice`'s own (unlimited) properties at all. See §6.
- The launcher script (not core Python) is responsible for exporting
  `XDG_RUNTIME_DIR=/run/user/<uid>` and `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus`
  before invoking `systemd-run --user`, OR the systemd unit for the gateway
  sets `Environment=` for those two variables directly (preferred — the
  gateway then needs no launcher-side probing beyond "does `systemd-run
  --user --scope` succeed").

## 1. Dispatcher tracking/reaping of non-child workers

Today (`hermes_cli/kanban_db.py` + `kanban_db_dispatch.py`): direct-child
dependence is `reap_worker_zombies()` (`os.waitpid(-1, os.WNOHANG)`,
records raw status into the in-process `_recent_worker_exits` map) feeding
`_classify_worker_exit(pid)` feeding `_classify_dead_worker()` inside
`_reclaim_dead_workers()`. `_pid_alive()` and `_defer_reclaim_for_live_worker()`
are parentage-independent already and need no change.

When `kanban.worker_launcher` is set, the spawned PID (`proc.pid`, still
returned by `_default_spawn`) is the **launcher's** PID (e.g.
`systemd-run`'s transient scope's main PID), not necessarily the `hermes`
process. `systemd-run --scope` (unlike `--unit`, which forks and returns
immediately) execs the target directly under the scope, so `proc.pid` **is**
the actual `hermes chat` PID and stays a real child of the dispatcher for
`waitpid` purposes as long as the scope's cgroup itself is not what gets
killed by a gateway restart — i.e. `waitpid`/`reap_worker_zombies()` keep
working *unchanged* for detecting the worker's own exit, because the
scope wraps the cgroup, not the process tree. What breaks is only the
*restart-survival* case: when `hermes-gateway.service` restarts, `KillMode=
mixed` no longer reaches into `hermes-workers.slice` (that's the entire
point), so the worker keeps running as a **still-live child** whose parent
(the old gateway process) is about to die. On Linux, when the parent dies,
the child is reparented to the nearest subreaper/init — at that point
`waitpid(-1, WNOHANG)` in the *new* gateway process's `reap_worker_zombies()`
correctly returns nothing for it (it was never this process's child), and
`os.waitpid` in the **old**, now-dying gateway process races the process
exit and may or may not observe it. This is the actual gap to close:

- **Exit-status source of truth becomes the scope unit's recorded exit code,
  not `waitpid`.** Add `_scope_exit_status(unit_name: str) -> Optional[tuple[str,int]]`:
  run `systemctl --user show <unit_name> -p ExecMainStatus -p ExecMainCode
  -p ActiveState --value` (bounded timeout, best-effort). `ActiveState=
  inactive`/`failed` + `ExecMainCode=exited` classifies via `ExecMainStatus`
  exactly like `WEXITSTATUS` does today (0 → `clean_exit`,
  `KANBAN_RATE_LIMIT_EXIT_CODE` → `rate_limited`, else `nonzero_exit`);
  `ExecMainCode=killed` classifies via `ExecMainStatus` as the signal number,
  same shape as `WTERMSIG` → `signaled`. `--collect` (already used) means the
  unit is auto-cleaned shortly after this becomes queryable, so this must be
  read promptly by the *same* dispatch tick that observes `_pid_alive()` go
  false, not lazily.
- **`reap_worker_zombies()` keeps running unconditionally** — it costs
  nothing when there's nothing to reap and still correctly reaps any
  worker spawned with the launcher unset (`worker_launcher: []`, the
  default) or any worker whose scope process is still this process's direct
  child (the common case — most ticks are not concurrent with a gateway
  restart).
- **`_classify_worker_exit(pid)` gains a launcher-aware fallback**: if `pid`
  is not in `_recent_worker_exits` (today's `"unknown"` case) AND the task
  row has a recorded `systemd_unit` name (new column, see below), attempt
  `_scope_exit_status(unit_name)` before falling back to `"unknown"`. This
  keeps the existing signature/behavior for the no-launcher path untouched.
- **New persisted field**: `tasks.worker_unit` (nullable TEXT), written by
  `_default_spawn` alongside `worker_pid` whenever `worker_launcher` produced
  a `--unit=<name>` scope. Needed because after a gateway restart the *new*
  dispatcher process has no in-memory `_recent_worker_exits` entry for a
  worker it never spawned — the unit name is the only durable handle to ask
  systemd for that worker's fate. Schema/migration is implementation, not
  spec, but the contract is: `worker_unit` must be queryable from a cold
  dispatcher process using only the DB row.
- **`_pid_alive(pid)` is unchanged.** It already works via `os.kill(pid, 0)`
  + `/proc/<pid>/status` zombie check, both parentage-independent. This
  remains the primary liveness signal every tick; the scope-status query is
  only consulted once `_pid_alive` has gone false, to get a *classification*
  rather than falling into the generic `"unknown"`→ crashed path.
- **`_defer_reclaim_for_live_worker()` is unchanged in logic**, but its
  termination caller (`_terminate_reclaimed_worker`) must, when a
  `worker_unit` is present, prefer `systemctl --user stop <unit>` (already
  implemented as `_stop_systemd_unit()` in `tools/process_registry.py`,
  reused not reinvented) over a bare `os.kill(pid, SIGTERM)` — a scope may
  contain descendants (double-forked children) that a single-PID signal
  does not reach, exactly the rationale already documented on
  `_stop_systemd_unit()` for #70716's reviewer gap #2. Bare PID `SIGTERM`
  remains the path when `worker_unit` is empty (default/no-launcher case),
  so today's behavior is preserved exactly.

## 2. Heartbeat/claim behavior across a gateway restart

No change to claim/heartbeat SQL semantics — `heartbeat_claim()`,
`release_stale_claims()`, `DEFAULT_CLAIM_TTL_SECONDS` (15m default),
`DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS` (1h wedged backstop) all operate
purely on DB rows plus `_pid_alive`, independent of parentage already. The
scenario to make correct end-to-end:

1. Gateway A dispatches task T, worker W spawned via `worker_launcher` into
   `hermes-workers.slice`, `worker_pid=<W's pid>`, `worker_unit=<unit>`
   persisted, `claim_lock="hostA:pidA"` (existing `_claimer_id()` format —
   unaffected, still host:pid of the *dispatcher*, not the worker).
2. Gateway A restarts (`systemctl restart hermes-gateway.service`). `KillMode=
   mixed` fans out inside `/system.slice/hermes-gateway.service` only — W is
   in `hermes-workers.slice`, untouched (this is the entire point of the
   spec). Gateway A's own process exits; W is reparented but keeps running
   and keeps calling `kanban_heartbeat` (a kanban worker's own tool calls,
   unrelated to the dispatcher process) — the DB row's `last_heartbeat_at`
   keeps advancing throughout the restart, because heartbeats are a
   worker-owned DB write, never routed through the dispatcher process at
   all.
3. Gateway B starts. `claim_lock` still reads `"hostA:pidA"` — same host,
   *different* dispatcher pid, because `_claimer_id()` embeds the
   dispatcher's own PID, which changed across the restart.
   **Re-adoption requirement**: `_reclaim_dead_workers()`'s existing
   host-prefix check (`lock.startswith(host_prefix)`, where
   `host_prefix = f"{socket.gethostname()}:"`, NOT `f"{hostname}:{pid}"`)
   already matches on hostname only, not full claimer id — re-read
   `_host_prefix()` confirms this. **This means re-adoption already works
   today for the liveness check** (`_pid_alive(worker_pid)` returns True
   regardless of which dispatcher PID is asking), and MUST continue to: do
   not tighten the host-prefix match to include the dispatcher PID, or
   restart-survival breaks silently. This is a **do-not-regress** contract,
   not new work — call it out explicitly in the PR that implements this
   spec, with a regression test (§7) pinning it.
4. Gateway B's first `_reclaim_dead_workers()` tick sees `worker_pid` still
   alive (`_pid_alive` is parentage-independent, confirmed in evidence) →
   task stays `running`, **no double-dispatch**, no reclaim. This is the
   steady state for as long as W runs.
5. When W eventually exits (clean/crash/rate-limit), Gateway B's tick
   observes `_pid_alive` false and classifies via `worker_unit` +
   `_scope_exit_status` per §1 (Gateway B has no `_recent_worker_exits`
   entry for a PID it never spawned/reaped — this is the case that needed
   the new fallback).

No claim-lock format change, no heartbeat cadence change. The only genuinely
new behavior is the exit-classification fallback in §1; everything else in
this section is confirming (with a regression test) that existing
`_pid_alive`-based reclaim logic is already restart-safe by construction —
the risk is a future refactor accidentally tightening the host-prefix
match, not the current code.

## 3. Host-shutdown behavior (SIGTERM → grace → SIGKILL)

- **Unit property providing the grace period: `TimeoutStopSec=` on
  `hermes-workers.slice`** (slice-level `TimeoutStopSec` applies to member
  scopes' stop sequencing per `systemd.resource-control(5)`), *not* on
  `hermes-gateway.service` — the gateway's own `TimeoutStopSec=60` (current
  value, confirmed via `systemctl show`) governs only the gateway unit's own
  shutdown, and must stay decoupled from worker shutdown now that workers
  are outside its cgroup. Recommended default: `TimeoutStopSec=30s` on the
  slice — long enough for a worker mid-`kanban_complete`/`kanban_block` write
  to flush, short enough that host shutdown doesn't hang.
- **Ordering vs. the gateway unit**: workers must NOT be ordered
  `After=hermes-gateway.service` / `Requires=hermes-gateway.service` at the
  slice level — that would reintroduce exactly the coupling this spec
  removes (stopping the gateway would pull the slice down transitively via
  systemd's default stop-propagation for `Requires=`, or at minimum
  complicate independent restart). The slice is deliberately **unordered**
  relative to the gateway; only genuine host shutdown (`systemd` default
  target teardown, all units get SIGTERM in dependency-appropriate but
  otherwise parallel order) or an explicit
  `systemctl --user stop hermes-workers.slice` reaches workers.
- **Mechanism**: on real host shutdown, systemd sends SIGTERM to each unit's
  main process (the worker's `hermes` process, since `--scope` execs it
  directly — no wrapper PID to relay through) and, after
  `TimeoutStopSec` elapses without the cgroup going empty, escalates to
  SIGKILL for the whole cgroup (same mechanism already documented for
  `_stop_systemd_unit()`/#70716). A worker's tool-call loop already reacts to
  SIGTERM via existing shell-hook/subprocess-tree-kill handling
  (`tests/agent/test_shell_hooks_tree_kill.py`,
  `tests/hermes_cli/test_signal_handler_kanban_worker.py` — both already
  exist and are unrelated to this spec beyond confirming the pattern is
  established) — no new signal-handling code needed in the worker itself;
  this section only pins the systemd-side property, not new Python.
- The gateway's own `ExecStopPost=gateway.cgroup_cleanup` (SIGKILL-sweeps its
  *own* cgroup on stop) must NOT be pointed at `hermes-workers.slice` — that
  cgroup is deliberately outside the gateway's reach; sweeping it from the
  gateway's stop hook would silently reintroduce the coupling for every
  *service* restart, not just host shutdown, defeating the entire point.
  This is a hard constraint on the infra PR, called out explicitly so a
  well-meaning "let's also clean up leftover workers here" edit doesn't land
  later.

## 4. Windows / macOS / non-systemd Linux fallbacks

- **Config schema** (in `hermes_cli/config_defaults.py`'s `"kanban"` dict,
  alongside `dispatch_in_gateway`, `failure_limit`, etc.):
  ```yaml
  kanban:
    worker_launcher: []   # e.g. ["systemd-run", "--user", "--scope",
                           #       "--slice=hermes-workers.slice", "--collect",
                           #       "--property", "MemoryAccounting=yes",
                           #       "--property", "MemoryHigh=...",
                           #       "--property", "MemoryMax=..."]
                           # `--unit=kanban-<task_id>-run-<run_id>` and the
                           # trailing `-- <argv>` are appended by the
                           # dispatcher itself — operators supply the prefix
                           # only, matching the shape already used internally
                           # by tools/process_registry.py's
                           # _build_systemd_scope_argv / restart_safe_gateway_child_argv.
  ```
  Default `[]` → `_default_spawn` behaves exactly as today: plain `Popen`,
  `start_new_session=True`, no wrapper. This is the ENTIRE fallback story —
  there is no OS-conditional branch to write in core, because the config
  default is empty everywhere the launcher isn't explicitly configured.
- **Detection/validation path**: at dispatcher startup (or lazily, cached
  like `_systemd_run_user_scope_available()` already is), if
  `worker_launcher` is non-empty:
  1. Resolve `worker_launcher[0]` via `shutil.which()`. If not found, log a
     warning once and fall back to `[]` behavior for that tick (fail open,
     not closed — a misconfigured launcher must not stop the board from
     making progress; contrast with `restart_safe_gateway_child_argv`'s
     fail-closed `RuntimeError`, which is deliberately stricter because it
     guards *actual* gateway-restart-survival for that one narrow supervised
     case — this new knob is opt-in operator config, so the failure
     philosophy is "degrade to today's behavior", not "hard stop").
  2. Optionally, a `--dry-run`-able cheap self-test (`<launcher argv> --
     /bin/true`-equivalent) at first use, cached process-wide with a short
     negative-result TTL, exactly mirroring
     `_systemd_run_user_scope_available()`'s existing double-checked-locking
     probe pattern — reuse that helper's shape rather than inventing a
     second one.
  3. `os.name == "nt"` (Windows) and `sys.platform == "darwin"` (macOS) are
     never special-cased in this code path at all: `worker_launcher`
     defaults to `[]` in `config_defaults.py` unconditionally, so a Windows
     or macOS install that never sets `kanban.worker_launcher` in
     `config.yaml` never touches any of this logic. If a Windows/macOS
     operator sets a launcher anyway (e.g. a future `job_object` wrapper — no
     such wrapper is proposed by this spec), the dispatcher does not
     validate that the launcher is "appropriate" for the host OS; it is a
     raw argv prefix, and an operator who configures a Linux-only tool on
     Windows gets a `FileNotFoundError` from `shutil.which()`, degrading to
     `[]` per step 1. Nothing OS-specific is hardcoded.
  4. Non-systemd Linux (containers without systemd, `sysvinit`, WSL1, etc.):
     identical story — `shutil.which("systemd-run")` fails, degrade to `[]`.
     `_is_supervised_gateway_process()`'s existing `INVOCATION_ID` check
     (already present for the *other* systemd-run use in
     `tools/process_registry.py`) is the right template for an *optional*
     additional guard: only apply `worker_launcher` when running under a
     supervisor that makes it meaningful, though this is a refinement, not a
     hard requirement — the resolvable-binary check alone is sufficient for
     "strictly optional, defaults to plain Popen."

## 5. Infra contract: `hermes-workers.slice` unit properties

Derived from evidence: p50 ≈230MiB/p95≈300MiB/max≈300MiB per-worker RSS
sample (60s window, 6-worker concurrency observed live). RSS undercounts
true memory.max exposure (cgroup accounting includes page cache attributed
to the cgroup, buffers, etc.), and the sample is short-horizon (not a
long-tail peak distribution per the evidence report's own caveat) — so the
contract below intentionally leaves headroom rather than fitting the sample
tightly.

```ini
# hermes-workers.slice (installed under the user manager: as a --user scope
# child, this file lives at ~hermes/.config/systemd/user/hermes-workers.slice
# or is created transiently the first time systemd-run --slice= references
# it — either is valid; the infra PR should ship it as a real unit file for
# visibility in `systemctl --user status hermes-workers.slice` rather than
# relying on transient auto-creation).
[Unit]
Description=Hermes kanban worker pool (decoupled from hermes-gateway.service)

[Slice]
# Per-worker MemoryMax is set on each scope individually via
# `--property MemoryMax=<value>` in the worker_launcher argv (mirrors the
# existing per-worker _worker_memory_max_bytes() pattern in
# tools/process_registry.py — reuse that function, don't reinvent a second
# per-worker sizing formula). This slice-level setting is the AGGREGATE
# ceiling across all concurrently-running workers.
MemoryHigh=6G
MemoryMax=8G
# No TasksMax override — inherit system default; kanban tasks are not
# fork-bomb-shaped workloads by design intent, and setting one is scope
# creep beyond this card's evidence.
```

- **`MemoryHigh=6G` / `MemoryMax=8G` derivation**: 6 workers × 300MiB max
  observed ≈ 1.8GiB steady-state, but the evidence explicitly warns this
  is NOT a long-horizon peak (only a 60s window). Sizing headroom: assume
  worst case 2-3x burst per worker during heavy tool use (browser/terminal
  subprocess trees the worker itself spawns are inside its own scope too,
  since `--scope` places the exec'd process and everything it forks in the
  same cgroup) at the currently observed default concurrency cap
  (`derive_default_max_in_progress()` — memory-derived, not hardcoded, so
  this number moves with host RAM already; 6 was the observed live
  concurrency during evidence gathering, not a hard limit). `MemoryHigh` at
  6G gives ~10x the measured steady-state aggregate before throttling
  begins; `MemoryMax` at 8G is the hard OOM boundary. These are **starting
  values for the infra PR to tune against real production `memory.peak`
  data once the slice is live** — the current 20G/24G gateway-wide numbers
  were themselves evidently undersized for 6 workers' worth of headroom
  (7,320 throttle events/day), so this spec deliberately does not just
  carve the existing 20G into the same shape; it prices the workers'
  *actual* measured footprint with wide margin and leaves the gateway's own
  ceiling to be revisited separately (out of scope for this card — the
  gateway's `MemoryHigh=20G`/`MemoryMax=24G` can stay as-is, or infra may
  choose to shrink it now that workers are no longer inside it; that's an
  infra-PR judgment call to make with fresh `memory.peak` data post-cutover,
  not this spec's to dictate).
- **`TimeoutStopSec=30s`** on the slice per §3.
- **No `Slice=` wiring needed on `hermes-gateway.service` itself** — the
  gateway's own unit is unchanged; only the *worker* launcher argv
  references `--slice=hermes-workers.slice`.
- Per-worker `--property MemoryMax=<bytes>` in the launcher argv should
  reuse `tools/process_registry.py::_worker_memory_max_bytes()`'s existing
  sizing logic (tighter of cgroup `memory.max`/half-of-physical-RAM/4GiB cap,
  with a `TERMINAL_LOCAL_MEMORY_MAX_MB` override already wired) rather than
  a second bespoke per-worker constant — one sizing formula, two call sites.

## 6. Test plan

Unit-testable with a **fake launcher** (`kanban.worker_launcher` set to a
short Python/echo script fixture, no real systemd dependency, runs on any
CI host including macOS/Windows lanes):

- `_default_spawn` prepends `worker_launcher` argv exactly, with
  `--unit=<computed-name>`/trailing `-- <cmd>` appended by the dispatcher
  (not hand-typed by the operator) — assert the constructed argv shape.
- `worker_launcher=[]` (default) produces byte-identical `Popen` argv to
  today's behavior — regression-pins the "strictly optional" contract from
  §4 directly, not just by inspection.
- `worker_unit` is persisted on the task row iff the launcher argv contains
  `--unit=`; absent for the `[]` default — asserts the new column only
  gets populated conditionally.
- `_classify_worker_exit` fallback: with a task row carrying a
  `worker_unit` but no `_recent_worker_exits` entry (simulating a
  cold/restarted dispatcher), a monkeypatched `_scope_exit_status` stub
  returning `("nonzero_exit", 3)` must be consulted and its result
  returned, vs. the pre-existing `"unknown"` when `worker_unit` is empty —
  this is the core new branch and needs direct coverage, not just
  integration coverage.
- Re-adoption regression test per §2 point 3: construct a claim row with
  `claim_lock` embedding a **different** PID than the current process's own
  `_claimer_id()`-equivalent PID (simulating "gateway restarted, new
  dispatcher PID"), mock `_pid_alive` True, and assert
  `_reclaim_dead_workers()` does NOT reclaim it — pins the host-prefix
  (not full-claimer) matching contract explicitly so a future refactor
  can't silently tighten it and reintroduce double-dispatch after every
  gateway restart.
- Termination path: with `worker_unit` set, `_terminate_reclaimed_worker`
  (or its caller) must call the systemd-unit-stop path
  (`_stop_systemd_unit`-equivalent) rather than bare `os.kill` — assert via
  a mocked `signal_fn`/stop-callable that the unit-stop path is invoked and
  the raw-PID path is not, and vice versa when `worker_unit` is empty.

Needs a **systemd-gated integration test** (skip unless `shutil.which
("systemd-run")` and a working user D-Bus session are both present — mirror
the existing gating pattern used by `tests/tools/test_process_registry.py`'s
#70716 tests, which already skip cleanly on hosts without a systemd user
manager):

- Real `systemd-run --user --scope --slice=hermes-workers.slice` spawn of a
  short-lived fake "worker" script that writes a sentinel file and exits
  with `KANBAN_RATE_LIMIT_EXIT_CODE`; assert `_scope_exit_status()` reads
  back `("rate_limited", <code>)` from the real unit, not a mock — this is
  the one piece of behavior that cannot be faked (real
  `systemctl --user show -p ExecMainStatus` semantics).
  `--collect` behavior (unit disappears after query) should be asserted too,
  since it constrains *when* the dispatcher tick must read the status (see
  §1's "must be read promptly" note) — assert a second read after the
  collect window returns `None`/absent gracefully rather than raising.
- Real scope placed under `hermes-workers.slice` structurally surviving a
  simulated "gateway restart": spawn the scope from a throwaway Python
  process (standing in for the gateway) with a real PID and unit name
  persisted, kill *that* throwaway process (not `systemctl restart
  hermes-gateway`, matching this card's own guardrail against restarting
  the real service in any test/dev context), then assert the scope's PID
  is still alive and still queryable by unit name from an unrelated
  process. This directly exercises the parentage-independent claims in §1/§2
  without touching the real gateway unit.

**Gateway-restart survival assertion strategy** (the property this whole
spec exists to deliver): do NOT restart the real `hermes-gateway.service` in
any automated test (repo-wide guardrail, and this card's own guardrail).
Instead:

1. The unit-level tests above assert the *mechanism* (scope structurally
   outside the killer's cgroup, unit-name-based status query working from a
   cold/unrelated process) in isolation.
2. The re-adoption regression test in the unit-test list above asserts the
   *dispatcher-side* contract (host-prefix match, not full claimer id) that
   makes re-adoption correct once the mechanism holds.
3. Together these two prove restart-survival compositionally without ever
   invoking `systemctl restart` on a real gateway unit in CI — matching how
   the evidence-gathering card itself was scoped (structural proof, not a
   destructive live test) and this spec card's own explicit guardrail.
4. A manual/staged verification step (documented in the eventual
   implementation card, not this spec, and not run against the shared
   production gateway) — spin up a throwaway `hermes-gateway.service`-alike
   unit in a disposable VM/container, dispatch a real kanban task, restart
   that throwaway unit, and confirm the task's run continues and completes
   — is the appropriate place for an end-to-end live check, explicitly
   deferred out of the automated suite per repo policy on destructive
   systemctl operations.

## Platform impact

Linux systemd-specific feature, strictly opt-in via `kanban.worker_launcher`
(default `[]`). Windows and macOS are unaffected because the default value
produces byte-identical behavior to current `Popen`-based spawning on every
platform; no platform-conditional branch is introduced in core beyond the
existing `_IS_WINDOWS` guard already present in `_default_spawn` for
`creationflags=subprocess.CREATE_NO_WINDOW`. Non-systemd Linux hosts get the
same no-op default.
