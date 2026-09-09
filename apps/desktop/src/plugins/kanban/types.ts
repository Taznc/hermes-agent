/** The slice of the kanban REST contract the board renders. The backend
 *  (`plugins/kanban/dashboard/plugin_api.py`) returns much more per task; we
 *  type only what the UI reads so a schema addition never breaks the build. */

/** One card. `status` is the column id (see COLUMN_META). */
export interface KanbanTask {
  id: string
  title: string
  body?: null | string
  status: string
  assignee?: null | string
  priority?: number
  tenant?: null | string
  created_at?: number
  latest_summary?: null | string
  comment_count?: number
  link_counts?: { parents: number; children: number }
  /** N-of-M child completion, or null when the task has no children. */
  progress?: null | { done: number; total: number }
  /** Compact diagnostics rollup — present only when a card has warnings. */
  warnings?: null | { count: number; highest_severity?: null | string }
  /** id of the first image attachment (content_type starting `image/`), if
   *  any — drives the card's thumbnail indicator. Fetch bytes via
   *  `fetchAttachmentDataUrl(id)`. */
  image_attachment_id?: null | number | string
  /** Worker liveness (present on running cards) — drives the arc + run clock. */
  started_at?: null | number
  worker_pid?: null | number
  last_heartbeat_at?: null | number
  /** Present only in the consolidated All Boards view (GET /board/all) — the
   *  owning board's slug + display name, so a merged card can be attributed
   *  and every mutation can be routed back to ITS board, never the sentinel. */
  board?: null | string
  board_name?: null | string
  /** Unblock-loop breaker state, on the CARD (not just the detail endpoint) so
   *  the board can gate a drag without opening the drawer first.
   *  `block_kind` is one of VALID_BLOCK_KINDS or null for a legacy/un-typed
   *  block; `block_recurrences` counts same-cause re-blocks and, at the
   *  backend's BLOCK_RECURRENCE_LIMIT, is what parked the card in `triage`. */
  block_kind?: null | string
  block_recurrences?: number
}

export interface KanbanColumn {
  name: string
  tasks: KanbanTask[]
}

export interface KanbanBoard {
  columns: KanbanColumn[]
  tenants: string[]
  assignees: string[]
  /** Every dependency edge on the board — the parent BLOCKS the child.
   *  Absent on older backends, so always guard. Two shapes, one per endpoint:
   *  single-board `GET /board` sends `[parent_id, child_id]` tuples, while the
   *  consolidated `GET /board/all` sends `{board, parent, child}` objects so
   *  each edge carries the board its two ids belong to (ids are only unique
   *  per board). `buildGraph` in deps.ts normalizes both. */
  link_edges?: Array<[string, string] | BoardAllLinkEdge>
  latest_event_id: number
  now: number
  /** Present only when this payload came from the consolidated All Boards
   *  view (`fetchAllBoards`, sentinel `$boardSlug === ALL_BOARDS`) — the
   *  per-board roster (for the filter chips + card badges), never present
   *  on a single-board `GET /board` response. */
  boards?: BoardAllInfo[]
  /** Per-board `latest_event_id`, for seeding a future multi-board events
   *  socket subscription without a gap or a replay (follow-on card). */
  cursors?: Record<string, number>
  /** Boards that failed to load in this consolidated fetch — the view stays
   *  up for every board that succeeded; render this as a non-blocking notice
   *  naming the failed boards rather than blanking the page. */
  errors?: BoardAllError[]
}

/** One board's roster entry in the consolidated All Boards view — display
 *  chrome (name/color/icon) plus how many live cards it contributed. */
export interface BoardAllInfo {
  slug: string
  name: string
  color: string
  icon: string
  project_name?: null | string
  task_count: number
}

/** One board that failed to load in `GET /board/all` — reported instead of
 *  failing the whole consolidated view. */
export interface BoardAllError {
  board: string
  detail: string
}

/** One dependency edge from `GET /board/all`. Both ids belong to `board` —
 *  links only ever exist within one board's DB — which is what lets the
 *  merged client index key the chain on the (board, id) pair. */
export interface BoardAllLinkEdge {
  board: string
  parent: string
  child: string
}

/** A dependency resolved against the board cache for display: the linked
 *  task's own identity, so a row can be read without opening it. `missing`
 *  marks an id the board no longer has (deleted, or filtered out by the
 *  current tenant/archive view). */
export interface ResolvedLink {
  id: string
  title: string
  status: string
  assignee?: null | string
  missing: boolean
}

/** A structured recovery action attached to a diagnostic. */
export interface DiagnosticAction {
  kind: string
  label: string
  payload?: Record<string, unknown>
  suggested?: boolean
}

/** One active distress signal on a task (kanban_diagnostics.Diagnostic). */
export interface Diagnostic {
  kind: string
  severity: 'critical' | 'error' | 'warning' | 'info'
  title: string
  detail: string
  actions: DiagnosticAction[]
  count: number
  last_seen_at: number
  data: Record<string, unknown>
}

export interface KanbanRun {
  id: number | string
  profile?: null | string
  status: string
  outcome?: null | string
  summary?: null | string
  error?: null | string
  metadata?: null | Record<string, unknown> | string
  worker_pid?: null | number
  started_at?: null | number
  ended_at?: null | number
  model?: null | string
  provider?: null | string
  reasoning_effort?: null | string
  model_source?: null | 'card_override' | 'profile_default' | 'routing'
  session_id?: null | string
  input_tokens?: null | number
  output_tokens?: null | number
  cache_read_tokens?: null | number
  reasoning_tokens?: null | number
  api_calls?: null | number
  tool_calls?: null | number
  estimated_cost_usd?: null | number
}

/** A structured multiple-choice answer, persisted alongside a comment's plain
 *  `body` (see docs/design/blocked-callout-multiple-choice-spec.md).
 *  `question_event_id` binds the answer to the specific `blocked` /
 *  `block_loop_detected` event it answers, so re-blocking with a new
 *  question never gets confused with an old answer. */
export interface ChoiceResponse {
  key: string
  label: string
  question_event_id: number
}

export interface KanbanComment {
  id: number | string
  author: string
  body: string
  created_at: number
  /** Present only when this comment was submitted by clicking a rendered
   *  choice option; absent/null for every free-text comment (including all
   *  comments written before this feature existed). */
  choice?: null | ChoiceResponse
}

export interface KanbanEvent {
  id: number
  kind: string
  payload: unknown
  created_at: number
}

export interface KanbanAttachment {
  id: number | string
  filename: string
  content_type?: null | string
  size?: null | number
}

/** A file (typically a pasted image) uploaded before the owning task exists —
 *  from the "new task" dialog's paste-to-upload flow. Promoted into a real
 *  KanbanAttachment via `pending_attachment_tokens` on task create; the
 *  `token` is the only client-facing handle until then. */
export interface StagedAttachment {
  token: string
  filename: string
  content_type?: null | string
  size: number
  created_at?: number
}

/** Fields present only on the detail endpoint (beyond the card's KanbanTask).
 *  `started_at`/`worker_pid`/`last_heartbeat_at` are inherited — they live on
 *  KanbanTask now that the board's liveness arc reads them. */
export interface KanbanTaskFull extends KanbanTask {
  result?: null | string
  created_by?: null | string
  /** Per-task worker overrides. Null/absent = the assigned profile's own
   *  model, provider, and reasoning effort decide. */
  model_override?: null | string
  provider_override?: null | string
  reasoning_effort?: null | string
  completed_at?: null | number
  last_failure_error?: null | string
  workspace_kind?: null | string
  workspace_path?: null | string
  branch_name?: null | string
  consecutive_failures?: number
  diagnostics?: Diagnostic[]
}

/** GET /tasks/:id — the task plus its related collections, which are SIBLINGS
 *  of `task`, not nested inside it. */
export interface KanbanTaskDetail {
  task: KanbanTaskFull
  comments: KanbanComment[]
  events: KanbanEvent[]
  /** Kanban backends before attachments landed (#35395, May 2026) omit this
   *  key and have no /tasks/{id}/attachments endpoints; absent/null hides the
   *  section instead of offering uploads the backend would 404 on. */
  attachments?: KanbanAttachment[] | null
  links: { parents: string[]; children: string[] }
  runs: KanbanRun[]
}

/** GET /boards — every board on disk + which one is the server's current. */
export interface BoardMeta {
  slug: string
  name?: null | string
  description?: null | string
  is_current?: boolean
  total?: number
  /** Board-level project directory new tasks inherit (empty = none). */
  default_workdir?: null | string
  /** Recommended workspace kind derived from default_workdir by the backend
   *  (`scratch` when unset, `worktree` in a git repo, else `dir`). */
  default_workspace_kind?: null | string
  /** First-class Project the board is scoped to (id) + resolved name. */
  project_id?: null | string
  project_name?: null | string
}

/** POST /boards/{slug}/export — the archive the backend wrote. */
export interface BoardExportResult {
  board: string
  archive: string
  size: number
}

/** POST /boards/import — the NEW board the archive landed as. */
export interface BoardImportResult {
  board: string
  name: string
  /** True when the archive's slug was taken and the import got a suffix. */
  renamed: boolean
  requested_board: string
  counts: Record<string, number>
  /** Human-readable notes (parked tasks, dropped attachments). */
  warnings: string[]
}

/** GET /projects — first-class Hermes projects available to scope a board. */
export interface KanbanProject {
  id: string
  slug: string
  name: string
  primary_path?: null | string
  icon?: null | string
  color?: null | string
}

/** POST /tasks/:id/estimate — rough auxiliary-model estimate (never dollars). */
export interface TaskEstimate {
  ok: boolean
  reason?: null | string
  est_tokens?: number
  complexity?: 'L' | 'M' | 'S' | null
  rationale?: null | string
  model?: null | string
}
export interface BoardsResponse {
  boards: BoardMeta[]
  current: string
}

/** GET /tasks/:id/log — the worker's stdout/stderr tail. */
export interface WorkerLog {
  exists: boolean
  size_bytes: number
  content: string
  truncated: boolean
}

/** GET /orchestration — dispatcher knobs from config.yaml + resolved values. */
export interface OrchestrationSettings {
  orchestrator_profile: string
  default_assignee: string
  auto_decompose: boolean
  resolved_orchestrator_profile: string
  resolved_default_assignee: string
}

/** GET /dispatch/status — the pause circuit plus the drain signal. */

/** A maintenance action queued to fire once the scope drains to 0 running.
 *  The trigger is server-side (the dispatcher tick), so this record advances
 *  whether or not this dashboard is open. */
export interface PostDrainAction {
  action_kind: string
  /** Allowlisted unit for `service_restart`; null for target-less kinds. */
  target: null | string
  requested_by: string
  requested_at: number
  expires_at: number
  /** `waiting` -> `firing` -> `succeeded | failed | expired | cancelled`. */
  state: 'cancelled' | 'expired' | 'failed' | 'firing' | 'succeeded' | 'waiting'
  /** Seconds until expiry, derived SERVER-side so the countdown shares the
   *  clock that will actually expire the record. Null when unbounded. */
  expires_in_seconds: null | number
  /** Observed failure detail — present only in the `failed` state. */
  error?: string
  /** Shared by every board armed in one aggregate request. */
  group_id?: string
  /** Aggregate scope only: how many boards carry this action. */
  board_count?: number
}

/** One entry the "after drain" selector may offer. Derived from the backend's
 *  own registry + allowlist, so the UI can never offer an action that 400s. */
export interface PostDrainActionOption {
  action_kind: string
  /** Allowlisted targets; empty for target-less kinds like `reboot`. */
  targets: string[]
}

export interface DispatchStatus {
  paused: boolean
  /** Raw circuit record: `reason` is `operator_paused` for a maintenance
   *  drain, or a fault code for a systemic circuit. Null when running or when
   *  this is an aggregate status. */
  state: null | {
    reason: string
    paused_at?: number
    paused_by?: string
    note?: null | string
    [key: string]: unknown
  }
  /** Workers still running in this scope — 0 means safe to restart. */
  running_count: number
  /** Server-rendered human status; null when running or aggregate. */
  message: null | string
  /** The action armed for this scope, or null. Absent on older backends. */
  post_drain?: null | PostDrainAction
  /** Actions this host accepts. Absent on older backends (selector hidden). */
  post_drain_actions?: PostDrainActionOption[]
  /** Aggregate-only fields returned for the explicit `boards=*` scope. */
  all_paused?: boolean
  board_count?: number
  paused_count?: number
  boards?: Array<DispatchStatus & { board: string }>
  errors?: Array<{ board: string; error: string }>
}

/** POST /dispatch/post-drain. Aggregate scope reports per-board outcomes. */
export interface PostDrainQueueResult {
  queued: boolean
  state?: PostDrainAction
  board_count?: number
  queued_count?: number
  group_id?: string
  results?: Array<{ board: string; state: PostDrainAction }>
  failures?: Array<{ board: string; error: string }>
}

/** DELETE /dispatch/post-drain. `cancelled: false` means nothing was waiting. */
export interface PostDrainCancelResult {
  cancelled: boolean
  state?: null | PostDrainAction
  board_count?: number
  cancelled_count?: number
  results?: Array<{ board: string; cancelled: boolean }>
  failures?: Array<{ board: string; error: string }>
}

/** POST /dispatch/pause. `paused: false` is a REFUSAL (a dispatch tick owns
 *  at least one target lock), delivered as a normal 200 — never treat it as success. */
export interface DispatchPauseResult {
  paused: boolean
  state: DispatchStatus['state']
  reason?: string
  board_count?: number
  paused_count?: number
  results?: Array<DispatchPauseResult & { board: string }>
  failures?: Array<{ board: string; error: string }>
}

/** POST /dispatch/resume. */
export interface DispatchResumeResult {
  resumed: boolean
  was_paused: boolean
  previous?: DispatchStatus['state']
  reason?: string
  board_count?: number
  resumed_count?: number
  results?: Array<DispatchResumeResult & { board: string }>
  failures?: Array<{ board: string; error: string }>
}

/** GET /profiles — the roster the decomposer routes across. */
export interface KanbanProfile {
  name: string
  is_default: boolean
  description: string
  description_auto: boolean
  /** The profile's own configured model/provider/depth — what a worker
   *  actually runs when the task carries no override. Empty = unset
   *  (provider defaults) or an older backend that doesn't report them. */
  model?: string
  provider?: string
  reasoning_effort?: string
}

/** Column presentation — codicon + tone only. Labels + help live in i18n
 *  (plugin bundles); see `columnLabel`/`columnHelp` in i18n.ts. Order follows
 *  the backend's BOARD_COLUMNS; anything the backend adds renders via the
 *  fallback. */
export const COLUMN_META: Record<string, { codicon: string; tone: string }> = {
  triage: { codicon: 'inbox', tone: 'var(--ui-text-tertiary)' },
  todo: { codicon: 'circle-outline', tone: 'var(--ui-text-secondary)' },
  scheduled: { codicon: 'watch', tone: '#a78bfa' },
  ready: { codicon: 'play-circle', tone: '#60a5fa' },
  running: { codicon: 'sync', tone: '#34d399' },
  blocked: { codicon: 'error', tone: '#f87171' },
  on_hold: { codicon: 'debug-pause', tone: '#94a3b8' },
  review: { codicon: 'eye', tone: '#fbbf24' },
  done: { codicon: 'pass', tone: 'var(--ui-text-tertiary)' },
  archived: { codicon: 'archive', tone: 'var(--ui-text-quaternary)' },
  // The wishlist lanes. Muted on purpose: they sit UPSTREAM of the
  // authorization line, so nothing in them is work in flight and they must
  // never read as loud as a live lane.
  idea: { codicon: 'lightbulb', tone: 'var(--ui-text-quaternary)' },
  roadmap: { codicon: 'map', tone: 'var(--ui-text-tertiary)' }
}

export const columnMeta = (name: string) =>
  COLUMN_META[name] ?? { codicon: 'circle-outline', tone: 'var(--ui-text-secondary)' }

/** The inert wishlist lanes, in render order (leftmost first) — they precede
 *  `triage` because they are upstream of the authorization line. The backend
 *  appends them to `BOARD_COLUMNS` instead (they trail the live lanes there),
 *  so the client reorders; see `orderLanes`. Mirrors
 *  `kanban_db.ROADMAP_LANE_STATUSES`. */
export const ROADMAP_LANES = ['idea', 'roadmap'] as const

export const isRoadmapLane = (name: string): boolean => name === 'idea' || name === 'roadmap'

/**
 * Allowed lane transitions, `from -> {to, ...}` — the client-side mirror of
 * `kanban_db.ROADMAP_LANE_TRANSITIONS`. One-directional out of the wishlist:
 * no live status may move INTO a lane (a card enters only at creation or via
 * idea capture), so an existing drag habit can never park real work here.
 */
export const ROADMAP_LANE_TRANSITIONS: Readonly<Record<string, ReadonlySet<string>>> = {
  idea: new Set(['roadmap', 'archived']),
  roadmap: new Set(['triage', 'ready', 'idea', 'archived'])
}

/** Where a `roadmap` card may be spawned into (`triage` is the default — the
 *  standing decision is to let auto-decompose re-specify it first). Mirrors
 *  `kanban_db.ROADMAP_SPAWN_TARGETS`. */
export const ROADMAP_SPAWN_TARGETS = ['triage', 'ready'] as const

/**
 * Whether a card in `from` may be moved to `to`, as far as the wishlist lanes
 * are concerned. THE single predicate behind every move affordance (lane drop,
 * card context menu, drawer status picker, bulk move bar) so they cannot drift
 * apart, and the client's optimistic move can never paint a transition the
 * backend will refuse with a 400.
 *
 * Anything not touching a lane is left alone (`true`) — lane rules are the
 * only thing this answers; `isLockedTarget` still owns the dispatcher-owned
 * columns.
 */
export const laneDropAllowed = (from: string, to: string): boolean => {
  if (isRoadmapLane(from)) {
    return ROADMAP_LANE_TRANSITIONS[from].has(to)
  }

  return !isRoadmapLane(to)
}

/** Lane columns first (`idea`, then `roadmap`), every other column keeping the
 *  backend's own left-to-right order. Pure and total: a board with no lane
 *  columns comes back untouched. */
export function orderLanes<T extends { name: string }>(columns: readonly T[]): T[] {
  const rank = (name: string) => {
    const index = (ROADMAP_LANES as readonly string[]).indexOf(name)

    return index === -1 ? ROADMAP_LANES.length : index
  }

  return [...columns].sort((a, b) => rank(a.name) - rank(b.name))
}

export const SEVERITY_TONE: Record<Diagnostic['severity'], string> = {
  critical: 'var(--destructive, #f87171)',
  error: 'var(--destructive, #f87171)',
  warning: '#fbbf24',
  info: 'var(--ui-text-tertiary, #60a5fa)'
}

/** Run outcome → row tone. Every value resolves through COLUMN_META or
 *  SEVERITY_TONE, so a run row reads the same color the board would give the
 *  same state — no second palette to keep in sync. Unknown outcomes stay
 *  neutral rather than borrowing a meaning they don't have. */
const OUTCOME_TONE: Record<string, string> = {
  blocked: COLUMN_META.blocked.tone,
  changes_requested: COLUMN_META.review.tone,
  completed: COLUMN_META.running.tone,
  crashed: SEVERITY_TONE.error,
  failed: SEVERITY_TONE.error,
  gave_up: SEVERITY_TONE.error,
  review_requested: COLUMN_META.review.tone,
  timed_out: SEVERITY_TONE.error
}

export const outcomeTone = (outcome?: null | string): string =>
  OUTCOME_TONE[outcome ?? ''] ?? 'var(--ui-text-quaternary)'

/** Activity event kind → dot tone. Deliberately sparse: only kinds a human
 *  scans for get color. `heartbeat` is the highest-volume kind by an order of
 *  magnitude, so it is absent here and falls through to the quietest value —
 *  coloring it would turn the feed into noise. Failure kinds are tinted
 *  destructive precisely because they are what you scroll a long event feed
 *  looking for. */
const EVENT_TONE: Record<string, string> = {
  block_loop_detected: COLUMN_META.blocked.tone,
  blocked: COLUMN_META.blocked.tone,
  changes_requested: COLUMN_META.review.tone,
  claimed: COLUMN_META.ready.tone,
  commented: 'var(--ui-text-secondary)',
  completed: COLUMN_META.running.tone,
  crashed: SEVERITY_TONE.error,
  dependency_wait: COLUMN_META.todo.tone,
  gave_up: SEVERITY_TONE.error,
  held: COLUMN_META.on_hold.tone,
  interrupted: SEVERITY_TONE.warning,
  promoted: COLUMN_META.ready.tone,
  protocol_violation: SEVERITY_TONE.error,
  reclaimed: COLUMN_META.review.tone,
  respawn_guarded: SEVERITY_TONE.warning,
  review_no_verdict: COLUMN_META.review.tone,
  review_requested: COLUMN_META.review.tone,
  review_round_cap: SEVERITY_TONE.warning,
  scheduled: COLUMN_META.scheduled.tone,
  spawned: COLUMN_META.ready.tone,
  stale: SEVERITY_TONE.warning,
  timed_out: SEVERITY_TONE.error,
  unblocked: COLUMN_META.review.tone
}

export const eventTone = (kind: string): string => EVENT_TONE[kind] ?? 'var(--ui-text-quaternary)'
