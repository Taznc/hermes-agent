import type { TranslationOverrides } from '../define-locale'

// Japanese values for the fork-added keys that have a translation. Keys absent
// here fall back to English through defineLocale(), exactly as before.

export const forkJa: TranslationOverrides = {
  rightSidebar: {
    terminalUnavailableTitle: '埋め込みターミナルは利用できません',
    terminalUnavailableBody: '対話型シェルへのアクセスには Hermes デスクトップアプリが必要です。'
  },
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
  composer: {
    recommend: {
      trigger: 'おすすめ',
      presetLabel: 'おすすめの基準',
      presets: {
        balanced: 'バランス',
        save_codex: 'Codex を温存',
        best_quality: '品質優先'
      },
      presetDescriptions: {
        balanced: 'バランス：妥当なコストで十分な品質を狙います。',
        save_codex: 'Codex を温存：他のルートを優先し、Codex の利用枠を長持ちさせます。',
        best_quality: '品質優先：コストにかかわらず最も強力なルートを選びます。'
      },
      presetLoading: '設定を読み込み中…',
      presetUnsaved: 'このバックエンドでは保存されません — 今回のセッションのみ有効です。',
      resultsLabel: 'モデルのおすすめ',
      privacy:
        '現在の下書きと添付ファイルの名前・種類のみを確認します。会話履歴、ファイルの内容、プロジェクトファイルは送信されません。',
      pending: '確認中…',
      apply: '適用',
      applyUnconfirmed: 'まだ適用されていません — 切り替えを確認するか、手動でモデルを選択してください。',
      applyFailed: '切り替えできませんでした。以前のモデルのままです — もう一度試すか、手動で選択してください。',
      applyUnrestored:
        '切り替えが完了せず、以前のモデルにも戻せませんでした。送信する前にモデルメニューで確認してください。',
      retry: '再試行',
      failed: 'おすすめの取得に失敗しました。',
      unavailable: '利用できるおすすめがありません。設定でおすすめ用ルーターを構成してください。',
      unsupported: 'この Hermes バックエンドはおすすめ機能に対応していません。',
      stale: '下書きが変更されたため、これらのおすすめは適用できません。',
      refresh: '再確認',
      emptyDraft: '先に下書きを入力してから確認してください。',
      draftTooLong: 'この下書きは長すぎて確認できません。',
      tooManyAttachments: '添付ファイルが多すぎて確認できません（最大 32 件）。',
      attachmentUnsupported: '添付ファイル名が長すぎて確認できません。',
      availability: {
        failed: '空き状況の確認に失敗',
        fresh: '最新',
        stale: '空き状況が古い可能性',
        unavailable: '利用不可',
        unsupported: '空き状況は不明'
      },
      limitReached: '上限に達しました',
      notAllowed: '現在のプランでは利用できません'
    }
  },
  settings: {
    gateway: {
      openLogsFailed: 'ログフォルダを開けませんでした',
      singleBackendTitle: 'バックエンドは 1 つ、サーバー側で管理',
      singleBackendDesc: (host: string) =>
        `このブラウザ版は ${host} 上の単一の Hermes バックエンドに固定されています。リモート・SSH・Cloud のゲートウェイを登録するには Hermes デスクトップアプリが必要です。`,
      singleBackendDescNoHost:
        'このブラウザ版は単一の Hermes バックエンドに固定されています。リモート・SSH・Cloud のゲートウェイを登録するには Hermes デスクトップアプリが必要です。',
      singleBackendDocsLink: 'デスクトップを複数の Hermes インスタンスに接続する'
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
