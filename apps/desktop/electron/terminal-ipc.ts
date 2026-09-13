// The embedded terminal's PTY host: shell resolution, env scrubbing, session
// registry, and the hermes:terminal:* IPC surface. Extracted from main.ts; the
// factory owns the session map and returns the dispose helpers main.ts needs
// for SSH teardown. findOnPath / logging / connection routing stay injected.
import { execFile } from 'node:child_process'
import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import { app, ipcMain } from 'electron'
import nodePty from 'node-pty'

import { resolveTerminalConnectionForSender } from './connection-apply'
import { ensureSpawnHelperExecutable } from './spawn-helper-perms'
import { SSH_BINARY_MISSING_MESSAGE } from './ssh-binary'
import { buildInteractiveSshArgs } from './ssh-connection'
import { createTerminalOutputBatcher } from './terminal-output-batcher'
import { createTerminalOutputGate } from './terminal-output-gate'
import { buildWindowsInteractiveCommand } from './windows-remote-lifecycle'

export interface TerminalIpcDeps {
  isWindows: boolean
  findOnPath: (command: string) => null | string
  rememberLog: (line: string) => void
  activeSshTerminalTarget: (webContentsId: number) => unknown
  ensureBackend: (webContentsId: number) => Promise<unknown>
  getSshConnectionState: (scope: string) => undefined | { remotePlatform?: string }
  /** Shared System32 → PATH → Git-for-Windows ladder (see ssh-binary.ts). */
  resolveSshBinary: () => null | string
}

export interface TerminalIpcApi {
  disposeTerminalSession: (id: string) => boolean
  disposeTerminalSessionsForSshScope: (scope: string) => void
  disposeAllTerminalSessions: () => void
}

export function registerTerminalIpc({
  isWindows,
  findOnPath,
  rememberLog,
  activeSshTerminalTarget,
  ensureBackend,
  getSshConnectionState,
  resolveSshBinary
}: TerminalIpcDeps): TerminalIpcApi {
  const terminalSessions = new Map()
  // One 'destroyed' listener per webContents id, not one per terminal. Every
  // terminal start used to add its own `event.sender.once('destroyed', ...)`;
  // a sender that opens more than ~10 terminals over its lifetime (routine on
  // a long-lived tab) trips Node's MaxListenersExceededWarning and pins one
  // closure per terminal ever opened on that webContents, even after the
  // terminal itself was disposed. Track session ids per webContents id and
  // install a single shared listener the first time that id is seen.
  const sessionIdsByWebContentsId = new Map<number, Set<string>>()

  function forgetTerminalSession(id: string) {
    const sessionInfo = terminalSessions.get(id)

    terminalSessions.delete(id)

    if (sessionInfo) {
      const siblingIds = sessionIdsByWebContentsId.get(sessionInfo.webContentsId)

      siblingIds?.delete(id)

      if (siblingIds && siblingIds.size === 0) {
        sessionIdsByWebContentsId.delete(sessionInfo.webContentsId)
      }
    }

    return sessionInfo
  }

  function trackTerminalSessionForWebContents(webContentsId: number, id: string, sender: Electron.WebContents) {
    let siblingIds = sessionIdsByWebContentsId.get(webContentsId)

    if (!siblingIds) {
      siblingIds = new Set()
      sessionIdsByWebContentsId.set(webContentsId, siblingIds)

      sender.once('destroyed', () => {
        for (const siblingId of [...(sessionIdsByWebContentsId.get(webContentsId) ?? [])]) {
          disposeTerminalSession(siblingId)
        }

        sessionIdsByWebContentsId.delete(webContentsId)
      })
    }

    siblingIds.add(id)
  }

  function isExecutableFile(filePath) {
    if (!filePath || !path.isAbsolute(filePath)) {
      return false
    }

    try {
      fs.accessSync(filePath, fs.constants.X_OK)

      return true
    } catch {
      return false
    }
  }

  function posixShellSpec(shellPath) {
    const shellName = path.basename(shellPath)
    const interactiveArgs = shellName.includes('zsh') || shellName.includes('bash') ? ['-il'] : ['-i']

    return { args: interactiveArgs, command: shellPath, name: shellName }
  }

  // Windows PowerShell 5.1 ships at a fixed System32 path on every Windows box;
  // prefer it only after PowerShell 7+ (`pwsh`).
  function windowsPowerShellPath() {
    const systemRoot = process.env.SystemRoot || process.env.windir || 'C:\\Windows'
    const builtin = path.join(systemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')

    return isExecutableFile(builtin) ? builtin : findOnPath('powershell.exe')
  }

  // Map a resolved shell path to its spawn spec, picking interactive flags by
  // family: PowerShell drops its logo banner (so the prompt sits flush like the
  // POSIX shells), cmd needs nothing, and everything else (zsh/bash/fish/sh…)
  // gets POSIX interactive-login flags.
  function shellSpecFor(shellPath) {
    const name = path.basename(shellPath).toLowerCase()

    if (name.startsWith('pwsh') || name.startsWith('powershell')) {
      return { args: ['-NoLogo'], command: shellPath, name }
    }

    if (name.startsWith('cmd')) {
      return { args: [], command: shellPath, name }
    }

    return posixShellSpec(shellPath)
  }

  // Best installed Windows shell: PowerShell 7+ (`pwsh`), then Windows PowerShell
  // 5.1, then comspec/cmd.exe as the universal fallback.
  function windowsShellSpec() {
    const command =
      findOnPath('pwsh.exe') || findOnPath('pwsh') || windowsPowerShellPath() || process.env.COMSPEC || 'cmd.exe'

    return shellSpecFor(command)
  }

  // Resolve the interactive shell for the embedded terminal: an explicit user
  // override wins, otherwise auto-detect the best one installed for the platform.
  function terminalShellCommand() {
    // HERMES_DESKTOP_SHELL is the cross-platform escape hatch (a path or a bare
    // name on PATH); $SHELL is honored on POSIX, where it's the user's canonical
    // choice, but ignored on Windows, where it's usually a stray MSYS/Git path
    // node-pty can't spawn natively.
    const override = (process.env.HERMES_DESKTOP_SHELL || (isWindows ? '' : process.env.SHELL) || '').trim()

    if (override) {
      const resolved = isExecutableFile(override) ? override : findOnPath(override)

      if (resolved) {
        return shellSpecFor(resolved)
      }
    }

    if (isWindows) {
      return windowsShellSpec()
    }

    const shellPath = ['/bin/zsh', '/bin/bash', '/bin/sh'].find(candidate => isExecutableFile(candidate))

    return posixShellSpec(shellPath || '/bin/sh')
  }

  function safeTerminalCwd(cwd) {
    const candidate = path.resolve(String(cwd || app.getPath('home')))

    try {
      const stat = fs.statSync(candidate)

      return stat.isDirectory() ? candidate : path.dirname(candidate)
    } catch {
      return app.getPath('home')
    }
  }

  function terminalShellEnv() {
    const env = { ...process.env }

    // Electron is commonly launched through `npm run dev`; do not leak npm's
    // managed prefix into a user's interactive shell (nvm/proto warn loudly).
    for (const key of Object.keys(env)) {
      if (key === 'npm_config_prefix' || key.startsWith('npm_config_') || key.startsWith('npm_package_')) {
        delete env[key]
      }
    }

    // Strip color/theme-detection vars that ride along when Electron is launched
    // from a non-tty agent shell (Cursor's runner sets NO_COLOR/FORCE_COLOR=0
    // /TERM=dumb; some terminals set COLORFGBG which would flip Hermes' TUI into
    // light-mode). Our PTY is a real xterm-compat terminal — force truecolor.
    delete env.NO_COLOR
    delete env.FORCE_COLOR
    delete env.COLORFGBG

    env.COLORTERM = 'truecolor'
    env.LC_CTYPE = env.LC_CTYPE || 'UTF-8'
    env.TERM = 'xterm-256color'
    env.TERM_PROGRAM = 'Hermes'
    env.TERM_PROGRAM_VERSION = app.getVersion()

    // Let a hermes/--tui launched in this pane know it's embedded in the desktop
    // GUI (build_environment_hints surfaces this). Distinct from HERMES_DESKTOP,
    // which marks the agent *backend* and gates cron/gateway behavior.
    env.HERMES_DESKTOP_TERMINAL = '1'

    return env
  }

  function terminalChannel(id, suffix) {
    return `hermes:terminal:${id}:${suffix}`
  }

  // Best-effort read of a live PTY child's current working directory so a
  // reopened tab can restart the shell where the user last `cd`'d, instead of the
  // tab's original launch dir. Shell-agnostic (no prompt/OSC config needed) on
  // POSIX; Windows has no cheap per-process cwd query without a native module, so
  // it returns null and the caller falls back to the launch cwd.
  function readProcessCwd(pid) {
    return new Promise(resolve => {
      if (!Number.isInteger(pid) || pid <= 0) {
        resolve(null)

        return
      }

      if (process.platform === 'linux') {
        fs.promises
          .readlink(`/proc/${pid}/cwd`)
          .then(target => resolve(target || null))
          .catch(() => resolve(null))

        return
      }

      if (process.platform === 'darwin') {
        // lsof ships with macOS; -Fn emits the cwd fd's path on an `n<path>` line.
        execFile('lsof', ['-a', '-p', String(pid), '-d', 'cwd', '-Fn'], { timeout: 2000 }, (err, stdout) => {
          if (err) {
            resolve(null)

            return
          }

          const line = String(stdout || '')
            .split('\n')
            .find(entry => entry.startsWith('n'))

          resolve(line ? line.slice(1) : null)
        })

        return
      }

      resolve(null)
    })
  }

  function disposeTerminalSession(id: string) {
    const sessionInfo = forgetTerminalSession(id)

    if (!sessionInfo) {
      return false
    }

    sessionInfo.outputBatcher.dispose()

    try {
      sessionInfo.pty.kill()
    } catch {
      // Process may already be gone.
    }

    return true
  }

  // SSH teardown: close every pane whose PTY rode the disconnected tunnel.
  function disposeTerminalSessionsForSshScope(scope: string) {
    for (const [id, info] of [...terminalSessions.entries()]) {
      if (info.sshScope === scope) {
        disposeTerminalSession(id)
      }
    }
  }

  // App shutdown: kill every open PTY before environment teardown.
  function disposeAllTerminalSessions() {
    for (const id of [...terminalSessions.keys()]) {
      disposeTerminalSession(id)
    }
  }

  // node-pty's published tarball ships the POSIX `spawn-helper` without an exec
  // bit; the dev flow resolves node-pty straight from node_modules (nothing
  // chmods it there), so the first terminal spawn dies with `posix_spawnp
  // failed`. Restore the bit once, lazily, right before the first spawn. Packaged
  // builds already stage an executable copy, so this is a no-op there.
  let _spawnHelperEnsured = false

  function ensureNodePtySpawnHelper() {
    if (_spawnHelperEnsured || isWindows) {
      return
    }

    _spawnHelperEnsured = true

    try {
      const nodePtyRoot = path.dirname(require.resolve('node-pty/package.json'))
      const { fixed, errors } = ensureSpawnHelperExecutable(nodePtyRoot)

      for (const helperPath of fixed) {
        rememberLog(`[terminal] restored +x on node-pty spawn-helper: ${helperPath}`)
      }

      for (const failure of errors) {
        rememberLog(`[terminal] could not chmod spawn-helper ${failure.path}: ${failure.error}`)
      }
    } catch (error) {
      rememberLog(
        `[terminal] spawn-helper exec check skipped: ${error instanceof Error ? error.message : String(error)}`
      )
    }
  }

  ipcMain.handle('hermes:terminal:start', async (event, payload = {}) => {
    ensureNodePtySpawnHelper()

    const id = crypto.randomUUID()
    const { args, command, name } = terminalShellCommand()
    const cwd = safeTerminalCwd(payload?.cwd)
    const cols = Math.max(2, Number.parseInt(String(payload?.cols || 80), 10) || 80)
    const rows = Math.max(2, Number.parseInt(String(payload?.rows || 24), 10) || 24)

    const sshTarget = await resolveTerminalConnectionForSender(event.sender.id, activeSshTerminalTarget, ensureBackend)

    const remote = Boolean(sshTarget)
    const remoteState = remote ? getSshConnectionState(sshTarget.scope) : null

    const remoteCommand =
      remoteState?.remotePlatform === 'Windows'
        ? buildWindowsInteractiveCommand(String(payload?.cwd || '').trim())
        : undefined

    let ptyProcess

    if (remote) {
      const sshBinary = resolveSshBinary()

      if (!sshBinary) {
        throw new Error(SSH_BINARY_MISSING_MESSAGE)
      }

      ptyProcess = nodePty.spawn(
        sshBinary,
        buildInteractiveSshArgs(sshTarget.ssh, String(payload?.cwd || '').trim(), undefined, remoteCommand),
        { cols, cwd: app.getPath('home'), env: terminalShellEnv(), name: 'xterm-256color', rows }
      )
    } else {
      ptyProcess = nodePty.spawn(command, args, { cols, cwd, env: terminalShellEnv(), name: 'xterm-256color', rows })
    }

    const send = (suffix, payload) => {
      if (event.sender.isDestroyed()) {
        return
      }

      event.sender.send(terminalChannel(id, suffix), payload)
    }

    // Output pipeline: pty -> batcher -> gate -> renderer.
    // The batcher coalesces PTY output into batched sends and applies ack-based
    // flow control (pause the pty past the high-water mark, resume once the
    // renderer acks enough of the outstanding backlog) — see
    // terminal-output-batcher.ts. The gate holds everything (data AND exit)
    // until the renderer has subscribed via hermes:terminal:attach, so output
    // produced between `start` resolving and the listener being installed is
    // never lost — see terminal-output-gate.ts.
    const outputGate = createTerminalOutputGate({
      onExitFlushed: () => forgetTerminalSession(id),
      sendData: data => send('data', data),
      sendExit: payload => send('exit', payload)
    })

    const outputBatcher = createTerminalOutputBatcher({
      pause: () => {
        try {
          ptyProcess.pause()
        } catch {
          // Process may already be gone.
        }
      },
      resume: () => {
        try {
          ptyProcess.resume()
        } catch {
          // Process may already be gone.
        }
      },
      send: data => outputGate.data(data)
    })

    terminalSessions.set(id, {
      outputBatcher,
      outputGate,
      pty: ptyProcess,
      webContentsId: event.sender.id,
      ...(remote ? { sshScope: sshTarget.scope, remoteCwd: String(payload?.cwd || '') } : {})
    })

    ptyProcess.onData(data => outputBatcher.push(data))
    ptyProcess.onExit(({ exitCode, signal }) => {
      // Flush any output still buffered so it lands before the exit message —
      // without this a shell's final printf could be silently dropped or
      // reordered after 'exit' reaches the renderer. The gate then forgets the
      // session once the exit has actually been delivered (which may be
      // deferred until the renderer attaches).
      outputBatcher.flush()
      outputBatcher.dispose()
      outputGate.exit({ code: exitCode, signal: signal == null ? null : String(signal) })
    })
    trackTerminalSessionForWebContents(event.sender.id, id, event.sender)

    return { cwd: remote ? null : cwd, id, shell: remote ? 'ssh' : name }
  })

  ipcMain.handle('hermes:terminal:attach', (event, id) => {
    const sessionInfo = terminalSessions.get(String(id || ''))

    if (!sessionInfo || sessionInfo.webContentsId !== event.sender.id) {
      return false
    }

    sessionInfo.outputGate.attach()

    return true
  })

  ipcMain.on('hermes:terminal:write', (_event, id, data) => {
    const sessionInfo = terminalSessions.get(String(id || ''))

    if (!sessionInfo) {
      return
    }

    sessionInfo.pty.write(String(data || ''))
  })

  // Renderer's ack of processed output bytes, driving the pty's pause/resume
  // flow control (see terminal-output-batcher.ts). Fire-and-forget like write:
  // a lost ack just means flow control stays conservative for a bit longer,
  // never a hang, since the next ack (or a fresh session) can still resume it.
  ipcMain.on('hermes:terminal:ack', (_event, id, bytes) => {
    const sessionInfo = terminalSessions.get(String(id || ''))

    if (!sessionInfo) {
      return
    }

    const acked = Number(bytes)

    if (Number.isFinite(acked) && acked > 0) {
      sessionInfo.outputBatcher.ack(acked)
    }
  })

  ipcMain.on('hermes:terminal:resize', (_event, id, size = {}) => {
    const sessionInfo = terminalSessions.get(String(id || ''))

    if (!sessionInfo) {
      return
    }

    const cols = Math.max(2, Number.parseInt(String(size?.cols || 80), 10) || 80)
    const rows = Math.max(2, Number.parseInt(String(size?.rows || 24), 10) || 24)

    sessionInfo.pty.resize(cols, rows)
  })
  ipcMain.handle('hermes:terminal:cwd', async (_event, id) => {
    const sessionInfo = terminalSessions.get(String(id || ''))

    if (!sessionInfo) {
      return null
    }

    return sessionInfo.sshScope !== undefined ? null : readProcessCwd(sessionInfo.pty.pid)
  })

  ipcMain.handle('hermes:terminal:dispose', (_event, id) => disposeTerminalSession(String(id || '')))

  return { disposeTerminalSession, disposeTerminalSessionsForSshScope, disposeAllTerminalSessions }
}
