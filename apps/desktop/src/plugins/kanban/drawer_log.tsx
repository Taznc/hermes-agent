/**
 * The drawer's LOG tab: the worker's stdout/stderr tail plus the task's
 * attachments (images as a lightbox strip, everything else as a file list).
 */

import { Button, Codicon, CopyButton, LogView, Tip, useQuery } from '@hermes/plugin-sdk'
import { useRef, useState } from 'react'

import { fetchAttachmentDataUrl } from './api'
import type { KanbanAttachment, WorkerLog } from './types'
import { ScrollFade, Section, useKanban } from './ui'

export const DEFAULT_LOG_TAIL_BYTES = 16_384
export const MAX_LOG_TAIL_BYTES = 1_048_576 // 1 MiB — well under the backend's 2 MiB rotation size.

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

/** The worker log tail. Gets the whole tab's height rather than the old 16rem
 *  peephole — reading the log IS the reason to be on this tab. */
export function WorkerLogSection({
  log,
  onShowMore,
  tail
}: {
  log?: WorkerLog
  onShowMore: () => void
  tail: number
}) {
  const k = useKanban()

  if (!log?.exists || !log.content) {
    return <p className="text-[0.75rem] text-(--ui-text-quaternary)">{k.noLogYet}</p>
  }

  return (
    <Section
      action={<CopyButton appearance="icon" buttonSize="icon-xs" buttonVariant="ghost" text={() => log.content} />}
      label={log.truncated ? k.workerLogTail : k.workerLog}
    >
      <ScrollFade deps={log.content.length} max="30rem">
        <LogView className="border-0 px-0" content={log.content} numbered />
      </ScrollFade>
      {log.truncated && tail < MAX_LOG_TAIL_BYTES && (
        <Button className="self-start" onClick={onShowMore} size="xs" variant="text">
          {k.workerLogShowMore}
        </Button>
      )}
    </Section>
  )
}
