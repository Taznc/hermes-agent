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

import { markWebReloadPending, registerNativeWebReload } from '@/store/web-reload'

// ── HMR full-reload trap (DEV only) ─────────────────────────────────────────
// Vite's built-in HMR client calls window.location.reload() directly whenever
// an edited module can't Fast Refresh (any file that also exports a
// non-component value — a store, an i18n locale file, a helper) and again on
// dev-server WebSocket reconnect. `vite:beforeFullReload` listeners cannot
// cancel that call — Vite notifies them and proceeds regardless — so the only
// real interception point is the browser API itself. Capture the native
// reload first (the "Refresh" statusbar item calls it back), then replace
// `location.reload` with a flag flip. Gated on DEV so this never ships in a
// production web build. See docs/web-ui-hard-refresh-diagnosis.md.
//
// This module itself gets RE-EVALUATED by Vite's own HMR (it's imported by
// index-web.html, and an edit anywhere upstream of it can trigger a fresh
// module graph load without a real page navigation). A second evaluation
// must not repeat the install:
//   1. `Object.defineProperty(window.location, 'reload', ...)` can throw
//      `TypeError: Cannot redefine property: reload` — some environments
//      expose `reload` as a non-configurable own property, and even a
//      `configurable: true` redefinition attempt on top of an existing
//      non-configurable descriptor is rejected. That throw happens BEFORE
//      `window.hermesDesktop = shim` runs at the bottom of this file, so an
//      unguarded throw here takes down the entire shim — every
//      `window.hermesDesktop?.xxx()` call site then reads `undefined` and
//      the app renders as fully crashed, which is strictly worse than the
//      individual reload call sites this trap is meant to fix.
//   2. Even when it doesn't throw, re-running `registerNativeWebReload`
//      would capture our OWN flag-flip function as "native" (since the
//      first pass already replaced `window.location.reload`), permanently
//      losing the real native reload the "Refresh" button depends on.
// A `window`-level flag (surviving across a fresh module instance, unlike a
// module-scoped variable) plus a try/catch guards both failure modes: if
// installation ever fails, native `reload()` is left alone for this session
// — HMR full-reloads act as they did before this whole feature (immediate,
// ungated hard refresh) rather than crashing the app.
const RELOAD_TRAP_FLAG = '__hermesWebReloadTrapInstalled'

if (import.meta.env.DEV && !(window as unknown as Record<string, unknown>)[RELOAD_TRAP_FLAG]) {
  try {
    registerNativeWebReload(window.location.reload.bind(window.location))
    Object.defineProperty(window.location, 'reload', {
      configurable: true,
      value: () => markWebReloadPending()
    })
    ;(window as unknown as Record<string, unknown>)[RELOAD_TRAP_FLAG] = true
  } catch (err) {
    console.warn(
      '[web-bridge-shim] could not trap window.location.reload; HMR full-reloads will navigate directly this session',
      err
    )
  }
}

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

// Composer images: the renderer hands the bridge raw bytes and expects a
// gateway-visible PATH back (attachments travel to the model as paths, not
// blobs). A browser can't write to disk, so the honest equivalent is to POST
// the bytes to the backend's existing chat image-upload route, which stores
// them under HERMES_HOME/images/ — the same directory clipboard.paste and
// image.attach already use — and returns the absolute path.
const IMAGE_MIME_BY_EXT: Record<string, string> = {
  '.bmp': 'image/bmp',
  '.gif': 'image/gif',
  '.jpeg': 'image/jpeg',
  '.jpg': 'image/jpeg',
  '.png': 'image/png',
  '.webp': 'image/webp'
}

// btoa() needs a binary string; String.fromCharCode(...bytes) blows the call
// stack on multi-MB screenshots, so fold in fixed-size chunks.
function bytesToBase64(bytes: Uint8Array): string {
  let binary = ''
  const CHUNK = 0x8000

  for (let offset = 0; offset < bytes.length; offset += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + CHUNK))
  }

  return btoa(binary)
}

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
      new Blob([Uint8Array.from(bytes)], { type: request.upload.contentType ?? 'application/octet-stream' }),
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

  // ── composer images ──────────────────────────────────────────────────────
  // Pasted/dropped image bytes → a real path on the gateway host. Called by
  // use-composer-actions.attachImageBlob with `?.` on the OBJECT, so a missing
  // method here throws "saveImageBuffer is not a function" and every paste
  // fails. Non-image extensions (the .html artifact-staging callers in
  // lib/local-preview.ts and preview-artifact.tsx) have no browser equivalent:
  // throw a clear error so their existing catch surfaces a real toast rather
  // than silently handing back a file:// URL this browser can never open.
  saveImageBuffer: async (data: ArrayBuffer | Uint8Array, ext: string) => {
    const bytes = data instanceof Uint8Array ? data : new Uint8Array(data)
    const raw = String(ext || '.png').trim().toLowerCase()
    const suffix = raw.startsWith('.') ? raw : `.${raw}`
    const mimeType = IMAGE_MIME_BY_EXT[suffix]

    if (!mimeType) {
      throw new Error(`Staging ${suffix} files to disk is not supported in the browser build`)
    }

    const result = await api<{ path?: string }>({
      path: '/api/chat/image-upload',
      method: 'POST',
      body: {
        data_url: `data:${mimeType};base64,${bytesToBase64(bytes)}`,
        filename: `pasted${suffix}`
      }
    })

    return result?.path ?? ''
  },

  // The server-side clipboard is the HOST's, not the browser user's, so
  // reading it would attach the wrong machine's image. The DOM paste event
  // already delivers real clipboard bytes to attachImageBlob; this only runs
  // as the composer's empty-paste fallback, where '' means "nothing to
  // attach" and is passed `{ silent: true }`.
  saveClipboardImage: async () => '',
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
  //
  // readFileDataUrl in particular MUST stay omitted: desktop-fs's
  // readDesktopFileDataUrlLocalFirst tries the bridge before the gateway, so
  // defining it would shadow the remote /api/fs/read-data-url read that makes
  // composer thumbnails work here.
} as const

;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = shim

export {}
