// vite.config.web.ts — SPIKE: serve the desktop renderer as a plain web page.
//
// Wraps the real vite.config.ts (function config) and overlays:
//   - /api proxy (HTTP + WS) to a private loopback `hermes serve`
//     (HERMES_WEB_SPIKE_BACKEND, default http://127.0.0.1:9219)
//   - port 5175 so the normal Electron dev flow on 5174 is untouched
//
// Run:  npx vite --config vite.config.web.ts
// Open: http://127.0.0.1:5175/index-web.html?token=<session-token>
//
// UNTRACKED SPIKE FILE — not part of the app. Do not commit without review.
import { defineConfig, mergeConfig, type ConfigEnv, type UserConfig } from 'vite'

import baseConfig from './vite.config'

const BACKEND = process.env.HERMES_WEB_SPIKE_BACKEND ?? 'http://127.0.0.1:9219'

export default defineConfig(async (env: ConfigEnv): Promise<UserConfig> => {
  const resolved = await (typeof baseConfig === 'function' ? baseConfig(env) : baseConfig)

  return mergeConfig(resolved, {
    server: {
      // 0.0.0.0 so the operator can open the spike from another machine on
      // the LAN. The backend itself stays loopback-only; only this vite
      // process is exposed, and every /api call still requires the session
      // token. SPIKE-ONLY tradeoff — a real deployment terminates TLS+auth
      // in front.
      host: '0.0.0.0',
      port: 5175,
      strictPort: true,
      // Vite 8 rejects requests whose Host header it doesn't recognize (DNS
      // rebinding guard). Allow the Traefik-fronted public name.
      allowedHosts: ['hermes-desktop.jashworth.com'],
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
