// web-prod-serve.ts — fork-owned helpers for serving the web-desktop renderer
// as a PRODUCTION build (`vite build` + `vite preview`, both with
// --config vite.config.web.ts) instead of the Vite dev server.
//
// The dev server ships ~1.9k unbundled ESM requests (~113 MB with inline
// sourcemaps, Cache-Control: no-cache). Behind Traefik + Authelia every request
// pays a forward-auth round trip, so a phone over cellular took minutes to
// boot. The build is a few dozen content-hashed chunks; these helpers add what
// `vite preview` lacks for that: brotli/gzip precompression at build time,
// serving the precompressed siblings, and immutable caching for /assets/*.
//
// No new dependencies: node:zlib + node:fs only (vite 8's preview already
// on-the-fly gzips anything not precompressed via its bundled @polka/compression).
import fs from 'node:fs'
import path from 'node:path'
import { promisify } from 'node:util'
import zlib from 'node:zlib'

/** Extensions worth compressing (fonts/images are already compressed). */
const COMPRESSIBLE: Record<string, string> = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.txt': 'text/plain; charset=utf-8',
  '.wasm': 'application/wasm',
  '.webmanifest': 'application/manifest+json'
}

const MIN_BYTES = 1024
export const IMMUTABLE = 'public, max-age=31536000, immutable'
export const REVALIDATE = 'no-cache'

const ENCODINGS = [
  { name: 'br', ext: '.br', re: /\bbr\b/ },
  { name: 'gzip', ext: '.gz', re: /\bgzip\b/ }
] as const

const brotli = promisify(zlib.brotliCompress)
const gzip = promisify(zlib.gzip)

function* walk(dir: string): Generator<string> {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const abs = path.join(dir, entry.name)

    if (entry.isDirectory()) {
      yield* walk(abs)
    } else if (entry.isFile()) {
      yield abs
    }
  }
}

/** Write `<file>.br` and `<file>.gz` next to every compressible file in
 *  `outDir` (kept only when smaller than the original). Returns the count. */
export async function precompressDir(outDir: string): Promise<{ files: number; rawBytes: number; brBytes: number }> {
  const targets = [...walk(outDir)].filter(file => {
    const ext = path.extname(file)

    return ext in COMPRESSIBLE && fs.statSync(file).size >= MIN_BYTES
  })

  let rawBytes = 0
  let brBytes = 0

  await Promise.all(
    targets.map(async file => {
      const raw = fs.readFileSync(file)

      const [br, gz] = await Promise.all([
        brotli(raw, {
          params: {
            [zlib.constants.BROTLI_PARAM_QUALITY]: 11,
            [zlib.constants.BROTLI_PARAM_SIZE_HINT]: raw.length
          }
        }),
        gzip(raw, { level: 9 })
      ])

      rawBytes += raw.length
      brBytes += Math.min(br.length, raw.length)

      if (br.length < raw.length) {
        fs.writeFileSync(`${file}.br`, br)
      }

      if (gz.length < raw.length) {
        fs.writeFileSync(`${file}.gz`, gz)
      }
    })
  )

  return { files: targets.length, rawBytes, brBytes }
}

/** `/` and the Electron entry (`/index.html`) both serve the web entry.
 *  Rewrite (not redirect): the `?token=` bootstrap query is kept verbatim. */
export function rewriteRootUrl(url: string): string {
  const queryAt = url.indexOf('?')
  const pathname = queryAt === -1 ? url : url.slice(0, queryAt)
  const search = queryAt === -1 ? '' : url.slice(queryAt)

  return pathname === '/' || pathname === '/index.html' ? `/index-web.html${search}` : url
}

export type Req = { url?: string; method?: string; headers: Record<string, string | string[] | undefined> }

export type Res = {
  statusCode: number
  setHeader: (name: string, value: string | number) => unknown
  end: (chunk?: unknown) => unknown
}

type Next = () => void

/** The file under `distDir` a request names, or null (outside the dir, a
 *  directory, missing, or not a GET/HEAD — e.g. a proxied /api call). */
export function distFileFor(distDir: string, req: Req): string | null {
  if (req.method && req.method !== 'GET' && req.method !== 'HEAD') {
    return null
  }

  let pathname = (req.url ?? '/').split('?')[0]

  try {
    pathname = decodeURIComponent(pathname)
  } catch {
    return null
  }

  const root = path.resolve(distDir)
  const abs = path.resolve(root, `.${pathname}`)

  if (abs !== root && !abs.startsWith(root + path.sep)) {
    return null
  }

  try {
    return fs.statSync(abs).isFile() ? abs : null
  } catch {
    return null
  }
}

/** Cache policy for a built file: content-hashed /assets/* never change under
 *  their name; everything else (index-web.html, the service worker, public
 *  icons, emojibase data) must revalidate so a new release is picked up. */
export function cacheControlFor(distDir: string, file: string): string {
  const rel = path.relative(path.resolve(distDir), file).split(path.sep)

  return rel[0] === 'assets' && rel.length > 1 ? IMMUTABLE : REVALIDATE
}

/** connect middleware for `vite preview`: sets Cache-Control on every built
 *  file and serves the precompressed `.br` / `.gz` sibling when the client
 *  accepts it. Anything else falls through (to the proxy, then vite's sirv,
 *  which keeps the Cache-Control set here and on-the-fly gzips via
 *  @polka/compression). Non-dist paths (/api/*) are never touched. */
export function prodStaticMiddleware(distDir: string) {
  return (req: Req, res: Res, next: Next): void => {
    const file = distFileFor(distDir, req)

    if (!file) {
      return next()
    }

    res.setHeader('Cache-Control', cacheControlFor(distDir, file))
    const type = COMPRESSIBLE[path.extname(file)]

    if (!type) {
      return next()
    }

    res.setHeader('Vary', 'Accept-Encoding')
    const accept = String(req.headers['accept-encoding'] ?? '')

    for (const enc of ENCODINGS) {
      if (!enc.re.test(accept)) {
        continue
      }

      let stats: fs.Stats

      try {
        stats = fs.statSync(file + enc.ext)
      } catch {
        continue
      }

      const etag = `W/"${enc.name}-${stats.size}-${stats.mtime.getTime()}"`
      res.setHeader('ETag', etag)

      if (req.headers['if-none-match'] === etag) {
        res.statusCode = 304
        res.end()

        return
      }

      res.statusCode = 200
      res.setHeader('Content-Type', type)
      res.setHeader('Content-Encoding', enc.name)
      res.setHeader('Content-Length', stats.size)
      res.setHeader('Last-Modified', stats.mtime.toUTCString())

      if (req.method === 'HEAD') {
        res.end()

        return
      }

      fs.createReadStream(file + enc.ext).pipe(res as unknown as NodeJS.WritableStream)

      return
    }

    next()
  }
}
