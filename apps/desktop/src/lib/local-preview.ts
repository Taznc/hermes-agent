import DOMPurify from 'dompurify'

import { isDesktopFsRemoteMode, readDesktopFileDataUrl, readDesktopFileText } from '@/lib/desktop-fs'
import type { PreviewTarget } from '@/store/preview'

const HTML_EXTENSIONS = new Set(['.htm', '.html'])
const IMAGE_EXTENSIONS = new Set(['.bmp', '.gif', '.jpeg', '.jpg', '.png', '.svg', '.webp'])
const PDF_EXTENSIONS = new Set(['.pdf'])
// Mirrors `_FS_DATA_URL_MAX_BYTES` in the backend filesystem endpoint.
const REMOTE_HTML_PREVIEW_MAX_BYTES = 16 * 1024 * 1024
const REMOTE_HTML_PREVIEW_MAX_BASE64_BYTES = Math.ceil(REMOTE_HTML_PREVIEW_MAX_BYTES / 3) * 4

const LANGUAGE_BY_EXT: Record<string, string> = {
  '.c': 'c',
  '.conf': 'ini',
  '.cpp': 'cpp',
  '.css': 'css',
  '.csv': 'csv',
  '.go': 'go',
  '.graphql': 'graphql',
  '.h': 'c',
  '.hpp': 'cpp',
  '.html': 'html',
  '.java': 'java',
  '.js': 'javascript',
  '.json': 'json',
  '.jsx': 'jsx',
  '.log': 'text',
  '.lua': 'lua',
  '.md': 'markdown',
  '.mjs': 'javascript',
  '.py': 'python',
  '.rb': 'ruby',
  '.rs': 'rust',
  '.sh': 'shell',
  '.sql': 'sql',
  '.svg': 'xml',
  '.toml': 'toml',
  '.ts': 'typescript',
  '.tsx': 'tsx',
  '.txt': 'text',
  '.xml': 'xml',
  '.yaml': 'yaml',
  '.yml': 'yaml',
  '.zsh': 'shell'
}

function basename(value: string) {
  return value.split(/[\\/]/).filter(Boolean).pop() || value
}

function extension(value: string) {
  const clean = value.split(/[?#]/, 1)[0] || value
  const idx = clean.lastIndexOf('.')

  return idx >= 0 ? clean.slice(idx).toLowerCase() : ''
}

function joinPath(base: string, rel: string) {
  if (!base) {
    return rel
  }

  return `${base.replace(/\/+$/, '')}/${rel.replace(/^\.?\//, '')}`
}

export function pathToFileUrl(path: string) {
  const isWindowsUnc = path.startsWith('\\\\')
  const normalized = isWindowsUnc || /^[a-z]:[\\/]/i.test(path) ? path.replace(/\\/g, '/') : path

  const encoded = normalized
    .split('/')
    .map(part => encodeURIComponent(part))
    .join('/')

  if (isWindowsUnc) {
    return `file://${encoded.slice(2)}`
  }

  return `file://${encoded.startsWith('/') ? encoded : `/${encoded}`}`
}

/**
 * The single resolver for a chat-link href naming a file: plain `/abs`,
 * `~/…`, or `file://…` (percent-encoded). Every file verb — Open with
 * default app, Reveal, Copy path — must derive its path through this, not
 * a per-call-site strip/encode, or the shapes drift out of sync (#103951
 * follow-up: `~` reaching the URL host, `file://` paths staying encoded).
 *
 * `~` is expanded HERE, renderer-side, before any `file:` URL is built —
 * it must never reach the host position of a `file:` URL (`new
 * URL('file://~/x').host === '~'`, which `fileURLToPath` rejects on every
 * OS). Expansion needs a home directory; the renderer has no `os.homedir()`
 * so the caller supplies one (`window.hermesDesktop`'s reported home, when
 * available) — with no home known, `~/…` is left as a literal leading
 * segment rather than silently mis-resolving into `process.cwd()`-relative.
 *
 * `~` expansion is further gated on the path actually naming THIS machine's
 * filesystem (`isLocalHost`, defaulted from `!isDesktopFsRemoteMode()`). On
 * a remote gateway the reported home dir is always the LOCAL Electron
 * host's (`app.getPath('home')`), never the remote backend's — expanding
 * there would put a real-looking but wrong-machine path on the clipboard.
 * Remote mode already hides every other file verb (`canUseNativeFileActions`),
 * so Copy path is the only one reachable here, and it must stay portable:
 * copy the literal `~/…` back.
 *
 * A `file://` href whose host is non-empty and not `localhost` is likewise
 * untrustworthy: `new URL(raw).pathname` silently drops the host, so
 * `file://~/todo.md` would resolve to `/todo.md` — a real-looking but
 * entirely different file. A host of exactly `~` is the one shape chat
 * links can produce (from a `~/…` href re-wrapped as `file://`) and is
 * routed back through the tilde branch; any other host returns the raw
 * href untouched rather than fabricate a truncated path.
 */
export interface ChatLinkPath {
  /** The raw on-disk path — what Reveal and Copy path act on. */
  path: string
  /** The `file:` URL built from `path` — what Open-with-default-app hands
   *  the OS (via `openExternal`). */
  url: string
}

export function resolveChatLinkPath(
  href: string,
  homeDir?: null | string,
  isLocalHost: boolean = !isDesktopFsRemoteMode()
): ChatLinkPath {
  const raw = href.trim()

  if (/^file:\/\//i.test(raw)) {
    let parsed: URL | null

    try {
      parsed = new URL(raw)
    } catch {
      parsed = null
    }

    const host = parsed?.host ?? ''

    if (host && host !== 'localhost') {
      if (host === '~') {
        let tildePath = '/'

        try {
          tildePath = decodeURIComponent(parsed!.pathname)
        } catch {
          tildePath = parsed!.pathname
        }

        return resolveChatLinkPath(`~${tildePath}`, homeDir, isLocalHost)
      }

      // Any other non-empty host cannot be trusted: `.pathname` would
      // silently drop it and hand every verb a truncated, WRONG path.
      // Fail closed — return the href untouched rather than fabricate one.
      return { path: raw, url: raw }
    }

    let decoded: string

    try {
      decoded = decodeURIComponent((parsed ?? new URL(raw)).pathname)
    } catch {
      decoded = raw.replace(/^file:\/\//i, '')
    }

    return { path: decoded, url: pathToFileUrl(decoded) }
  }

  if (raw === '~' || raw.startsWith('~/')) {
    const expanded = isLocalHost && homeDir ? `${homeDir.replace(/\/+$/, '')}${raw.slice(1)}` : raw

    return { path: expanded, url: pathToFileUrl(expanded) }
  }

  return { path: raw, url: pathToFileUrl(raw) }
}

export function validatedRemoteHtmlDataUrl(value: string): string | null {
  const prefix = 'data:text/html;base64,'

  if (!value.startsWith(prefix)) {
    return null
  }

  const payload = value.slice(prefix.length)

  if (payload.length > REMOTE_HTML_PREVIEW_MAX_BASE64_BYTES || payload.length % 4 !== 0) {
    return null
  }

  try {
    const decoded = atob(payload)

    return decoded.length <= REMOTE_HTML_PREVIEW_MAX_BYTES && btoa(decoded) === payload ? value : null
  } catch {
    return null
  }
}

export function remoteHtmlPreviewDocument(dataUrl: string): string | null {
  const validated = validatedRemoteHtmlDataUrl(dataUrl)

  if (!validated) {
    return null
  }

  const csp = `default-src 'none'; base-uri 'none'; form-action 'none'; img-src data:; media-src data:; font-src data:; style-src 'unsafe-inline'`

  const html = new TextDecoder().decode(
    Uint8Array.from(atob(validated.slice(validated.indexOf(',') + 1)), char => char.charCodeAt(0))
  )

  const document = new DOMParser().parseFromString(
    DOMPurify.sanitize(html, {
      WHOLE_DOCUMENT: true,
      FORBID_TAGS: ['script', 'template', 'iframe', 'frame', 'object', 'embed'],
      FORBID_ATTR: ['href', 'xlink:href', 'action', 'formaction', 'target']
    }),
    'text/html'
  )

  document.querySelectorAll('meta[http-equiv]').forEach(element => {
    if (element.getAttribute('http-equiv')?.toLowerCase() === 'refresh') {
      element.remove()
    }
  })
  document.querySelectorAll('*').forEach(element => {
    for (const attribute of Array.from(element.attributes)) {
      if (attribute.localName === 'href' || attribute.localName === 'ping') {
        element.removeAttributeNode(attribute)
      }
    }
  })
  const policy = document.createElement('meta')
  policy.httpEquiv = 'Content-Security-Policy'
  policy.content = csp
  document.head.prepend(policy)

  return `<!doctype html>${document.documentElement.outerHTML}`
}

export async function openPreviewTargetInBrowser(target: PreviewTarget): Promise<void> {
  const bridge = window.hermesDesktop

  if (!bridge?.openPreviewInBrowser) {
    throw new Error('Desktop preview browser bridge is unavailable')
  }

  const dataUrl = target.dataUrl && validatedRemoteHtmlDataUrl(target.dataUrl)

  if (!dataUrl) {
    if (target.transient) {
      throw new Error('Remote HTML preview could not be loaded')
    }

    await bridge.openPreviewInBrowser(target.url)

    return
  }

  if (!bridge.saveImageBuffer) {
    throw new Error('Desktop preview buffer bridge is unavailable')
  }

  const decoded = atob(dataUrl.slice(dataUrl.indexOf(',') + 1))
  const bytes = Uint8Array.from(decoded, char => char.charCodeAt(0))
  const filePath = await bridge.saveImageBuffer(bytes, '.html')

  if (!filePath) {
    throw new Error('Could not stage remote HTML preview')
  }

  await bridge.openPreviewInBrowser(pathToFileUrl(filePath))
}

export function localPreviewTarget(rawTarget: string, cwd?: string | null): PreviewTarget | null {
  const raw = rawTarget.trim().replace(/^`|`$/g, '')

  if (!raw) {
    return null
  }

  if (/^https?:\/\//i.test(raw)) {
    return { kind: 'url', label: basename(raw), source: raw, url: raw }
  }

  let path = raw

  if (/^file:\/\//i.test(raw)) {
    try {
      path = decodeURIComponent(new URL(raw).pathname)
    } catch {
      path = raw.replace(/^file:\/\//i, '')
    }
  } else if (!raw.startsWith('/') && cwd) {
    path = joinPath(cwd, raw)
  }

  const ext = extension(path)
  const isHtml = HTML_EXTENSIONS.has(ext)
  const isImage = IMAGE_EXTENSIONS.has(ext)
  const isPdf = PDF_EXTENSIONS.has(ext)

  return {
    kind: 'file',
    label: basename(path),
    language: LANGUAGE_BY_EXT[ext] || 'text',
    path,
    // Renderer fallback can't stat/sniff without reading; assume text unless
    // image/html/pdf extension says otherwise. LocalFilePreview still guards
    // binary/large files when readFileText/readFileDataUrl returns metadata.
    previewKind: isHtml ? 'html' : isImage ? 'image' : isPdf ? 'pdf' : 'text',
    source: raw,
    url: pathToFileUrl(path)
  }
}

async function enrichPreviewTarget(target: PreviewTarget | null): Promise<PreviewTarget | null> {
  if (
    !isDesktopFsRemoteMode() ||
    !target ||
    target.kind !== 'file' ||
    target.previewKind === 'image' ||
    target.previewKind === 'pdf'
  ) {
    return target
  }

  if (target.previewKind === 'html') {
    try {
      const dataUrl = validatedRemoteHtmlDataUrl(await readDesktopFileDataUrl(target.path || target.source))

      return dataUrl ? { ...target, dataUrl } : { ...target, renderMode: 'source', transient: true }
    } catch {
      return { ...target, renderMode: 'source', transient: true }
    }
  }

  try {
    const result = await readDesktopFileText(target.path || target.source)

    return {
      ...target,
      binary: result.binary,
      byteSize: result.byteSize,
      language: result.language || target.language,
      large: (result.byteSize ?? 0) > 512 * 1024,
      mimeType: result.mimeType
    }
  } catch {
    return target
  }
}

export async function normalizeOrLocalPreviewTarget(
  rawTarget: string,
  cwd?: string | null
): Promise<PreviewTarget | null> {
  try {
    const normalized = await window.hermesDesktop?.normalizePreviewTarget?.(rawTarget, cwd || undefined)

    if (normalized) {
      return enrichPreviewTarget(normalized)
    }
  } catch {
    // Running Electron may still have the old HTML-only preview IPC. Fall
    // through to renderer-side local classification so text/images still open.
  }

  return enrichPreviewTarget(localPreviewTarget(rawTarget, cwd))
}
