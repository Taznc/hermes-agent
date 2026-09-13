# Web UI Hard Refresh — Diagnosis Report

Task: t_ff94388f (parent t_d22029ef "Web UI Hard Refresh")

## 0. Which app is actually in play

Two unrelated things in this repo could be called "the Hermes Desktop web
UI" — this matters because they have completely different reload paths.

| Service | Port | Serves | What it is |
|---|---|---|---|
| `hermes-webdesktop-dev` / `-stable` | 5176 / 5175 | `apps/desktop` via `vite.config.web.ts` + `index-web.html` | **This is the target.** A SPIKE that serves the real Electron desktop React app as a plain browser page (`apps/desktop/src/web-bridge-shim.ts` stands in for the Electron preload bridge). |
| `hermes-dashboard` (`hermes dashboard` CLI cmd) | 9119 | top-level `web/` | The CLI's own separate dashboard SPA. Unrelated app, unrelated reload code (`web/src/lib/dashboard-auth-reload.ts`, `web/src/components/ModelReloadConfirm.tsx` — both deliberate, user/one-time reloads, not the reported symptom). |

The user's dev VM browser tab is the `hermes-webdesktop-dev` renderer
(`apps/desktop`, served on :5176, proxied by Traefik to
`hermes-desktop-dev.jashworth.com`). All findings below are about that app.
`web/` is out of scope; don't conflate the two when implementing t_57673138.

## 1. Root cause of the hard refresh

The hard refresh is **Vite's own built-in HMR client**, not anything Hermes
wrote. It fires automatically and unconditionally — there is currently no
Hermes code path that decides to reload; it's stock `vite` dev-server
behavior applied to a codebase where agents are constantly editing the exact
tree being served live (`hermes-agent-dev`, per the workspace `AGENTS.md`).

Mechanism, in `node_modules/vite/dist/client/client.mjs` (installed under
`apps/desktop/node_modules/vite`, same version 8.2.0 used by both
`vite.config.ts` (Electron, :5174) and `vite.config.web.ts` (web spike,
:5175/:5176)):

- `case "full-reload":` (~L1008-1017) — on receiving a `full-reload` WS
  message from the vite dev server, it notifies `vite:beforeFullReload`
  listeners (fire-and-forget — **listeners cannot cancel or delay it**) and
  then calls `pageReload()`.
- `pageReload = debounceReload(20)` (~L930) — debounces 20ms, then calls
  `location.reload()` directly (~L926).
- A **second, independent** auto-reload path: `case "custom":` handling
  `vite:ws:disconnect` (~L997-1005) — if the WS to the dev server drops and
  later reconnects, it also calls `location.reload()` once the ping to the
  server succeeds. This fires on `hermes-webdesktop-dev.service` restarts,
  Traefik hiccups, or the vite process itself restarting (`Restart=on-failure`
  in the unit).

Why it fires so often here specifically: Vite's React plugin can only Fast
Refresh a module whose exports are *only* React components. Any edit to a
file that also exports non-component values (a nanostore, a constant, a
helper function, an i18n locale object, `DIRECTIVE_ACTIONS`, etc.) is
**not** Fast-Refreshable, so Vite invalidates it and falls back to a full
page reload for that update. This repo's renderer is full of exactly that
shape (`store/*.ts`, `i18n/*.ts`, `lib/*.ts` files consumed by components).
Confirmed live in the running unit's journal — a single edit burst produced
a dozen `page reload src/i18n/en.ts`, `page reload src/store/preview.ts`,
`page reload src/components/assistant-ui/directive-text.tsx`, etc. within
the same second (`journalctl -u hermes-webdesktop-dev.service`, ~02:58:02 and
03:01:29 in the current session). Every one of those calls
`window.location.reload()` in the connected browser tab, which is the
"hard refresh" losing in-progress composer/chat state.

**Important corollary:** this is not unique to the web build. The Electron
renderer window in dev mode loads the identical vite dev server code
(`vite.config.ts`, port 5174) and is subject to the exact same
Fast-Refresh-incompatible full-reloads for the exact same files. The
Electron app is not immune to this class of reload — it's just less visible
(no browser chrome, and see §2: the one blue button that *is* visible in
Electron guards a completely different failure mode).

## 2. How the dev (Electron) app's blue "Restart" button actually works

There is exactly one blue, manually-triggered restart affordance in the
codebase today, and it is **not** wired to the renderer HMR full-reload at
all — it exists to solve a narrower, unrelated problem: **Electron's main
process cannot hot-swap itself.** Renderer edits hot-reload live via Vite;
an edit under `apps/desktop/electron/*.ts` rebuilds the main-process bundle
on disk, but the already-running Electron main process keeps executing the
old code until the whole app relaunches. Rather than silently restarting
Electron under the user (killing whatever they were doing), the app shows an
explicit, opt-in affordance:

- **Watcher (main process):** `apps/desktop/electron/main.ts`
  - `watchDevMainBundle()` (~L10360-10411): active only when
    `!IS_PACKAGED && DEV_SERVER` is set. Takes a signature of the built main
    bundle at boot, `fs.watch`es it, and on a real content change (150ms
    debounce) sets a module-level flag `devMainBundleStale = true`
    (~L10347-10348, L10398-10399).
  - `broadcastDevBundleStale()` (~L10350-10358) pushes
    `hermes:dev:main-bundle-stale` to every `BrowserWindow`'s `webContents`.
  - IPC handlers (~L10425-10456): `hermes:dev:main-bundle-stale` returns
    `{ stale, supported }` (`supported: !IS_PACKAGED && Boolean(DEV_SERVER)`
    — this is how the UI stays hidden in a packaged build).
    `hermes:dev:restart` performs the actual restart: if
    `process.env.HERMES_DEV_WATCH === '1'` it exits with sentinel code
    `DEV_RESTART_EXIT_CODE = 86` so the external supervisor
    (`apps/desktop/scripts/dev-electron-watch.mjs`) respawns the process
    with Vite left running; otherwise it falls back to
    `app.relaunch(); app.exit(0)`.
- **Preload bridge:** `apps/desktop/electron/preload.ts` L465-475 exposes
  `getDevMainBundleStale()`, `restartForDevBundle()`,
  `onDevMainBundleStale(cb)` on `window.hermesDesktop`.
- **Renderer state + component:**
  `apps/desktop/src/app/shell/hooks/use-statusbar-items.tsx`:
  - L423-444: `devBundleStale` is a plain `useState`, seeded by polling
    `getDevMainBundleStale()` once on mount and kept live via
    `onDevMainBundleStale`.
  - L446-464 (`devRestartItem`): a `StatusbarItem`, rendered **only** while
    `devBundleStale` is true. `className: 'px-2 font-semibold bg-blue-600
    text-white hover:bg-blue-500'` — this is the actual blue color the user
    is describing. `label: 'Restart to apply'` (hardcoded string, not
    i18n'd). `onSelect: () => void window.hermesDesktop.restartForDevBundle?.()`.
  - L491-500 (`coreLeftStatusbarItems`): mounted leftmost alongside the
    fork-build marker, i.e. always visible near the left edge of the
    statusbar whenever it's active.

**On the "Refresh vs Restart" label logic specifically:** `devRestartItem`
itself has no dual label — it is always "Restart to apply" for its one
binary condition (main bundle stale / not stale). The actual precedent for a
state-driven Refresh-vs-Restart *label* lives in a different, unrelated
subsystem — the self-update flow:
`apps/desktop/src/lib/version-status.ts` → `resolveVersionStatus()`
(L58-111). Its `label` field appends `copy.update` ("update") while an
update apply is in progress, or swaps to `copy.restart` ("restart") once the
apply state machine reaches `stage === 'restart'` (an update was installed
and the app/backend is now restarting itself) — see
`resolveVersionStatus()` L107: `` `${base} · ${restarting ? copy.restart : copy.update}` ``.
This is consumed by both the command-center's `clientVersionItem` /
`backendVersionItem` (`use-statusbar-items.tsx` L309-394) and the command
palette (`app/command-palette/index.tsx` L597-613). It answers "is this a
plain refresh-scale event or a restart-scale event", but it's about the
self-update pipeline, not about HMR/dev-server reloads — no existing code
path decides Refresh-vs-Restart for the symptom in this task.

## 3. Recommended implementation approach (for t_57673138)

Goal: stop Vite's HMR client from silently calling `location.reload()` in
the web-served build, and instead surface a blue, manually-clicked
"Refresh"/"Restart" affordance using the **same visual language and
statusbar wiring** as `devRestartItem`, per the parent task's explicit ask
to reuse the existing pattern rather than invent a new one.

Key constraint discovered above: `import.meta.hot.on('vite:beforeFullReload',
cb)` **cannot** prevent the reload — Vite's client notifies listeners and
then unconditionally proceeds to `pageReload()` regardless of what a
listener does. The only real interception point is the browser API call
itself.

1. **Trap the reload call, don't try to cancel the HMR event.** Add a small
   web-only shim, loaded the same way and same load-order slot as
   `apps/desktop/src/web-bridge-shim.ts` (i.e. a `<script type="module">`
   tag in `apps/desktop/index-web.html`, evaluated before `main.tsx`).
   Gate it on `import.meta.env.DEV` (this must never ship in a production
   build). Redefine `window.location.reload` via
   `Object.defineProperty(window.location, 'reload', { configurable: true,
   value: trappedReload })` (Location.prototype.reload is
   configurable/writable in Chromium — verify in the target browsers this
   Traefik-fronted instance is actually used from before relying on it
   cross-browser). Keep the native function (`window.location.reload.bind(...)`
   captured before overriding) so the button's click handler can still
   perform a real reload.
2. **State:** a tiny nanostore atom, e.g. `$webReloadPending` in a new
   `apps/desktop/src/store/web-reload.ts`, flipped `true` by the trapped
   `reload()` call instead of navigating.
3. **Statusbar item:** add a sibling to `devRestartItem` in
   `use-statusbar-items.tsx` (same file, same list —
   `coreLeftStatusbarItems`), e.g. `webReloadItem`:
   - Same `className: 'px-2 font-semibold bg-blue-600 text-white
     hover:bg-blue-500'` for visual parity with the thing the user already
     recognizes.
   - `label`: default to "Refresh" (a page reload is genuinely sufficient
     here — there is no Electron main process to restart in the browser
     build). Reserve "Restart" wording only if this is later extended to
     also cover the WS-disconnect-reconnect reload path (`vite:ws:disconnect`,
     §1) or the loopback-auth reload in `web/` — those are arguably closer
     to "the connection needs restarting" than "new code is ready". Simplest
     correct v1: always "Refresh".
   - `onSelect`: calls the captured native reload function.
   - Only mount it when running the web build (the shim already reports
     `platform: 'web'` from `getVersion()` — gate on
     `desktopVersion?.platform === 'web'`, mirroring how `devRestartItem`
     gates on Electron-only state).
4. **Do not touch** `web/src/lib/dashboard-auth-reload.ts` or
   `web/src/components/ModelReloadConfirm.tsx` — different app (§0),
   different (already-deliberate, already button/confirm-driven) reload
   semantics.
5. **Verify manually** per the parent card's acceptance bar:
   - Electron dev app (`npm run dev` / `hgui`): confirm `devRestartItem`
     behavior (main-process bundle staleness → blue "Restart to apply") is
     completely unchanged — this shim must be excluded from
     `vite.config.ts`'s output entirely (it only ever loads via
     `index-web.html`, which Electron's `index.html` does not include —
     confirm that stays true).
   - Web build (`hermes-webdesktop-dev`, :5176): make an edit to a
     non-component module (e.g. touch `src/i18n/en.ts` or any `store/*.ts`)
     while the page is open and confirm (a) the page does NOT auto-reload,
     (b) the blue "Refresh" item appears in the statusbar, (c) clicking it
     performs the reload and the item disappears afterward.
   - `npm run typecheck` / `npx vitest run --maxWorkers=4` in
     `apps/desktop` pass (per `AGENTS.md`/`fork-dev-workflow` skill
     conventions).
