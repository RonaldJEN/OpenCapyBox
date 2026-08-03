export const MAX_TEXT_BLOCK_CHARS = 10000;
export const UPLOAD_TARGET_UNCERTAIN_MESSAGE = '文件上传失败：无法确认目标文件是否已存在。为避免覆盖已有文件，本次上传已取消，请稍后重试。';

interface ApiErrorLike {
  response?: { data?: unknown; status?: unknown };
  message?: unknown;
  status?: unknown;
}

interface ValidationErrorLike {
  type?: unknown;
  loc?: unknown;
  msg?: unknown;
  ctx?: { max_length?: unknown };
}

function apiErrorLike(error: unknown): ApiErrorLike {
  return error && typeof error === 'object' ? error as ApiErrorLike : {};
}

function responseDetail(data: unknown): unknown {
  return data && typeof data === 'object'
    ? (data as { detail?: unknown }).detail
    : undefined;
}

async function blobText(blob: Blob): Promise<string> {
  if (typeof blob.text === 'function') return blob.text();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(typeof reader.result === 'string' ? reader.result : '');
    reader.onerror = () => reject(reader.error || new Error('读取错误响应失败'));
    reader.readAsText(blob);
  });
}

export function messageTooLongText(length: number): string {
  return `消息太长（${length} 字），当前最多支持 ${MAX_TEXT_BLOCK_CHARS} 字。请拆成多条发送，或保存为文件后上传。`;
}

export function messageTooLongLimitText(maxLength = MAX_TEXT_BLOCK_CHARS): string {
  return `消息太长，当前最多支持 ${maxLength} 字。请拆成多条发送，或保存为文件后上传。`;
}

function fieldLabelFromLoc(loc: unknown): string {
  if (!Array.isArray(loc)) return '';
  if (loc.includes('preferred_skill_keys')) return '优先 Skill';
  if (loc.includes('text')) return '消息内容';
  if (loc.includes('content')) return '消息内容';
  if (loc.includes('idempotency_key')) return '请求标识';
  if (loc.includes('file')) return '文件';
  return '';
}

function isMessageContentLoc(loc: unknown): boolean {
  return Array.isArray(loc)
    && !loc.includes('preferred_skill_keys')
    && (loc.includes('text') || loc.includes('content'));
}

function preferredSkillLimitMessage(record: ValidationErrorLike): string {
  if (!Array.isArray(record.loc) || !record.loc.includes('preferred_skill_keys')) return '';
  const maxLength = Number(record.ctx?.max_length);
  if (!Number.isFinite(maxLength) || maxLength <= 0) return '';
  if (record.type === 'string_too_long') {
    return `优先 Skill：每项最多 ${maxLength} 字符`;
  }
  if (record.type === 'too_long') {
    return `优先 Skill：最多 ${maxLength} 项`;
  }
  return '';
}

function isTooLongValidation(record: ValidationErrorLike): boolean {
  return record.type === 'string_too_long'
    || record.type === 'too_long'
    || String(record.msg || '').includes('at most');
}

function hasMessageTooLongValidation(detail: unknown): boolean {
  return Array.isArray(detail) && detail.some((item) => {
    if (!item || typeof item !== 'object') return false;
    const record = item as ValidationErrorLike;
    return isMessageContentLoc(record.loc) && isTooLongValidation(record);
  });
}

export function validationDetailToMessage(detail: unknown): string {
  if (!detail) return '';
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (!item || typeof item !== 'object') return String(item);
        const record = item as ValidationErrorLike;
        const preferredSkillLimit = preferredSkillLimitMessage(record);
        if (preferredSkillLimit) return preferredSkillLimit;
        const label = fieldLabelFromLoc(record.loc);
        const msg = String(record.msg || '').trim();
        return label && msg ? `${label}: ${msg}` : msg;
      })
      .filter(Boolean)
      .join('；');
  }
  if (typeof detail === 'object' && 'msg' in detail) {
    return String((detail as { msg?: unknown }).msg || '');
  }
  return '';
}

export function detailToMessage(detail: unknown): string {
  if (Array.isArray(detail)) {
    const tooLong = detail.find((item) => {
      if (!item || typeof item !== 'object') return false;
      const record = item as ValidationErrorLike;
      return isMessageContentLoc(record.loc) && isTooLongValidation(record);
    }) as ValidationErrorLike | undefined;
    if (tooLong) {
      return messageTooLongLimitText(Number(tooLong.ctx?.max_length || MAX_TEXT_BLOCK_CHARS));
    }
  }
  return validationDetailToMessage(detail);
}

export function extractErrorMessage(err: unknown): string {
  const error = apiErrorLike(err);
  const detailMessage = detailToMessage(responseDetail(error.response?.data));
  if (detailMessage) return detailMessage;
  if (typeof error.message === 'string') return error.message;
  return '';
}

export async function extractBlobAwareErrorMessage(err: unknown): Promise<string> {
  const error = apiErrorLike(err);
  const data = error.response?.data;
  if (typeof Blob !== 'undefined' && data instanceof Blob) {
    const rawText = await blobText(data);
    const status = Number(error.response?.status);
    return formatHttpErrorMessage(Number.isFinite(status) ? status : 0, rawText);
  }
  return extractErrorMessage(err);
}

export function extractValidationErrorMessage(err: unknown): string {
  const error = apiErrorLike(err);
  const detailMessage = validationDetailToMessage(responseDetail(error.response?.data));
  if (detailMessage) return detailMessage;
  if (typeof error.message === 'string') return error.message;
  return '';
}

export function formatUploadError(err: unknown): string {
  const message = extractErrorMessage(err);
  if (message.includes('无法确认上传目标是否存在')) {
    return UPLOAD_TARGET_UNCERTAIN_MESSAGE;
  }
  return message ? `文件上传失败：${message}` : '文件上传失败';
}

export function formatSendError(err: unknown): string {
  const error = apiErrorLike(err);
  const message = extractErrorMessage(err);
  const status = error.status ?? error.response?.status;
  if (
    status === 413
    || (status === 422 && hasMessageTooLongValidation(responseDetail(error.response?.data)))
  ) {
    return '消息太长，已超过当前输入限制。请拆成多条发送，或保存为文件后上传。';
  }
  return message || '发送消息失败';
}

export function formatDownloadError(err: unknown): string {
  const message = extractErrorMessage(err);
  if (!message) return '下载文件失败';
  if (message === 'HTTP 404') return '下载文件失败：文件不存在或尚未生成';
  return message.startsWith('下载文件失败') ? message : `下载文件失败：${message}`;
}

export function formatHttpErrorMessage(status: number, rawText: string): string {
  if (status === 413) {
    return '消息太长，已超过当前输入限制。请拆成多条发送，或保存为文件后上传。';
  }

  try {
    const parsed = JSON.parse(rawText);
    const detailMessage = detailToMessage(parsed?.detail);
    if (detailMessage) return detailMessage;
  } catch {
    // Non-JSON error bodies fall through to the raw text below.
  }

  return rawText || `HTTP ${status}`;
}
