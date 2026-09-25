// Run every workspace check at the same time and report all failures.
//
// The unit of work is a CHECK, and not a workspace. A package that declares
// `check:*` sub-scripts gives one unit for each sub-script. A package with a
// plain `check` gives that. This is the same selection rule the old CI matrix
// used, so the set of commands is unchanged. Only the schedule is different.
//
// This is not `npm run --ws check`, because that command is serial and stops
// at the first workspace that fails. This runs every unit and fails at the
// end with the full list.
//
// The output of each unit goes to a buffer and prints on completion inside a
// group that collapses. Children that write to one stdout together interleave
// their lines, and a failure is then hard to read.
//
// This also runs on a laptop: `node .github/scripts/run-workspace-checks.mjs`.
// `--concurrency N` sets the limit. `--list` prints the units and exits.

import { execFileSync, spawn } from 'node:child_process'
import { availableParallelism } from 'node:os'

const IS_CI = Boolean(process.env.GITHUB_ACTIONS)
// Standard (non-paid) fork runners have 4 cores. vitest, tsc and eslint each
// size their own worker pool from availableParallelism() independently, so
// running several units at once multiplies that oversubscription — the
// exact contention that produced the web/TUI timing failures on PR14. The
// NousResearch org runs this on a 32-core runner (js-tests.yml), where one
// unit's worker pool already uses the whole machine and concurrent units are
// the intended win. A local, non-CI run (laptop) is neither: keep it at full
// parallelism since there is no shared/standard-runner contention to bound.
const IS_NOUS_CI = IS_CI && process.env.GITHUB_REPOSITORY_OWNER === 'NousResearch'
const NPM = process.platform === 'win32' ? 'npm.cmd' : 'npm'

/** @returns {{pkg: string, script: string}[]} */
function discoverUnits() {
  const raw = execFileSync(NPM, ['query', '.workspace'], {
    encoding: 'utf-8',
    shell: process.platform === 'win32',
  })
  /** @type {{location: string, scripts?: Record<string,string>}[]} */
  const pkgs = JSON.parse(raw)

  /** @type {{pkg: string, script: string}[]} */
  const units = []
  for (const pkg of pkgs) {
    const scripts = pkg.scripts || {}
    const subs = Object.keys(scripts).filter((s) => /^check:.+$/.test(s))
    if (subs.length > 0) {
      for (const script of subs) units.push({ pkg: pkg.location, script })
    } else if (scripts.check) {
      units.push({ pkg: pkg.location, script: 'check' })
    }
  }
  return units
}

/** @param {{pkg: string, script: string}} unit */
function runUnit(unit) {
  return new Promise((resolve) => {
    const started = Date.now()
    const child = spawn(NPM, ['run', '--prefix', unit.pkg, unit.script], {
      // Buffer, and do not inherit. Children that share one stdout
      // interleave their lines, and a failure is then hard to read.
      stdio: ['ignore', 'pipe', 'pipe'],
      shell: process.platform === 'win32',
    })
    /** @type {Buffer[]} */
    const chunks = []
    child.stdout.on('data', (c) => chunks.push(c))
    child.stderr.on('data', (c) => chunks.push(c))
    child.on('error', (err) => {
      chunks.push(Buffer.from(`failed to spawn: ${err.message}\n`))
      resolve({ unit, code: 1, output: Buffer.concat(chunks).toString('utf-8'), ms: Date.now() - started })
    })
    child.on('close', (code) => {
      resolve({
        unit,
        code: code ?? 1,
        output: Buffer.concat(chunks).toString('utf-8'),
        ms: Date.now() - started,
      })
    })
  })
}

// process.stdout.write() to a pipe (what GitHub Actions and any `cmd |
// other-cmd` pipeline attach) is asynchronous: a large write can return
// `false` (the kernel pipe buffer is full) and finish flushing later, on
// its own tick. `process.exit()` tears the process down immediately and
// does not wait for that pending flush, so a large buffered failure — a
// check that printed megabytes before dying — could get truncated mid-log
// with no indication anything was cut. Wrapping every write in a
// drain-aware promise, and never force-exiting, makes the full output (and
// the trailing summary/footer after it) land before the process is
// allowed to end.
/** @param {string} text */
function writeOut(text) {
  return new Promise((resolve, reject) => {
    const flushed = process.stdout.write(text, (err) => (err ? reject(err) : resolve()))
    if (flushed) resolve()
  })
}

async function main() {
  const argv = process.argv.slice(2)
  const units = discoverUnits()

  if (units.length === 0) {
    console.error(
      '::error::No workspace package declares a check script — refusing to report green having run nothing.',
    )
    process.exitCode = 1
    return
  }

  if (argv.includes('--list')) {
    for (const u of units) console.log(`${u.pkg} :: ${u.script}`)
    return
  }

  const flagIdx = argv.indexOf('--concurrency')
  const concurrency = Math.max(
    1,
    flagIdx !== -1
      ? Number(argv[flagIdx + 1])
      : IS_CI && !IS_NOUS_CI
        ? 1
        : Math.min(units.length, availableParallelism()),
  )

  console.log(`running ${units.length} checks, up to ${concurrency} at a time:`)
  for (const u of units) console.log(`  ${u.pkg} :: ${u.script}`)
  console.log('')

  const queue = [...units]
  /** @type {{unit: {pkg: string, script: string}, code: number, output: string, ms: number}[]} */
  const results = []

  async function worker() {
    for (;;) {
      const unit = queue.shift()
      if (!unit) return
      const res = await runUnit(unit)
      results.push(res)
      const label = `${res.unit.pkg} :: ${res.unit.script}`
      const secs = (res.ms / 1000).toFixed(1)
      const status = res.code === 0 ? 'PASS' : 'FAIL'
      if (IS_CI) console.log(`::group::${status} ${label} (${secs}s)`)
      else console.log(`----- ${status} ${label} (${secs}s) -----`)
      await writeOut(res.output.endsWith('\n') ? res.output : res.output + '\n')
      if (IS_CI) console.log('::endgroup::')
    }
  }

  await Promise.all(Array.from({ length: Math.min(concurrency, units.length) }, worker))

  const failed = results.filter((r) => r.code !== 0)
  console.log('\n=== summary ===')
  for (const r of [...results].sort((a, b) => b.ms - a.ms)) {
    console.log(
      `  ${r.code === 0 ? 'pass' : 'FAIL'}  ${(r.ms / 1000).toFixed(1).padStart(6)}s  ${r.unit.pkg} :: ${r.unit.script}`,
    )
  }

  if (failed.length > 0) {
    for (const r of failed) console.error(`::error::${r.unit.pkg} :: ${r.unit.script} failed`)
    console.error(`::error::${failed.length} of ${results.length} checks failed`)
    process.exitCode = 1
    return
  }
  console.log(`\nall ${results.length} checks passed`)
}

await main()
