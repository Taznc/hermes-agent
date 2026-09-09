/** Whether this renderer can create a real interactive PTY-backed shell. */
export function interactiveTerminalAvailable(): boolean {
  return typeof window !== 'undefined' && Boolean(window.hermesDesktop?.terminal)
}
