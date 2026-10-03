/**
 * `host.fork.kanban` v1 — the ONLY door between upstream's bundled Kanban
 * plugin (which may import nothing but `@hermes/plugin-sdk`) and the fork's
 * focus mode + All Boards (ledger Part C, fork extension point). Every member
 * is a pure call-site target for one anchored line in upstream:
 *
 *  - `routeRest(r, $boardSlug)`        api.ts `bindApi` — wraps the REST door
 *                                      (All Boards fetch + per-board routing).
 *  - `frameCard(Card, props)`          board.tsx `Card` — the per-card frame
 *                                      (data-card-key, trace toggle, rings,
 *                                      dimming, lane fold, board badge).
 *  - `boardOverlay({...})`             board.tsx page — answer bar, arrows,
 *                                      Esc/click-off, fold, All Boards chips.
 *  - `allBoardsItem(slug, boards)`     board-switcher.tsx menu item.
 *  - `boardLabel(slug)`                board-switcher.tsx trigger label.
 *  - `dispatchControl()`               orchestration.tsx — pause/resume
 *                                      dispatch for the board or all boards.
 *
 * Plugins feature-detect: `host.fork?.kanban?.version >= 1`, falling back to
 * upstream behaviour when absent. With no `fork-kanban` backend on the
 * connection every member degrades to a no-op/pass-through.
 */

import { ALL_BOARDS, routeRest } from '@/fork/kanban/all-boards'
import { boardOverlay } from '@/fork/kanban/board-overlay'
import { frameCard } from '@/fork/kanban/card-frame'
import { dispatchControl } from '@/fork/kanban/dispatch-pause'
import { allBoardsItem, boardLabel } from '@/fork/kanban/switcher'

export const forkKanban = {
  version: 1 as const,
  ALL_BOARDS,
  allBoardsItem,
  boardLabel,
  boardOverlay,
  dispatchControl,
  frameCard,
  routeRest
}

export type ForkKanban = typeof forkKanban
