import type { TranslationOverrides } from '../define-locale'

// Traditional Chinese values for the fork-added keys that have a translation.
// Keys absent here fall back to English through defineLocale(), as before.

export const forkZhHant: TranslationOverrides = {
  boot: {
    failure: {
      openLogsFailed: '無法開啟日誌資料夾'
    }
  },
  settings: {
    gateway: {
      openLogsFailed: '無法開啟日誌資料夾'
    }
  },
  sidebar: {
    row: {
      providerConfigured: family => `設定的模型：${family}`,
      providerVia: family => `經由 ${family}`,
      providerConfiguredVia: (configuredFamily, servedFamily) =>
        `設定的模型：${configuredFamily}，目前經由 ${servedFamily} 提供服務`
    }
  },
  errors: {
    openLogsFailed: '無法開啟日誌資料夾'
  }
}
