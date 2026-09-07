import type { TranslationOverrides } from '../define-locale'

// Japanese values for the fork-added keys that have a translation. Keys absent
// here fall back to English through defineLocale(), exactly as before.

export const forkJa: TranslationOverrides = {
  assistant: {
    thread: {
      review: {
        showDetails: '詳細を表示',
        showDetailsWithFailures: count => `詳細を表示（${count} 件失敗）`,
        hideDetails: '詳細を隠す',
        hideRecordDetails: '記録の詳細を隠す',
        legacyDetail: 'この古い Hermes バージョンでは詳細を利用できません。',
        target: target =>
          ({ memory: 'メモリ', skill: 'スキル', user: 'ユーザープロフィール' })[target] ?? 'レビュー項目',
        operation: operation =>
          ({ add: '追加', create: '作成', edit: '編集', patch: '更新', remove: '削除', replace: '置換' })[operation] ??
          '更新',
        recordSummary: (target, operation, state) => `${target} · ${operation} · ${state}`,
        showRecordDetails: target => `${target} のレビュー詳細を表示`,
        state: state =>
          ({ completed: '完了', declined: '却下', failed: '失敗', no_op: '変更なし', skipped: 'スキップ' })[state] ??
          '完了'
      },
      workComplete: '作業が完了しました',
      workNeedsAttention: '作業に対応が必要です'
    }
  },
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
