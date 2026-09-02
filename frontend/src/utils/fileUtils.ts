/**
 * 文件類型判斷、圖標映射、文件名正規化等共用工具函數。
 *
 * 原先散落在 ChatInput / Round / ChatV2 三個組件中，現統一到此處。
 */

import {
  File,
  FileArchive,
  FileCode,
  Folder,
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
  const dotIndex = filename.lastIndexOf('.');
  const filenameExt = dotIndex >= 0 ? filename.slice(dotIndex + 1).toLowerCase() : '';
  if (raw.includes('/')) {
    // MIME 格式：取有效擴展名 > MIME 子類型
    return filenameExt || raw.split('/').pop()?.toLowerCase() || 'unknown';
  }
  // 非 MIME 格式：原樣或從文件名推斷
  return raw || filenameExt || 'unknown';
}

// ─── 文件圖標映射 ────────────────────────────────────────────────

const SPREADSHEET_EXTS = new Set(['xlsx', 'xls', 'csv', 'et']);
const DOCUMENT_EXTS = new Set(['doc', 'docx', 'txt', 'md', 'markdown', 'pdf']);
const CODE_EXTS = new Set([
  'js', 'ts', 'jsx', 'tsx', 'py', 'json', 'yaml', 'yml',
  'xml', 'html', 'css', 'sql',
]);
const PRESENTATION_EXTS = new Set(['ppt', 'pptx']);
const ARCHIVE_EXTS = new Set(['zip', 'rar', '7z', 'tar', 'gz', 'bz2', 'xz']);
const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'avif', 'heic', 'heif']);

export type FileCategory = 'image' | 'sheet' | 'pdf' | 'doc' | 'ppt' | 'code' | 'archive' | 'other';

function getExtFromName(filename: string): string {
  return filename.split('.').pop()?.toLowerCase() || '';
}

export function detectFileCategory(file: { name: string; type?: string }): FileCategory {
  const ext = getExtFromName(file.name);
  const rawType = (file.type || '').toLowerCase();

  if (isImageFile(file)) return 'image';
  if (['xlsx', 'xls', 'csv', 'et'].includes(ext) || rawType.includes('spreadsheet') || rawType.includes('excel')) return 'sheet';
  if (ext === 'pdf' || rawType.includes('pdf')) return 'pdf';
  if (['doc', 'docx', 'txt', 'md', 'markdown', 'rtf'].includes(ext) || rawType.includes('wordprocessingml') || rawType.includes('msword') || rawType.includes('document')) return 'doc';
  if (['ppt', 'pptx'].includes(ext) || rawType.includes('presentationml') || rawType.includes('powerpoint')) return 'ppt';
  if (ARCHIVE_EXTS.has(ext) || rawType.includes('zip') || rawType.includes('archive') || rawType.includes('compressed')) return 'archive';
  if (CODE_EXTS.has(ext) || rawType.includes('json') || rawType.includes('javascript') || rawType.includes('python') || rawType.includes('code')) return 'code';
  return 'other';
}

/**
 * 判斷文件是否為圖片：同時兼容 MIME（image/*）與副檔名。
 */
type FileLike = { name: string; type?: string; is_directory?: boolean };

export function isImageFile(file: FileLike): boolean {
  if (file.is_directory) return false;
  if (file.type?.startsWith('image/')) {
    return true;
  }
  const ext = inferFileType(file.name, file.type);
  return IMAGE_EXTS.has(ext);
}

/**
 * 根據文件信息返回對應的 Lucide 圖標組件。
 */
export function getFileIcon(file: FileLike): LucideIcon {
  if (file.is_directory) return Folder;
  const type = normalizeFileType(file.name, file.type);
  if (isImageFile(file)) return FileImage;
  if (SPREADSHEET_EXTS.has(type)) return FileSpreadsheet;
  if (DOCUMENT_EXTS.has(type)) return FileText;
  if (CODE_EXTS.has(type)) return FileCode;
  if (PRESENTATION_EXTS.has(type)) return Presentation;
  if (ARCHIVE_EXTS.has(type)) return FileArchive;
  return File;
}

/**
 * 返回右上角的擴展名標籤文字（大寫，最多 8 字符）。
 */
export function getFileExtLabel(file: FileLike): string {
  if (file.is_directory) return '文件夹';
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

/**
 * 返回文件的語義類型標籤（如 Document / Spreadsheet），用於卡片副標題。
 */
export function getFileCategoryLabel(file: { name: string; type?: string }): string {
  switch (detectFileCategory(file)) {
    case 'image':
      return 'Image';
    case 'sheet':
      return 'Spreadsheet';
    case 'pdf':
      return 'PDF';
    case 'doc':
      return 'Document';
    case 'ppt':
      return 'Presentation';
    case 'code':
      return 'Code';
    case 'archive':
      return 'Archive';
    default:
      return 'File';
  }
}

export function getFileBadgeClass(file: FileLike): string {
  if (file.is_directory) return 'bg-claude-accent/15 text-claude-accent';
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
    case 'archive':
      return 'bg-claude-file/12 text-claude-file-strong';
    default:
      return 'bg-black/65 text-white';
  }
}

export function getFileIconClass(file: FileLike): string {
  if (file.is_directory) return 'text-claude-accent';
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
    case 'archive':
      return 'text-claude-file';
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
  const fileInfo = file as FileInfo;
  const attachment = file as AttachmentInfo;
  const isFileInfo = typeof fileInfo.modified === 'string' || Boolean(fileInfo.content_mode);
  const snapshotPath = attachment.snapshot_path || fileInfo.snapshot_path;
  const resolvedPath = fileInfo.content_mode === 'captured'
    ? snapshotPath || file.path
    : isFileInfo
      ? file.path
      : snapshotPath || file.path;
  return {
    name: file.name,
    path: resolvedPath,
    size: file.size ?? 0,
    modified: (file as FileInfo).modified || '',
    type: normalizeFileType(file.name, file.type),
    data_url: file.data_url,
    session_id: file.session_id || fallbackSessionId,
    source: file.source,
    entry_id: file.entry_id,
    revision: file.revision,
    version_id: file.version_id,
    version_sequence: file.version_sequence,
    snapshot_path: snapshotPath,
    tree_revision: file.tree_revision,
    manifest_sha256: file.manifest_sha256,
    workspace_path: (file as FileInfo).workspace_path || (file as AttachmentInfo).origin_path,
    is_directory: file.is_directory || (file as AttachmentInfo).kind === 'directory',
    content_mode: (file as FileInfo).content_mode,
    assistant_ref_id: (file as FileInfo).assistant_ref_id,
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
  preview = true,
): string {
  const encodedPath = filePath
    .split('/')
    .map((segment) => encodeURIComponent(segment))
    .join('/');
  const base = `/api/sessions/${encodeURIComponent(sessionId)}/files/${encodedPath}`;
  const params = new URLSearchParams();
  if (preview) {
    params.set('preview', 'true');
  }
  const query = params.toString();
  return query ? `${base}?${query}` : base;
}
