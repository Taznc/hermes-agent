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
  /** Text of the enclosing chat message, when the click landed in one. */
  messageText: string
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

/** The chat message the click landed in, if any. Assistant replies expose their
 *  rendered body; user prompts and system rows their root. All are `data-slot`
 *  hooks the transcript already stamps. */
const MESSAGE_BODY_SELECTOR =
  '[data-slot="aui_assistant-message-content"], [data-slot="aui_user-message-root"], [data-slot="aui_system-message-root"]'

/** A message root whose rendered text also carries chrome (timestamp, reaction
 *  badge, checkpoint controls) stamps the exact text to copy here, so Copy
 *  message yields the message and never its metadata. */
const MESSAGE_COPY_TEXT_ATTR = 'data-message-copy-text'

function messageTextFrom(body: Element | null | undefined): string {
  if (!(body instanceof HTMLElement)) {
    return ''
  }

  const stamped = body.getAttribute(MESSAGE_COPY_TEXT_ATTR)

  if (stamped !== null) {
    return stamped.trim()
  }

  // `innerText` respects rendered line breaks (so a copied reply keeps its
  // paragraphs); `textContent` is the fallback where layout is unavailable.
  return (body.innerText ?? body.textContent ?? '').trim()
}

export function resolveDomTarget(element: Element | null): ContextMenuDomTarget {
  const anchor = element?.closest('a[href]')
  const dialogContent = element?.closest('[data-slot="dialog-content"]')
  const image = element?.closest('img')
  const linkUrl = anchor?.getAttribute('href')?.trim() ?? ''
  const messageBody = element?.closest(MESSAGE_BODY_SELECTOR)

  return {
    dialogPortalContainer: dialogContent instanceof HTMLElement ? dialogContent : null,
    editable: editableFrom(element),
    // A placeholder anchor is not a link the menu can act on.
    linkUrl: linkUrl === '#' ? '' : linkUrl,
    imageUrl: image instanceof HTMLImageElement ? image.currentSrc || image.src : '',
    messageText: messageTextFrom(messageBody),
    onImage: Boolean(image),
    selectionText: window.getSelection()?.toString().trim() ?? ''
  }
}

/** True when `url` is something the in-app browser can render. */
export function isWebUrl(url: string): boolean {
  return /^https?:\/\//i.test(url)
}
