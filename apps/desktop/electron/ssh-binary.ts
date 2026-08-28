// ssh-binary.ts
//
// One resolver for "where is ssh.exe" on Windows, shared by every embedded-
// terminal/SSH-config code path. Before this module the four call sites each
// hardcoded `%SystemRoot%\System32\OpenSSH\ssh.exe` with no existence check —
// that path only exists when the Windows OpenSSH Client optional feature is
// installed, which is NOT the default on Windows 10/11 LTSC/IoT SKUs and can
// be removed on any SKU. A box with a perfectly working `ssh` on PATH (e.g.
// via Git for Windows, or a manually-installed OpenSSH) would still see the
// embedded terminal and SSH-config resolution silently fail because the
// hardcoded path didn't exist. `ssh-connection.ts` had the opposite bug:
// it always spawned bare `ssh`, which only works when something is on PATH.
//
// Resolution order (first match wins), Windows only:
//   1. The built-in Windows OpenSSH Client feature, if actually installed
//      (`%SystemRoot%\System32\OpenSSH\ssh.exe`, existence-checked).
//   2. `ssh.exe` on PATH.
//   3. Git for Windows' bundled `usr\bin\ssh.exe`, resolved relative to
//      wherever `find-git-bash.ts` located Git Bash (same install, so if
//      bash.exe is there ssh.exe almost certainly is too). bash.exe can sit
//      at either `<gitRoot>\bin\bash.exe` (top-level shim) or
//      `<gitRoot>\usr\bin\bash.exe` (the real MSYS2 layout) depending on
//      which candidate matched in findGitBash(); ssh.exe always lives at
//      `<gitRoot>\usr\bin\ssh.exe` in both layouts, so normalize up to
//      gitRoot first rather than assuming ssh sits next to bash.
// Off Windows, `ssh` is expected on PATH and spawn's own PATH search handles
// it — this resolver returns the literal string `'ssh'` unchanged.
//
// Returns null when no candidate exists; callers surface a clear "OpenSSH
// client not installed" error instead of spawning a path that doesn't exist.

import path from 'node:path'

export interface ResolveSshBinaryOptions {
  isWindows: boolean
  env: Record<string, string | undefined>
  fileExists: (filePath: string) => boolean
  findOnPath?: (command: string) => string | null
  /** Result of find-git-bash.ts's findGitBash() — reused, not re-derived. */
  gitBashPath?: null | string
}

export function resolveSshBinary(opts: ResolveSshBinaryOptions): null | string {
  const { isWindows, env, fileExists, findOnPath, gitBashPath } = opts

  if (!isWindows) {
    return 'ssh'
  }

  const systemRoot = env.SystemRoot || env.windir || 'C:\\Windows'
  const builtin = path.win32.join(systemRoot, 'System32', 'OpenSSH', 'ssh.exe')

  if (fileExists(builtin)) {
    return builtin
  }

  const onPath = findOnPath ? findOnPath('ssh.exe') : null

  if (onPath) {
    return onPath
  }

  if (gitBashPath) {
    const bashDir = path.win32.dirname(gitBashPath)
    // Both `...\Git\bin\bash.exe` and `...\Git\usr\bin\bash.exe` reduce to
    // `...\Git`: the `usr\bin` shape needs two levels stripped off bashDir,
    // the plain `bin` shape needs one.
    const usrBinSuffix = `${path.win32.sep}usr${path.win32.sep}bin`
    const gitRoot = bashDir.toLowerCase().endsWith(usrBinSuffix.toLowerCase())
      ? path.win32.dirname(path.win32.dirname(bashDir))
      : path.win32.dirname(bashDir)
    const candidate = path.win32.join(gitRoot, 'usr', 'bin', 'ssh.exe')

    if (fileExists(candidate)) {
      return candidate
    }
  }

  return null
}

/** User-facing error for every call site when no ssh binary resolves. */
export const SSH_BINARY_MISSING_MESSAGE =
  'OpenSSH client not installed. Install the Windows OpenSSH Client optional feature, or install Git for Windows (which bundles ssh.exe).'
