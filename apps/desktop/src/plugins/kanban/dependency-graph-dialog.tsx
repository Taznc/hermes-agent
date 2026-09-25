/**
 * The dependency-graph overlay and the focus-bar controls that open it.
 *
 * `DependencyGraphDialog` draws the focused card's FULL chain (see
 * `dependency-graph-layout.ts`) as absolutely-positioned HTML nodes over an
 * SVG edge layer. Arrows run parent→child, left→right: a blocker points at
 * the card it blocks. Clicking a node re-centres the graph on it; the small
 * "open card" control hands the key back to the board's drawer.
 *
 * `FocusDepthControls` is the row of controls the board's focus hint bar
 * shows while a trace is live — the direct/chain toggle and the graph button.
 * It lives here rather than in board.tsx to keep that file from growing.
 */

import {
  Button,
  cn,
  Codicon,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  SegmentedControl,
  Tip
} from '@hermes/plugin-sdk'
import { useMemo } from 'react'

import {
  canvasSize,
  edgePath,
  type GraphLayout,
  layoutChain,
  NODE_H,
  NODE_W,
  nodePosition
} from './dependency-graph-layout'
import { type DependencyGraph, isGating } from './deps'
import type { KanbanTask } from './types'
import { Avatar, columnLabel, shortId, useKanban } from './ui'

export type FocusDepth = 'chain' | 'direct'

export function FocusDepthControls({
  depth,
  onDepth,
  onShowGraph
}: {
  depth: FocusDepth
  onDepth: (depth: FocusDepth) => void
  onShowGraph: () => void
}) {
  const k = useKanban()

  return (
    <div className="flex shrink-0 items-center gap-0.5">
      <SegmentedControl
        onChange={onDepth}
        options={[
          { id: 'direct', label: k.depFocusDirect },
          { id: 'chain', label: k.depFocusChain }
        ]}
        value={depth}
      />
      <Button onClick={onShowGraph} size="xs" variant="ghost">
        <Codicon name="type-hierarchy" size="0.75rem" />
        {k.depShowGraph}
      </Button>
    </div>
  )
}

const ARROW_ID = 'kanban-dep-arrow'
const ARROW_DONE_ID = 'kanban-dep-arrow-done'

function GraphNodeBox({
  focused,
  nodeKey,
  onOpenCard,
  onRecentre,
  task,
  x,
  y
}: {
  focused: boolean
  /** The node's cardKey (board-qualified in All Boards mode), not the bare id. */
  nodeKey: string
  onOpenCard: () => void
  onRecentre: () => void
  task: KanbanTask
  x: number
  y: number
}) {
  const k = useKanban()

  return (
    <div
      className={cn(
        'group absolute flex flex-col justify-center gap-1 rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-bg-elevated) px-2.5 py-2 text-left transition-[box-shadow,background-color] hover:bg-primary/[0.06]',
        focused && 'ring-2 ring-(--ui-stroke-primary)'
      )}
      data-node-key={nodeKey}
      style={{ left: x, top: y, width: NODE_W, height: NODE_H }}
    >
      <button
        aria-current={focused || undefined}
        className="absolute inset-0 cursor-pointer rounded-md focus-visible:outline-2 focus-visible:outline-(--ui-stroke-primary)"
        onClick={onRecentre}
        type="button"
      >
        <span className="sr-only">{task.title || task.id}</span>
      </button>
      <span className="pointer-events-none truncate pr-5 text-[0.75rem] font-medium leading-snug text-foreground">
        {task.title || task.id}
      </span>
      <span className="pointer-events-none flex min-w-0 items-center gap-1.5 text-[0.625rem] text-(--ui-text-tertiary)">
        <span className="font-mono text-(--ui-text-quaternary)">{shortId(task.id)}</span>
        <span className="truncate">{columnLabel(k, task.status)}</span>
        {task.assignee && (
          <span className="ml-auto flex shrink-0 items-center gap-1">
            <Avatar name={task.assignee} size="0.75rem" />
            <span className="max-w-[5rem] truncate">{task.assignee}</span>
          </span>
        )}
      </span>
      <Tip label={k.depGraphOpenCard}>
        <button
          aria-label={k.depGraphOpenCard}
          className="absolute top-1 right-1 grid size-5 place-items-center rounded text-(--ui-text-quaternary) opacity-0 transition-opacity group-hover:opacity-100 hover:bg-(--chrome-action-hover) hover:text-foreground focus-visible:opacity-100"
          onClick={event => {
            event.stopPropagation()
            onOpenCard()
          }}
          type="button"
        >
          <Codicon name="link-external" size="0.7rem" />
        </button>
      </Tip>
    </div>
  )
}

function GraphCanvas({
  focusedKey,
  index,
  layout,
  onOpenCard,
  onRecentre
}: {
  focusedKey: string
  index: Map<string, KanbanTask>
  layout: GraphLayout
  onOpenCard: (key: string) => void
  onRecentre: (key: string) => void
}) {
  const { height, width } = canvasSize(layout)
  const positions = useMemo(() => new Map(layout.nodes.map(node => [node.key, nodePosition(node)])), [layout])

  return (
    <div className="relative shrink-0" style={{ height, width }}>
      <svg aria-hidden className="absolute inset-0 overflow-visible" data-edge-layer height={height} width={width}>
        <defs>
          <marker id={ARROW_ID} markerHeight="6" markerWidth="6" orient="auto-start-reverse" refX="5" refY="3" viewBox="0 0 6 6">
            <path d="M 0 0 L 6 3 L 0 6 z" fill="var(--ui-stroke-primary)" />
          </marker>
          <marker id={ARROW_DONE_ID} markerHeight="6" markerWidth="6" orient="auto-start-reverse" refX="5" refY="3" viewBox="0 0 6 6">
            <path d="M 0 0 L 6 3 L 0 6 z" fill="var(--ui-stroke-secondary)" />
          </marker>
        </defs>
        {layout.edges.map(([parent, child]) => {
          const from = positions.get(parent)
          const to = positions.get(child)

          if (!from || !to) {
            return null
          }

          // A satisfied blocker (done/archived/wishlist) no longer gates: draw
          // it muted so the eye lands on the arrows that still hold cards up.
          const parentTask = index.get(parent)
          const gating = !parentTask || isGating(parentTask.status)

          return (
            <path
              className={cn(gating ? 'stroke-(--ui-stroke-primary)' : 'stroke-(--ui-stroke-secondary) opacity-50')}
              d={edgePath(from, to)}
              data-edge={`${parent}->${child}`}
              data-gating={gating}
              fill="none"
              key={`${parent}\u0001${child}`}
              markerEnd={`url(#${gating ? ARROW_ID : ARROW_DONE_ID})`}
              strokeWidth={gating ? 1.5 : 1}
            />
          )
        })}
      </svg>
      {layout.nodes.map(node => {
        const task = index.get(node.key)
        const at = positions.get(node.key)

        if (!task || !at) {
          return null
        }

        return (
          <GraphNodeBox
            focused={node.key === focusedKey}
            key={node.key}
            nodeKey={node.key}
            onOpenCard={() => onOpenCard(node.key)}
            onRecentre={() => onRecentre(node.key)}
            task={task}
            x={at.x}
            y={at.y}
          />
        )
      })}
    </div>
  )
}

export function DependencyGraphDialog({
  focusedKey,
  graph,
  hasEdges,
  index,
  onClose,
  onOpenCard,
  onRecentre
}: {
  /** The card at rank 0; `null` closes the dialog. */
  focusedKey: null | string
  graph: DependencyGraph
  hasEdges: boolean
  index: Map<string, KanbanTask>
  onClose: () => void
  onOpenCard: (key: string) => void
  onRecentre: (key: string) => void
}) {
  const k = useKanban()
  const layout = useMemo(() => (focusedKey ? layoutChain(graph, focusedKey) : null), [graph, focusedKey])
  const focusedTask = focusedKey ? index.get(focusedKey) : undefined
  const drawable = Boolean(hasEdges && layout && layout.edges.length > 0)

  return (
    <Dialog onOpenChange={open => !open && onClose()} open={focusedKey !== null}>
      <DialogContent bodyClassName="flex min-h-0 flex-col gap-3 p-4" className="w-[min(72rem,94vw)] max-w-none">
        <DialogHeader>
          <DialogTitle>{k.depGraphTitle}</DialogTitle>
          <DialogDescription>
            {focusedTask ? focusedTask.title || focusedTask.id : ''}
            <span className="mt-1 block text-(--ui-text-quaternary)">{k.depGraphLegend}</span>
          </DialogDescription>
        </DialogHeader>
        {drawable && layout && focusedKey ? (
          <div className="min-h-0 overflow-auto rounded-md bg-(--ui-bg-quinary)">
            <GraphCanvas
              focusedKey={focusedKey}
              index={index}
              layout={layout}
              onOpenCard={key => {
                onClose()
                onOpenCard(key)
              }}
              onRecentre={onRecentre}
            />
          </div>
        ) : (
          <p className="py-6 text-center text-xs text-(--ui-text-tertiary)">{k.depGraphEmpty}</p>
        )}
      </DialogContent>
    </Dialog>
  )
}
