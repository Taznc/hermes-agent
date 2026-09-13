// Fork-owned: process-constant window capabilities handed to preload via
// `webPreferences.additionalArguments` instead of a first-paint-stalling
// `ipcRenderer.sendSync` round-trip (see the anchor site in main.ts).
//
// The renderer needs translucency support (glass/vibrancy) and the HUD's
// windowing profile (X11 vs Wayland vs native desktop) before its first
// paint. Both used to be answered by a preload `ipcRenderer.sendSync`,
// which stalls the renderer's first script on a round-trip into main —
// coupling first paint to whatever main is busy doing (a slow backend
// probe, boot). Neither value ever changes for the life of the process, so
// we compute it once and hand it to preload via
// `webPreferences.additionalArguments`: zero IPC, no stall, same answer
// every window gets.

import { hudWindowingView, resolveHudWindowing } from '../hud-windowing'

export interface WindowCapsOptions {
  glassSupported: boolean
  translucencySupported: boolean
  platform: string
  env: NodeJS.ProcessEnv
  argv: string[]
}

export interface WindowCapsIntegration {
  /** The `--hermes-window-caps=<json>` argument preload parses off argv. */
  WINDOW_CAPS_ARGUMENT: string
  /**
   * Every BrowserWindow whose preload is PRELOAD_PATH must carry the caps
   * argument so the renderer's first script sees translucency/HUD
   * capabilities without an IPC round-trip. Centralized so a new window
   * kind can't forget it.
   */
  withWindowCapsArgument(webPreferences: Record<string, unknown>): Record<string, unknown>
}

export function createWindowCapsIntegration(options: WindowCapsOptions): WindowCapsIntegration {
  const hud = hudWindowingView(resolveHudWindowing(options.platform, options.env, options.argv))

  const WINDOW_CAPS_ARGUMENT = `--hermes-window-caps=${encodeURIComponent(
    JSON.stringify({ glass: options.glassSupported, translucency: options.translucencySupported, hud })
  )}`

  return {
    WINDOW_CAPS_ARGUMENT,
    withWindowCapsArgument(webPreferences: Record<string, unknown>): Record<string, unknown> {
      return {
        ...webPreferences,
        additionalArguments: [...((webPreferences.additionalArguments as string[]) || []), WINDOW_CAPS_ARGUMENT]
      }
    }
  }
}
