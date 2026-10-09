// Browser Desktop's standalone-plugin removal door. Keep filesystem authority
// on the authenticated backend; the renderer sends only a folder name.
interface RemovalResult {
  ok: boolean
  error?: string
  path?: string
}

interface ApiRequest {
  path: string
  method: 'POST'
  body: { name: string }
}

export function createWebDesktopPluginRemoval(
  api: <T>(request: ApiRequest) => Promise<T>,
  bridgeApi: string,
  failureMessage: (error: unknown) => string
): (payload: { name: string }) => Promise<RemovalResult> {
  return async payload => {
    try {
      return await api<RemovalResult>({
        path: `${bridgeApi}/desktop-remove`,
        method: 'POST',
        body: { name: payload.name }
      })
    } catch (error) {
      return { ok: false, error: failureMessage(error) }
    }
  }
}
