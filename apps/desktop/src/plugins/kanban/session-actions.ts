import type { BoardMeta, KanbanRun, KanbanTaskFull } from './types'

export interface ActionSpec {
  id: 'explain' | 'failure' | 'rough' | 'scope' | 'unblock'
  label: string
  intent: string
  predicate?: (task: KanbanTaskFull, runs: KanbanRun[]) => boolean
}

export interface HermesActionContext {
  board: BoardMeta
  commentsCount: number
  runs: KanbanRun[]
  task: KanbanTaskFull
}

const EXPLAIN: ActionSpec = {
  id: 'explain',
  label: 'Explain this card',
  intent: 'Give me a read-only orientation: explain what this card is, why it exists, and its current state. Do not modify it.'
}

const FAILURE: ActionSpec = {
  id: 'failure',
  label: 'Investigate the failure',
  intent:
    'Inspect the latest run log and card events, diagnose the failure, and recommend a concrete recovery. Do not re-run anything blindly.',
  predicate: (task, runs) => (task.consecutive_failures ?? 0) > 0 || runs.at(-1)?.status === 'errored'
}

export const STATUS_ACTIONS: Record<string, ActionSpec[]> = {
  '*': [EXPLAIN],
  blocked: [
    {
      id: 'unblock',
      label: 'Help me unblock it',
      intent: 'Inspect the block reason and comments, then propose a concrete way to unblock this card. Do not change the card.'
    }
  ],
  idea: [
    {
      id: 'rough',
      label: 'Rough this out',
      intent:
        'Draft a worker-ready Roadmap body following kanban-card-workflow §2c. Post the draft in chat for my sign-off; do not refine the card unprompted.'
    }
  ],
  roadmap: [
    {
      id: 'scope',
      label: 'Scope this',
      intent:
        'Identify the likely files and seams, decide whether this is one shippable outcome or several, and check upstream prior art before spawning any work.'
    }
  ]
}

const PREDICATE_ACTIONS: ActionSpec[] = [FAILURE]

export function getHermesActions(task: KanbanTaskFull, runs: KanbanRun[]): ActionSpec[] {
  return [...STATUS_ACTIONS['*'], ...(STATUS_ACTIONS[task.status] ?? []), ...PREDICATE_ACTIONS].filter(
    action => !action.predicate || action.predicate(task, runs)
  )
}

export function resolveActionCwd(task: KanbanTaskFull, board: BoardMeta): string | undefined {
  return task.workspace_path?.trim() || board.default_workdir?.trim() || undefined
}

export function buildActionDraft(action: ActionSpec, context: HermesActionContext): string {
  const { board, commentsCount, runs, task } = context
  const latestRun = runs.at(-1)
  const project = [board.project_name, board.project_id].filter(Boolean).join(' / ') || 'unscoped project'

  const runContext = latestRun
    ? `Latest loaded run: ${latestRun.id} (${latestRun.status}).`
    : 'No runs are currently loaded.'

  return [
    `Work with Kanban card ${task.id} on board ${board.slug}.`,
    `Board: ${board.name || board.slug}. Project: ${project}. Card status: ${task.status}. Loaded comments: ${commentsCount}.`,
    runContext,
    `Start by calling kanban_show for the exact card id ${task.id} on board ${board.slug}, then use the already stated intent below.`,
    '',
    action.intent
  ].join('\n')
}

export function openHermesAction(
  action: ActionSpec,
  context: HermesActionContext,
  open: (options: { cwd: string; draft: string; openTab: true }) => void
): boolean {
  const cwd = resolveActionCwd(context.task, context.board)

  if (!cwd || !context.board.slug.trim()) {
    return false
  }

  open({ cwd, draft: buildActionDraft(action, context), openTab: true })

  return true
}
