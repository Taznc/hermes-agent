// Fork-added Desktop bridge declarations.
//
// Upstream owns `src/global.d.ts`; the fork's additions to the renderer-facing
// bridge live here. An upstream sync then never has to merge two sets of
// appended members into one interface body.
//
// Two mechanisms, chosen per target:
//
//  - `declare module '../global'` — for fields the fork adds to upstream's
//    EXPORTED, NAMED interfaces. Declaration merging reaches those with no
//    line at all in upstream's file, so this is preferred wherever it applies.
//  - `ForkDesktopApi` — for methods the fork adds to `window.hermesDesktop`.
//    That property is an anonymous inline object type, and TypeScript cannot
//    merge members into an anonymous type, so this one needs the single
//    intersection anchor in `global.d.ts`.
//
// The runtime counterpart of `ForkDesktopApi` is
// `electron/fork/preload-bridge.ts`; the two are one contract and change
// together.

/**
 * Honest, renderer-facing OS-keychain state for stored desktop secrets. See
 * the `secretStorageState` fields on DesktopConnectionConfig and
 * DesktopConnectionsRegistry for the exact semantics.
 */
export interface DesktopSecretStorageState {
  available: boolean
  policyOn: boolean
}

/**
 * Return shape of `getSecretStorageEncryption` / `setSecretStorageEncryption`.
 *
 * Unlike the members of `ForkDesktopApi`, these two methods EXIST upstream; the
 * fork only widened their return to carry the honest `secretStorageState`
 * alongside the gated `on` flag, so the renderer can update its hint
 * immediately after a toggle instead of waiting for the next
 * getConnectionConfig() hydration (which could otherwise show a stale hint for
 * an enable that just succeeded, or a stale absence of the hint for a disable
 * that just took effect).
 *
 * A widened member cannot be extracted the way an added one can: intersecting
 * two declarations of the same method name produces an overload set, and the
 * call site resolves to upstream's narrower signature, silently dropping the
 * field. Naming the type here at least keeps the fork's edit in `global.d.ts`
 * down to the two signature lines with no prose attached.
 */
export interface ForkSecretStorageEncryptionResult {
  on: boolean
  secretStorageState?: DesktopSecretStorageState
}

/** Fork-added methods on `window.hermesDesktop`. */
export interface ForkDesktopApi {
  /**
   * Chunked non-image attach read: bounds main's transient memory and the
   * per-call IPC payload to a fixed slice (ATTACHMENT_CHUNK_BYTES in
   * electron/hardening.ts) regardless of file size, unlike
   * readFileDataUrlForAttach's whole-file-in-memory read. Callers drive
   * repeated calls at increasing `offset` and concatenate the returned base64
   * until `bytesRead < totalBytes` accounts for every byte. Absent on older
   * shells — readFileDataUrlForAttach/readFileDataUrl remain the fallback
   * ladder.
   */
  readFileChunkForAttach?: (
    filePath: string,
    offset: number
  ) => Promise<{ base64: string; bytesRead: number; mimeType: string; totalBytes: number }>
  /** Dev-only: whether the built main-process bundle is newer than the running
   *  one. `supported` is false in a packaged build. */
  getDevMainBundleStale: () => Promise<{ stale: boolean; supported: boolean }>
  restartForDevBundle: () => Promise<{ ok: boolean; reason?: string }>
  onDevMainBundleStale: (callback: (payload: { stale: boolean }) => void) => () => void
  /** Dev-only: whether backend Python source (agent/ tui_gateway/ tools/
   *  hermes_cli/) the running `hermes serve` child already imported has changed
   *  on disk. `supported` is false in a packaged build and when the primary
   *  backend is remote (Phase 2.9). */
  getDevBackendStale: () => Promise<{
    state: 'fresh' | 'stale' | 'restarting' | 'failed'
    supported: boolean
  }>
  restartDevBackend: () => Promise<{ ok: boolean; reason?: string }>
  onDevBackendStale: (callback: (payload: { state: 'fresh' | 'stale' | 'restarting' | 'failed' }) => void) => () => void
}

// Fork-added fields on upstream's exported interfaces — no line in global.d.ts.
declare module '../global' {
  interface DesktopVersionInfo {
    /** Build provenance from install-stamp.json; null on builds without a stamp. */
    buildBranch?: null | string
    buildCommit?: null | string
    buildAt?: null | string
    buildDirty?: boolean | null
    buildSource?: null | string
  }

  interface DesktopConnectionConfig {
    /**
     * The honest counterpart to `secureTokenStorage`, which reads `true`
     * whenever keychain encryption is opted OUT (the default) because it exists
     * only to gate the plain-text CONFIRM dialog. This one says whether a
     * secret saved right now would actually be OS-keychain encrypted:
     * `policyOn` is the user's opt-in choice (Settings → "Encrypt saved secrets
     * with the OS keychain"), and `available` is only meaningful when
     * `policyOn` is true, telling you whether the keychain itself is currently
     * usable. Drives a one-time, non-blocking "stored without OS keychain
     * encryption" hint — never a blocking assertion of security. Optional so a
     * rolling app update (older Electron main, pre-release build, or a
     * hand-crafted test fixture) that hasn't started sending it degrades to no
     * hint rather than a crash.
     */
    secretStorageState?: DesktopSecretStorageState
  }

  interface DesktopConnectionsRegistry {
    /** The honest { available, policyOn } state — see DesktopConnectionConfig's
     *  `secretStorageState` for the exact semantics. Optional for the same
     *  rolling-update / fixture-compatibility reason as there. */
    secretStorageState?: DesktopSecretStorageState
  }
}
