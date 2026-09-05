import type { ForkTranslations } from './types'

// Simplified Chinese values for the fork-added keys. Merged into the authored
// zh catalog at the single anchor in ../zh.ts, so locale-parity's AUTHORED
// check still sees a complete key set.

export const forkZh: ForkTranslations = {
  boot: {
    errors: {
      gatewaySessionsStale: '已重新连接，但会话/设置未能刷新。部分列表可能已过期。'
    },
    failure: {
      wsAuthTitle: '需要登录',
      wsAuthDescription:
        'Hermes 正在正常运行并响应请求——只是这次连接的访问凭据被拒绝了。这些操作不会删除你的对话或设置。',
      wsAuthHint: '请重新打开你获得的访问链接（其中包含新的凭据），或向为你设置此实例的人索取新的链接。',
      openLogsFailed: '无法打开日志文件夹'
    }
  },
  settings: {
    gateway: {
      secretStorageHintTitle: '未使用系统钥匙串加密存储',
      secretStorageHintDesc:
        '网关 token 和登录凭据以仅当前用户可读的普通文件形式存储。可在下方启用系统钥匙串加密以获得更强保护。',
      secretStorageHintEnable: '启用加密',
      secretStorageHintDismiss: '关闭',
      openLogsFailed: '无法打开日志文件夹'
    },
    sessions: {
      rateLimitRecoveryTitle: 'When a turn hits a rate limit',
      rateLimitRecoveryDesc:
        'Ask each time (default) always shows the failure card and lets you choose. Resume automatically starts a brief, cancelable countdown before scheduling a resume.',
      rateLimitRecoveryFailed: 'Could not update the rate-limit recovery preference'
    }
  },
  commandCenter: {
    maintenance: {
      curatorLoadFailed: '无法加载维护器状态',
      memoryLoadFailed: '无法加载记忆数据',
      retry: '重试',
      actionTailLost: '已失去该任务的跟踪 — 请在活动栏中查看'
    }
  },
  profiles: {
    switchToAgent: (profile, device) => `切换到 ${device} 上的 ${profile}`,
    connectToAgent: device => `连接到 ${device}`,
    notConnected: '尚未连接',
    agentsHeading: '智能体与连接',
    thisDevice: '本设备',
    sourceUnreachable: '无法连接'
  },
  sidebar: {
    row: {
      rateLimited: {
        withTime: time => `Rate limited — retry at ${time}`,
        unknown: 'Rate limited — reset time unknown'
      },
      providerConfigured: family => `配置的模型：${family}`,
      providerVia: family => `经由 ${family}`,
      providerConfiguredVia: (configuredFamily, servedFamily) =>
        `配置的模型：${configuredFamily}，当前经由 ${servedFamily} 提供服务`
    }
  },
  composer: {
    reconnectingBanner: '正在重新连接 Hermes — 你仍可以阅读和输入。',
    catchingUpNotice: '已重新连接 — 正在追上进度…',
    turnLostNotice: '此次对话在断线期间可能未完成。',
    turnLostRegenerate: '重新生成'
  },
  assistant: {
    thread: {
      showEarlierFailed: '无法加载更早的消息',
      rateLimit: {
        message: provider => `${provider || 'The provider'} is rate limiting this account.`,
        resetsAt: time => `Retry at ${time}`,
        resetUnknown: 'Reset time unknown',
        resumeAtReset: 'Resume at reset',
        makeDefault: 'Make this the default',
        switchModelAndRetry: 'Switch model & retry',
        configureFallback: 'Configure automatic fallback…',
        switchedNotice: (from, to) => `Switched from ${from} to ${to} and continued`,
        countdownLabel: seconds => `Resuming in ${seconds}s… Cancel`,
        cancelCountdown: 'Cancel',
        jobScheduled: time => `Resume scheduled for ${time}`,
        jobCancel: 'Cancel resume',
        jobCancelFailed: 'Could not cancel the scheduled resume',
        jobScheduleFailed: 'Could not schedule the resume',
        jobDuplicate: 'A resume is already scheduled for this turn',
        switchModelFailed: 'Could not switch models'
      },
      review: {
        showDetails: '显示详情',
        showDetailsWithFailures: count => `显示详情（${count} 项失败）`,
        hideDetails: '隐藏详情',
        failedReason: message => `失败：${message}`
      }
    },
    tool: {
      memoryActions: {
        add: { done: '已保存笔记', pending: '正在保存笔记' },
        search: { done: '已搜索记忆', pending: '正在搜索记忆' },
        probe: { done: '已回忆记忆', pending: '正在回忆记忆' },
        related: { done: '已找到相关记忆', pending: '正在查找相关记忆' },
        reason: { done: '已跨记忆推理', pending: '正在跨记忆推理' },
        contradict: { done: '已检查记忆冲突', pending: '正在检查记忆冲突' },
        update: { done: '已更新记忆', pending: '正在更新记忆' },
        remove: { done: '已从记忆中移除', pending: '正在从记忆中移除' },
        list: { done: '已列出记忆', pending: '正在列出记忆' }
      }
    }
  },
  errors: {
    openLogsFailed: '无法打开日志文件夹'
  }
}
