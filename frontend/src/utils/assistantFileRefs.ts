import type { FileInfo } from '../types';
import { normalizeFileType } from './fileUtils';

export type AssistantContentBlock =
  | { type: 'markdown'; content: string }
  | { type: 'file'; file: FileInfo; originalLine: string };

const FILE_HINT_LINE_RE = /^\s*(?:[-*]\s*)?(?:\*\*)?\s*(?:文件位置|文件路径|保存位置|已保存到|输出文件|生成文件|文件)\s*(?:[:：]\s*)?(?:\*\*)?\s*(?:[:：]\s*)?(.+?)\s*$/;
const INLINE_CODE_RE = /`([^`]+)`/;
const INLINE_FILE_REF_RE = /`([^`\n]+)`/g;
const SESSION_PATH_PREFIX = '/home/user/sessions/';
const URL_RE = /^[a-z][a-z0-9+.-]*:\/\//i;
const FILE_PREVIEW_EXTS = new Set([
  'txt', 'log', 'ini', 'conf', 'cfg', 'toml',
  'md', 'markdown', 'html', 'htm',
  'js', 'ts', 'jsx', 'tsx', 'py', 'java', 'cpp', 'c', 'go', 'rs', 'sh', 'bash', 'sql', 'css', 'json', 'xml', 'yaml', 'yml', 'rb', 'php', 'swift', 'kt', 'scala', 'r', 'dart', 'lua',
  'docx', 'doc', 'csv', 'xlsx', 'xls', 'pptx', 'ppt',
  'jpg', 'jpeg', 'png', 'gif', 'svg', 'webp', 'bmp', 'pdf',
]);

export function extractAssistantContentBlocks(content: string, sessionId?: string): AssistantContentBlock[] {
  if (!sessionId) {
    return [{ type: 'markdown', content }];
  }

  const blocks: AssistantContentBlock[] = [];
  const markdownLines: string[] = [];
  const seenPaths = new Set<string>();
  let inFence = false;

  const flushMarkdown = () => {
    const markdown = markdownLines.join('\n').trimEnd();
    markdownLines.length = 0;
    if (markdown) {
      blocks.push({ type: 'markdown', content: markdown });
    }
  };

  for (const line of content.split('\n')) {
    if (line.trimStart().startsWith('```')) {
      inFence = !inFence;
      markdownLines.push(line);
      continue;
    }

    if (inFence) {
      markdownLines.push(line);
      continue;
    }

    const file = parseFileHintLine(line, sessionId);
    if (file) {
      if (seenPaths.has(file.path)) {
        continue;
      }

      flushMarkdown();
      seenPaths.add(file.path);
      blocks.push({ type: 'file', file, originalLine: line });
      continue;
    }

    const inlineFiles = parseInlineFileRefs(line, sessionId);
    if (inlineFiles.length === 0) {
      markdownLines.push(line);
      continue;
    }

    let cursor = 0;
    for (const inlineFile of inlineFiles) {
      markdownLines.push(line.slice(cursor, inlineFile.start));
      flushMarkdown();
      blocks.push({ type: 'file', file: inlineFile.file, originalLine: line });
      cursor = inlineFile.end;
    }
    const tail = line.slice(cursor);
    if (!isOnlyPunctuation(tail)) {
      markdownLines.push(tail);
    }
  }

  flushMarkdown();
  return blocks.length > 0 ? blocks : [{ type: 'markdown', content }];
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

function normalizeAssistantFilePath(candidate: string, sessionId: string): string | null {
  const normalized = candidate.replace(/\\/g, '/').replace(/^\.\//, '');
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

function isOnlyPunctuation(value: string): boolean {
  return /^[\s。。，，；;、,.!?！？:：]*$/.test(value);
}