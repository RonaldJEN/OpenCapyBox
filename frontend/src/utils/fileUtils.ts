/**
 * 文件類型判斷、圖標映射、文件名正規化等共用工具函數。
 *
 * 原先散落在 ChatInput / Round / ChatV2 三個組件中，現統一到此處。
 */

import {
  File,
  FileCode,
  FileImage,
  FileSpreadsheet,
  FileText,
  Presentation,
  type LucideIcon,
} from 'lucide-react';
import type { FileInfo, AttachmentInfo } from '../types';

// ─── 文件類型推斷 ────────────────────────────────────────────────

/**
 * 從文件名 / MIME 類型推斷出可用於分類的短類型字串。
 *
 * 優先取 MIME 子類型（`image/png` → `png`），否則取擴展名。
 */
export function inferFileType(filename: string, mime?: string): string {
  if (mime && mime.includes('/')) {
    return mime.split('/')[1]?.toLowerCase() || '';
  }
  return filename.split('.').pop()?.toLowerCase() || '';
}

/**
 * 將 MIME 或混合 type 字串正規化為純擴展名風格的短類型。
 *
 * - `image/png` → `png`
 * - `png`       → `png`（原樣）
 * - 空值        → 從文件名推斷，最終 fallback `unknown`
 */
export function normalizeFileType(filename: string, mimeOrType?: string): string {
  const raw = mimeOrType || '';
  if (raw.includes('/')) {
    // MIME 格式：取擴展名 > MIME 子類型
    return (
      filename.split('.').pop()?.toLowerCase() ||
      raw.split('/').pop()?.toLowerCase() ||
      'unknown'
    );
  }
  // 非 MIME 格式：原樣或從文件名推斷
  return raw || filename.split('.').pop()?.toLowerCase() || 'unknown';
}

// ─── 文件圖標映射 ────────────────────────────────────────────────

const SPREADSHEET_EXTS = new Set(['xlsx', 'xls', 'csv']);
const DOCUMENT_EXTS = new Set(['doc', 'docx', 'txt', 'md', 'markdown', 'pdf']);
const CODE_EXTS = new Set([
  'js', 'ts', 'jsx', 'tsx', 'py', 'json', 'yaml', 'yml',
  'xml', 'html', 'css', 'sql',
]);
const PRESENTATION_EXTS = new Set(['ppt', 'pptx']);
const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'avif', 'heic', 'heif']);

type FileCategory = 'image' | 'sheet' | 'pdf' | 'doc' | 'ppt' | 'code' | 'other';

function getExtFromName(filename: string): string {
  return filename.split('.').pop()?.toLowerCase() || '';
}

function detectFileCategory(file: { name: string; type?: string }): FileCategory {
  const ext = getExtFromName(file.name);
  const rawType = (file.type || '').toLowerCase();

  if (isImageFile(file)) return 'image';
  if (['xlsx', 'xls', 'csv'].includes(ext) || rawType.includes('spreadsheet') || rawType.includes('excel')) return 'sheet';
  if (ext === 'pdf' || rawType.includes('pdf')) return 'pdf';
  if (['doc', 'docx', 'txt', 'md', 'markdown', 'rtf'].includes(ext) || rawType.includes('wordprocessingml') || rawType.includes('msword') || rawType.includes('document')) return 'doc';
  if (['ppt', 'pptx'].includes(ext) || rawType.includes('presentationml') || rawType.includes('powerpoint')) return 'ppt';
  if (CODE_EXTS.has(ext) || rawType.includes('json') || rawType.includes('javascript') || rawType.includes('python') || rawType.includes('code')) return 'code';
  return 'other';
}

/**
 * 判斷文件是否為圖片：同時兼容 MIME（image/*）與副檔名。
 */
export function isImageFile(file: { name: string; type?: string }): boolean {
  if (file.type?.startsWith('image/')) {
    return true;
  }
  const ext = inferFileType(file.name, file.type);
  return IMAGE_EXTS.has(ext);
}

/**
 * 根據文件信息返回對應的 Lucide 圖標組件。
 */
export function getFileIcon(file: { name: string; type?: string }): LucideIcon {
  const type = inferFileType(file.name, file.type);
  if (isImageFile(file)) return FileImage;
  if (SPREADSHEET_EXTS.has(type)) return FileSpreadsheet;
  if (DOCUMENT_EXTS.has(type)) return FileText;
  if (CODE_EXTS.has(type)) return FileCode;
  if (PRESENTATION_EXTS.has(type)) return Presentation;
  return File;
}

/**
 * 返回右上角的擴展名標籤文字（大寫，最多 8 字符）。
 */
export function getFileExtLabel(file: { name: string; type?: string }): string {
  const ext = getExtFromName(file.name);
  if (ext) {
    if (ext === 'jpeg') return 'JPG';
    return ext.toUpperCase().slice(0, 8);
  }

  switch (detectFileCategory(file)) {
    case 'sheet':
      return 'XLSX';
    case 'pdf':
      return 'PDF';
    case 'doc':
      return 'DOCX';
    case 'ppt':
      return 'PPTX';
    case 'image':
      return 'IMG';
    case 'code':
      return 'CODE';
    default:
      return 'FILE';
  }
}

export function getFileBadgeClass(file: { name: string; type?: string }): string {
  switch (detectFileCategory(file)) {
    case 'sheet':
      return 'bg-claude-success/15 text-claude-success';
    case 'pdf':
      return 'bg-claude-error/15 text-claude-error';
    case 'doc':
      return 'bg-claude-accent/18 text-claude-text';
    case 'ppt':
      return 'bg-claude-warning/15 text-claude-warning';
    case 'code':
      return 'bg-claude-text/10 text-claude-secondary';
    case 'image':
      return 'bg-claude-accent/20 text-claude-accent';
    default:
      return 'bg-black/65 text-white';
  }
}

export function getFileIconClass(file: { name: string; type?: string }): string {
  switch (detectFileCategory(file)) {
    case 'sheet':
      return 'text-claude-success';
    case 'pdf':
      return 'text-claude-error';
    case 'doc':
      return 'text-claude-accent';
    case 'ppt':
      return 'text-claude-warning';
    case 'code':
      return 'text-claude-secondary';
    case 'image':
      return 'text-claude-accent';
    default:
      return 'text-claude-muted';
  }
}

// ─── AttachmentInfo → FileInfo 轉換 ─────────────────────────────

/**
 * 將 AttachmentInfo（持久化的附件元數據）轉成 FileInfo（前端預覽用）。
 */
export function toFileInfo(
  file: AttachmentInfo | FileInfo,
  fallbackSessionId?: string,
): FileInfo {
  return {
    name: file.name,
    path: file.path,
    size: file.size ?? 0,
    modified: (file as FileInfo).modified || '',
    type: normalizeFileType(file.name, file.type),
    data_url: file.data_url,
    session_id: file.session_id || fallbackSessionId,
  };
}

// ─── 沙箱文件 URL 構建 ──────────────────────────────────────────

/**
 * 構建沙箱文件的 API URL（用於圖片直接展示）。
 *
 * 將 URL 構建邏輯集中於此，避免組件與 apiService 直接耦合。
 */
export function buildSandboxFileUrl(
  sessionId: string,
  filePath: string,
  authSessionId: string,
  preview = true,
): string {
  const encodedPath = filePath
    .split('/')
    .map((segment) => encodeURIComponent(segment))
    .join('/');
  const base = `/api/sessions/${encodeURIComponent(sessionId)}/files/${encodedPath}`;
  const params = new URLSearchParams({
    user_id: authSessionId,
  });
  if (preview) {
    params.set('preview', 'true');
  }
  return `${base}?${params.toString()}`;
}
