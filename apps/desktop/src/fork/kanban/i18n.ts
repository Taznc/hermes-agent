/**
 * Strings for the fork's Kanban additions (focus mode + All Boards), registered
 * under their own plugin id (`fork-kanban`) so upstream's kanban catalog and
 * the core locale files stay untouched. Text is dev's, verbatim. Keys missing
 * from a locale fall back to English (plugin-i18n's DEFAULT_LOCALE fallback).
 */

import type { PluginLocaleBundles, PluginMessages } from '@/i18n/plugin-i18n'

export const en = {
  depBlockedByHeading: (n: number) => `Blocked by ${n}`,
  depBlocksHeading: (n: number) => `Blocks ${n}`,
  depChevrons: 'Chevrons',
  depClearFocus: 'Clear focus',
  depFlow: 'Moving dots',
  depFocusChain: 'Full chain',
  depFocusDirect: 'Direct links',
  depFocused: 'Focused',
  depLegendHead: 'the arrowhead lands on the card that is held up',
  depLegendLead: "Line colour = the blocker's status:",
  depMissing: 'not on this board',
  depMissingTip: 'This linked task was deleted, or is hidden by the current tenant/archive filter.',
  depNothingBlocks: 'Nothing',
  depNothingWaits: 'Nothing waits on it',
  depRowTip: 'Hover to highlight its line · click to focus this card',
  depStatusCount: (n: number, status: string) => `${n} ${status.toLowerCase()}`,
  depVerdictClear: (total: number) =>
    total === 1 ? 'Its blocker is done — ready to move' : `All ${total} blockers done — ready to move`,
  depVerdictNone: 'No blockers — nothing holds this card',
  depVerdictStalled: (open: number) =>
    open === 1 ? 'Stalled — its open blocker is On hold' : `Stalled — all ${open} open blockers are On hold`,
  depVerdictWaiting: (open: number, parts: string, cleared: number) => `Waiting on ${open}: ${parts}${cleared ? ` · ${cleared} done` : ''}`,
  depGap: (n: number) => (n === 1 ? '+1 card' : `+${n} cards`),
  depGapShow: 'Not linked to the focused card — click to show',
  depFocusHint: 'Click a card to trace its dependency chain · Esc to clear',
  allBoards: 'All Boards',
  allBoardsTip: 'Show every board at once',
  boardChipTip: (name: string) => `Show or hide ${name}`,
  boardsError: (n: number) => (n === 1 ? '1 board could not be read' : `${n} boards could not be read`),
  dispatchControl: 'Dispatch',
  pauseDispatch: 'Pause dispatch',
  resumeDispatch: 'Resume dispatch',
  draining: (running: number) => `${running} running — draining`,
  safeToRestart: '0 running — safe to restart',
  runningCount: (running: number) => `${running} running`,
  statusUnknown: (boards: string) => `running count unknown — could not read ${boards}`,
  statusStale: 'status refresh failed — last known state, running count unknown',
  dispatchRunning: 'Dispatching normally',
  pauseBusy: 'A dispatch tick is in progress — try pausing again in a moment.',
  pauseAllBoards: 'Pause all boards',
  resumeAllBoards: 'Resume all boards',
  boardsPaused: (paused: number, total: number) => `${paused} of ${total} boards paused`,
  pauseHint:
    'Stops new workers being claimed and spawned. Workers already running are never killed — wait for the count to reach 0 before restarting the gateway.',
  dispatchPaused: 'Dispatch paused',
  estopEngaged: (reason: string) =>
    `Hermes is paused (hermes pause${reason ? `: ${reason}` : ''}) — no board dispatches until \`hermes resume\`.`
} satisfies PluginMessages

const ja = {
  depBlockedByHeading: (n: number) => `ブロック元 ${n}`,
  depBlocksHeading: (n: number) => `ブロック先 ${n}`,
  depChevrons: 'シェブロン',
  depClearFocus: 'フォーカス解除',
  depFlow: '流れる点',
  depFocusChain: '全チェーン',
  depFocusDirect: '直接リンク',
  depFocused: 'フォーカス中',
  depLegendHead: '矢印の先が止められているカードです',
  depLegendLead: '線の色 = ブロック元のステータス:',
  depMissing: 'このボードにありません',
  depMissingTip: 'リンク先のタスクは削除されたか、現在のテナント／アーカイブ絞り込みで非表示です。',
  depNothingBlocks: 'なし',
  depNothingWaits: '待機しているカードはありません',
  depRowTip: 'ホバーで線を強調 · クリックでこのカードにフォーカス',
  depStatusCount: (n: number, status: string) => `${status} ${n}`,
  depVerdictClear: (total: number) =>
    total === 1 ? 'ブロック元は完了 — 移動できます' : `${total} 件のブロック元がすべて完了 — 移動できます`,
  depVerdictNone: 'ブロックなし — このカードを止めているものはありません',
  depVerdictStalled: (open: number) =>
    open === 1 ? '停滞 — 未完了のブロック元が保留中です' : `停滞 — 未完了のブロック元 ${open} 件すべてが保留中です`,
  depVerdictWaiting: (open: number, parts: string, cleared: number) => `${open} 件を待機中: ${parts}${cleared ? ` · ${cleared} 件完了` : ''}`,
  depGap: (n: number) => `+${n} 件`,
  depGapShow: 'フォーカス中のカードとは無関係です — クリックで表示',
  depFocusHint: 'カードをクリックすると依存関係をたどれます · Esc で解除',
  allBoards: 'すべてのボード',
  dispatchControl: 'ディスパッチ',
  pauseDispatch: 'ディスパッチを一時停止',
  resumeDispatch: 'ディスパッチを再開',
  dispatchPaused: 'ディスパッチ一時停止中',
  draining: (running: number) => `実行中 ${running} 件 — 排出中`,
  safeToRestart: '実行中 0 件 — 再起動しても安全',
  runningCount: (running: number) => `実行中 ${running} 件`,
  statusUnknown: (boards: string) => `実行中の件数は不明 — ${boards} を読み取れませんでした`,
  statusStale: '状態の更新に失敗 — 最後に確認した状態、実行中の件数は不明',
  dispatchRunning: '通常どおりディスパッチ中',
  pauseBusy: 'ディスパッチのティック実行中です。少し待ってからもう一度お試しください。',
  pauseAllBoards: 'すべてのボードを一時停止',
  resumeAllBoards: 'すべてのボードを再開',
  boardsPaused: (paused: number, total: number) => `${total} 件中 ${paused} 件のボードが一時停止中`,
  pauseHint:
    '新しいワーカーの取得と起動を停止します。実行中のワーカーが強制終了されることはありません。ゲートウェイを再起動する前に、件数が 0 になるまで待ってください。',
  estopEngaged: (reason: string) =>
    `Hermes は一時停止中です（hermes pause${reason ? `: ${reason}` : ''}）— \`hermes resume\` までどのボードもディスパッチしません。`
} satisfies PluginMessages

const zh = {
  depBlockedByHeading: (n: number) => `被阻塞 ${n}`,
  depBlocksHeading: (n: number) => `阻塞 ${n}`,
  depChevrons: '方向箭头',
  depClearFocus: '清除聚焦',
  depFlow: '流动圆点',
  depFocusChain: '完整链路',
  depFocusDirect: '直接链接',
  depFocused: '当前聚焦',
  depLegendHead: '箭头指向被卡住的卡片',
  depLegendLead: '连线颜色 = 阻塞项的状态：',
  depMissing: '不在此看板中',
  depMissingTip: '关联任务已被删除，或被当前租户／归档筛选隐藏。',
  depNothingBlocks: '无',
  depNothingWaits: '没有任务在等待它',
  depRowTip: '悬停以高亮其连线 · 点击以聚焦此卡片',
  depStatusCount: (n: number, status: string) => `${status} ${n}`,
  depVerdictClear: (total: number) => (total === 1 ? '其阻塞项已完成 — 可以移动' : `全部 ${total} 项阻塞已完成 — 可以移动`),
  depVerdictNone: '无阻塞 — 没有任务卡住此卡片',
  depVerdictStalled: (open: number) =>
    open === 1 ? '停滞 — 其未完成的阻塞项处于搁置' : `停滞 — 全部 ${open} 项未完成阻塞均处于搁置`,
  depVerdictWaiting: (open: number, parts: string, cleared: number) =>
    `正在等待 ${open} 项：${parts}${cleared ? ` · ${cleared} 项已完成` : ''}`,
  depGap: (n: number) => `+${n} 张卡片`,
  depGapShow: '与聚焦卡片无关 — 点击显示',
  depFocusHint: '点击卡片可追踪其依赖链 · 按 Esc 清除',
  allBoards: '所有面板',
  dispatchControl: '调度',
  pauseDispatch: '暂停调度',
  resumeDispatch: '恢复调度',
  dispatchPaused: '调度已暂停',
  draining: (running: number) => `${running} 个运行中 — 正在排空`,
  safeToRestart: '0 个运行中 — 可以安全重启',
  runningCount: (running: number) => `${running} 个运行中`,
  statusUnknown: (boards: string) => `运行数量未知 — 无法读取 ${boards}`,
  statusStale: '状态刷新失败 — 显示上次已知状态，运行数量未知',
  dispatchRunning: '调度正常运行中',
  pauseBusy: '正在执行一次调度周期，请稍后再试。',
  pauseAllBoards: '暂停所有面板',
  resumeAllBoards: '恢复所有面板',
  boardsPaused: (paused: number, total: number) => `${total} 个面板中有 ${paused} 个已暂停`,
  pauseHint: '停止领取和启动新的工作者。已在运行的工作者不会被终止 — 请等待计数归零后再重启网关。',
  estopEngaged: (reason: string) =>
    `Hermes 已暂停（hermes pause${reason ? `：${reason}` : ''}）— 在 \`hermes resume\` 之前所有面板都不会调度。`
} satisfies PluginMessages

const zhHant = {
  depBlockedByHeading: (n: number) => `被阻擋 ${n}`,
  depBlocksHeading: (n: number) => `阻擋 ${n}`,
  depChevrons: '方向箭頭',
  depClearFocus: '清除聚焦',
  depFlow: '流動圓點',
  depFocusChain: '完整鏈路',
  depFocusDirect: '直接連結',
  depFocused: '目前聚焦',
  depLegendHead: '箭頭指向被卡住的卡片',
  depLegendLead: '連線顏色 = 阻擋項的狀態：',
  depMissing: '不在此看板中',
  depMissingTip: '關聯任務已被刪除，或被目前租戶／封存篩選隱藏。',
  depNothingBlocks: '無',
  depNothingWaits: '沒有任務在等待它',
  depRowTip: '懸停以醒目提示其連線 · 點擊以聚焦此卡片',
  depStatusCount: (n: number, status: string) => `${status} ${n}`,
  depVerdictClear: (total: number) => (total === 1 ? '其阻擋項已完成 — 可以移動' : `全部 ${total} 項阻擋已完成 — 可以移動`),
  depVerdictNone: '無阻擋 — 沒有任務卡住此卡片',
  depVerdictStalled: (open: number) =>
    open === 1 ? '停滯 — 其未完成的阻擋項處於擱置' : `停滯 — 全部 ${open} 項未完成阻擋均處於擱置`,
  depVerdictWaiting: (open: number, parts: string, cleared: number) =>
    `正在等待 ${open} 項：${parts}${cleared ? ` · ${cleared} 項已完成` : ''}`,
  depGap: (n: number) => `+${n} 張卡片`,
  depGapShow: '與聚焦卡片無關 — 點擊顯示',
  depFocusHint: '點擊卡片可追蹤其相依鏈 · 按 Esc 清除',
  allBoards: '所有面板',
  dispatchControl: '調度',
  pauseDispatch: '暫停調度',
  resumeDispatch: '恢復調度',
  dispatchPaused: '調度已暫停',
  draining: (running: number) => `${running} 個執行中 — 正在排空`,
  safeToRestart: '0 個執行中 — 可以安全重啟',
  runningCount: (running: number) => `${running} 個執行中`,
  statusUnknown: (boards: string) => `執行數量未知 — 無法讀取 ${boards}`,
  statusStale: '狀態重新整理失敗 — 顯示上次已知狀態，執行數量未知',
  dispatchRunning: '調度正常執行中',
  pauseBusy: '正在執行一次調度週期，請稍後再試。',
  pauseAllBoards: '暫停所有面板',
  resumeAllBoards: '恢復所有面板',
  boardsPaused: (paused: number, total: number) => `${total} 個面板中有 ${paused} 個已暫停`,
  pauseHint: '停止領取與啟動新的工作者。已在執行的工作者不會被終止 — 請等待計數歸零後再重啟閘道。',
  estopEngaged: (reason: string) =>
    `Hermes 已暫停（hermes pause${reason ? `：${reason}` : ''}）— 在 \`hermes resume\` 之前所有面板都不會調度。`
} satisfies PluginMessages

export type KanbanForkMessages = typeof en

export const KANBAN_FORK_LOCALES: PluginLocaleBundles = { en, ja, zh, 'zh-hant': zhHant }
