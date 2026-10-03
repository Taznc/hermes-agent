/**
 * All Boards in upstream's board switcher: the dropdown item (anchored into
 * `board-switcher.tsx`'s menu) and the trigger label while it is selected.
 * Both render nothing / fall through unless the fork backend answered the
 * probe, so a backend without `fork-kanban` never offers the option.
 */

import { Codicon } from '@/components/ui/codicon'
import { DropdownMenuItem } from '@/components/ui/dropdown-menu'
import { ALL_BOARDS, enterAllBoards } from '@/fork/kanban/all-boards'
import { useForkBackend } from '@/fork/kanban/backend'
import { kanbanForkText, useKanban } from '@/fork/kanban/text'

function AllBoardsItem({ boards, slug }: { boards: ReadonlyArray<{ total?: number }>; slug: string }) {
  const k = useKanban()
  const backend = useForkBackend()

  if (backend !== true) {
    return null
  }

  const total = boards.reduce((sum, meta) => sum + (meta.total ?? 0), 0)

  return (
    <DropdownMenuItem data-all-boards onSelect={enterAllBoards} title={k.allBoardsTip}>
      <Codicon name="layers" size="0.8rem" />
      {k.allBoards}
      <span className="text-[0.625rem] tabular-nums text-(--ui-text-quaternary)">{total}</span>
      {slug === ALL_BOARDS && <Codicon className="ml-auto" name="check" size="0.8rem" />}
    </DropdownMenuItem>
  )
}

/** The `board-switcher.tsx` menu anchor. */
export const allBoardsItem = (slug: string, boards: ReadonlyArray<{ total?: number }>) => (
  <AllBoardsItem boards={boards} slug={slug} />
)

/** The `board-switcher.tsx` label anchor: the trigger text while All Boards
 *  is selected, else '' so upstream's own label wins. */
export const boardLabel = (slug: string): string => (slug === ALL_BOARDS ? kanbanForkText('allBoards') : '')
