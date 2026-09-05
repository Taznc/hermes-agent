import type { TranslationOverrides } from '../define-locale'

// Japanese values for the fork-added keys that have a translation. Keys absent
// here fall back to English through defineLocale(), exactly as before.

export const forkJa: TranslationOverrides = {
  boot: {
    failure: {
      openLogsFailed: 'ログフォルダを開けませんでした'
    }
  },
  settings: {
    gateway: {
      openLogsFailed: 'ログフォルダを開けませんでした'
    }
  },
  sidebar: {
    row: {
      providerConfigured: family => `設定済みモデル: ${family}`,
      providerVia: family => `${family} 経由`,
      providerConfiguredVia: (configuredFamily, servedFamily) =>
        `設定済みモデル: ${configuredFamily}（現在は ${servedFamily} 経由で応答中）`
    }
  },
  errors: {
    openLogsFailed: 'ログフォルダを開けませんでした'
  }
}
