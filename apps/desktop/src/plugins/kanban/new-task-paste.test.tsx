/**
 * Focused tests for the "new task" dialog's paste-to-upload image flow
 * (t_31ee24d1): pasting an image stages it immediately, shows an inline
 * preview, supports removal before submit, supports multiple images, and
 * ships as `pending_attachment_tokens` (never inlined into the body text)
 * on create.
 *
 * Exercises the real component tree via @hermes/plugin-sdk, matching the
 * pattern in model-override.test.tsx / drawer.cta.test.tsx. The kanban data
 * layer (./api) is mocked at the module boundary so no real network/REST
 * calls happen; `stageAttachment`/`createTask`/etc. assertions drive the
 * behavior checks.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { KanbanTask } from './types'

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: vi.fn().mockResolvedValue({ providers: [] }),
  setApiRequestProfile: vi.fn()
}))

// Plugins only ever see @hermes/plugin-sdk; stub usePluginI18n to echo the
// dotted key (same shim other kanban tests use) so assertions target stable
// keys instead of translated English text.
vi.mock('@hermes/plugin-sdk', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@hermes/plugin-sdk')

  return { ...actual, usePluginI18n: () => (key: string) => key }
})

const stageAttachmentMock = vi.fn()
const deleteStagedAttachmentMock = vi.fn()
const createTaskMock = vi.fn()

vi.mock('./api', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('./api')

  return {
    ...actual,
    createTask: (...args: unknown[]) => createTaskMock(...args),
    deleteStagedAttachment: (...args: unknown[]) => deleteStagedAttachmentMock(...args),
    fetchBoards: vi.fn().mockResolvedValue({ boards: [], current: '' }),
    fetchProfiles: vi.fn().mockResolvedValue({ profiles: [] }),
    stageAttachment: (...args: unknown[]) => stageAttachmentMock(...args)
  }
})

// jsdom has no createObjectURL/revokeObjectURL.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
  URL.createObjectURL = vi.fn(() => 'blob:mock-preview-url')
  URL.revokeObjectURL = vi.fn()
})

beforeEach(() => {
  stageAttachmentMock.mockReset()
  deleteStagedAttachmentMock.mockReset().mockResolvedValue({ ok: true })
  createTaskMock.mockReset().mockResolvedValue({ task: { id: 't_new', status: 'ready' } as KanbanTask })
})

afterEach(() => {
  cleanup()
})

/** A minimal DataTransferItem-like clipboard image item, as the browser
 *  hands the paste handler for an image pasted from the OS clipboard. */
function imageClipboardItem(blob: File) {
  return { getAsFile: () => blob, kind: 'file', type: blob.type }
}

/** jsdom's synthetic paste events don't accept `clipboardData` via the usual
 *  fireEvent init (it's a readonly DOM property on the real event), so build
 *  the event by hand and attach it with defineProperty before dispatching. */
function pasteImages(target: Element, blobs: File[]) {
  const event = new Event('paste', { bubbles: true, cancelable: true })

  Object.defineProperty(event, 'clipboardData', {
    configurable: true,
    value: { items: blobs.map(imageClipboardItem) }
  })
  fireEvent(target, event)
}

async function renderDialog() {
  // Import after mocks are registered.
  const { NewTaskDialog } = await import('./board')
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onClose = vi.fn()

  render(
    <QueryClientProvider client={client}>
      <NewTaskDialog onClose={onClose} parents={[]} target="ready" />
    </QueryClientProvider>
  )

  return { onClose }
}

describe('new-task dialog: paste-to-upload images', () => {
  it('stages a pasted image and shows an inline preview', async () => {
    stageAttachmentMock.mockResolvedValue({
      attachment: { content_type: 'image/png', filename: 'screenshot.png', size: 123, token: 'st_1' }
    })

    await renderDialog()

    const textarea = screen.getByPlaceholderText('descPlaceholder')
    const blob = new File(['fake-bytes'], 'screenshot.png', { type: 'image/png' })

    pasteImages(textarea, [blob])

    await waitFor(() => expect(stageAttachmentMock).toHaveBeenCalledTimes(1))
    expect(stageAttachmentMock).toHaveBeenCalledWith(
      expect.objectContaining({ contentType: 'image/png', filename: 'screenshot.png' })
    )

    // Preview thumbnail rendered from the staged attachment.
    await waitFor(() => expect(screen.getByAltText('screenshot.png')).toBeTruthy())
  })

  it('supports pasting multiple images, each staged independently', async () => {
    stageAttachmentMock
      .mockResolvedValueOnce({ attachment: { content_type: 'image/png', filename: 'a.png', size: 10, token: 'st_a' } })
      .mockResolvedValueOnce({ attachment: { content_type: 'image/png', filename: 'b.png', size: 20, token: 'st_b' } })

    await renderDialog()

    const textarea = screen.getByPlaceholderText('descPlaceholder')
    const blobA = new File(['a'], 'a.png', { type: 'image/png' })
    const blobB = new File(['b'], 'b.png', { type: 'image/png' })

    pasteImages(textarea, [blobA])
    await waitFor(() => expect(stageAttachmentMock).toHaveBeenCalledTimes(1))

    pasteImages(textarea, [blobB])
    await waitFor(() => expect(stageAttachmentMock).toHaveBeenCalledTimes(2))

    await waitFor(() => {
      expect(screen.getByAltText('a.png')).toBeTruthy()
      expect(screen.getByAltText('b.png')).toBeTruthy()
    })
  })

  it('removing a pending image deletes the staged upload and drops the preview', async () => {
    stageAttachmentMock.mockResolvedValue({
      attachment: { content_type: 'image/png', filename: 'screenshot.png', size: 123, token: 'st_1' }
    })

    await renderDialog()

    const textarea = screen.getByPlaceholderText('descPlaceholder')
    const blob = new File(['fake-bytes'], 'screenshot.png', { type: 'image/png' })

    pasteImages(textarea, [blob])
    await waitFor(() => expect(screen.getByAltText('screenshot.png')).toBeTruthy())

    fireEvent.click(screen.getByLabelText('removeImage'))

    expect(deleteStagedAttachmentMock).toHaveBeenCalledWith('st_1')
    await waitFor(() => expect(screen.queryByAltText('screenshot.png')).toBeNull())
  })

  it('submits pending_attachment_tokens for staged images, never inlining them into body', async () => {
    stageAttachmentMock.mockResolvedValue({
      attachment: { content_type: 'image/png', filename: 'screenshot.png', size: 123, token: 'st_1' }
    })

    await renderDialog()

    fireEvent.change(screen.getByPlaceholderText('titlePlaceholder'), { target: { value: 'A task with an image' } })

    const textarea = screen.getByPlaceholderText('descPlaceholder')
    const blob = new File(['fake-bytes'], 'screenshot.png', { type: 'image/png' })

    pasteImages(textarea, [blob])
    await waitFor(() => expect(screen.getByAltText('screenshot.png')).toBeTruthy())

    fireEvent.click(screen.getByText('createTask'))

    await waitFor(() => expect(createTaskMock).toHaveBeenCalledTimes(1))
    const body = createTaskMock.mock.calls[0][0] as Record<string, unknown>

    expect(body.pending_attachment_tokens).toEqual(['st_1'])
    expect(String(body.body ?? '')).not.toContain('st_1')
    expect(String(body.body ?? '')).not.toContain('base64')
  })

  it('disables Create while an image upload is still in flight', async () => {
    let resolveUpload: (value: { attachment: { content_type: string; filename: string; size: number; token: string } }) => void =
      () => undefined

    stageAttachmentMock.mockReturnValue(
      new Promise(resolve => {
        resolveUpload = resolve
      })
    )

    await renderDialog()

    fireEvent.change(screen.getByPlaceholderText('titlePlaceholder'), { target: { value: 'A task with an image' } })

    const textarea = screen.getByPlaceholderText('descPlaceholder')
    const blob = new File(['fake-bytes'], 'screenshot.png', { type: 'image/png' })

    pasteImages(textarea, [blob])

    await waitFor(() => expect(screen.getByText('createTask').closest('button')?.disabled).toBe(true))

    resolveUpload({ attachment: { content_type: 'image/png', filename: 'screenshot.png', size: 1, token: 'st_1' } })

    await waitFor(() => expect(screen.getByText('createTask').closest('button')?.disabled).toBe(false))
  })
})
