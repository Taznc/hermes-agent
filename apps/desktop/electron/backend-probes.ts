/**
 * backend-probes.ts
 *
 * Cheap "does this candidate backend actually work" checks used by
 * resolveHermesBackend (main.ts). The resolver walks a ladder of
 * candidates -- bootstrap marker, `hermes` on PATH, system Python with
 * hermes_cli installed -- and historically returned the first candidate
 * whose binary existed on disk. That assumption breaks when a user has
 * a pre-installed Python 3.11-3.13 (so findSystemPython() returns a
 * path) but no hermes_cli in its site-packages: the resolver hands back
 * a backend the spawn step can't actually run, and the user gets a
 * dead-on-arrival "ModuleNotFoundError: No module named 'hermes_cli'"
 * instead of the first-launch installer.
 *
 * These probes give the resolver a way to verify a candidate before
 * trusting it. Failure (non-zero exit, exception, timeout) means "skip
 * this rung, try the next one"; success means "spawn this for real."
 * Falling off the bottom of the ladder lands on the bootstrap-needed
 * sentinel, which is exactly what we want when nothing pre-existing
 * actually works.
 *
 * Both probes are deliberately fast and forgiving:
 *   - default 15s timeout (5s was too short on cold Windows disks / AV;
 *     issue #61764 death-loop) with HERMES_PROBE_TIMEOUT_MS override
 *   - one automatic retry after a timeout before declaring the runtime dead
 *   - stdio ignored (we only care about exit code; stdout/stderr are
 *     not surfaced to the user, just to recentHermesLog for forensics
 *     via the caller's catch block if it chooses)
 *   - any throw -> false (never propagate -- resolver wants a boolean)
 *
 * Kept in a standalone ts module so it can be unit-tested with
 * `node --test` without dragging in the electron runtime (same pattern
 * as bootstrap-platform.ts and hardening.ts).
 */

import { execFile } from 'node:child_process'
import { promisify } from 'node:util'

const execFileAsync = promisify(execFile)

/** Default probe budget. 5s false-negativeed healthy Windows cold starts (#61764). */
const DEFAULT_PROBE_TIMEOUT_MS = 15_000

/**
 * Resolve the backend probe timeout (ms).
 * Honours HERMES_PROBE_TIMEOUT_MS when it parses as a positive integer.
 */
function resolveProbeTimeoutMs(env: NodeJS.ProcessEnv = process.env): number {
  const raw = env.HERMES_PROBE_TIMEOUT_MS

  if (raw == null || raw === '') {
    return DEFAULT_PROBE_TIMEOUT_MS
  }

  const n = Number.parseInt(String(raw), 10)

  if (!Number.isFinite(n) || n <= 0) {
    return DEFAULT_PROBE_TIMEOUT_MS
  }

  // Clamp absurd values (ms) so a typo can't hang startup forever.
  return Math.min(n, 120_000)
}

const PROBE_TIMEOUT_MS = resolveProbeTimeoutMs()

function isTimeoutError(err: unknown): boolean {
  if (!err || typeof err !== 'object') {
    return false
  }

  const e = err as { code?: string; killed?: boolean; signal?: string }

  if (e.killed === true) {
    return true
  }

  if (e.code === 'ETIMEDOUT') {
    return true
  }

  // Node marks timed-out execFileSync with SIGTERM on some platforms.
  if (e.signal === 'SIGTERM') {
    return true
  }

  return false
}

/**
 * Run execFileAsync; on timeout only, retry once before failing. Async so a
 * cold-cache / AV-scanned probe (measured up to ~10.5s on Windows) never
 * blocks the Electron main-process event loop -- a synchronous probe here
 * froze the whole app (window resize, other IPC, the renderer itself) for
 * the full probe duration, on cold boot and on every profile spawn.
 * Non-timeout failures (ENOENT, non-zero exit) fail immediately.
 */
async function execProbeAsync(
  command: string,
  args: string[],
  options: {
    cwd?: string
    env?: NodeJS.ProcessEnv
    timeout: number
    shell?: boolean
    windowsHide?: boolean
  }
): Promise<void> {
  try {
    await execFileAsync(command, args, options)
  } catch (err) {
    if (!isTimeoutError(err)) {
      throw err
    }

    // One cold-cache / AV miss should not force hermes-setup --update (#61764).
    await execFileAsync(command, args, options)
  }
}

/**
 * Return the Python snippet used to verify Hermes can import far enough to
 * launch the CLI. Kept exported for tests so dependency regressions are
 * caught without needing a real broken venv fixture.
 *
 * @returns {string}
 */
function hermesRuntimeImportProbe() {
  return 'import yaml; import dotenv; import hermes_cli.config'
}

/**
 * Return true iff the Hermes runtime import probe exits 0.
 *
 * Used to gate the "fallback to system Python with hermes_cli installed"
 * rung of resolveHermesBackend. Without this, a system Python 3.11-3.13
 * registered in PEP 514 makes findSystemPython() succeed regardless of
 * whether hermes_cli has actually been pip-installed into its
 * site-packages -- and the resolver returns a backend that immediately
 * dies on spawn.
 *
 * The probe intentionally imports hermes_cli.config, not just the top-level
 * package: a broken/empty Windows launcher venv can still see the source tree
 * through PYTHONPATH but lack PyYAML, then die on the first real CLI import.
 *
 * Async: the probe is a real subprocess spawn+cold-import that can take up
 * to ~10s on a loaded Windows box, and running it synchronously on the
 * Electron main thread froze the whole app (window resize, IPC, renderer)
 * for the duration. In-flight calls for the SAME (pythonPath, env) pair are
 * single-flighted -- the resolver's ladder can probe the same candidate from
 * more than one caller in close succession (e.g. a profile spawn racing the
 * primary boot), and de-duping the promise means only one subprocess runs.
 *
 * @param {string} pythonPath - Absolute path to a python.exe / python.
 * @param {object} [opts.env] - Additional environment for the probe.
 * @returns {Promise<boolean>}
 */
const _importProbeCache = new Map<string, Promise<boolean>>()

async function canImportHermesCli(pythonPath: string, opts: { env?: Record<string, string> } = {}): Promise<boolean> {
  if (!pythonPath) {
    return false
  }

  const envKey = opts.env ? JSON.stringify(Object.entries(opts.env).sort()) : ''
  const key = `${pythonPath}::${envKey}`
  const cached = _importProbeCache.get(key)

  if (cached) {
    return cached
  }

  const probe = execProbeAsync(pythonPath, ['-c', hermesRuntimeImportProbe()], {
    env: { ...process.env, ...(opts.env || {}) },
    timeout: PROBE_TIMEOUT_MS,
    windowsHide: true
  }).then(
    () => true,
    () => false
  )

  _importProbeCache.set(key, probe)

  return probe
}

/**
 * Return true iff `<hermesCommand> --version` exits 0.
 *
 * Used to gate the "existing `hermes` on PATH" rung. Without this, a
 * stale hermes.cmd shim left behind by an uninstalled pip install (or
 * a half-built venv whose `hermes` entry-point points at a deleted
 * Python) survives findOnPath() and gets selected as the backend.
 *
 * We intentionally avoid invoking the command with the dashboard args
 * here -- `--version` is the cheapest "is this binary alive" smoke
 * test that every hermes_cli entry-point has supported since 0.1.
 *
 * Async for the same reason as canImportHermesCli (blocking main-thread
 * exec froze the whole app); single-flighted per (command, shell) pair.
 *
 * @param {string} hermesCommand - Resolved absolute path to a hermes
 *   executable (or an interpreter+script wrapper).
 * @param {boolean} [opts.shell] - Whether to run through a shell. For
 *   .cmd/.bat shims on Windows execFile needs shell:true to find
 *   the cmd interpreter; mirrors the same flag isCommandScript() drives
 *   in resolveHermesBackend.
 * @returns {Promise<boolean>}
 */
/**
 * An explicit desktop backend command is a deployment contract, not a PATH
 * discovery candidate. In particular, the Nix desktop wrapper points this at
 * its immutable, matching Hermes package; it must never fall through to the
 * mutable install-script bootstrap path if a best-effort probe is slow.
 */
function shouldTrustHermesOverride(hermesOverride?: string) {
  return typeof hermesOverride === 'string' && hermesOverride.trim().length > 0
}

const _versionProbeCache = new Map<string, Promise<boolean>>()

async function verifyHermesCli(hermesCommand: string, opts?: { shell?: boolean }): Promise<boolean> {
  if (!hermesCommand) {
    return false
  }

  const key = `${hermesCommand}::${Boolean(opts?.shell)}`
  const cached = _versionProbeCache.get(key)

  if (cached) {
    return cached
  }

  const probe = execProbeAsync(hermesCommand, ['--version'], {
    timeout: PROBE_TIMEOUT_MS,
    shell: Boolean(opts?.shell),
    windowsHide: true
  }).then(
    () => true,
    () => false
  )

  _versionProbeCache.set(key, probe)

  return probe
}

export {
  canImportHermesCli,
  DEFAULT_PROBE_TIMEOUT_MS,
  execProbeAsync,
  hermesRuntimeImportProbe,
  PROBE_TIMEOUT_MS,
  resolveProbeTimeoutMs,
  shouldTrustHermesOverride,
  verifyHermesCli
}
