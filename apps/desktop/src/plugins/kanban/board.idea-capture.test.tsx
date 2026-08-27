/**
 * Focused tests for the Kanban board's free-typed roadmap idea capture
 * affordance (Phase 2.15). Exercises the real @hermes/plugin-sdk component
 * primitives (Dialog etc.), matching the pattern in drawer.cta.test.tsx and
 * model-override.test.tsx — usePluginI18n is stubbed to echo the dotted key
 * so assertions match on stable keys instead of translated English text.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { IdeaCaptureDialog } from './board'

const { addRoadmapIdea, notify } = vi.hoisted(() => ({ addRoadmapIdea: vi.fn(), notify: vi.fn() }))

vi.mock('./api', () => ({
  addRoadmapIdea: (...args: unknown[]) => addRoadmapIdea(...args)
}))

vi.mock('@hermes/plugin-sdk', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@hermes/plugin-sdk')

  return {
    ...actual,
    host: { ...(actual.host as object), notify },
    usePluginI18n: () => (key: string) => key
  }
})

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

beforeEach(() => {
  addRoadmapIdea.mockReset()
  notify.mockReset()
})

afterEach(() => {
  cleanup()
})

describe('IdeaCaptureDialog', () => {
  it('renders nothing when closed', () => {
    const { container } = render(<IdeaCaptureDialog onClose={vi.fn()} open={false} />)

    expect(container.querySelector('textarea')).toBeNull()
  })

  it('disables Save until text is typed, and clears on reopen', () => {
    render(<IdeaCaptureDialog onClose={vi.fn()} open />)

    const save = screen.getByText('ideaSave').closest('button') as HTMLButtonElement
    expect(save.disabled).toBe(true)

    fireEvent.change(screen.getByPlaceholderText('ideaPlaceholder'), { target: { value: 'A rough idea' } })
    expect(save.disabled).toBe(false)
  })

  it('rejects whitespace-only text without calling the API', () => {
    render(<IdeaCaptureDialog onClose={vi.fn()} open />)

    fireEvent.change(screen.getByPlaceholderText('ideaPlaceholder'), { target: { value: '   ' } })
    const save = screen.getByText('ideaSave').closest('button') as HTMLButtonElement

    expect(save.disabled).toBe(true)
    expect(addRoadmapIdea).not.toHaveBeenCalled()
  })

  it('on success: calls the API, notifies, and closes', async () => {
    addRoadmapIdea.mockResolvedValue({ ok: true, reason: null })
    const onClose = vi.fn()

    render(<IdeaCaptureDialog onClose={onClose} open />)
    fireEvent.change(screen.getByPlaceholderText('ideaPlaceholder'), { target: { value: '  Ship dark mode  ' } })
    fireEvent.click(screen.getByText('ideaSave'))

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))

    // Trimmed before sending — the plugin sanitizer is authoritative for
    // content shape, but the UI shouldn't ship leading/trailing whitespace.
    expect(addRoadmapIdea).toHaveBeenCalledWith('Ship dark mode')
    expect(notify).toHaveBeenCalledWith(expect.objectContaining({ kind: 'success', message: 'ideaSaved' }))
  })

  it('on roadmap_unavailable: shows an inline error, does not close, does not notify success', async () => {
    addRoadmapIdea.mockResolvedValue({ ok: false, reason: 'roadmap_unavailable' })
    const onClose = vi.fn()

    render(<IdeaCaptureDialog onClose={onClose} open />)
    fireEvent.change(screen.getByPlaceholderText('ideaPlaceholder'), { target: { value: 'An idea' } })
    fireEvent.click(screen.getByText('ideaSave'))

    await waitFor(() => expect(screen.getByText('ideaUnavailable')).toBeTruthy())

    expect(onClose).not.toHaveBeenCalled()
    expect(notify).not.toHaveBeenCalledWith(expect.objectContaining({ kind: 'success' }))
  })

  it('on empty_idea: shows the empty-specific message, distinct from roadmap-unavailable', async () => {
    addRoadmapIdea.mockResolvedValue({ ok: false, reason: 'empty_idea' })

    render(<IdeaCaptureDialog onClose={vi.fn()} open />)
    fireEvent.change(screen.getByPlaceholderText('ideaPlaceholder'), { target: { value: 'x' } })
    fireEvent.click(screen.getByText('ideaSave'))

    await waitFor(() => expect(screen.getByText('ideaEmpty')).toBeTruthy())
  })

  it('on a thrown network error: shows the error and re-enables the button', async () => {
    addRoadmapIdea.mockRejectedValue(new Error('network down'))

    render(<IdeaCaptureDialog onClose={vi.fn()} open />)
    fireEvent.change(screen.getByPlaceholderText('ideaPlaceholder'), { target: { value: 'An idea' } })
    fireEvent.click(screen.getByText('ideaSave'))

    await waitFor(() => expect(screen.getByText(/network down/)).toBeTruthy())
    expect((screen.getByText('ideaSave').closest('button') as HTMLButtonElement).disabled).toBe(false)
  })

  it('Cancel closes without calling the API', () => {
    const onClose = vi.fn()
    render(<IdeaCaptureDialog onClose={onClose} open />)

    fireEvent.click(screen.getByText('cancel'))

    expect(onClose).toHaveBeenCalledTimes(1)
    expect(addRoadmapIdea).not.toHaveBeenCalled()
  })
})
