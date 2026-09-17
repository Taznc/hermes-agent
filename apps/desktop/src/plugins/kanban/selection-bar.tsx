/**
 * Floating bulk-actions bar, shown while cards are ⌘-selected. Deliberately
 * leaner than the dashboard's always-on toolbar: move / assign / archive /
 * delete cover the real fleet chores (requeue a batch, archive a sweep of
 * done, reassign after a profile change) via POST /tasks/bulk, which applies
 * per-id and reports partial failures — failed cards stay selected.
 */

import {
  Button,
  Codicon,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  host,
  Tip,
  useMutation,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { useState } from 'react'

import { bulkTasks, deleteTask, fetchProfiles, PROFILES_KEY } from './api'
import { cardKey, parseCardKey } from './deps'
import { columnMeta, isRoadmapLane, type KanbanTask, laneDropAllowed } from './types'
import { Avatar, columnLabel, errText, isLockedTarget, useKanban } from './ui'

export function SelectionBar({
  columns,
  index,
  onClear,
  onDone,
  selected
}: {
  columns: string[]
  /** cardKey→task (see `indexBoard`); `selected` holds cardKeys. */
  index: Map<string, KanbanTask>
  onClear: () => void
  onDone: (failed: string[]) => void
  selected: ReadonlySet<string>
}) {
  const k = useKanban()
  const qc = useQueryClient()
  const { data: roster } = useQuery({ queryKey: PROFILES_KEY, queryFn: fetchProfiles, staleTime: 60_000 })

  const finish = (failed: Array<{ error?: string; key: string }>) => {
    void qc.invalidateQueries({ queryKey: ['kanban', 'board'] })

    if (failed.length > 0) {
      host.notify({
        kind: 'warning',
        message: k.bulkFailed(failed.length, selected.size, failed[0].error ?? k.refused)
      })
    }

    onDone(failed.map(f => f.key))
  }

  // Group the selection by its OWN board (populated only in All Boards mode;
  // single-board mode's tasks carry no `board`, so everything lands in one
  // group under `undefined` — byte-identical to the pre-existing single call).
  // `/tasks/bulk` is a single-board endpoint, so a selection spanning boards
  // fans out to one call per board rather than sending a foreign id.
  //
  // Selection holds `cardKey`s; the wire wants bare ids, so each group carries
  // both and the per-id results are mapped back to their key by the pair.
  const byBoard = (keys: string[]): Map<string | undefined, string[]> => {
    const groups = new Map<string | undefined, string[]>()

    for (const key of keys) {
      const taskBoard = index.get(key)?.board ?? parseCardKey(key).board ?? undefined
      const bucket = groups.get(taskBoard)

      bucket ? bucket.push(key) : groups.set(taskBoard, [key])
    }

    return groups
  }

  const bulk = useMutation({
    mutationFn: async (patch: Record<string, unknown>) => {
      const groups = byBoard([...selected])

      const results = await Promise.all(
        [...groups.entries()].map(async ([taskBoard, keys]) => {
          const { results } = await bulkTasks(
            keys.map(key => parseCardKey(key).id),
            patch,
            taskBoard
          )

          // The backend answers per bare id; re-attach the board so a failure
          // is reported against the exact card the user selected.
          return results.map(row => ({ ...row, key: cardKey(row.id, taskBoard) }))
        })
      )

      return { results: results.flat() }
    },
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSuccess: data => finish(data.results.filter(r => !r.ok))
  })

  // No bulk-delete on the backend — fan out per id, same partial-failure story.
  const bulkDelete = useMutation({
    mutationFn: async () => {
      const keys = [...selected]

      const settled = await Promise.allSettled(
        keys.map(key => {
          const { board: keyBoard, id } = parseCardKey(key)

          return deleteTask(id, index.get(key)?.board ?? keyBoard ?? undefined)
        })
      )

      return keys.flatMap((key, i) => {
        const result = settled[i]

        return result.status === 'rejected' ? [{ error: errText(result.reason), key }] : []
      })
    },
    onSuccess: finish
  })

  const busy = bulk.isPending || bulkDelete.isPending
  // One menu at a time — controlled, so a click on the second trigger can
  // never race Radix's dismiss layer into two open menus.
  const [menu, setMenu] = useState<'assign' | 'move' | null>(null)

  return (
    <div className="pointer-events-none absolute inset-x-0 bottom-4 z-10 flex justify-center px-4">
      {/* Flat overlay: stroke + elevated surface do the separating, no shadow. */}
      <div className="pointer-events-auto flex items-center gap-1 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-elevated) py-1 pr-1 pl-3">
        <span className="mr-1 text-xs tabular-nums text-(--ui-text-secondary)">{k.nSelected(selected.size)}</span>

        <DropdownMenu onOpenChange={open => setMenu(open ? 'move' : null)} open={menu === 'move'}>
          <DropdownMenuTrigger asChild>
            <Button disabled={busy} size="xs" variant="ghost">
              {k.moveToShort}
              <Codicon name="chevron-down" size="0.7rem" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="center">
            {columns
              // Bulk selection can span mixed statuses. A target is safe only
              // when the shared transition predicate accepts it for EVERY
              // selected card. Wishlist exits stay per-card because Ready
              // requires a confirmation and the bulk endpoint has no dialog
              // contract for partially accepted lane spawns.
              .filter(
                name =>
                  !isLockedTarget(name) &&
                  [...selected].every(key => {
                    const task = index.get(key)

                    return Boolean(task && !isRoadmapLane(task.status) && laneDropAllowed(task.status, name))
                  })
              )
              .map(name => (
                <DropdownMenuItem key={name} onSelect={() => bulk.mutate({ status: name })}>
                  <span className="size-2 rounded-full" style={{ backgroundColor: columnMeta(name).tone }} />
                  {columnLabel(k, name)}
                </DropdownMenuItem>
              ))}
          </DropdownMenuContent>
        </DropdownMenu>

        <DropdownMenu onOpenChange={open => setMenu(open ? 'assign' : null)} open={menu === 'assign'}>
          <DropdownMenuTrigger asChild>
            <Button disabled={busy} size="xs" variant="ghost">
              {k.assign}
              <Codicon name="chevron-down" size="0.7rem" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="center">
            {(roster?.profiles ?? []).map(profile => (
              <DropdownMenuItem
                key={profile.name}
                onSelect={() => bulk.mutate({ assignee: profile.name, reclaim_first: true })}
              >
                <Avatar name={profile.name} size="0.875rem" />
                {profile.name}
              </DropdownMenuItem>
            ))}
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => bulk.mutate({ assignee: '', reclaim_first: true })}>
              {k.unassignAction}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>

        <Button disabled={busy} onClick={() => bulk.mutate({ archive: true })} size="xs" variant="ghost">
          {k.archive}
        </Button>
        <Button
          className="text-destructive"
          disabled={busy}
          onClick={() => bulkDelete.mutate()}
          size="xs"
          variant="ghost"
        >
          {k.delete}
        </Button>

        <Tip label={k.clearSelection}>
          <Button aria-label={k.clearSelection} onClick={onClear} size="icon-xs" variant="ghost">
            <Codicon name="close" size="0.8rem" />
          </Button>
        </Tip>
      </div>
    </div>
  )
}
