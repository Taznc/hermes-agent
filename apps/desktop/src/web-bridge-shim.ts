/**
 * web-bridge-shim.ts — SPIKE: browser stand-in for the Electron preload bridge.
 *
 * Loaded by index-web.html BEFORE /src/main.tsx so window.hermesDesktop exists
 * when the renderer's module graph evaluates. Derived from the boot-path
 * inventory in /tmp/web-desktop-spike/bridge-surface.md — only the members the
 * boot path + first paint require; every omission is deliberate (call sites
 * are optional-chained or feature-gated).
 *
 * UNTRACKED SPIKE FILE — not part of the app. Do not commit without review.
 */

// Self-contained minimal types (structural subsets of src/global.d.ts shapes;
// kept local so the shim never affects the app's module graph).
interface SpikeApiRequest {
  path: string
  method?: string
  body?: unknown
  profile?: string
  timeoutMs?: number
  upload?: { bytes: ArrayBuffer | Uint8Array; contentType?: string; filename: string }
}

// Injected at serve time by vite.config.web.ts `define` — real git provenance
// of the checkout being served (branch/commit/dirty).
declare const __HERMES_WEB_BUILD_INFO__: { branch: string; commit: string; dirty: boolean } | undefined

// ── Server wiring ──────────────────────────────────────────────────────────
// Same-origin: vite dev proxies /api (HTTP + WS) to the private loopback
// `hermes serve`. Token arrives via ?token= (scraped from the ungated serve's
// HTML by the launcher) and is stashed in sessionStorage so in-app navigation
// keeps it.
const BASE_URL = window.location.origin
// Token delivery: ?token= on first visit, then persisted in localStorage so
// later visits (any tab, after browser restart) need no query param. Scrub it
// from the address bar/history once stored. Spike-grade; behind Authelia.
const tokenFromUrl = new URLSearchParams(window.location.search).get('token')
if (tokenFromUrl) {
  localStorage.setItem('hermes-web-spike-token', tokenFromUrl)
  const scrubbed = new URL(window.location.href)
  scrubbed.searchParams.delete('token')
  window.history.replaceState(null, '', scrubbed)
}
const TOKEN =
  tokenFromUrl ??
  localStorage.getItem('hermes-web-spike-token') ??
  sessionStorage.getItem('hermes-web-spike-token') ??
  ''
const WS_URL = `${BASE_URL.replace(/^http/, 'ws')}/api/ws${TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : ''}`

const unsub = () => () => {}

function connection(profile?: string | null) {
  return {
    baseUrl: BASE_URL,
    // 'remote' routes fs/git/media through the gateway REST API
    // (src/lib/desktop-fs.ts) instead of the missing Electron bridge.
    mode: 'remote' as const,
    remoteKind: 'url' as const,
    authMode: 'token' as const,
    isFullscreen: false,
    nativeOverlayWidth: 0,
    windowButtonPosition: null,
    token: TOKEN,
    wsUrl: WS_URL,
    logs: [] as string[],
    ...(profile ? { profile } : {})
  }
}

const READY_BOOT = {
  error: null,
  fakeMode: false,
  message: 'Backend ready',
  phase: 'ready',
  progress: 100,
  running: false,
  timestamp: Date.now()
}

async function api<T>(request: SpikeApiRequest): Promise<T> {
  const url = new URL(request.path, BASE_URL)
  if (request.profile) url.searchParams.set('profile', request.profile)

  const headers: Record<string, string> = {}
  if (TOKEN) headers['X-Hermes-Session-Token'] = TOKEN

  let body: BodyInit | undefined
  if (request.upload) {
    const form = new FormData()
    const bytes = request.upload.bytes instanceof Uint8Array ? request.upload.bytes : new Uint8Array(request.upload.bytes)
    form.append(
      'file',
      new Blob([bytes], { type: request.upload.contentType ?? 'application/octet-stream' }),
      request.upload.filename
    )
    body = form
  } else if (request.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(request.body)
  }

  const controller = new AbortController()
  const timer = request.timeoutMs ? setTimeout(() => controller.abort(), request.timeoutMs) : null
  try {
    const res = await fetch(url, {
      method: request.method ?? (body ? 'POST' : 'GET'),
      headers,
      body,
      signal: controller.signal,
      credentials: 'include'
    })
    if (!res.ok) throw new Error(`Hermes API ${request.path} failed: ${res.status}`)
    const text = await res.text()
    return (text ? JSON.parse(text) : undefined) as T
  } finally {
    if (timer) clearTimeout(timer)
  }
}

const shim = {
  // ── boot path ────────────────────────────────────────────────────────────
  getConnection: async (profile?: string | null) => connection(profile),
  getBootProgress: async () => READY_BOOT,
  onBootProgress: unsub, // never fires — server is already up
  onBackendExit: unsub, // no child process to exit
  onConnectionApplied: unsub,
  onPowerResume: unsub,
  onWindowStateChanged: unsub,
  revalidateConnection: async () => ({ ok: true, rebuilt: false }),
  // Token mode: resolveGatewayWsUrl falls back to conn.wsUrl; keep mint cheap.
  getGatewayWsUrl: async (_profile?: null | string) => WS_URL,
  profile: {
    get: async () => ({ profile: null }),
    set: async (name: string | null) => ({ profile: name })
  },

  // ── data layer ───────────────────────────────────────────────────────────
  api,

  // ── first-render adjacents ───────────────────────────────────────────────
  onPreviewFileChanged: unsub,
  notify: async (_payload: unknown) => false,

  // ── recovery/error surfaces ──────────────────────────────────────────────
  // The boot-failure overlay calls these with `window.hermesDesktop?.method()`
  // — optional-chained on the OBJECT, not the method — so with a shim object
  // present they must exist or the error boundary trips on the recovery
  // surface itself (observed on the dev branch behind Traefik).
  getRecentLogs: async () => ({ path: '(web spike: no desktop.log)', lines: [] as string[] }),
  revealLogs: async () => ({ ok: false, path: '', error: 'not available in the web spike' }),
  reportRendererError: (_report: unknown) => {},

  // ── updates namespace ────────────────────────────────────────────────────
  // Present so startUpdatePoller() (store/updates.ts) runs: it's the only
  // caller of refreshDesktopVersion(), which populates $desktopVersion — the
  // input for About and the dev branch's fork-build statusbar marker.
  // supported:false is the designed "updates don't apply here" answer.
  updates: {
    check: async () => ({ supported: false, reason: 'web spike: updates are managed on the server' }),
    apply: async () => ({ ok: false, error: 'not available in the web spike' }),
    getBranch: async () => ({ branch: '' }),
    setBranch: async (_name: string) => ({ branch: '' }),
    onProgress: unsub
  },

  // ── cheap browser natives ────────────────────────────────────────────────
  openExternal: async (url: string) => {
    window.open(url, '_blank', 'noopener,noreferrer')
  },
  // writeClipboard deliberately OMITTED → installClipboardShim early-returns,
  // native navigator.clipboard stays in charge.
  readClipboard: async () => {
    try {
      return await navigator.clipboard.readText()
    } catch {
      return ''
    }
  },
  fetchLinkTitle: async (_url: string) => '',
  requestMicrophoneAccess: async () => {
    try {
      await navigator.mediaDevices.getUserMedia({ audio: true })
      return true
    } catch {
      return false
    }
  },
  claimAmbientCue: async (_key: string) => true,
  touchBackend: async () => ({ ok: true }),
  sanitizeWorkspaceCwd: async (cwd?: null | string) => ({ cwd: cwd ?? '', sanitized: false }),
  selectPaths: async () => [] as string[],
  saveImageFromUrl: async (_url: string) => false,
  getPathForFile: (_file: File) => '',
  getVersion: async () => {
    // Injected by vite.config.web.ts `define` (real git provenance of the
    // served checkout); absent if an older config serves this file.
    const info =
      typeof __HERMES_WEB_BUILD_INFO__ !== 'undefined'
        ? __HERMES_WEB_BUILD_INFO__
        : { branch: '', commit: '', dirty: false }

    return {
      appVersion: 'web-spike',
      electronVersion: '',
      nodeVersion: '',
      platform: 'web',
      hermesRoot: '',
      // Fork-build marker inputs (dev branch feature; harmless extras on main).
      buildSource: 'local',
      buildBranch: info.branch,
      buildCommit: info.commit,
      buildDirty: info.dirty
    }
  },

  // Module-init platform facts — force the browser answer, not the UA sniff.
  glassSupported: false,
  translucencySupported: false

  // OMITTED ON PURPOSE (consumers optional-chained/feature-gated): terminal,
  // git, petOverlay, hud, quickEntry, wakeIndicator, zoom, updates, uninstall,
  // themes, cloud, connections, settings, findInPage*, getBootstrapState/
  // onBootstrapEvent (must stay omitted TOGETHER), readFileDataUrl,
  // openSessionWindow/openWindow, writeClipboard, setActiveWork,
  // setTranslucency, battery, readDir/readFileText (remote mode → /api/fs/*),
  // watchPreviewFile, contextMenu*, oauth*/ssh*/connection-config surfaces.
} as const

;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = shim

export {}
