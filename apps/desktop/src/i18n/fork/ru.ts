import type { TranslationOverrides } from '../define-locale'

// Russian values for fork-added translation keys.
export const forkRu: TranslationOverrides = {
  rightSidebar: {
    terminalUnavailableTitle: 'Встроенный терминал недоступен',
    terminalUnavailableBody: 'Для интерактивного доступа к оболочке требуется приложение Hermes для компьютера.'
  },
  sidebar: {
    row: {
      unarchive: 'Разархивировать',
      unarchiveSession: 'Разархивировать сеанс'
    }
  },
  desktop: {
    unarchived: 'Восстановлено',
    unarchiveFailed: 'Не удалось разархивировать'
  }
}
