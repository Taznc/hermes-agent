/**
 * What a right-click landed on, resolved from the DOM.
 *
 * One resolver so every surface agrees on ownership. Order encodes priority:
 * an editable wins over the link wrapping it (the caret is where the user is
 * working), a link wins over the image inside it for the LINK section — the
 * image section still appears because the target carries both.
 */

export interface ContextMenuDomTarget {
  /** The enclosing dialog content node, when the click landed inside one. */
  dialogPortalContainer: HTMLElement | null
  /** The clicked editable, when the click landed in one. */
  editable: HTMLElement | null
  /** `href` of the enclosing anchor, as written (never absolutized). */
  linkUrl: string
  /** Source URL of the clicked image, when the click landed on one. */
  imageUrl: string
  /** True when the click landed on an `<img>` (imageUrl may still be empty
   *  for a broken image; Copy image works through coordinates either way). */
  onImage: boolean
  /** The live selection's text at the moment of the click. */
  selectionText: string
}

/** Form fields and `contenteditable` hosts. Mirrors the keybind helper, but
 *  returns the element so the menu can act on it. */
function editableFrom(element: Element | null): HTMLElement | null {
  if (!element) {
    return null
  }

  if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
    return element.disabled || element.readOnly ? null : element
  }

  const host = element.closest('[contenteditable]')

  return host instanceof HTMLElement && host.isContentEditable ? host : null
}

export function resolveDomTarget(element: Element | null): ContextMenuDomTarget {
  const anchor = element?.closest('a[href]')
  const dialogContent = element?.closest('[data-slot="dialog-content"]')
  const image = element?.closest('img')
  const linkUrl = anchor?.getAttribute('href')?.trim() ?? ''

  return {
    dialogPortalContainer: dialogContent instanceof HTMLElement ? dialogContent : null,
    editable: editableFrom(element),
    // A placeholder anchor is not a link the menu can act on.
    linkUrl: linkUrl === '#' ? '' : linkUrl,
    imageUrl: image instanceof HTMLImageElement ? image.currentSrc || image.src : '',
    onImage: Boolean(image),
    selectionText: window.getSelection()?.toString().trim() ?? ''
  }
}

/** True when `url` is something the in-app browser can render. */
export function isWebUrl(url: string): boolean {
  return /^https?:\/\//i.test(url)
}

/**
 * Whether Electron's main process will emit its own `context-menu` event for
 * this gesture — the sole reason it is ever safe to leave a `contextmenu`
 * event unprevented.
 *
 * Chromium reports spellcheck facts and image coordinates to the HOST
 * process only when the renderer's `contextmenu` event is NOT prevented, and
 * in Electron the app never calls `Menu.popup` on that report — so an
 * unprevented gesture there costs nothing and preserves the forward (see
 * `electron/main.ts`'s `context-menu` handler). In a plain browser tab there
 * is no host process: "unprevented" IS Chromium's own native context menu,
 * so leaving it unprevented paints Chromium's menu on top of this app's.
 *
 * The Electron preload bridge exposes `contextMenuEdit` (and its
 * `contextMenuSpellcheck`/`onContextMenuSpellcheck` siblings); the web
 * build's `web-bridge-shim.ts` deliberately omits all three (there is no
 * host-side edit command to route to — the browser handles editing
 * natively). Their presence is therefore the natural sentinel for "is the
 * Electron main-process context-menu bridge actually here".
 */
export function nativeContextMenuHandled(): boolean {
  return typeof window.hermesDesktop?.contextMenuEdit === 'function'
}
