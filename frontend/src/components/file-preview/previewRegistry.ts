import type { FileInfo } from '../../types';
import { normalizeFileType } from '../../utils/fileUtils';

export type PreviewKind =
  | 'image'
  | 'pdf'
  | 'markdown'
  | 'html'
  | 'code'
  | 'text'
  | 'document'
  | 'spreadsheet'
  | 'archive'
  | 'presentation'
  | 'unsupported';

export interface PreviewDescriptor {
  kind: PreviewKind;
  type: string;
  language?: string;
  canRenderAsPdf?: boolean;
}

interface PreviewAdapterRegistration {
  kind: Exclude<PreviewKind, 'unsupported'>;
  types: ReadonlySet<string>;
  language?: (type: string) => string;
  canRenderAsPdf?: boolean;
}

const CODE_LANGUAGES: Record<string, string> = {
  bash: 'bash',
  c: 'c',
  cfg: 'ini',
  conf: 'ini',
  cpp: 'cpp',
  css: 'css',
  dart: 'dart',
  go: 'go',
  ini: 'ini',
  java: 'java',
  js: 'javascript',
  json: 'json',
  jsx: 'jsx',
  kt: 'kotlin',
  lua: 'lua',
  php: 'php',
  py: 'python',
  r: 'r',
  rb: 'ruby',
  rs: 'rust',
  scala: 'scala',
  sh: 'bash',
  sql: 'sql',
  swift: 'swift',
  toml: 'toml',
  ts: 'typescript',
  tsx: 'tsx',
  xml: 'xml',
  yaml: 'yaml',
  yml: 'yaml',
};

/**
 * FilePreview 的唯一格式分发表。新增格式时先在这里声明，再实现对应 loader/view。
 * 注册顺序有意义：同一扩展名只会命中第一项。
 */
export const previewAdapterRegistry: readonly PreviewAdapterRegistration[] = [
  {
    kind: 'image',
    types: new Set(['jpg', 'jpeg', 'png', 'gif', 'svg', 'webp', 'bmp', 'avif']),
  },
  { kind: 'pdf', types: new Set(['pdf']) },
  { kind: 'markdown', types: new Set(['md', 'markdown']) },
  { kind: 'html', types: new Set(['html', 'htm']) },
  {
    kind: 'code',
    types: new Set(Object.keys(CODE_LANGUAGES)),
    language: (type) => CODE_LANGUAGES[type] || type,
  },
  { kind: 'text', types: new Set(['txt', 'log']) },
  {
    kind: 'document',
    types: new Set(['doc', 'docx']),
    canRenderAsPdf: true,
  },
  { kind: 'spreadsheet', types: new Set(['xlsx', 'xls', 'csv', 'et']) },
  { kind: 'archive', types: new Set(['zip']) },
  {
    kind: 'presentation',
    types: new Set(['ppt', 'pptx', 'slides']),
    canRenderAsPdf: true,
  },
];

export function resolvePreviewDescriptor(file: Pick<FileInfo, 'name' | 'type'>): PreviewDescriptor {
  const type = normalizeFileType(file.name, file.type).toLowerCase();
  const registration = previewAdapterRegistry.find((candidate) => candidate.types.has(type));

  if (!registration) {
    return { kind: 'unsupported', type };
  }

  return {
    kind: registration.kind,
    type,
    language: registration.language?.(type),
    canRenderAsPdf: registration.canRenderAsPdf,
  };
}

/** 在已有 preview URL 上稳定追加/替换服务端渲染参数。 */
export function withRenderFormat(url: string, format: 'pdf'): string {
  const hashIndex = url.indexOf('#');
  const hash = hashIndex >= 0 ? url.slice(hashIndex) : '';
  const base = hashIndex >= 0 ? url.slice(0, hashIndex) : url;
  const renderPattern = /([?&])render=[^&#]*/;

  if (renderPattern.test(base)) {
    return `${base.replace(renderPattern, `$1render=${encodeURIComponent(format)}`)}${hash}`;
  }

  return `${base}${base.includes('?') ? '&' : '?'}render=${encodeURIComponent(format)}${hash}`;
}
