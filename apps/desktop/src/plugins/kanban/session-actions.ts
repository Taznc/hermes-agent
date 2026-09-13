import type { BoardMeta, KanbanRun, KanbanTaskFull } from './types'

export interface ActionSpec {
  id: 'explain' | 'failure' | 'review' | 'rough' | 'scope' | 'unblock'
  label: string
  intent: string
  predicate?: (task: KanbanTaskFull, runs: KanbanRun[]) => boolean
}

export type HermesActionLabels = Record<ActionSpec['id'], string>

export interface HermesActionContext {
  board: BoardMeta
  commentsCount: number
  runs: KanbanRun[]
  task: KanbanTaskFull
}

type ActionDefinition = Omit<ActionSpec, 'label'>

const EXPLAIN: ActionDefinition = {
  id: 'explain',
  intent: 'Give me a read-only orientation: explain what this card is, why it exists, and its current state. Do not modify it.'
}

const FAILURE: ActionDefinition = {
  id: 'failure',
  intent:
    'Inspect the latest run log and card events, diagnose the failure, and recommend a concrete recovery. Do not re-run anything blindly.',
  predicate: (task, runs) => (task.consecutive_failures ?? 0) > 0 || runs.at(-1)?.status === 'errored'
}

const REVIEW: ActionDefinition = {
  id: 'review',
  intent: "Inspect the branch and diff, then verify the work against the card's acceptance criteria. Do not modify the card."
}

export const STATUS_ACTIONS: Record<string, ActionDefinition[]> = {
  '*': [EXPLAIN],
  blocked: [
    {
      id: 'unblock',
      intent: 'Inspect the block reason and comments, then propose a concrete way to unblock this card. Do not change the card.'
    }
  ],
  idea: [
    {
      id: 'rough',
      intent:
        'Draft a worker-ready Roadmap body following kanban-card-workflow §2c. Post the draft in chat for my sign-off; do not refine the card unprompted.'
    }
  ],
  roadmap: [
    {
      id: 'scope',
      intent:
        'Identify the likely files and seams, decide whether this is one shippable outcome or several, and check upstream prior art before spawning any work.'
    }
  ],
  review: [REVIEW],
  done: [REVIEW]
}

const PREDICATE_ACTIONS: ActionDefinition[] = [FAILURE]

export function getHermesActions(task: KanbanTaskFull, runs: KanbanRun[], labels: HermesActionLabels): ActionSpec[] {
  return [...STATUS_ACTIONS['*'], ...(STATUS_ACTIONS[task.status] ?? []), ...PREDICATE_ACTIONS]
    .filter(action => !action.predicate || action.predicate(task, runs))
    .map(action => ({ ...action, label: labels[action.id] }))
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
  open: (options: { cwd?: string; draft: string; openTab: true }) => void
): boolean {
  const cwd = resolveActionCwd(context.task, context.board)

  if (!context.board.slug.trim()) {
    return false
  }

  open({ cwd, draft: buildActionDraft(action, context), openTab: true })

  return true
}
