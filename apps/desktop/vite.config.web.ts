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
// Run:  npx vite --config vite.config.web.ts
// Open: http://127.0.0.1:<port>/index-web.html?token=<session-token>
import { defineConfig, mergeConfig, type ConfigEnv, type UserConfig } from 'vite'
import { execSync } from 'node:child_process'

import baseConfig from './vite.config'

const BACKEND = process.env.HERMES_WEB_SPIKE_BACKEND ?? 'http://127.0.0.1:9219'
const PORT = Number(process.env.HERMES_WEB_PORT ?? 5175)
const PUBLIC_HOST = process.env.HERMES_WEB_PUBLIC_HOST ?? 'hermes-desktop.jashworth.com'

/** Real git provenance for this checkout so the renderer's fork-build marker
 *  (dev branch: src/lib/fork-build-marker.ts) can render honestly. Falls back
 *  to empty strings outside a git tree. */
function gitBuildInfo() {
  const run = (cmd: string): string => {
    try {
      return execSync(cmd, { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim()
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

export default defineConfig(async (env: ConfigEnv): Promise<UserConfig> => {
  const resolved = await (typeof baseConfig === 'function' ? baseConfig(env) : baseConfig)

  return mergeConfig(resolved, {
    define: {
      // Consumed by src/web-bridge-shim.ts getVersion(); JSON-stringified so
      // it lands as an object literal in the served module.
      __HERMES_WEB_BUILD_INFO__: JSON.stringify(gitBuildInfo())
    },
    server: {
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
  } satisfies UserConfig)
})
