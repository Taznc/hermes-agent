import type { TranslationOverrides } from '../define-locale'

// Arabic values for the fork-added keys that have a translation. Keys absent
// here fall back to English through defineLocale(), exactly as before.

export const forkAr: TranslationOverrides = {
  boot: {
    failure: {
      openLogsFailed: 'تعذّر فتح مجلد السجلات'
    }
  },
  settings: {
    gateway: {
      openLogsFailed: 'تعذّر فتح مجلد السجلات'
    }
  },
  sidebar: {
    row: {
      providerConfigured: family => `النموذج المُهيأ: ${family}`,
      providerVia: family => `عبر ${family}`,
      providerConfiguredVia: (configuredFamily, servedFamily) =>
        `النموذج المُهيأ: ${configuredFamily}، ويُخدَم حاليًا عبر ${servedFamily}`
    }
  },
  errors: {
    openLogsFailed: 'تعذّر فتح مجلد السجلات'
  }
}
