/**
 * Task drawer — the desktop port of the dashboard's task detail.
 *
 * FACADE. It owns the shell (status-colored header band, tab strip, the
 * queries and mutations) and delegates each tab's body to a `drawer_<topic>`
 * sibling:
 *   - `drawer_overview` — diagnostics, meta, description, deps, result
 *   - `drawer_activity` — event feed, runs, comments + composer
 *   - `drawer_log`      — worker log tail, attachments
 *   - `drawer_events`   — pure event/run text derivation (no React)
 *   - `drawer_cta`      — the call-to-action banner + choice questions
 *
 * Color: every tone here comes from `columnMeta(status)` / `SEVERITY_TONE`
 * and is applied through `wash()` — the drawer never picks a color itself, so
 * it stays consistent with the board by construction.
 */

import {
  Codicon,
  Dialog,
  DialogContent,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  ErrorState,
  host,
  Loader,
  useMutation,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { useEffect, useMemo, useState } from 'react'

import {
  $boardSlug,
  addComment,
  deleteTask,
  fetchLog,
  fetchTask,
  linkTasks,
  logKey,
  patchTask,
  reassignTask,
  reclaimTask,
  taskKey,
  unlinkTasks,
  uploadAttachment
} from './api'
import { ActivityRow, CommentsSection, RunsSection } from './drawer_activity'
import { CtaBanner } from './drawer_cta'
import { groupActivity } from './drawer_events'
import {
  AttachmentsSection,
  DEFAULT_LOG_TAIL_BYTES,
  ImagesSection,
  isImageAttachment,
  MAX_LOG_TAIL_BYTES,
  WorkerLogSection
} from './drawer_log'
import {
  AssigneeMenu,
  DependenciesSection,
  DescriptionSection,
  Diagnostics,
  EstimateSection,
  isAdminSummary,
  MetaRow
} from './drawer_overview'
import { ModelOverrideField, overridePatch } from './model-override'
import { PriorityPicker } from './priority-picker'
import { statusGuidance } from './status-guidance'
import { type ChoiceResponse, columnMeta, type KanbanTaskDetail, SEVERITY_TONE } from './types'
import {
  ago,
  Callout,
  errText,
  FIELD_LABEL,
  isLockedTarget,
  lockedReason,
  ScrollFade,
  Section,
  shortId,
  StatusMenu,
  TabStrip,
  useDefaultAssignee,
  useKanban,
  wash
} from './ui'

export { ActivityRow, RunErrorLine } from './drawer_activity'
// Re-exported for the plugin's existing test suite and for board.tsx, which
// import these by name. Behavior lives in the siblings; this is the door.
export { CtaBanner, parseBlockedChoices } from './drawer_cta'
export { type ActivityGroup, groupActivity, latestBlockReason, runErrorText } from './drawer_events'
export { ImagesSection, ImageThumb, isImageAttachment } from './drawer_log'

type TabId = 'activity' | 'log' | 'overview'

/**
 * Pending focus request for the comment composer. The CTA banner's Reply lives
 * on Overview while the composer lives on Activity, so the deep-link is a
 * two-beat action: switch tabs, then focus once the input has mounted. A
 * one-shot flag (rather than a direct querySelector at click time) is what
 * keeps Reply from being a silently dead button.
 */
const FOCUS_COMMENT_ATTEMPTS = 10

function focusCommentInput(attemptsLeft = FOCUS_COMMENT_ATTEMPTS): void {
  const el = document.querySelector<HTMLElement>('[data-kanban-comment-input="true"]')

  if (el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    el.focus()

    return
  }

  if (attemptsLeft > 0) {
    requestAnimationFrame(() => focusCommentInput(attemptsLeft - 1))
  }
}

export function TaskDrawer({
  board: taskBoard,
  columns,
  id,
  onClose,
  onOpen
}: {
  /** The card's own board, from the caller's board cache — REQUIRED to route
   *  every fetch/mutation correctly in All Boards mode, where `$boardSlug` is
   *  the `'*'` sentinel and cannot resolve a real board on its own. `undefined`
   *  in single-board mode (byte-identical to the pre-existing behavior: every
   *  call falls through to `$boardSlug`). */
  board?: string
  columns: string[]
  id: null | string
  onClose: () => void
  onOpen: (id: string) => void
}) {
  const k = useKanban()
  const qc = useQueryClient()
  const slug = useValue($boardSlug)
  const [lightbox, setLightbox] = useState<null | { filename: string; src: string }>(null)
  // Tab selection is pure presentation and belongs to this component — a
  // global store would make one drawer's tab leak into the next card.
  const [tab, setTab] = useState<TabId>('overview')

  // Socket-invalidated (bindApi); the interval is only the socketless heartbeat.
  const { data: detail, error } = useQuery({
    enabled: !!id,
    queryFn: () => fetchTask(id!, taskBoard),
    queryKey: taskKey(slug, id ?? ''),
    refetchInterval: 30_000
  })

  const task = detail?.task
  const running = task?.status === 'running'
  const defaultAssignee = useDefaultAssignee()

  // "Show more" widens the requested tail instead of leaving a bare `...]` —
  // the acceptance criteria want a visible affordance, not silent truncation.
  // Resets to the default whenever the drawer switches to a different task.
  const [logTail, setLogTail] = useState(DEFAULT_LOG_TAIL_BYTES)

  useEffect(() => setLogTail(DEFAULT_LOG_TAIL_BYTES), [id])
  // A different card starts on Overview — carrying the previous card's tab
  // over would open a log the user never asked for.
  useEffect(() => setTab('overview'), [id])

  const { data: log } = useQuery({
    enabled: !!id,
    queryFn: () => fetchLog(id!, logTail, taskBoard),
    queryKey: logKey(slug, id ?? '', logTail),
    refetchInterval: running ? 3_000 : 15_000
  })

  // Esc closes the drawer even though it isn't modal (no backdrop to click off).
  useEffect(() => {
    if (!id) {
      return
    }

    const onKey = (event: KeyboardEvent) => event.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)

    return () => window.removeEventListener('keydown', onKey)
  }, [id, onClose])

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: taskKey(slug, id!) })
    void qc.invalidateQueries({ queryKey: ['kanban', 'board', slug] })
  }

  // Optimistic status change against the task cache; rolls back + toasts on a
  // rejected transition (the backend enforces the workflow).
  const moveMut = useMutation({
    mutationFn: (status: string) => patchTask(id!, { status }, taskBoard),
    onMutate: async status => {
      await qc.cancelQueries({ queryKey: taskKey(slug, id!) })
      const previous = qc.getQueryData<KanbanTaskDetail>(taskKey(slug, id!))

      if (previous) {
        qc.setQueryData(taskKey(slug, id!), { ...previous, task: { ...previous.task, status } })
      }

      return { previous }
    },
    onError: (err, _status, context) => {
      if (context?.previous) {
        qc.setQueryData(taskKey(slug, id!), context.previous)
      }

      host.notify({ kind: 'error', message: errText(err) })
    },
    onSettled: invalidate
  })

  const mutate = (fn: () => Promise<unknown>, onDone?: () => void) => () =>
    fn().then(
      () => {
        invalidate()
        onDone?.()
      },
      (err: unknown) => host.notify({ kind: 'error', message: errText(err) })
    )

  const commentMut = useMutation({
    mutationFn: ({ body, choice }: { body: string; choice?: ChoiceResponse }) =>
      addComment(id!, body, choice, taskBoard),
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSuccess: invalidate
  })

  // "Note & requeue" for a running task: post the note, then reclaim so the
  // dispatcher re-runs it with the note in the worker's context — the one-click
  // replacement for the block → comment → unblock dance.
  const requeueMut = useMutation({
    mutationFn: async (body: string) => {
      await addComment(id!, body, undefined, taskBoard)
      await reclaimTask(id!, taskBoard)
    },
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSuccess: () => {
      host.notify({ kind: 'info', message: k.notePosted })
      invalidate()
    }
  })

  // Priority-only PATCH — never touches status/title/body/assignee.
  const priorityMut = useMutation({
    mutationFn: (priority: number) => patchTask(id!, { priority }, taskBoard),
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSuccess: invalidate
  })

  const uploadMut = useMutation({
    mutationFn: async (file: File) =>
      uploadAttachment(
        id!,
        {
          bytes: await file.arrayBuffer(),
          contentType: file.type || undefined,
          filename: file.name
        },
        taskBoard
      ),
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSuccess: invalidate
  })

  const activityGroups = useMemo(() => (detail ? groupActivity(detail.events, k) : []), [detail, k])

  if (!id) {
    return null
  }

  const errorMessage = error ? errText(error) : null
  const tone = columnMeta(task?.status ?? '').tone
  const attachmentCount = detail?.attachments.length ?? 0

  const move = (status: string) => {
    if (!task || status === task.status) {
      return
    }

    if (isLockedTarget(status)) {
      host.notify({ kind: 'info', message: lockedReason(k, status) })

      return
    }

    moveMut.mutate(status)
  }

  /** Reply deep-link: comments live on Activity, so switch there first and
   *  focus once the composer has mounted. */
  const focusComment = () => {
    setTab('activity')
    requestAnimationFrame(() => focusCommentInput())
  }

  return (
    <div className="absolute inset-y-0 right-0 z-20 flex w-[26rem] flex-col border-l border-(--ui-stroke-tertiary) bg-(--ui-bg-elevated) duration-150 ease-out animate-in fade-in slide-in-from-right-4">
      {/* Status-colored header band — the card's state is the first thing the
          eye lands on, and it's the same tone the board's column uses. */}
      <header
        className="flex flex-col gap-2 px-4 pt-3.5 pb-3"
        style={task ? { backgroundColor: wash(tone, 8), boxShadow: `inset 0 -1px 0 ${wash(tone, 22)}` } : undefined}
      >
        <div className="flex items-center gap-2">
          {task ? (
            <StatusMenu columns={columns} onMove={move} status={task.status} />
          ) : (
            <span className="font-mono text-sm text-(--ui-text-tertiary)">{shortId(id)}</span>
          )}
          {task && (
            <span className="font-mono text-[0.625rem] text-(--ui-text-quaternary)" data-selectable-text="true">
              {shortId(task.id)}
            </span>
          )}
          <div className="ml-auto flex items-center gap-0.5">
            {task && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button
                    aria-label={k.taskActions}
                    className="grid size-6 place-items-center rounded text-(--ui-text-tertiary) transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground"
                    type="button"
                  >
                    <Codicon name="ellipsis" size="0.9rem" />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  <DropdownMenuItem
                    onSelect={() => {
                      void navigator.clipboard.writeText(task.id)
                      host.notify({ kind: 'info', message: k.copiedId(task.id) })
                    }}
                  >
                    <Codicon name="copy" size="0.85rem" />
                    {k.copyTaskId}
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    onSelect={() => {
                      void navigator.clipboard.writeText(task.title || task.id)
                      host.notify({ kind: 'info', message: k.copiedTitle })
                    }}
                  >
                    <Codicon name="copy" size="0.85rem" />
                    {k.copyTitle}
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem onSelect={mutate(() => patchTask(task.id, { status: 'archived' }, taskBoard), onClose)}>
                    <Codicon name="archive" size="0.85rem" />
                    {k.archive}
                  </DropdownMenuItem>
                  <DropdownMenuItem className="text-destructive" onSelect={mutate(() => deleteTask(task.id, taskBoard), onClose)}>
                    <Codicon name="trash" size="0.85rem" />
                    {k.delete}
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            )}
            <button
              aria-label={k.close}
              className="grid size-6 place-items-center rounded text-(--ui-text-tertiary) transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground"
              onClick={onClose}
              type="button"
            >
              <Codicon name="close" size="0.9rem" />
            </button>
          </div>
        </div>
        {task && (
          <h2 className="text-sm leading-snug font-semibold text-foreground" data-selectable-text="true">
            {task.title || task.id}
          </h2>
        )}
      </header>

      {detail && task && (
        <TabStrip
          active={tab}
          onSelect={next => setTab(next as TabId)}
          tabs={[
            { id: 'overview', label: k.tabOverview },
            { id: 'activity', label: k.tabActivity, count: detail.events.length },
            { id: 'log', label: k.tabLog, count: attachmentCount }
          ]}
        />
      )}

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3" data-selectable-text="true">
        {errorMessage ? (
          <ErrorState title={errorMessage} />
        ) : !detail || !task ? (
          <div className="grid h-32 place-items-center">
            <Loader type="lemniscate-bloom" />
          </div>
        ) : (
          <div className="flex flex-col gap-4 text-sm" id={`kanban-tabpanel-${tab}`} role="tabpanel">
            {tab === 'overview' && (
              <>
                <CtaBanner
                  comments={detail.comments}
                  events={detail.events}
                  onFocusComment={focusComment}
                  onMove={move}
                  onSubmitChoice={(body, choice) => commentMut.mutateAsync({ body, choice })}
                  runs={detail.runs}
                  task={task}
                />

                <p className="text-[0.71rem] leading-relaxed text-(--ui-text-tertiary)">
                  {statusGuidance(task.status, task, detail.events, detail.runs, k)}
                </p>

                {task.status === 'ready' && !task.assignee && !defaultAssignee && (
                  <Callout title={k.readyUnassignedTitle} tone={SEVERITY_TONE.warning}>
                    <p className="text-[0.71rem] leading-relaxed text-(--ui-text-secondary)">{k.readyUnassignedBody}</p>
                  </Callout>
                )}

                {task.diagnostics && task.diagnostics.length > 0 && (
                  <Section label={k.diagnosticsN(task.diagnostics.length)} tone={SEVERITY_TONE.warning}>
                    <Diagnostics
                      items={task.diagnostics}
                      onReclaim={() => void mutate(() => reclaimTask(task.id, taskBoard))()}
                    />
                  </Section>
                )}

                <DescriptionSection
                  body={task.body}
                  onSave={body => void mutate(() => patchTask(task.id, { body }, taskBoard))()}
                />

                <div className="flex flex-col gap-1.5">
                  <div className={FIELD_LABEL}>{k.metaSectionLabel}</div>
                  <div className="grid grid-cols-[5.5rem_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-[0.71rem]">
                    <MetaRow label={k.assignee}>
                      <AssigneeMenu
                        current={task.assignee}
                        onReassign={profile => void mutate(() => reassignTask(task.id, profile, taskBoard))()}
                      />
                    </MetaRow>
                    <MetaRow label={k.metaPriority}>
                      <PriorityPicker onChange={priority => priorityMut.mutate(priority)} priority={task.priority} />
                    </MetaRow>
                    {task.tenant && <MetaRow label={k.metaTenant}>{task.tenant}</MetaRow>}
                    {task.workspace_path && (
                      <MetaRow
                        label={k.workspace}
                        title={`${task.workspace_kind ? `${task.workspace_kind}: ` : ''}${task.workspace_path}`}
                      >
                        {task.workspace_kind ? `${task.workspace_kind}: ` : ''}
                        {task.workspace_path}
                      </MetaRow>
                    )}
                    <MetaRow label={k.model}>
                      <ModelOverrideField
                        onChange={next => void mutate(() => patchTask(task.id, overridePatch(next), taskBoard))()}
                        value={{
                          effort: task.reasoning_effort ?? '',
                          model: task.model_override ?? '',
                          provider: task.provider_override ?? ''
                        }}
                      />
                    </MetaRow>
                    {task.created_by && <MetaRow label={k.metaCreatedBy}>{task.created_by}</MetaRow>}
                    {ago(task.created_at) && <MetaRow label={k.metaCreated}>{ago(task.created_at)}</MetaRow>}
                    {running && task.worker_pid ? <MetaRow label={k.metaWorkerPid}>{task.worker_pid}</MetaRow> : null}
                  </div>
                </div>

                {task.result && (
                  <Section label={k.result} tone={columnMeta('done').tone}>
                    <p className="whitespace-pre-wrap text-[0.8125rem] text-(--ui-text-secondary)">{task.result}</p>
                  </Section>
                )}

                {task.latest_summary && !isAdminSummary(task.latest_summary) && (
                  <Section label={k.latestSummary}>
                    <p className="whitespace-pre-wrap text-[0.8125rem] text-(--ui-text-secondary)">
                      {task.latest_summary}
                    </p>
                  </Section>
                )}

                <DependenciesSection
                  board={taskBoard}
                  detail={detail}
                  onLink={parentId => void mutate(() => linkTasks(parentId, task.id, taskBoard))()}
                  onOpen={onOpen}
                  onUnlink={(parentId, childId) => void mutate(() => unlinkTasks(parentId, childId, taskBoard))()}
                  slug={slug}
                  task={task}
                />

                <EstimateSection board={taskBoard} id={task.id} />
              </>
            )}

            {tab === 'activity' && (
              <>
                <CommentsSection
                  comments={detail.comments}
                  onRequeue={body => requeueMut.mutate(body)}
                  onSubmit={body => commentMut.mutate({ body })}
                  pending={commentMut.isPending || requeueMut.isPending}
                  running={running}
                />

                {detail.events.length > 0 ? (
                  <Section label={k.activity(detail.events.length)}>
                    <ScrollFade deps={detail.events.length} max="22rem">
                      <ul className="flex flex-col gap-1">
                        {activityGroups.map(group => (
                          <ActivityRow group={group} k={k} key={group.events[0].id} />
                        ))}
                      </ul>
                    </ScrollFade>
                  </Section>
                ) : (
                  <p className="text-[0.75rem] text-(--ui-text-quaternary)">{k.noActivityYet}</p>
                )}

                <RunsSection runs={detail.runs} />
              </>
            )}

            {tab === 'log' && (
              <>
                <WorkerLogSection
                  log={log}
                  onShowMore={() => setLogTail(t => Math.min(t * 4, MAX_LOG_TAIL_BYTES))}
                  tail={logTail}
                />

                <ImagesSection
                  attachments={detail.attachments.filter(isImageAttachment)}
                  board={taskBoard}
                  onOpen={(filename, src) => setLightbox({ filename, src })}
                />

                <AttachmentsSection
                  attachments={detail.attachments.filter(a => !isImageAttachment(a))}
                  onUpload={file => uploadMut.mutate(file)}
                  pending={uploadMut.isPending}
                />
              </>
            )}
          </div>
        )}
      </div>

      <Dialog onOpenChange={open => !open && setLightbox(null)} open={!!lightbox}>
        <DialogContent
          bodyClassName="block overflow-visible p-0"
          className="w-auto max-h-[calc(100vh-12rem)] max-w-[calc(100vw-12rem)] border-0 bg-transparent shadow-none"
          showCloseButton={false}
        >
          {lightbox && (
            <img
              alt={lightbox.filename}
              className="block max-h-[calc(100vh-12rem)] max-w-[calc(100vw-12rem)] cursor-zoom-out rounded-lg object-contain shadow-2xl"
              onClick={() => setLightbox(null)}
              onError={() => setLightbox(null)}
              src={lightbox.src}
            />
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}
