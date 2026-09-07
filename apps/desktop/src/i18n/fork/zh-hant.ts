import type { TranslationOverrides } from '../define-locale'

// Traditional Chinese values for the fork-added keys that have a translation.
// Keys absent here fall back to English through defineLocale(), as before.

export const forkZhHant: TranslationOverrides = {
  assistant: {
    thread: {
      review: {
        showDetails: '顯示詳細資料',
        showDetailsWithFailures: count => `顯示詳細資料（${count} 項失敗）`,
        hideDetails: '隱藏詳細資料',
        hideRecordDetails: '隱藏記錄詳細資料',
        legacyDetail: '此舊版 Hermes 無法提供詳細資料。',
        target: target => ({ memory: '記憶', skill: '技能', user: '使用者設定檔' })[target] ?? '審查項目',
        operation: operation =>
          ({ add: '新增', create: '建立', edit: '編輯', patch: '更新', remove: '移除', replace: '取代' })[operation] ??
          '更新',
        recordSummary: (target, operation, state) => `${target} · ${operation} · ${state}`,
        showRecordDetails: target => `顯示${target}審查詳細資料`,
        state: state =>
          ({ completed: '已完成', declined: '已拒絕', failed: '失敗', no_op: '無變更', skipped: '已略過' })[state] ??
          '已完成'
      }
    }
  },
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
