/**
 * Guards the web build's bridge shim against the "partial-shim trap".
 *
 * Call sites across the renderer are written as:
 *
 *     window.hermesDesktop?.someMethod(...)
 *
 * That optional-chains the OBJECT, not the METHOD. Under Electron every method
 * exists so this is safe. The web shim, however, is a partial stand-in: the
 * object is present, `?.` passes, and a missing method throws
 * "someMethod is not a function" at call time — which is how pasting an image
 * broke (saveImageBuffer).
 *
 * This suite scans real source instead of asserting a hand-written list, so a
 * NEW unguarded call site added upstream fails here rather than in the browser.
 *
 * A call site is considered SAFE when any of these hold:
 *   - it method-optional-chains          bridge?.foo?.(...)
 *   - a truthiness guard exists          if (!bridge.foo) / bridge.foo && / bridge.foo ? ...
 *   - a typeof guard exists              typeof bridge.foo === 'function'
 *   - it is behind a remote-mode branch  isDesktopFsRemoteMode()
 * Anything else must be defined by the shim.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'

const SRC = join(__dirname, '..')
const SHIM = join(SRC, 'web-bridge-shim.ts')

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry === 'dist') continue
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) walk(full, out)
    else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) out.push(full)
  }
  return out
}

/** Top-level keys of the shim's `const shim = { ... }` literal. */
function shimMembers(): Set<string> {
  const text = readFileSync(SHIM, 'utf8')
  const start = text.indexOf('const shim = {')
  expect(start, 'web-bridge-shim.ts must declare `const shim = {`').toBeGreaterThan(-1)

  const keys = new Set<string>()
  let depth = 0

  for (let i = text.indexOf('{', start); i < text.length; i += 1) {
    const ch = text[i]
    if (ch === '{') depth += 1
    else if (ch === '}') {
      depth -= 1
      if (depth === 0) break
    } else if (depth === 1 && ch === '\n') {
      const eol = text.indexOf('\n', i + 1)
      const line = text.slice(i + 1, eol === -1 ? undefined : eol)
      const match = /^\s*(\w+)\s*[:,]/.exec(line)
      if (match) keys.add(match[1])
    }
  }

  return keys
}

const BRIDGE = String.raw`(?:window\.hermesDesktop|bridge\(\)|bridge|desktop)`
const CALL = new RegExp(BRIDGE + String.raw`(?:\?)?\.(\w+)(\?)?\s*\(`, 'g')

/** Members reached through a namespace alias (`const u = bridge.updates`). */
const NAMESPACED = new Set([
  'apply', 'check', 'getBranch', 'setBranch', 'onProgress',
  'get', 'set', 'list', 'save', 'remove', 'setPrimary', 'run', 'test', 'log'
])

/**
 * Files whose bridge calls are unreachable in the web build because the whole
 * surface is gated on a SENTINEL member the shim deliberately omits. Verified
 * by hand — each renders a fallback (or never mounts) instead of calling.
 * Keyed by file, valued by the sentinel that proves the gate.
 */
const SENTINEL_GATED: Record<string, string> = {
  'app/settings/gateway-settings.tsx': 'getConnectionConfig',
  'components/boot-failure-overlay.tsx': 'getConnectionConfig',
  'components/desktop-install-overlay.tsx': 'onBootstrapEvent',
  // Rendered ONLY by desktop-install-overlay (its remote-setup step), which
  // bails before mount unless onBootstrapEvent exists — so this form never
  // reaches its connection-config calls in the web build.
  'components/first-run-remote-form.tsx': 'onBootstrapEvent',
  // contrib disk scan: diskRoots() returns [] without desktopPluginsRoot?.()/
  // agentPluginsRoot?.(), so the readDir/readFileText/watch calls never run —
  // and each is additionally wrapped in try/catch.
  'contrib/runtime-loader.ts': 'desktopPluginsRoot'
}

/** Files exempted by a gate that lives in ANOTHER file (the renderer). */
const GATE_OWNER: Record<string, string> = {
  'components/first-run-remote-form.tsx': 'components/desktop-install-overlay.tsx'
}

/**
 * Individually verified call sites that cannot surface a crash, keyed by
 * `file:method`. Each was checked by hand — keep the reason with the entry.
 */
const VERIFIED_SAFE: Record<string, string> = {
  // Every door runs inside attempt(), which returns false on any throw.
  'contrib/plugin.ts:writeClipboard': 'wrapped in attempt() try/catch',
  // Ternary: the web shim is always mode:'remote', so the bridge branch is
  // dead and gatewayMediaDataUrl() serves the image over the API instead.
  'components/assistant-ui/directive-text.tsx:readFileDataUrl': 'remote branch bypasses the bridge'
}

interface Site {
  file: string
  line: number
  method: string
  text: string
}

function unguardedSites(): Site[] {
  const members = shimMembers()
  const sites: Site[] = []

  for (const file of walk(SRC)) {
    if (file === SHIM || file.endsWith('global.d.ts')) continue
    const body = readFileSync(file, 'utf8')
    if (!body.includes('hermesDesktop')) continue

    const rel = relative(SRC, file).split('\\').join('/')

    // A sentinel gate only excuses a file while the gate is actually present
    // AND the shim still omits the sentinel. If either changes, the file goes
    // back under scrutiny instead of silently staying exempt.
    const sentinel = SENTINEL_GATED[rel]
    if (sentinel) {
      const gateFile = GATE_OWNER[rel] ?? rel
      const gateBody = gateFile === rel ? body : readFileSync(join(SRC, gateFile), 'utf8')
      if (gateBody.includes(sentinel) && !members.has(sentinel)) continue
    }

    const guarded = new Set<string>()
    const collect = (re: RegExp) => {
      for (const m of body.matchAll(re)) {
        for (const name of m[1].split(',')) guarded.add(name.trim())
      }
    }
    collect(new RegExp(String.raw`!\s*` + BRIDGE + String.raw`(?:\?)?\.(\w+)`, 'g'))
    collect(new RegExp(BRIDGE + String.raw`(?:\?)?\.(\w+)\s*&&`, 'g'))
    collect(new RegExp(BRIDGE + String.raw`(?:\?)?\.(\w+)\s*\?\s*`, 'g'))
    collect(new RegExp(String.raw`typeof\s+` + BRIDGE + String.raw`(?:\?)?\.(\w+)\s*===\s*'function'`, 'g'))
    // `if (bridge.foo) { ... bridge.foo(x) }`
    collect(new RegExp(String.raw`if\s*\(\s*` + BRIDGE + String.raw`(?:\?)?\.(\w+)\s*\)`, 'g'))
    collect(/const\s*\{\s*([\w\s,]+?)\}\s*=\s*(?:window\.)?hermesDesktop/g)
    collect(/if\s*\(\s*!\s*(\w+)\s*\)/g)
    collect(/const\s+(\w+)\s*=\s*(?:window\.)?hermesDesktop(?:\?)?\.\w+[\s\S]{0,80}?if\s*\(\s*!\1/g)

    // Remote-mode branches never reach the local bridge in the web build.
    const remoteGated = body.includes('isDesktopFsRemoteMode')

    body.split('\n').forEach((line, index) => {
      for (const m of line.matchAll(CALL)) {
        const [, method, methodOptional] = m
        if (methodOptional) continue
        if (members.has(method) || NAMESPACED.has(method) || guarded.has(method)) continue
        if (VERIFIED_SAFE[`${rel}:${method}`]) continue
        if (remoteGated && /^(readDir|readFileText|readFileDataUrl|writeTextFile|gitRoot)$/.test(method)) continue
        sites.push({ file: rel, line: index + 1, method, text: line.trim() })
      }
    })
  }

  return sites
}

describe('web bridge shim: partial-shim trap', () => {
  it('defines every bridge method the renderer calls without a method-level guard', () => {
    const offenders = unguardedSites()
    const report = offenders.map(s => `  ${s.file}:${s.line}  ${s.method}()  ${s.text}`).join('\n')

    expect(
      offenders,
      offenders.length === 0
        ? ''
        : `Unguarded window.hermesDesktop calls whose method is missing from web-bridge-shim.ts.\n` +
          `Each will throw "<method> is not a function" in the web build.\n` +
          `Fix by (a) adding the member to the shim, or (b) guarding the CALL SITE ` +
          `(bridge.foo?.() / if (!bridge.foo)).\n\n${report}\n`
    ).toEqual([])
  })

  it('keeps the composer image-paste surface present', () => {
    const members = shimMembers()

    // Regression lock: pasting an image threw "saveImageBuffer is not a function".
    expect(members.has('saveImageBuffer')).toBe(true)
    expect(members.has('saveClipboardImage')).toBe(true)
  })

  it('omits members whose presence would BREAK the web build', () => {
    const members = shimMembers()

    // readFileDataUrl: readDesktopFileDataUrlLocalFirst tries the bridge before
    // the gateway, so defining it shadows /api/fs/read-data-url and kills
    // composer thumbnails.
    expect(members.has('readFileDataUrl')).toBe(false)

    // writeClipboard: installClipboardShim would override navigator.clipboard
    // and route the browser user's copies to the SERVER's clipboard.
    expect(members.has('writeClipboard')).toBe(false)
  })

  it('keeps every sentinel-gated exemption honest', () => {
    const members = shimMembers()

    // The allowlist above suppresses findings in whole files. That is only
    // sound while the sentinel really gates them and the shim really omits it
    // — otherwise the exemption would hide live bugs.
    for (const [file, sentinel] of Object.entries(SENTINEL_GATED)) {
      const gateFile = GATE_OWNER[file] ?? file
      const body = readFileSync(join(SRC, gateFile), 'utf8')

      expect(body.includes(sentinel), `${gateFile} no longer references its gate ${sentinel}()`).toBe(true)
      expect(members.has(sentinel), `shim now defines ${sentinel}(), so ${file} is reachable`).toBe(false)
    }
  })
})
