/**
 * web-bridge-shim.ts — browser stand-in for the Electron preload bridge.
 *
 * Loaded by index-web.html BEFORE /src/main.tsx so window.hermesDesktop exists
 * when the renderer's module graph evaluates.
 *
 * SUPPORT LEVEL: this is production code. It began as a spike ("only the
 * members the boot path + first paint require") and its header still said
 * "UNTRACKED SPIKE FILE — not part of the app" long after it became the
 * bridge every web-served Desktop user actually runs. Treat it as shipped:
 * typed, tested, reviewed like any other renderer module.
 *
 * WHY OMISSIONS ARE NOT SELF-JUSTIFYING: the original omissions were judged
 * against one question — does the app boot and paint? — so a missing member
 * is NOT evidence that the feature shouldn't exist on web; it usually means
 * nobody evaluated it. Because nearly every call site is optional-chained,
 * the failure mode is silent: a dead control or a permanently-empty list,
 * never a crash. A 2026-09 audit found the Electron preload exposing 134
 * members against 46 here, and several user-visible features dead purely for
 * that reason while their backend routes returned 200.
 *
 * BEFORE ADDING A MEMBER: the shim reports `mode: 'remote'`, so
 * `isDesktopFsRemoteMode()` is true and the fs/git surface already routes to
 * the gateway's REST API (see desktop-fs.ts / desktop-git.ts). Check for an
 * existing `/api/*` route before designing anything; most gaps are a thin
 * wrapper over one, using the `api()` helper below for token + timeout parity.
 *
 * DELIBERATELY ABSENT (verified 2026-09; do not "fix" these): native window
 * management and pop-outs, tray/dock, HUD, pet overlay, wake indicator, zoom,
 * quick entry, keep-awake, deep links, battery, native context menus and
 * spellcheck, find-in-page, installer/bootstrap, relaunch/uninstall, and
 * recycleBackend. A browser has no equivalent, and their call sites are
 * capability-gated so the affordance is hidden rather than broken. Gateway
 * settings, rename/delete/reveal, and clipboard were likewise verified to
 * degrade correctly and need no work here.
 *
 * Omitting a member is a legitimate choice — but make it an explicit one, and
 * make sure the UI hides the affordance instead of offering something that
 * silently fails. See ROADMAP.md "Phase 2.17 — Web-served Desktop bridge
 * parity" for the audit and the outstanding gaps.
 */

import { markWebReloadPending, registerNativeWebReload } from '@/store/web-reload'

import { type AgentOverview, createAgentOverviewReader } from '../electron/agent-overview'

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

interface SpikeReadDirEntry {
  isDirectory: boolean
  name: string
  path: string
}

interface SpikeReadDirResult {
  entries: SpikeReadDirEntry[]
  error?: string
}

interface SpikeReadFileTextResult {
  binary?: boolean
  byteSize?: number
  language?: string
  mimeType?: string
  path: string
  text: string
  truncated?: boolean
}

// Structural subset of HermesSelectPathsOptions (src/global.d.ts).
interface SpikeSelectPathsOptions {
  title?: string
  defaultPath?: string
  directories?: boolean
  multiple?: boolean
  profile?: string
  filters?: Array<{ name: string; extensions: string[] }>
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

// ── Click-to-upload / drop staging for non-image files ──────────────────────
// The '+' menu's Files action and OS drag/drop both need a way to turn browser
// File bytes into a gateway-visible path — Electron's equivalents (native
// dialog + webUtils.getPathForFile) have no browser counterpart. Stage the
// bytes through the backend's generic chat file-upload route (mirrors the
// image-upload route above, but accepts any content and stores under
// HERMES_HOME/uploads/) and hand back the absolute path; file.attach/
// image.attach_bytes read it back from there like any other local pick.
//
// The staged path's basename is an internal name (timestamp/hash-prefixed —
// see upload_chat_file in hermes_cli/web_server.py), not the name the user
// picked or dropped. Remember that original name here, keyed by the staged
// path, so the composer chip can show it instead of the internal basename
// (getStagedDisplayName below). Electron never populates this map — it always
// attaches a real local path and derives the label from that directly.
const stagedFileDisplayNames = new Map<string, string>()

async function uploadFileBuffer(bytes: Uint8Array, filename: string, mimeType?: string): Promise<string> {
  const result = await api<{ path?: string }>({
    path: '/api/chat/file-upload',
    method: 'POST',
    body: {
      data_url: `data:${mimeType || 'application/octet-stream'};base64,${bytesToBase64(bytes)}`,
      filename: filename || 'upload'
    }
  })

  const path = result?.path ?? ''

  if (path && filename) {
    stagedFileDisplayNames.set(path, filename)
  }

  return path
}

async function uploadPickedFile(file: File): Promise<string> {
  const buffer = await file.arrayBuffer()

  return uploadFileBuffer(new Uint8Array(buffer), file.name, file.type)
}

function acceptAttrFromFilters(filters?: Array<{ name: string; extensions: string[] }>): string {
  if (!filters?.length) {
    return ''
  }

  return filters
    .flatMap(filter => filter.extensions)
    .filter(Boolean)
    .map(ext => `.${ext.replace(/^\./, '')}`)
    .join(',')
}

// Drives a throwaway <input type=file> to get real File handles out of the
// browser (the only picker surface a web page has). There is no 'cancel'
// event on <input type=file>; the standard workaround is to treat the
// window regaining focus after the native dialog closes as "done" — the
// 'change' event (when files WERE picked) fires before that focus event in
// every evergreen browser, and the settled guard makes the race harmless
// either way.
function pickBrowserFiles(options?: { multiple?: boolean; filters?: Array<{ extensions: string[]; name: string }> }): Promise<File[]> {
  return new Promise(resolve => {
    const input = document.createElement('input')

    input.type = 'file'
    input.style.position = 'fixed'
    input.style.top = '-1000px'
    input.style.left = '-1000px'

    if (options?.multiple !== false) {
      input.multiple = true
    }

    const accept = acceptAttrFromFilters(options?.filters)

    if (accept) {
      input.accept = accept
    }

    let settled = false

    const finish = (files: File[]) => {
      if (settled) {
        return
      }

      settled = true
      window.removeEventListener('focus', onWindowFocus)
      input.remove()
      resolve(files)
    }

    const onWindowFocus = () => {
      // The native picker's own focus-return races the 'change' event in a
      // few browsers; give 'change' a beat to win before treating this as a
      // cancel.
      setTimeout(() => finish(input.files ? Array.from(input.files) : []), 300)
    }

    input.addEventListener('change', () => finish(input.files ? Array.from(input.files) : []))
    window.addEventListener('focus', onWindowFocus)
    document.body.appendChild(input)
    input.click()
  })
}

// One backend: the same-origin `hermes serve` this shim's api() helper talks
// to. `pooled` is pre-populated with a single non-empty descriptor array so
// collectAgentOverview() short-circuits both the `connect()` round-trip and
// the on-demand `discoverParked` branch — there's nothing to discover, this
// IS the backend. The reader instance (and its internal 60s history cache)
// is created once per module evaluation, mirroring Electron's module-scoped
// `readAgentOverview` singleton in electron/main.ts.
const AGENT_OVERVIEW_SOURCE = { id: 'web', label: 'This backend', kind: 'local' } as const
const AGENT_OVERVIEW_POOLED = new Map<string, string[]>([[AGENT_OVERVIEW_SOURCE.id, ['web']]])
const readAgentOverview = createAgentOverviewReader<string>()

async function getAgentOverview(options?: { force?: boolean }): Promise<AgentOverview> {
  const overview = await readAgentOverview(
    {
      sources: [AGENT_OVERVIEW_SOURCE],
      pooled: AGENT_OVERVIEW_POOLED,
      connect: async () => [],
      fetch: (_descriptor, path) => api({ path })
    },
    { force: options?.force }
  )

  const [source] = overview.sources

  // collectAgentOverview() folds every per-descriptor failure into a
  // resolved, degraded source (state/errors) instead of rejecting — correct
  // for Electron, where a multi-source overview must keep painting sources
  // that DID answer. This shim has exactly one source, so when that source
  // is 'offline' (never obtained any history: non-2xx, a rejected fetch, or
  // the collector's own per-source budget expiring) there is nothing else to
  // show. Reject so store/agent-overview.ts's existing ErrorState path
  // fires, instead of the renderer painting a false "All quiet". Genuine
  // partial/compatibility states ('partial', 'unsupported', 'on-demand')
  // keep resolving, matching Electron's degraded-but-nonempty behavior.
  if (source?.state === 'offline') {
    throw new Error(source.error ?? source.errors[0]?.error ?? 'Agent overview backend unavailable.')
  }

  return overview
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

// ── VS Code Marketplace theme search (themes.searchMarketplace) ────────────
// Electron's counterpart (electron/vscode-marketplace.ts) runs this same
// query from the MAIN process; the gallery API sends
// `Access-Control-Allow-Origin: *` (verified live), so the browser can call
// it directly with no backend proxy and no new /api/* route. Mirrors
// searchMarketplaceThemes()'s filters/flags/icon-theme exclusion exactly so
// results match the Electron build byte-for-byte.
//
// themes.fetchMarketplace (theme INSTALL — downloading + unzipping a .vsix)
// is deliberately NOT implemented here: it needs to parse an untrusted zip
// and raw-deflate-inflate its entries client-side, a materially bigger and
// security-sensitive lift than this card's slice. src/themes/install.ts
// already throws a clear, visible "only available in the desktop app" error
// when this member is absent — an honest gap, not a silent one. Followup
// filed for install (t_d40923b6 completion metadata).
interface MarketplaceSearchItem {
  extensionId: string
  displayName: string
  publisher: string
  description: string
  installs: number
}

const GALLERY_QUERY_URL = 'https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery'

function looksLikeIconTheme(extension: {
  tags?: unknown
  displayName?: unknown
  shortDescription?: unknown
}): boolean {
  const tags = (Array.isArray(extension.tags) ? extension.tags : []).map(tag => String(tag).toLowerCase())

  if (tags.includes('icon-theme') || tags.includes('product-icon-theme')) {
    return true
  }

  const text = `${extension.displayName ?? ''} ${extension.shortDescription ?? ''}`.toLowerCase()

  return /\b(icon theme|file icons?|product icons?|icon pack|fileicons)\b/.test(text)
}

async function searchMarketplaceThemes(query: string): Promise<MarketplaceSearchItem[]> {
  const trimmedQuery = String(query || '').trim()
  const pageSize = 20

  // FilterType: 8=Target, 5=Category, 10=SearchText, 12=ExcludeWithFlags.
  const criteria: Array<{ filterType: number; value: string }> = [
    { filterType: 8, value: 'Microsoft.VisualStudio.Code' },
    { filterType: 5, value: 'Themes' },
    { filterType: 12, value: '4096' } // Exclude unpublished (Unpublished = 0x1000).
  ]

  if (trimmedQuery) {
    criteria.push({ filterType: 10, value: trimmedQuery })
  }

  const res = await fetch(GALLERY_QUERY_URL, {
    method: 'POST',
    headers: {
      Accept: 'application/json;api-version=3.0-preview.1',
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      // Over-fetch so the icon-theme filter below still leaves a full page.
      filters: [{ criteria, pageNumber: 1, pageSize: Math.min(pageSize * 2, 50), sortBy: 4, sortOrder: 0 }],
      // IncludeStatistics (0x100) | IncludeLatestVersionOnly (0x200) | IncludeCategoryAndTags (0x4).
      flags: 772
    })
  })

  if (!res.ok) {
    throw new Error(`VS Code Marketplace search failed: ${res.status}`)
  }

  const responseText = await res.text()
  const json = responseText ? JSON.parse(responseText) : {}
  const extensions: Array<Record<string, unknown>> = json?.results?.[0]?.extensions ?? []

  return extensions
    .filter(extension => !looksLikeIconTheme(extension))
    .slice(0, pageSize)
    .map(extension => {
      const publisher = (extension.publisher ?? {}) as { publisherName?: string; displayName?: string }
      const publisherName = publisher.publisherName ?? ''
      const stats = Array.isArray(extension.statistics) ? extension.statistics : []

      const installStat = stats.find(
        (stat): stat is { statisticName?: string; value?: number } =>
          Boolean(stat) && typeof stat === 'object' && (stat as { statisticName?: string }).statisticName === 'install'
      )

      return {
        extensionId: `${publisherName}.${extension.extensionName as string}`,
        displayName: (extension.displayName as string) || (extension.extensionName as string),
        publisher: publisher.displayName || publisherName,
        description: (extension.shortDescription as string) || '',
        installs: Math.round(installStat?.value ?? 0)
      }
    })
}

// ── Local Models plumbing (localModelsEnabled) ──────────────────────────────
// Electron's member is a LAUNCH-FLAG fact (--local), deliberately decoupled
// from whether the backend actually has local models configured — see the
// doc comment on $localModelsEnabled in store/local-models-flag.ts. The web
// build has no launch-flag equivalent (no separate opt-in build a user
// starts), so the only honest signal here is the backend route itself:
// GET /api/local-models/status -> { enabled: boolean, ... }.
//
// $localModelsEnabled reads window.hermesDesktop?.localModelsEnabled exactly
// ONCE at its own module-import time (matching Electron's "can't change
// mid-session" contract), so mutating this bridge field after the fact does
// nothing on its own — the correction below updates the store directly, the
// same thing a real launch-flag reader would have produced had it been true
// from the start. Safe default (false) until the async read resolves; a
// rejecting fetch leaves the store at that safe default rather than throwing
// unhandled.
async function correctLocalModelsEnabledFlag(): Promise<void> {
  try {
    const status = await api<{ enabled?: boolean }>({ path: '/api/local-models/status' })

    if (status?.enabled) {
      const { $localModelsEnabled } = await import('@/store/local-models-flag')

      $localModelsEnabled.set(true)
    }
  } catch (err) {
    console.warn('[web-bridge-shim] could not read /api/local-models/status; local models stay hidden', err)
  }
}

async function api<T>(request: SpikeApiRequest): Promise<T> {
  const url = new URL(request.path, BASE_URL)

  if (request.profile) {url.searchParams.set('profile', request.profile)}

  const headers: Record<string, string> = {}

  if (TOKEN) {headers['X-Hermes-Session-Token'] = TOKEN}

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

  // Mirror electron/hardening.ts DEFAULT_FETCH_TIMEOUT_MS: Electron main
  // clamps every hermesApi call to a 30s fallback even when the caller sets
  // no timeoutMs. This shim used to only arm the abort timer when timeoutMs
  // was explicitly set, so on the web-served desktop the ~60 api/*.ts exports
  // with no timeoutMs had NO ceiling at all — a stalled socket never
  // rejected, so useQuery's isError never flipped and the panel spun forever
  // with no retry affordance. A per-call timeoutMs now only RAISES the
  // budget above this default, matching Electron semantics exactly.
  const DEFAULT_FETCH_TIMEOUT_MS = 30_000
  const timeoutMs = request.timeoutMs ?? DEFAULT_FETCH_TIMEOUT_MS
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)

  try {
    const res = await fetch(url, {
      method: request.method ?? (body ? 'POST' : 'GET'),
      headers,
      body,
      signal: controller.signal,
      credentials: 'include'
    })

    if (!res.ok) {throw new Error(`Hermes API ${request.path} failed: ${res.status}`)}
    const text = await res.text()

    return (text ? JSON.parse(text) : undefined) as T
  } finally {
    clearTimeout(timer)
  }
}

// ── OS/browser notifications ────────────────────────────────────────────────
// Electron's real bridge shows notifications via `new Notification()` in the
// main process and wires click/action back over IPC (hermes:focus-session,
// hermes:notification-action, hermes:notification-activate). This is the
// browser-native equivalent:
//   - action buttons require a ServiceWorkerRegistration.showNotification()
//     (the plain `new Notification()` constructor silently ignores `actions`
//     in every browser), so a tiny SW (public/notification-sw.js) is
//     registered lazily and relays 'notificationclick' back via postMessage.
//   - without a SW (registration fails, or the browser lacks SW support at
//     all — e.g. Safari), degrade to a body-click-only Notification — still
//     useful, just without buttons.
//   - permission is asked for on first real (non-test) notify() attempt, not
//     eagerly at load: an unprompted permission popup on first paint is a
//     bad first impression and most browsers rate-limit/auto-deny silent
//     permission requests made outside a user gesture.
interface NotifyPayload {
  actions?: { id: string; text: string; activate?: string }[]
  activate?: string
  body?: string
  icon?: string
  kind?: string
  notifyId?: string
  sessionId?: string
  silent?: boolean
  tag?: string
  title?: string
}

interface NotificationActionPayload {
  actionId: string
  sessionId?: string
}

interface NotificationActivatePayload {
  actionId?: string
  activate?: string
  notifyId?: string
  tag?: string
}

type FocusSessionCallback = (sessionId: string) => void

const focusSessionCallbacks = new Set<FocusSessionCallback>()
const notificationActionCallbacks = new Set<(payload: NotificationActionPayload) => void>()
const notificationActivateCallbacks = new Set<(payload: NotificationActivatePayload) => void>()

function subscribe<T>(set: Set<T>, callback: T): () => void {
  set.add(callback)

  return () => set.delete(callback)
}

// Collapse duplicate kind+session/tag notifications the same way Electron's
// isDuplicateNotification() does for multiple full windows — here it guards
// against the renderer's own per-tab throttle racing a second browser tab
// open on the same origin.
const DEDUPE_WINDOW_MS = 4000
const recentlyShown = new Map<string, number>()

function isDuplicate(key: string): boolean {
  const now = Date.now()

  for (const [k, at] of recentlyShown) {
    if (now - at >= DEDUPE_WINDOW_MS) {
      recentlyShown.delete(k)
    }
  }

  if (recentlyShown.has(key)) {
    return true
  }

  recentlyShown.set(key, now)

  return false
}

let swRegistrationPromise: Promise<ServiceWorkerRegistration | null> | null = null

// Route relayed SW clicks into the same callback sets onFocusSession /
// onNotificationAction / onNotificationActivate feed from the Electron path,
// so use-desktop-integrations.ts needs no web-specific branch at all.
function handleServiceWorkerMessage(event: MessageEvent) {
  const message = event.data as { actionId?: string; data?: NotifyPayload; source?: string } | undefined

  if (!message || message.source !== 'hermes-notification-sw') {
    return
  }

  const payload = message.data ?? {}
  const actionId = message.actionId

  if (!actionId && payload.sessionId) {
    for (const cb of focusSessionCallbacks) {cb(payload.sessionId)}

    return
  }

  // Approvals keep the session-scoped action channel — mirrors main.ts's
  // 'action' handler: sessionId present, no notifyId/activate → approval.
  if (actionId && payload.sessionId && !payload.notifyId && !payload.activate) {
    for (const cb of notificationActionCallbacks) {cb({ actionId, sessionId: payload.sessionId })}

    return
  }

  const action = payload.actions?.find(a => a.id === actionId)

  for (const cb of notificationActivateCallbacks) {
    cb({
      actionId,
      activate: action?.activate ?? payload.activate,
      notifyId: payload.notifyId,
      tag: payload.tag
    })
  }

  if (payload.sessionId) {
    for (const cb of focusSessionCallbacks) {cb(payload.sessionId)}
  }
}

async function ensureNotificationServiceWorker(): Promise<ServiceWorkerRegistration | null> {
  if (!('serviceWorker' in navigator)) {
    return null
  }

  if (!swRegistrationPromise) {
    swRegistrationPromise = navigator.serviceWorker
      .register('/notification-sw.js')
      .catch(err => {
        console.warn('[web-bridge-shim] notification service worker registration failed', err)

        return null
      })

    navigator.serviceWorker.addEventListener('message', handleServiceWorkerMessage)
  }

  return swRegistrationPromise
}

async function getNotificationPermission(): Promise<'granted' | 'denied' | 'default' | 'unsupported'> {
  if (typeof Notification === 'undefined') {
    return 'unsupported'
  }

  return Notification.permission
}

async function requestNotificationPermission(): Promise<'granted' | 'denied' | 'default' | 'unsupported'> {
  if (typeof Notification === 'undefined') {
    return 'unsupported'
  }

  if (Notification.permission !== 'default') {
    return Notification.permission
  }

  try {
    return await Notification.requestPermission()
  } catch {
    return Notification.permission
  }
}

async function notify(payload: NotifyPayload): Promise<boolean> {
  if (typeof Notification === 'undefined') {
    return false
  }

  if (isDuplicate(`${payload.kind ?? ''}:${payload.sessionId ?? payload.tag ?? ''}`)) {
    return true
  }

  const permission = await requestNotificationPermission()

  if (permission !== 'granted') {
    return false
  }

  const actions = Array.isArray(payload.actions) ? payload.actions : []
  const data: NotifyPayload = { ...payload }

  const registration = await ensureNotificationServiceWorker()

  if (registration && 'showNotification' in registration) {
    try {
      await registration.showNotification(payload.title || 'Hermes', {
        body: payload.body || '',
        silent: Boolean(payload.silent),
        ...(payload.icon ? { icon: payload.icon } : {}),
        tag: payload.tag || payload.sessionId || undefined,
        data,
        // Cast: TS DOM lib's NotificationOptions omits `actions` in some lib
        // targets even though every evergreen browser (and the spec) supports
        // it on SW-shown notifications.
        ...(actions.length ? { actions: actions.map(a => ({ action: a.id, title: a.text })) } : {})
      } as NotificationOptions)

      return true
    } catch (err) {
      console.warn('[web-bridge-shim] showNotification via service worker failed, falling back', err)
    }
  }

  // No SW (unsupported browser, or registration/show failed): plain
  // Notification still delivers title/body/click, just without buttons.
  try {
    const plain = new Notification(payload.title || 'Hermes', {
      body: payload.body || '',
      silent: Boolean(payload.silent),
      ...(payload.icon ? { icon: payload.icon } : {}),
      tag: payload.tag || payload.sessionId || undefined
    })

    plain.onclick = () => {
      window.focus()
      plain.close()

      if (payload.sessionId) {
        for (const cb of focusSessionCallbacks) {cb(payload.sessionId)}
      }

      if (payload.activate || payload.notifyId) {
        for (const cb of notificationActivateCallbacks) {
          cb({ activate: payload.activate, notifyId: payload.notifyId, tag: payload.tag })
        }
      }
    }

    return true
  } catch (err) {
    console.warn('[web-bridge-shim] Notification constructor failed', err)

    return false
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

  // Sessions/agents overview (Agents → Sessions tab). Single-backend web
  // build: one source (`AGENT_OVERVIEW_SOURCE`), the same-origin server
  // this shim already talks to via api(). Reuses the pure collector from
  // electron/agent-overview.ts — same pagination, 60s history cache, and
  // missingCapability() 404/405/501 fallback Electron's IPC handler uses.
  getAgentOverview,

  // ── data layer ───────────────────────────────────────────────────────────
  api,

  // ── disk-plugin door (proxied over /api/fs/*) ───────────────────────────
  // contrib/runtime-loader.ts's diskRoots() calls desktopPluginsRoot()/
  // agentPluginsRoot() to discover the two on-disk plugin scan roots
  // (<hermes home>/desktop-plugins/*, <hermes home>/plugins/*/desktop/) and
  // then readDir()/readPluginSource() (readFileText() on older shells) to
  // walk and load them. Without these members diskRoots() short-circuits to
  // [] and NO on-disk plugin — including account-limits — ever loads in this
  // build. There's no Electron main process here to resolve <hermes home> or
  // touch the filesystem directly, so every member proxies through the
  // backend's /api/fs/* gateway REST routes, the same seam desktop-fs.ts's
  // remote-mode branch already uses for the editor/preview file surfaces.
  desktopPluginsRoot: async () => (await api<{ path: string }>({ path: '/api/fs/desktop-plugins-root' })).path,
  agentPluginsRoot: async () => (await api<{ path: string }>({ path: '/api/fs/agent-plugins-root' })).path,
  readDir: async (dirPath: string) =>
    api<SpikeReadDirResult>({ path: `/api/fs/list?path=${encodeURIComponent(dirPath)}` }),
  readFileText: async (filePath: string) =>
    api<SpikeReadFileTextResult>({ path: `/api/fs/read-text?path=${encodeURIComponent(filePath)}` }),
  // Full-source, non-truncating read — runtime-loader.ts prefers this over
  // readFileText for evaluating plugin.js (readFileText silently truncates at
  // 512 KiB, which would evaluate half a module).
  readPluginSource: async (filePath: string) =>
    api<SpikeReadFileTextResult>({ path: `/api/fs/read-plugin-source?path=${encodeURIComponent(filePath)}` }),

  // ── first-render adjacents ───────────────────────────────────────────────
  onPreviewFileChanged: unsub,
  notify,
  getNotificationPermission,
  requestNotificationPermission,
  onFocusSession: (callback: FocusSessionCallback) => subscribe(focusSessionCallbacks, callback),
  onNotificationAction: (callback: (payload: NotificationActionPayload) => void) =>
    subscribe(notificationActionCallbacks, callback),
  onNotificationActivate: (callback: (payload: NotificationActivatePayload) => void) =>
    subscribe(notificationActivateCallbacks, callback),

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
  selectPaths: async (options?: SpikeSelectPathsOptions) => {
    if (options?.directories) {
      // No File System Access API fallback attempted here — the in-app
      // RemoteFolderPicker dialog (browsed over the gateway's REST fs API)
      // already handles directory selection for the web build instead of
      // calling this bridge method. See selectDesktopPaths in
      // lib/desktop-fs.ts, which only reaches this branch for a file pick.
      return [] as string[]
    }

    const files = await pickBrowserFiles({ filters: options?.filters, multiple: options?.multiple !== false })
    const paths: string[] = []

    for (const file of files) {
      try {
        paths.push(await uploadPickedFile(file))
      } catch (err) {
        console.warn('[web-bridge-shim] selectPaths: could not stage picked file', file.name, err)
      }
    }

    return paths.filter(Boolean)
  },
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

  // Web-build counterpart to Electron's real filesystem path: the '+' Files
  // action and OS drops for NON-image files have raw browser bytes and no
  // local path (see selectPaths/getPathForFile above). Optional on the
  // HermesDesktop type — Electron never defines it since it always has a
  // real path already — so use-composer-actions.attachFileBlob checks for
  // it explicitly rather than relying on `?.` (same partial-shim trap
  // saveImageBuffer exists to close for images).
  saveFileBuffer: async (data: ArrayBuffer | Uint8Array, filename: string) => {
    const bytes = data instanceof Uint8Array ? data : new Uint8Array(data)

    return uploadFileBuffer(bytes, filename || 'upload')
  },

  // Web build only: the composer chip label should show the name the user
  // picked/dropped, not the staged path's internal timestamp/hash basename.
  // saveFileBuffer/selectPaths record the mapping as they stage each file;
  // attachContextFilePath (use-composer-actions.ts) reads it back here to
  // label the chip. Undefined on Electron — real local paths already carry
  // their true name in the basename, so pathLabel(path) is correct there.
  getStagedDisplayName: (path: string) => stagedFileDisplayNames.get(path),

  // The server-side clipboard is the HOST's, not the browser user's, so
  // reading it would attach the wrong machine's image. The DOM paste event
  // already delivers real clipboard bytes to attachImageBlob; this only runs
  // as the composer's empty-paste fallback, where '' means "nothing to
  // attach" and is passed `{ silent: true }`.
  saveClipboardImage: async () => '',

  // ── connection-config surfaces (reachable, unsupported) ─────────────────
  // The profile rail's remote-override dialog and the Settings → Connections
  // registry editor both mount in the web build and call these UNGUARDED on
  // the object (`window.hermesDesktop.applyConnectionConfig(...)`), so a
  // partial shim throws "... is not a function" at click time. Connection and
  // OAuth state live in the Electron main process's desktop config — a
  // browser tab has no equivalent, and faking a success shape would make the
  // dialogs report a save/sign-in that never happened. Throw a clear error
  // instead: every call site wraps these in try/catch and surfaces the
  // message honestly (inline dialog error / notifyError toast), the same
  // honest-failure pattern as saveImageBuffer's non-image branch.
  applyConnectionConfig: async (_payload: unknown): Promise<never> => {
    throw new Error('Connection settings are managed on the server in the web build')
  },
  oauthLoginConnectionConfig: async (_remoteUrl: string): Promise<never> => {
    throw new Error('OAuth sign-in is not available in the web build')
  },

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
  translucencySupported: false,

  // ── theme marketplace (search only; install stays omitted, see above) ────
  themes: {
    searchMarketplace: searchMarketplaceThemes
  },

  // ── local models: safe default, corrected async below (no launch flag) ──
  localModelsEnabled: false

  // OMITTED ON PURPOSE (consumers optional-chained/feature-gated): terminal,
  // git, petOverlay, hud, quickEntry, wakeIndicator, zoom, updates, uninstall,
  // themes.fetchMarketplace (install), installDesktopPlugin, probePluginRepo,
  // mcpOauth (browser popup fallback lives in lib/mcp-dashboard-oauth.ts
  // instead — no loopback listener possible from a tab), cloud, connections,
  // settings, findInPage*, getBootstrapState/onBootstrapEvent (must stay
  // omitted TOGETHER), readFileDataUrl, openSessionWindow/openWindow,
  // writeClipboard, setActiveWork, setTranslucency, battery,
  // watchPreviewFile/watchDirectory/stopPreviewFileWatch, contextMenu*, and
  // the REMAINING oauth*/ssh*/connection-config surfaces (getConnectionConfig
  // stays omitted — it is the sentinel that gates Settings → Gateway and the
  // boot-failure overlay; applyConnectionConfig + oauthLoginConnectionConfig
  // above are the two reachable exceptions).
  //
  // readFileDataUrl in particular MUST stay omitted: desktop-fs's
  // readDesktopFileDataUrlLocalFirst tries the bridge before the gateway, so
  // defining it would shadow the remote /api/fs/read-data-url read that makes
  // composer thumbnails work here.
  //
  // watchPreviewFile/watchDirectory are optional on the loader's own contract
  // (`desktop.watchPreviewFile?.()`/`desktop.watchDirectory?.()`, wrapped in
  // try/catch) — omitting them costs live hot-reload of an edited plugin.js
  // and hands folder-churn detection to runtime-loader.ts's 5s poll fallback
  // instead of a push notification; disk plugins (account-limits included)
  // still discover and load correctly on the initial scan/poll via
  // desktopPluginsRoot/agentPluginsRoot + readDir/readPluginSource above.
}

;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = shim

// Fire-and-forget: corrects $localModelsEnabled once the backend answers.
// Must run AFTER window.hermesDesktop is assigned (the store import chain
// eventually reads it), and must never block first paint.
void correctLocalModelsEnabledFlag()

export {}
