// Fork-owned: Windows Python runtime discovery for the desktop backend
// resolver — the registry/filesystem/py.exe probe ladder that used to live
// inline in main.ts's findSystemPython(). The platform dispatch (POSIX PATH
// lookup vs this ladder) stays at the anchor in main.ts; the Windows
// command/path/encoding details live here.
//
// Windows PATH-based detection has TWO landmines we have to dodge:
//
//  (1) The Microsoft Store "Python stub" lives at
//      %LOCALAPPDATA%\Microsoft\WindowsApps\python.exe and is on PATH
//      by default on modern Windows. It's a redirector that opens the
//      Store window if no Store Python is installed. Running it for
//      `-m venv` would either succeed (real Store install — fine) or
//      pop the Store dialog (bad UX during boot).
//  (2) `py.exe` (Python launcher) is missing from per-user installs
//      that didn't check the launcher option, so PATH-only checks
//      miss real Python 3.13 installs (user-reported case).
//
// We also restrict ourselves to Python 3.11–3.13. 3.14 is the latest
// CPython but several Hermes deps (notably pywinpty's Rust-built
// windows_x86_64_msvc crate) don't yet publish 3.14 wheels, and
// `pip install -e .` falls back to source-build, which fails without
// a Rust toolchain. install.ps1 sidesteps this by pinning to 3.11
// via uv; until we add the same uv-managed Python pathway here, the
// simplest fix is to refuse 3.14 detection and let the NSIS prereq
// page offer to install 3.11 alongside.
//
// Strategy: probe in three passes, in order from most-precise to
// least-precise, and ONLY use PATH lookup as a last resort after
// confirming the candidate isn't the WindowsApps redirector.
//
//  Pass 1: PEP 514 registry — every standards-compliant Python
//          installer registers itself at SOFTWARE\Python\PythonCore.
//          The MS Store stub does NOT register here, so a hit means
//          a real Python install. Versions are explicit so we
//          inherently filter 3.14 out.
//  Pass 2: Filesystem probe of standard install locations
//          (Program Files, LocalAppData\Programs\Python). Same
//          version filtering by directory name.
//  Pass 3: PATH lookup of `py.exe` (the launcher itself never
//          triggers the Store) — but call it with a version flag so
//          we resolve to a SPECIFIC supported version, not whatever
//          py.exe's default is (which on a 3.14-only box would be
//          3.14).

import path from 'node:path'

import { hiddenWindowsChildOptions } from '../windows-child-options'
import { runRegQuery } from '../windows-user-env'

const SUPPORTED_VERSIONS = ['3.11', '3.12', '3.13']
const SUPPORTED_VERSIONS_NO_DOT = ['311', '312', '313']

export interface WindowsPythonProbeDeps {
  env: NodeJS.ProcessEnv
  fileExists(filePath: string): boolean
  findOnPath(command: string): string | null
  /** Promisified execFile — shared with the other async boot probes. */
  execFileAsync(
    file: string,
    args: string[],
    options: { encoding: BufferEncoding; windowsHide?: boolean; timeout?: number }
  ): Promise<{ stdout: string }>
  /** Probe budget for python.exe under cold cache / AV scan. */
  probeTimeoutMs: number
}

export async function findWindowsSystemPython(deps: WindowsPythonProbeDeps): Promise<string | null> {
  // Pass 1: registry. Use `reg query` (through the shared PowerShell/UTF-8
  // wrapper — see windows-user-env.ts's runRegQuery) since main process
  // doesn't have a reliable in-process registry API across all electron
  // versions, and reg.exe's raw output is in the console codepage: a
  // straight `execFileSync('reg', ...)` + utf8 decode mangles any
  // non-ASCII byte, which for `InstallPath` values under a CJK-named
  // Program Files/user directory means the resolved python.exe path comes
  // back mojibake'd and fileExists() below silently (and wrongly) fails.
  // The (hive, version) probes are independent reads, so run them all
  // concurrently (this used to be a fully synchronous serial loop that
  // could block the main thread for the sum of every probe's latency);
  // priority among the settled results is still HKLM-before-HKCU,
  // lowest-version-first, exactly like the old loop order.
  const registryCandidates: Array<{ hive: string; version: string }> = []

  for (const hive of ['HKLM', 'HKCU']) {
    for (const version of SUPPORTED_VERSIONS) {
      registryCandidates.push({ hive, version })
    }
  }

  const registryResults = await Promise.all(
    registryCandidates.map(async ({ hive, version }) => {
      try {
        const stdout = await runRegQuery<Promise<string>>(
          ['query', `${hive}\\SOFTWARE\\Python\\PythonCore\\${version}\\InstallPath`, '/ve', '/reg:64'],
          // Registry reads are near-instant; the bound only exists so a
          // pathologically wedged reg.exe can't hang boot forever.
          {
            exec: async (file, args, options) => (await deps.execFileAsync(file, args, options)).stdout,
            timeout: 5_000
          }
        )

        // Output format: "    (Default)    REG_SZ    C:\Path\To\Python\"
        const match = String(stdout).match(/REG_SZ\s+(.+?)\s*$/m)

        if (!match) {
          return null
        }

        const pythonExe = path.join(match[1].trim(), 'python.exe')

        return deps.fileExists(pythonExe) ? pythonExe : null
      } catch {
        // Key not present — try next.
        return null
      }
    })
  )

  const registryHit = registryResults.find(Boolean)

  if (registryHit) {
    return registryHit
  }

  // Pass 2: filesystem probe of standard locations.
  const programFiles = deps.env['ProgramFiles'] || 'C:\\Program Files'
  const localAppData = deps.env.LOCALAPPDATA || ''

  for (const versionDir of SUPPORTED_VERSIONS_NO_DOT) {
    const systemWide = path.join(programFiles, `Python${versionDir}`, 'python.exe')

    if (deps.fileExists(systemWide)) {
      return systemWide
    }

    if (localAppData) {
      const perUser = path.join(localAppData, 'Programs', 'Python', `Python${versionDir}`, 'python.exe')

      if (deps.fileExists(perUser)) {
        return perUser
      }
    }
  }

  // Pass 3: py.exe with explicit version flag. The launcher itself is
  // safe to invoke (no Store popup) and `py -3.13 -c "import sys;
  // print(sys.executable)"` resolves to the actual python.exe path of
  // the requested version. Probed concurrently; priority among settled
  // results stays version-priority order (3.11 before 3.12 before 3.13),
  // matching the old serial-loop's first-hit-wins semantics.
  const pyExe = deps.findOnPath('py.exe')

  if (pyExe) {
    const pyResults = await Promise.all(
      SUPPORTED_VERSIONS.map(async version => {
        try {
          const { stdout } = await deps.execFileAsync(
            pyExe,
            [`-${version}`, '-c', 'import sys; print(sys.executable)'],
            hiddenWindowsChildOptions({
              encoding: 'utf8',
              // Bare interpreter startup — much lighter than the hermes-import
              // probes, but still python.exe under cold cache / AV scan, so
              // share the probe budget rather than running unbounded.
              timeout: deps.probeTimeoutMs
            })
          )

          const candidate = String(stdout).trim()

          return candidate && deps.fileExists(candidate) ? candidate : null
        } catch {
          // py couldn't find that version — try next.
          return null
        }
      })
    )

    const pyHit = pyResults.find(Boolean)

    if (pyHit) {
      return pyHit
    }
  }

  // We deliberately do NOT fall back to plain `python.exe` on PATH.
  // Without a way to verify the version safely (running `python -V`
  // risks the Microsoft Store popup), accepting whatever's there
  // could land us on 3.14 and trigger the Rust-build-from-source
  // failure. Better to return null and let the NSIS prereq page
  // offer to install a known-good 3.11 via winget.
  return null
}
