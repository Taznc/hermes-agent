/**
 * WEB BUILD BROWSER PANE — no Electron guest, so no pseudo-browser.
 *
 * jsdom IS the served web build's runtime shape: `<webview>` never upgrades,
 * `loadURL` is undefined, and `src` navigates nothing. This file deliberately
 * does NOT install the guest seam (`src/test/webview-guest.ts`) that
 * `preview-pane.test.tsx` uses, so every test here runs the capability probe
 * against the real inert element and exercises the branch the web-served
 * desktop actually takes.
 *
 * Reproduces the reported bug — the Browser tab accepted `https://google.com`
 * and stayed an empty pane — and pins the fix: the address opens as a normal
 * top-level browser tab through the bridge, and the pane says so.
 */

import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $connection } from '@/store/session'

import { PreviewPane } from './preview-pane'

vi.mock('./real-profile-consent-dialog', () => ({
  RealProfileConsentDialog: () => null
}))

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }

function browserTab(openExternal = vi.fn(async () => undefined)) {
  desktopWindow.hermesDesktop = { openExternal } as unknown as Window['hermesDesktop']

  return openExternal
}

function renderBrowserPane(url = 'about:blank') {
  return render(<PreviewPane tabId="url:browser" target={{ kind: 'url', label: 'Browser', source: url, url }} />)
}

afterEach(() => {
  cleanup()
  $connection.set(null)
  vi.restoreAllMocks()
  delete desktopWindow.hermesDesktop
})

describe('PreviewPane without an Electron guest (the web build)', () => {
  // The premise, asserted rather than assumed: in a plain browser the tag the
  // Electron path drives is inert, which is why the pane must not build one.
  it('sanity: <webview> is inert here, so the guest probe is false', () => {
    expect((document.createElement('webview') as { loadURL?: unknown }).loadURL).toBeUndefined()
  })

  it('creates no webview at all — the blank pane had nothing behind it', async () => {
    const rendered = renderBrowserPane('https://google.com')

    expect(rendered.container.querySelector('webview')).toBeNull()
  })

  // The reported failure, end to end: type google.com, press Enter. It used to
  // do nothing visible; now it opens a real top-level tab through the bridge.
  it('opens a typed address in a top-level browser tab through the bridge', async () => {
    const openExternal = browserTab()
    const rendered = renderBrowserPane()

    const address = rendered.getByRole('textbox', { name: 'Address' }) as HTMLInputElement

    await act(async () => {
      fireEvent.focus(address)
      fireEvent.change(address, { target: { value: 'google.com' } })
      fireEvent.keyDown(address, { key: 'Enter' })
    })

    await waitFor(() => expect(openExternal).toHaveBeenCalledWith('https://google.com'))
    expect(rendered.container.querySelector('webview')).toBeNull()
  })

  // No bridge installed at all (a bare renderer): still a real top-level tab,
  // with the opener severed. Never an iframe, never a proxy.
  it('falls back to a severed-opener window.open when no bridge is present', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue({} as Window)
    const rendered = renderBrowserPane()

    const address = rendered.getByRole('textbox', { name: 'Address' }) as HTMLInputElement

    await act(async () => {
      fireEvent.focus(address)
      fireEvent.change(address, { target: { value: 'google.com' } })
      fireEvent.keyDown(address, { key: 'Enter' })
    })

    await waitFor(() =>
      expect(open).toHaveBeenCalledWith('https://google.com', '_blank', 'noopener,noreferrer')
    )
    expect(rendered.container.querySelector('iframe')).toBeNull()
  })

  // Honesty: the pane explains itself instead of painting an empty rectangle.
  it('explains that pages open in a browser tab rather than showing a blank canvas', () => {
    const rendered = renderBrowserPane()

    expect(rendered.getByText('Pages open in a browser tab')).toBeTruthy()
    expect(rendered.container.textContent).toContain('opens it in a new browser tab')
    // The Electron-only blank-page copy is the WRONG story here: it tells the
    // user to type an address into a pane that will never render one.
    expect(rendered.container.textContent).not.toContain('Type an address above')
  })

  // Accessibility: the fallback is a real, reachable action, not just prose.
  it('offers the current address as a named button that opens the tab', async () => {
    const openExternal = browserTab()
    const rendered = renderBrowserPane('https://google.com')

    // The same compact-url form the pane's own title uses.
    const action = rendered.getByRole('button', { name: 'Open google.com/ in a browser tab' })

    await act(async () => {
      fireEvent.click(action)
    })

    await waitFor(() => expect(openExternal).toHaveBeenCalledWith('https://google.com'))
  })

  // Every one of these acts THROUGH the guest. Leaving them mounted would be a
  // row of buttons that silently do nothing.
  it('hides the guest-only controls instead of leaving dead buttons', () => {
    const rendered = renderBrowserPane('https://google.com')

    expect(rendered.queryByRole('button', { name: 'Show preview console' })).toBeNull()
    expect(rendered.queryByRole('button', { name: 'Open preview DevTools' })).toBeNull()
    expect(rendered.queryByRole('button', { name: 'Annotate' })).toBeNull()
    expect(rendered.queryByRole('button', { name: 'Reload page' })).toBeNull()
    // History can only be reported by a guest, so it stays visibly disabled.
    expect((rendered.getByRole('button', { name: 'Back' }) as HTMLButtonElement).disabled).toBe(true)
    expect((rendered.getByRole('button', { name: 'Forward' }) as HTMLButtonElement).disabled).toBe(true)
  })

  // The address bar is the whole point of keeping the pane: it must stay,
  // normalize, and still refuse the schemes it always refused.
  it('keeps the address bar working and still rejects script-bearing schemes', async () => {
    const openExternal = browserTab()
    const rendered = renderBrowserPane()

    const address = rendered.getByRole('textbox', { name: 'Address' }) as HTMLInputElement

    await act(async () => {
      fireEvent.focus(address)
      fireEvent.change(address, { target: { value: 'javascript:alert(1)' } })
      fireEvent.keyDown(address, { key: 'Enter' })
    })

    expect(openExternal).not.toHaveBeenCalled()
  })

  // The address bar's own reach probe is Electron main's SSH-forward handler,
  // which does not exist here; the tab loads on the user's machine regardless.
  it('does not ask the missing Electron main process to reach the URL', async () => {
    const reachPreviewUrl = vi.fn(async (url: string) => url)
    $connection.set({ mode: 'remote' } as never)
    desktopWindow.hermesDesktop = {
      openExternal: vi.fn(async () => undefined),
      reachPreviewUrl
    } as unknown as Window['hermesDesktop']

    const rendered = renderBrowserPane()
    const address = rendered.getByRole('textbox', { name: 'Address' }) as HTMLInputElement

    await act(async () => {
      fireEvent.focus(address)
      fireEvent.change(address, { target: { value: 'google.com' } })
      fireEvent.keyDown(address, { key: 'Enter' })
    })

    expect(reachPreviewUrl).not.toHaveBeenCalled()
  })
})
