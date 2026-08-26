import type { FileInfo } from '../types';
import { normalizeFileType } from './fileUtils';

const FILE_HINT_LINE_RE = /^\s*(?:[-*]\s*)?(?:\*\*)?\s*(?:文件位置|文件路径|保存位置|已保存到|输出文件|生成文件|文件)\s*(?:[:：]\s*)?(?:\*\*)?\s*(?:[:：]\s*)?(.+?)\s*$/;
const INLINE_CODE_RE = /`([^`]+)`/;
const INLINE_FILE_REF_RE = /`([^`\n]+)`/g;
const SESSION_PATH_PREFIX = '/home/user/sessions/';
const URL_RE = /^[a-z][a-z0-9+.-]*:\/\//i;
const FILE_PREVIEW_EXTS = new Set([
  'txt', 'log', 'ini', 'conf', 'cfg', 'toml',
  'md', 'markdown', 'html', 'htm',
  'js', 'ts', 'jsx', 'tsx', 'py', 'java', 'cpp', 'c', 'go', 'rs', 'sh', 'bash', 'sql', 'css', 'json', 'xml', 'yaml', 'yml', 'rb', 'php', 'swift', 'kt', 'scala', 'r', 'dart', 'lua',
  'docx', 'doc', 'csv', 'xlsx', 'xls', 'et', 'pptx', 'ppt',
  'jpg', 'jpeg', 'png', 'gif', 'svg', 'webp', 'bmp', 'pdf',
  'zip',
]);

/**
 * 从助手回复文本中解析出被提及的文件，按 path 去重后返回（保留首次出现顺序）。
 * 正文本身不再被拆分，文件卡片统一在消息底部展示。
 */
export function extractAssistantFiles(content: string, sessionId?: string): FileInfo[] {
  if (!sessionId) {
    return [];
  }

  const files: FileInfo[] = [];
  const seenPaths = new Set<string>();
  let inFence = false;

  const pushFile = (file: FileInfo) => {
    if (seenPaths.has(file.path)) {
      return;
    }
    seenPaths.add(file.path);
    files.push(file);
  };

  for (const line of content.split('\n')) {
    if (line.trimStart().startsWith('```')) {
      inFence = !inFence;
      continue;
    }

    if (inFence) {
      continue;
    }

    const hintFile = parseFileHintLine(line, sessionId);
    if (hintFile) {
      pushFile(hintFile);
      continue;
    }

    for (const inlineFile of parseInlineFileRefs(line, sessionId)) {
      pushFile(inlineFile.file);
    }
  }

  return files;
}

function parseFileHintLine(line: string, sessionId: string): FileInfo | null {
  const match = line.match(FILE_HINT_LINE_RE);
  if (!match) {
    return null;
  }

  return createFileInfoFromCandidate(match[1], sessionId);
}

function parseInlineFileRefs(line: string, sessionId: string) {
  const refs: Array<{ start: number; end: number; file: FileInfo }> = [];
  let match: RegExpExecArray | null;
  INLINE_FILE_REF_RE.lastIndex = 0;

  while ((match = INLINE_FILE_REF_RE.exec(line)) !== null) {
    if (!isInlinePathCandidate(match[1])) {
      continue;
    }

    const file = createFileInfoFromCandidate(match[1], sessionId);
    if (file) {
      refs.push({ start: match.index, end: match.index + match[0].length, file });
    }
  }

  return refs;
}

function createFileInfoFromCandidate(raw: string, sessionId: string): FileInfo | null {
  const candidate = extractPathCandidate(raw);
  const path = normalizeAssistantFilePath(candidate, sessionId);
  if (!path) {
    return null;
  }

  const name = path.split('/').pop() || '';
  const type = normalizeFileType(name);
  if (!FILE_PREVIEW_EXTS.has(type.toLowerCase())) {
    return null;
  }

  return {
    name,
    path,
    size: 0,
    modified: '',
    type,
    is_directory: false,
    session_id: sessionId,
  };
}

function extractPathCandidate(raw: string): string {
  const inlineCode = raw.match(INLINE_CODE_RE);
  const value = inlineCode ? inlineCode[1] : raw;
  return value
    .trim()
    .replace(/^[[({<\s]+/, '')
    .replace(/[\])}>\s。。，，；;：:]+$/, '')
    .replace(/^['"“”‘’]+/, '')
    .replace(/['"“”‘’]+$/, '');
}

function decodeAssistantPathCandidate(candidate: string): string | null {
  try {
    const decodedSegments: string[] = [];
    for (const encodedSegment of candidate.replace(/\\/g, '/').split('/')) {
      const decodedSegment = decodeURIComponent(encodedSegment);
      // 百分号编码不能偷偷引入新的路径层级、Windows 分隔符或 NUL。
      if (
        decodedSegment.includes('/')
        || decodedSegment.includes('\\')
        || decodedSegment.includes('\0')
      ) {
        return null;
      }
      decodedSegments.push(decodedSegment);
    }
    return decodedSegments.join('/');
  } catch {
    // 畸形百分号编码不是合法 Markdown 文件路径。
    return null;
  }
}

function normalizeAssistantFilePath(candidate: string, sessionId: string): string | null {
  const decoded = decodeAssistantPathCandidate(candidate);
  if (decoded === null) {
    return null;
  }
  const normalized = decoded.replace(/^\.\//, '');
  if (!normalized || URL_RE.test(normalized) || normalized.includes('\0')) {
    return null;
  }

  let relativePath = normalized;
  if (normalized.startsWith(SESSION_PATH_PREFIX)) {
    const remainder = normalized.slice(SESSION_PATH_PREFIX.length);
    const [candidateSessionId, ...pathParts] = remainder.split('/');
    if (candidateSessionId !== sessionId) {
      return null;
    }
    relativePath = pathParts.join('/');
  } else if (normalized.startsWith('/')) {
    return null;
  }

  const segments = relativePath.split('/').filter(Boolean);
  if (segments.length === 0 || segments.some((segment) => segment === '..' || segment === '.')) {
    return null;
  }

  const name = segments[segments.length - 1];
  if (!name.includes('.')) {
    return null;
  }

  return segments.join('/');
}

function isInlinePathCandidate(raw: string): boolean {
  return !/\s/.test(extractPathCandidate(raw));
}

/** 将 Markdown href 解析为当前会话内可预览文件；外链和越界路径返回 null。 */
export function createAssistantFileInfoFromHref(
  href: string,
  sessionId?: string,
): FileInfo | null {
  if (!sessionId) return null;
  return createFileInfoFromCandidate(href, sessionId);
}
