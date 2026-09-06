import {
  cn,
  Codicon,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger
} from '@hermes/plugin-sdk'

import { PRIORITY_LEVELS, priorityLevel, type PriorityLevel } from './priority'
import { useKanban } from './ui'

const PRIORITY_TONE: Record<PriorityLevel['id'], string> = {
  critical: 'border-red-500/35 bg-red-500/10 text-red-600 dark:text-red-400',
  high: 'border-amber-500/35 bg-amber-500/10 text-amber-700 dark:text-amber-400',
  normal: 'border-(--ui-stroke-secondary) bg-(--ui-bg-quinary) text-(--ui-text-secondary)',
  low: 'border-(--ui-stroke-secondary) bg-(--ui-bg-quinary) text-(--ui-text-tertiary)',
  custom: 'border-(--ui-stroke-secondary) bg-(--ui-bg-quinary) text-(--ui-text-tertiary)'
}

function priorityCopy(level: PriorityLevel, k: ReturnType<typeof useKanban>): string {
  switch (level.id) {
    case 'critical':
      return k.priorityCritical

    case 'high':
      return k.priorityHigh

    case 'normal':
      return k.priorityNormal

    case 'low':
      return k.priorityLow

    case 'custom':
      return k.priorityCustom(level.value)
  }
}

/** A support-facing severity picker over the numeric scheduler tiebreaker. */
export function PriorityPicker({
  onChange,
  priority
}: {
  onChange: (priority: number) => void
  priority?: number
}) {
  const k = useKanban()
  const current = priorityLevel(priority)

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          aria-label={priorityCopy(current, k)}
          className={cn(
            'inline-flex shrink-0 items-center gap-1 rounded border px-1.5 py-0.5 text-[0.625rem] font-medium transition-colors hover:bg-(--chrome-action-hover) focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-(--dt-composer-ring)',
            PRIORITY_TONE[current.id]
          )}
          draggable={false}
          onDragStart={event => event.stopPropagation()}
          type="button"
        >
          <Codicon name="arrow-up" size="0.65rem" />
          {priorityCopy(current, k)}
          <Codicon className="opacity-60" name="chevron-down" size="0.6rem" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-36">
        {PRIORITY_LEVELS.map(level => (
          <DropdownMenuItem key={level.id} onSelect={() => onChange(level.value)}>
            <span className={cn('size-1.5 rounded-full', PRIORITY_TONE[level.id])} />
            {priorityCopy(level, k)}
            {current.value === level.value && <Codicon className="ml-auto" name="check" size="0.75rem" />}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
