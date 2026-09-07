import type { TranslationOverrides } from '../define-locale'

// Arabic values for the fork-added keys that have a translation. Keys absent
// here fall back to English through defineLocale(), exactly as before.

export const forkAr: TranslationOverrides = {
  boot: {
    failure: {
      openLogsFailed: 'تعذّر فتح مجلد السجلات'
    }
  },
  composer: {
    recommend: {
      trigger: 'اقترح',
      presetLabel: 'تفضيل الاقتراح',
      presets: {
        balanced: 'متوازن',
        save_codex: 'توفير Codex',
        best_quality: 'أفضل جودة'
      },
      presetUnsaved: 'لم يُحفَظ على هذه الخدمة الخلفية — ينطبق على هذه الجلسة فقط.',
      resultsLabel: 'اقتراحات النماذج',
      privacy:
        'يفحص مسودتك الحالية وأسماء المرفقات فقط. لا يتم إرسال سجل المحادثة أو محتويات الملفات أو ملفات المشروع.',
      pending: 'جارٍ الفحص…',
      apply: 'تطبيق',
      applyFailed: 'تعذّر التطبيق — أعد المحاولة أو اختر نموذجًا يدويًا.',
      retry: 'إعادة المحاولة',
      failed: 'فشل فحص الاقتراح.',
      unavailable: 'لا يوجد اقتراح متاح. اضبط موجّه الاقتراحات في الإعدادات لتفعيل هذه الميزة.',
      unsupported: 'هذه الخدمة الخلفية من Hermes لا تدعم الاقتراحات.',
      draftTooLong: 'هذه المسودة أطول من أن تُفحَص.',
      tooManyAttachments: 'عدد المرفقات كبير جدًا للفحص (32 كحد أقصى).',
      attachmentUnsupported: 'اسم أحد المرفقات أطول من أن يُفحَص.',
      availability: {
        failed: 'فشل فحص التوفر',
        fresh: 'مباشر',
        stale: 'بيانات توفر قديمة',
        unavailable: 'غير متاح',
        unsupported: 'التوفر غير معروف'
      }
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
