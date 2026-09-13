/**
 * An Electron-shaped `<webview>` guest for jsdom.
 *
 * jsdom is the WEB build's runtime shape: `<webview>` stays an inert unknown
 * element, so `previewGuestSupported()` is correctly false there and the pane
 * takes its no-guest branch. Tests that mean to exercise the ELECTRON branch
 * have to make the element real first.
 *
 * Electron upgrades the tag at creation — `document.createElement('webview')`
 * hands back an element that already carries `loadURL` — and it does NOT go
 * through `customElements`, which would reject the hyphen-less name anyway.
 * So this patches creation the same way, and the pane's capability probe then
 * runs for real against the element instead of being mocked away.
 *
 * The element is a genuine `<webview>` node, so `querySelector('webview')`,
 * `appendChild`, and event dispatch all behave as before. Only `loadURL` is
 * pre-installed (it is what the probe reads); every other guest method a test
 * needs is assigned onto the instance by that test, exactly as before.
 *
 * Module scope, once per file — the patch is idempotent and is not undone,
 * because a test file that installs a guest wants it for every test in it.
 */
export function installWebviewGuest(): void {
  const doc = document as Document & { __hermesWebviewGuest?: true }

  if (doc.__hermesWebviewGuest) {
    return
  }

  const create = doc.createElement.bind(doc)

  doc.createElement = ((tag: string, options?: ElementCreationOptions) => {
    const element = create(tag, options)

    if (tag.toLowerCase() === 'webview') {
      Object.assign(element, { loadURL: () => Promise.resolve() })
    }

    return element
  }) as Document['createElement']

  doc.__hermesWebviewGuest = true
}
