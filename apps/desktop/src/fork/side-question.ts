// Side questions asked from an Ask card (fork-ask plugin). The plugin starts the question
// text with this marker, and the card renders the answer itself, so the core's persistent
// `[btw "…"]` transcript line (the whole prompt, card description included) is skipped.
// Keep the marker in sync with SIDE_MARKER in desktop-plugins/fork-ask/plugin.js.
export const ASK_SIDE_MARKER = '[fork-ask side question]'

export function isAskSideQuestion(question: unknown): boolean {
  return typeof question === 'string' && question.trimStart().startsWith(ASK_SIDE_MARKER)
}
