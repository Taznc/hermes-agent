import type { TranslationOverrides } from '../define-locale'

// Traditional Chinese values for the fork-added keys that have a translation.
// Keys absent here fall back to English through defineLocale(), as before.

export const forkZhHant: TranslationOverrides = {
  rightSidebar: {
    terminalUnavailableTitle: '嵌入式終端機無法使用',
    terminalUnavailableBody: '互動式 shell 存取需要 Hermes 桌面應用程式。'
  },
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
      },
      workComplete: '工作已完成',
      workNeedsAttention: '工作需要處理'
    }
  },
  boot: {
    failure: {
      openLogsFailed: '無法開啟日誌資料夾'
    }
  },
  composer: {
    recommend: {
      trigger: '推薦',
      presetLabel: '推薦偏好',
      presets: {
        balanced: '均衡',
        save_codex: '節省 Codex',
        best_quality: '最佳品質'
      },
      presetDescriptions: {
        balanced: '均衡：以合理成本取得良好結果。',
        save_codex: '節省 Codex：優先選擇其他路由，讓 Codex 額度更持久。',
        best_quality: '最佳品質：不計成本選擇最強路由。'
      },
      presetLoading: '正在載入你的偏好…',
      presetUnsaved: '此後端未儲存此設定 — 僅在本次工作階段生效。',
      resultsLabel: '模型推薦',
      privacy: '僅檢查目前的草稿以及附件的名稱與類型。對話紀錄、檔案內容與專案檔案都不會傳送。',
      pending: '檢查中…',
      apply: '套用',
      applyUnconfirmed: '尚未套用 — 請確認此切換，或手動選擇模型。',
      applyFailed: '此切換未能完成。你仍在使用先前的模型 — 請重試或手動選擇模型。',
      applyUnrestored: '此切換未能完成，且無法還原先前的模型。傳送前請先在模型選單中確認。',
      retry: '重試',
      failed: '推薦檢查失敗。',
      unavailable: '目前沒有可用的推薦。請在設定中設定推薦路由以啟用此功能。',
      unsupported: '此 Hermes 後端不支援推薦功能。',
      stale: '你的草稿已變更，這些推薦不再適用。',
      refresh: '重新檢查',
      emptyDraft: '請先寫下草稿，再進行檢查。',
      draftTooLong: '此草稿過長，無法檢查。',
      tooManyAttachments: '附件過多，無法檢查（最多 32 個）。',
      attachmentUnsupported: '附件名稱過長，無法檢查。',
      availability: {
        failed: '可用性檢查失敗',
        fresh: '即時',
        stale: '可用性資料已過期',
        unavailable: '無法使用',
        unsupported: '可用性未知'
      },
      limitReached: '已達上限',
      notAllowed: '你的方案不支援此路由'
    }
  },
  settings: {
    gateway: {
      openLogsFailed: '無法開啟日誌資料夾',
      singleBackendTitle: '單一後端，由伺服器管理',
      singleBackendDesc: (host: string) =>
        `此瀏覽器版本固定連線到 ${host} 上的單一 Hermes 後端。註冊遠端、SSH 或 Cloud 閘道需要使用 Hermes 桌面應用程式。`,
      singleBackendDescNoHost:
        '此瀏覽器版本固定連線到單一 Hermes 後端。註冊遠端、SSH 或 Cloud 閘道需要使用 Hermes 桌面應用程式。',
      singleBackendDocsLink: '將桌面應用程式連線到多個 Hermes 執行個體'
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
