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

const PRIORITY_DOT: Record<PriorityLevel['id'], string> = {
  critical: 'bg-red-500',
  high: 'bg-amber-500',
  normal: 'bg-(--ui-text-tertiary)',
  low: 'bg-(--ui-text-quaternary)',
  custom: 'bg-(--ui-text-quaternary)'
}

const PRIORITY_TEXT: Record<PriorityLevel['id'], string> = {
  critical: 'text-red-600 dark:text-red-400',
  high: 'text-amber-700 dark:text-amber-400',
  normal: 'text-(--ui-text-secondary)',
  low: 'text-(--ui-text-tertiary)',
  custom: 'text-(--ui-text-tertiary)'
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

function priorityChipCopy(level: PriorityLevel, k: ReturnType<typeof useKanban>): string {
  return level.id === 'custom' ? `P${level.value}` : priorityCopy(level, k)
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
            'inline-flex shrink-0 items-center gap-1 rounded-[4px] border border-transparent bg-transparent px-1 py-0.5 text-[0.625rem] font-medium transition-colors hover:bg-(--chrome-action-hover) focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-(--dt-composer-ring) data-[state=open]:bg-(--ui-control-active-background)',
            PRIORITY_TEXT[current.id]
          )}
          draggable={false}
          onDragStart={event => event.stopPropagation()}
          title={priorityCopy(current, k)}
          type="button"
        >
          <span aria-hidden className={cn('size-1.5 rounded-full', PRIORITY_DOT[current.id])} />
          {priorityChipCopy(current, k)}
          <Codicon className="opacity-60" name="chevron-down" size="0.6rem" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-36">
        {PRIORITY_LEVELS.map(level => (
          <DropdownMenuItem key={level.id} onSelect={() => onChange(level.value)}>
            <span className={cn('size-1.5 rounded-full', PRIORITY_DOT[level.id])} />
            {priorityCopy(level, k)}
            {current.value === level.value && <Codicon className="ml-auto" name="check" size="0.75rem" />}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
