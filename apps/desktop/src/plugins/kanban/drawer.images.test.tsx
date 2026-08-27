/**
 * Focused tests for rendering attached images in the task detail view
 * (#cae4c2ba): the drawer fetches an image attachment's bytes as a data URL
 * (fetchAttachmentDataUrl — the desktop plugin host has no authenticated
 * `<img src>` door of its own), shows a thumbnail strip separate from the
 * generic Files list, opens a lightbox on click, and degrades gracefully
 * (no crash) when the fetch or the decode fails.
 *
 * Exercises the real component tree via @hermes/plugin-sdk, matching the
 * pattern in new-task-paste.test.tsx / drawer.cta.test.tsx.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { ImagesSection, ImageThumb, isImageAttachment } from './drawer'
import type { KanbanAttachment } from './types'

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: vi.fn().mockResolvedValue({ providers: [] }),
  setApiRequestProfile: vi.fn()
}))

vi.mock('@hermes/plugin-sdk', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@hermes/plugin-sdk')

  return { ...actual, usePluginI18n: () => (key: string) => key }
})

const fetchAttachmentDataUrlMock = vi.fn()

vi.mock('./api', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('./api')

  return {
    ...actual,
    fetchAttachmentDataUrl: (...args: unknown[]) => fetchAttachmentDataUrlMock(...args)
  }
})

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
})

beforeEach(() => {
  fetchAttachmentDataUrlMock.mockReset()
})

afterEach(() => {
  cleanup()
})

function withClient(node: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>)
}

const imageAttachment = (overrides: Partial<KanbanAttachment> = {}): KanbanAttachment => ({
  content_type: 'image/png',
  filename: 'screenshot.png',
  id: 1,
  size: 123,
  ...overrides
})

describe('isImageAttachment', () => {
  it('is true for image/* content types, false otherwise (including missing)', () => {
    expect(isImageAttachment(imageAttachment())).toBe(true)
    expect(isImageAttachment(imageAttachment({ content_type: 'application/pdf' }))).toBe(false)
    expect(isImageAttachment(imageAttachment({ content_type: null }))).toBe(false)
    expect(isImageAttachment(imageAttachment({ content_type: undefined }))).toBe(false)
  })
})

describe('ImagesSection', () => {
  it('renders nothing when there are no image attachments', () => {
    const { container } = withClient(<ImagesSection attachments={[]} onOpen={vi.fn()} />)

    expect(container.innerHTML).toBe('')
  })

  it('fetches and shows a thumbnail for each image attachment, opening the lightbox on click', async () => {
    fetchAttachmentDataUrlMock.mockResolvedValue({
      content_type: 'image/png',
      data_url: 'data:image/png;base64,AAAA',
      size: 4
    })

    const onOpen = vi.fn()
    const attachments = [imageAttachment({ id: 1, filename: 'a.png' }), imageAttachment({ id: 2, filename: 'b.png' })]

    withClient(<ImagesSection attachments={attachments} onOpen={onOpen} />)

    await waitFor(() => expect(fetchAttachmentDataUrlMock).toHaveBeenCalledTimes(2))
    expect(fetchAttachmentDataUrlMock).toHaveBeenCalledWith(1)
    expect(fetchAttachmentDataUrlMock).toHaveBeenCalledWith(2)

    const thumbs = await screen.findAllByRole('button', { name: 'openImage' })

    expect(thumbs).toHaveLength(2)

    fireEvent.click(thumbs[0])
    expect(onOpen).toHaveBeenCalledWith('a.png', 'data:image/png;base64,AAAA')
  })
})

describe('ImageThumb: broken/missing image handling (no crash)', () => {
  it('shows a disabled broken-image placeholder when the data-url fetch fails', async () => {
    fetchAttachmentDataUrlMock.mockRejectedValue(new Error('attachment not found'))

    withClient(<ImageThumb attachment={imageAttachment()} onOpen={vi.fn()} />)

    const button = await screen.findByRole('button', { name: 'brokenImage' })

    expect(button).toBeTruthy()
    expect((button as HTMLButtonElement).disabled).toBe(true)
  })

  it('falls back to the broken placeholder when the <img> itself fails to decode', async () => {
    fetchAttachmentDataUrlMock.mockResolvedValue({
      content_type: 'image/png',
      data_url: 'data:image/png;base64,not-really-an-image',
      size: 4
    })
    const onOpen = vi.fn()

    withClient(<ImageThumb attachment={imageAttachment()} onOpen={onOpen} />)

    const img = await screen.findByAltText('screenshot.png')

    fireEvent.error(img)

    const button = await screen.findByRole('button', { name: 'brokenImage' })

    expect(button).toBeTruthy()
    expect((button as HTMLButtonElement).disabled).toBe(true)

    fireEvent.click(button)
    expect(onOpen).not.toHaveBeenCalled()
  })
})
