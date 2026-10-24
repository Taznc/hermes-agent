/** Fork-owned (web desktop only). Fit the browser-hosted shell to the visible
 *  area on phones; loaded by index-web.html before the app bundle. Port of
 *  upstream PR #141864's `lib/browser-viewport.ts` (retire when it lands).
 *
 *  `interactive-widget=resizes-content` (index-web.html viewport meta) handles
 *  browsers that support it; VisualViewport covers the rest. Pan/zoom stays
 *  with the browser: a pinch must not turn its smaller visible area into a
 *  smaller application layout. */
import './web-viewport.css'

export function installBrowserViewport(win: Window = window): () => void {
  const viewport = win.visualViewport
  const doc = win.document
  const root = doc.getElementById('root')

  if (!viewport || !root || doc.documentElement.dataset.hermesDesktopHost !== 'browser') {
    return () => {}
  }

  let frame = 0
  let unzoomedTop = 0

  const reset = () => {
    root.removeAttribute('data-browser-viewport')
    root.style.removeProperty('--browser-viewport-height')
    root.style.removeProperty('--browser-viewport-top')
  }

  const update = () => {
    frame = 0

    // Remove pinch magnification so the keyboard still updates available
    // space while zoomed. No focus(), scrollIntoView() or zoom changes here.
    const height = viewport.height * viewport.scale

    if (!Number.isFinite(height) || height <= 0) {
      return
    }

    // iOS Safari reports the visible area as innerHeight, zoomed or not, and
    // scrolls the focused field into view itself; fitting under that scroll
    // would shrink the app out of the visible area.
    if (Math.abs(viewport.height - win.innerHeight) < 1) {
      unzoomedTop = 0
      reset()

      return
    }

    // Offset during a pinch belongs to the user's pan, not the app shell.
    if (Math.abs(viewport.scale - 1) < 0.01) {
      unzoomedTop = Math.max(0, viewport.offsetTop)
    }

    const top = Math.min(unzoomedTop, Math.max(0, win.innerHeight - height))

    if (Math.abs(height - win.innerHeight) < 1 && top < 1) {
      unzoomedTop = 0
      reset()

      return
    }

    // Hermes' UI scale is CSS zoom on <html>, separate from pinch zoom.
    const zoom = Number.parseFloat(win.getComputedStyle(doc.documentElement).zoom) || 1
    root.style.setProperty('--browser-viewport-height', `${height / zoom}px`)
    root.style.setProperty('--browser-viewport-top', `${top / zoom}px`)
    root.setAttribute('data-browser-viewport', '')
  }

  const schedule = () => {
    if (!frame) {
      frame = win.requestAnimationFrame(update)
    }
  }

  viewport.addEventListener('resize', schedule)
  viewport.addEventListener('scroll', schedule)
  win.addEventListener('resize', schedule)
  update()

  return () => {
    win.cancelAnimationFrame(frame)
    viewport.removeEventListener('resize', schedule)
    viewport.removeEventListener('scroll', schedule)
    win.removeEventListener('resize', schedule)
    reset()
  }
}

if (typeof window !== 'undefined' && !import.meta.env?.VITEST) {
  installBrowserViewport()
}
