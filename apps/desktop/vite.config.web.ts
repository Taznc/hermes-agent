import { execSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// vite.config.web.ts — SPIKE: serve the desktop renderer as a plain web page.
//
// Wraps the real vite.config.ts (function config) and overlays:
//   - /api proxy (HTTP + WS) to a private loopback `hermes serve`
//     (HERMES_WEB_SPIKE_BACKEND, default http://127.0.0.1:9219)
//   - HERMES_WEB_PORT (default 5175) so two checkouts (stable + dev) can run
//     side by side; the normal Electron dev flow on 5174 is untouched either way
//   - HERMES_WEB_PUBLIC_HOST: the Traefik-fronted hostname this instance is
//     served under (vite 8 rejects unknown Host headers — DNS rebinding guard)
//
// Dev (unbundled ESM, HMR):
//   npx vite --config vite.config.web.ts
// Production (bundled, minified, content-hashed, precompressed; same port,
// proxy, host guard and `/` rewrite — see scripts/web-prod-serve.ts):
//   npx vite build --config vite.config.web.ts     # -> dist-web/
//   npx vite preview --config vite.config.web.ts
// Open: http://127.0.0.1:<port>/index-web.html?token=<session-token>
import { type ConfigEnv, defineConfig, mergeConfig, type Plugin, type UserConfig } from 'vite'

import { precompressDir, prodStaticMiddleware, rewriteRootUrl } from './scripts/web-prod-serve'
import baseConfig from './vite.config'

const __dirname: string = path.dirname(fileURLToPath(import.meta.url))
// Fork-owned output dir, separate from the Electron renderer's dist/. It is
// self-ignoring (a `*` .gitignore is written into it after each build), so no
// upstream .gitignore edit is needed.
const OUT_DIR = 'dist-web'

const BACKEND = process.env.HERMES_WEB_SPIKE_BACKEND ?? 'http://127.0.0.1:9219'
const PORT = Number(process.env.HERMES_WEB_PORT ?? 5175)
const PUBLIC_HOST = process.env.HERMES_WEB_PUBLIC_HOST ?? 'hermes-desktop.jashworth.com'

/** Real git provenance for this checkout so the renderer's fork-build marker
 *  (dev branch: src/lib/fork-build-marker.ts) can render honestly. Falls back
 *  to empty strings outside a git tree. */
function gitBuildInfo() {
  const run = (cmd: string): string => {
    try {
      return execSync(cmd, { stdio: ['ignore', 'pipe', 'ignore'] })
        .toString()
        .trim()
    } catch {
      return ''
    }
  }

  return {
    branch: run('git branch --show-current'),
    commit: run('git rev-parse HEAD'),
    dirty: run('git status --porcelain --untracked-files=no') !== ''
  }
}

/** Serve the web entry at `/`.
 *
 * `index.html` is the ELECTRON entry: it loads /src/main.tsx directly, with no
 * web-bridge-shim, so `window.hermesDesktop` is undefined and the first bare
 * `window.hermesDesktop.x` call site (use-statusbar-items' getDevMainBundleStale)
 * throws into the root error boundary. Only `index-web.html` loads the shim
 * first. Visiting the bare hostname is the natural thing to do, so rewrite the
 * root (and the Electron entry) onto the web entry rather than leaving a
 * guaranteed crash on the front door. Rewrite, not redirect: the URL stays
 * clean and the `?token=` bootstrap query is preserved verbatim.
 */
function webRootEntryPlugin(): Plugin {
  const rewrite: MiddlewareFn = (req, _res, next) => {
    req.url = rewriteRootUrl(req.url ?? '/')
    next()
  }

  return {
    name: 'hermes-web-root-entry',
    configureServer(server) {
      server.middlewares.use(rewrite)
    },
    // `vite preview`: these run before preview's proxy, compression and sirv.
    configurePreviewServer(server) {
      server.middlewares.use(rewrite)
      server.middlewares.use(
        prodStaticMiddleware(path.resolve(server.config.root, server.config.build.outDir)) as MiddlewareFn
      )
    }
  }
}

/** Build-only: brotli + gzip siblings for every compressible output file, and
 *  a self-ignoring .gitignore so dist-web/ never shows up as untracked. */
function webPrecompressPlugin(): Plugin {
  let outDir = ''

  return {
    name: 'hermes-web-precompress',
    apply: 'build',
    configResolved(config) {
      outDir = path.resolve(config.root, config.build.outDir)
    },
    async closeBundle() {
      const started = Date.now()
      const { files, rawBytes, brBytes } = await precompressDir(outDir)
      fs.writeFileSync(path.join(outDir, '.gitignore'), '*\n')
      const mb = (n: number) => (n / 1048576).toFixed(1)
      console.log(
        `[hermes-web-precompress] ${files} files, ${mb(rawBytes)} MB -> ${mb(brBytes)} MB br in ${Date.now() - started} ms`
      )
    }
  }
}

type MiddlewareFn = (req: { url?: string }, res: any, next: () => void) => void

export default defineConfig(async (env: ConfigEnv): Promise<UserConfig> => {
  const resolved = await (typeof baseConfig === 'function' ? baseConfig(env) : baseConfig)

  // One host/port/proxy contract for both the dev server and `vite preview`
  // (the base config pins its own preview host/port, so preview must be set
  // explicitly rather than inherited from `server`).
  const serve = {
    // 0.0.0.0 so the Traefik host (and LAN, if UFW admits it) can reach the
    // instance. The backend itself stays loopback-only; only this vite
    // process is exposed, and every /api call still requires the session
    // token. SPIKE-ONLY tradeoff — a real deployment terminates TLS+auth
    // in front.
    host: '0.0.0.0',
    port: PORT,
    strictPort: true,
    allowedHosts: [PUBLIC_HOST],
    proxy: {
      '/api': {
        target: BACKEND,
        changeOrigin: true,
        ws: true,
        // The loopback backend 403s WS upgrades whose Origin is not a
        // loopback origin (June 2026 hardening). The proxy leg is
        // same-machine, so pin Origin to the backend's own origin.
        headers: { Origin: BACKEND }
      }
    }
  }

  return mergeConfig(resolved, {
    // Served at the web root (not file://), so absolute asset URLs; the app
    // routes with HashRouter, so no path ever needs a relative base.
    base: '/',
    plugins: [webRootEntryPlugin(), webPrecompressPlugin()],
    define: {
      // Consumed by src/web-bridge-shim.ts getVersion(); JSON-stringified so
      // it lands as an object literal in the served module.
      __HERMES_WEB_BUILD_INFO__: JSON.stringify(gitBuildInfo())
    },
    build: {
      outDir: OUT_DIR,
      emptyOutDir: true,
      sourcemap: false,
      // The Electron launch-time manifest has no consumer on the web.
      manifest: false,
      rolldownOptions: {
        input: path.resolve(__dirname, 'index-web.html')
      }
    },
    server: serve,
    preview: serve
  } satisfies UserConfig)
})
