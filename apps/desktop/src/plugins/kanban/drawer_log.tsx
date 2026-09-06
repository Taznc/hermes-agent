/**
 * The drawer's LOG tab: the worker's stdout/stderr tail plus the task's
 * attachments (images as a lightbox strip, everything else as a file list).
 */

import { Button, Codicon, CopyButton, LogView, Tip, useQuery } from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'

import { fetchAttachmentDataUrl } from './api'
import type { KanbanAttachment, WorkerLog } from './types'
import { Section, useKanban } from './ui'

// The API caps both a single response and the active on-disk artifact at 2 MiB.
// Requesting that ceiling once means the drawer shows every retained line instead
// of asking readers to page through an arbitrary short tail.
export const FULL_LOG_TAIL_BYTES = 2_000_000

export const isImageAttachment = (a: KanbanAttachment) => (a.content_type ?? '').startsWith('image/')

/** One image tile: fetches its bytes lazily as a base64 data URL (the
 *  desktop plugin host has no authenticated `<img src>` door — REST goes
 *  over the Electron IPC JSON bridge) and swaps in a broken-image
 *  placeholder on either a fetch failure or a decode failure, never a
 *  crash. */
export function ImageThumb({
  attachment,
  board,
  onOpen
}: {
  attachment: KanbanAttachment
  board?: string
  onOpen: (filename: string, src: string) => void
}) {
  const k = useKanban()
  const [decodeFailed, setDecodeFailed] = useState(false)

  const { data, isError, isLoading } = useQuery({
    queryFn: () => fetchAttachmentDataUrl(attachment.id, board),
    queryKey: ['kanban', 'attachment-data-url', attachment.id],
    retry: false,
    staleTime: Infinity
  })

  const broken = isError || decodeFailed
  const src = data?.data_url

  return (
    <Tip label={broken ? k.brokenImage : attachment.filename}>
      <button
        aria-label={broken ? k.brokenImage : k.openImage}
        className="grid size-16 shrink-0 place-items-center overflow-hidden rounded border border-(--ui-stroke-tertiary) bg-(--ui-bg-quaternary) text-(--ui-text-quaternary) transition-colors hover:border-(--ui-stroke-secondary)"
        disabled={!src || broken}
        onClick={() => src && !broken && onOpen(attachment.filename, src)}
        type="button"
      >
        {broken ? (
          <Codicon name="warning" size="1rem" />
        ) : src ? (
          <img
            alt={attachment.filename}
            className="size-full object-cover"
            onError={() => setDecodeFailed(true)}
            src={src}
          />
        ) : (
          <Codicon name="sync" size="0.9rem" spinning={isLoading} />
        )}
      </button>
    </Tip>
  )
}

/** Image strip above the generic Attachments/Files section — every task
 *  attachment whose content_type starts with `image/`. Click to enlarge in
 *  a lightbox. */
export function ImagesSection({
  attachments,
  board,
  onOpen
}: {
  attachments: KanbanAttachment[]
  board?: string
  onOpen: (filename: string, src: string) => void
}) {
  const k = useKanban()

  if (attachments.length === 0) {
    return null
  }

  return (
    <Section label={k.images(attachments.length)}>
      <div className="flex flex-wrap gap-2">
        {attachments.map(attachment => (
          <ImageThumb attachment={attachment} board={board} key={attachment.id} onOpen={onOpen} />
        ))}
      </div>
    </Section>
  )
}

export function AttachmentsSection({
  attachments,
  onUpload,
  pending
}: {
  attachments: KanbanAttachment[]
  onUpload: (file: File) => void
  pending: boolean
}) {
  const k = useKanban()
  const fileRef = useRef<HTMLInputElement>(null)

  return (
    <Section
      action={
        <>
          <input
            hidden
            onChange={event => {
              const file = event.target.files?.[0]

              if (file) {
                onUpload(file)
              }

              event.target.value = ''
            }}
            ref={fileRef}
            type="file"
          />
          <Button
            aria-label={k.uploadAttachment}
            disabled={pending}
            onClick={() => fileRef.current?.click()}
            size="icon-xs"
            variant="ghost"
          >
            <Codicon name={pending ? 'sync' : 'cloud-upload'} size="0.8rem" spinning={pending} />
          </Button>
        </>
      }
      label={k.attachments(attachments.length)}
    >
      {attachments.length > 0 ? (
        <ul className="flex flex-col gap-1">
          {attachments.map(attachment => (
            <li className="flex items-center gap-1.5 text-[0.75rem] text-(--ui-text-tertiary)" key={attachment.id}>
              <Codicon name="file" size="0.75rem" />
              {attachment.filename}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-[0.75rem] text-(--ui-text-quaternary)">{k.noAttachments}</p>
      )}
    </Section>
  )
}

/** The complete retained worker log. It follows a running worker until the
 * reader deliberately scrolls away, then offers an explicit return-to-live
 * control rather than hijacking the inspection position. */
export function WorkerLogSection({
  log,
  live
}: {
  log?: WorkerLog
  live: boolean
}) {
  const k = useKanban()
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const stickRef = useRef(true)
  const [following, setFollowing] = useState(true)
  const [wrap, setWrap] = useState(true)

  const jumpToLatest = () => {
    const viewport = scrollRef.current

    if (viewport) {
      viewport.scrollTop = viewport.scrollHeight
    }

    stickRef.current = true
    setFollowing(true)
  }

  useEffect(() => {
    if (stickRef.current) {
      jumpToLatest()
    }
  }, [log?.content])

  if (!log?.exists || !log.content) {
    return <p className="text-[0.75rem] text-(--ui-text-quaternary)">{k.noLogYet}</p>
  }

  return (
    <Section
      action={
        <div className="flex items-center gap-1">
          <Button
            aria-label={k.workerLogWrap}
            aria-pressed={wrap}
            onClick={() => setWrap(value => !value)}
            size="xs"
            variant="text"
          >
            {k.workerLogWrap}
          </Button>
          <CopyButton appearance="icon" buttonSize="icon-xs" buttonVariant="ghost" text={() => log.content} />
        </div>
      }
      label={log.truncated ? k.workerLogRetained : k.workerLog}
    >
      <LogView
        className="max-h-[30rem] border-0 px-0"
        content={log.content}
        data-kanban-worker-log="true"
        numbered
        onScroll={event => {
          const viewport = event.currentTarget
          const nextFollowing = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 24

          stickRef.current = nextFollowing
          setFollowing(nextFollowing)
        }}
        ref={scrollRef}
        wrap={wrap}
      />
      {live && (
        <div className="flex items-center justify-between gap-2 text-[0.6875rem] text-(--ui-text-secondary)">
          <span className="flex items-center gap-1.5" data-kanban-worker-log-state={following ? 'following' : 'paused'}>
            <span
              className="size-1.5 rounded-full"
              style={{ backgroundColor: following ? 'var(--theme-primary)' : 'var(--ui-text-quaternary)' }}
            />
            {following ? k.workerLogLive : k.workerLogPaused}
          </span>
          {!following && (
            <Button onClick={jumpToLatest} size="xs" variant="textStrong">
              {k.workerLogJumpToLatest}
            </Button>
          )}
        </div>
      )}
    </Section>
  )
}
