import './chat-transcript.css';
import {
  type AssistantFileReference,
  type AttachmentInfo,
  type FileInfo,
  type RoundData,
  type SubagentTask,
} from '../types';
import { useState } from 'react';
import {
  AlignLeft,
  Archive,
  BookOpenCheck,
  Check,
  Code2,
  Copy,
  Database,
  File,
  Image,
  Loader2,
  Presentation,
  Table2,
  User,
  type LucideIcon,
} from 'lucide-react';
import { projectRoundTranscript } from '../transcript/projectRoundTranscript';
import { InlineRoundTranscript } from './InlineRoundTranscript';
import type { ChatRunRuntimeState } from '../runtime/chatRuntimeTypes';
import { FileAttachment } from './FileAttachment';
import { differenceInCalendarDays, format, isSameDay } from 'date-fns';
import { zhCN } from 'date-fns/locale/zh-CN';
import { parseMessageContent } from '../utils/messageParser';
import {
  assistantFileReferenceToFileInfo,
} from '../utils/assistantFileRefs';
import { detectFileCategory, getFileIcon, getFileExtLabel, getFileIconClass, toFileInfo, buildSandboxFileUrl, isImageFile } from '../utils/fileUtils';
import { AuthenticatedImage } from './AuthenticatedImage';

interface RoundProps {
  round: RoundData;
  run?: ChatRunRuntimeState;
  reveal?: { messageId?: string; nonce: number };
  onRetryStop?: () => void;
  onInspectProcess?: () => void;
  onOpenSubtask?: (task: SubagentTask) => void;
  showUserMessage?: boolean;
  isStreaming?: boolean;
  preparing?: boolean;
  disableMotion?: boolean;
  userAttachments?: AttachmentInfo[];
  sessionId?: string;
  onPreviewAttachment?: (file: FileInfo, index: number) => void;
  onOpenFileInPanel?: (file: FileInfo) => void;
}

async function copyTextToClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }

  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.left = '-9999px';
  document.body.appendChild(textarea);
  textarea.select();
  try {
    document.execCommand('copy');
  } finally {
    document.body.removeChild(textarea);
  }
}

function MessageTimestamp({ value, label }: { value: string; label: string }) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;

  const now = new Date();
  const time = format(date, 'HH:mm');
  const calendarDaysAgo = differenceInCalendarDays(now, date);
  const displayTime = isSameDay(date, now)
    ? `今天 ${time}`
    : calendarDaysAgo > 0 && calendarDaysAgo < 7
      ? `${format(date, 'EEEE', { locale: zhCN })} ${time}`
      : format(
        date,
        date.getFullYear() === now.getFullYear() ? 'M月d日 HH:mm' : 'yyyy年M月d日 HH:mm',
        { locale: zhCN },
      );

  return (
    <time
      dateTime={date.toISOString()}
      aria-label={`${label}：${displayTime}`}
      className="inline-flex text-xs leading-7 text-claude-muted"
    >
      {displayTime}
    </time>
  );
}

function AssistantActions({ content }: { content: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await copyTextToClipboard(content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch (error) {
      console.error('Failed to copy assistant reply:', error);
    }
  };

  return (
    <div className="chat-response-actions">
      <button
        type="button"
        onClick={handleCopy}
        className="inline-flex h-7 w-7 items-center justify-center rounded-md text-claude-muted transition-colors hover:bg-claude-surface hover:text-claude-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/35"
        title={copied ? '已复制' : '复制回复'}
        aria-label="复制回复"
      >
        {copied ? <Check size={16} className="text-claude-success" /> : <Copy size={16} />}
      </button>
    </div>
  );
}

function getAssistantFileGlyph(file: FileInfo): LucideIcon {
  switch (detectFileCategory(file)) {
    case 'doc':
    case 'pdf':
      return AlignLeft;
    case 'code':
      return Code2;
    case 'sheet':
      return Table2;
    case 'ppt':
      return Presentation;
    case 'archive':
      return Archive;
    case 'image':
      return Image;
    default:
      return File;
  }
}

function AssistantFileTypeIcon({ file }: { file: FileInfo }) {
  const Glyph = getAssistantFileGlyph(file);
  const category = detectFileCategory(file);

  return (
    <span
      className="relative flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-[7px] bg-claude-file text-white shadow-[0_1px_2px_rgba(39,67,170,0.22)] transition-colors group-hover:bg-claude-file-strong"
      data-file-category={category}
      aria-hidden="true"
    >
      <span className="absolute right-0 top-0 h-2.5 w-2.5 rounded-bl-[3px] bg-white/25" />
      <Glyph size={18} strokeWidth={2.4} aria-hidden="true" />
    </span>
  );
}

function AssistantFileCard({
  reference,
  onOpen,
}: {
  reference: AssistantFileReference;
  onOpen?: (file: FileInfo) => void;
}) {
  const file = assistantFileReferenceToFileInfo(reference);
  const previewSessionId = file.session_id;
  const imagePreviewPath = reference.source === 'session'
    ? reference.snapshot_path || reference.path
    : file.path;
  const showImagePreview = isImageFile(file) && (file.data_url || (previewSessionId && imagePreviewPath));

  return (
    <div className="not-prose w-full max-w-[520px] sm:w-fit sm:min-w-[280px]">
      <button
        data-reading-block={`assistant-file:${reference.ref_id}`}
        type="button"
        onClick={() => onOpen?.(file)}
        className="group flex min-h-[52px] w-full items-center gap-2.5 rounded-[10px] border border-transparent bg-claude-surface px-2.5 py-2 text-left transition-[background-color,border-color,transform] hover:border-claude-border hover:bg-claude-hover active:scale-[0.995] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/35"
        aria-label={`打开 ${file.name}`}
        title={`打开 ${file.name}`}
      >
        {showImagePreview ? (
          <span className="flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-[7px] bg-white/75">
            <AuthenticatedImage
              src={file.data_url || buildSandboxFileUrl(previewSessionId!, imagePreviewPath, true)}
              alt={file.name}
              className="h-full w-full object-cover"
              fallback={<AssistantFileTypeIcon file={file} />}
            />
          </span>
        ) : (
          <AssistantFileTypeIcon file={file} />
        )}
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[15px] font-medium leading-5 text-claude-text sm:max-w-[430px]">
            {file.name}
          </span>
          <span className="mt-0.5 block text-[11px] leading-4 text-claude-muted">
            {reference.source === 'workspace' ? '工作区文件' : '会话文件'}
          </span>
        </span>
      </button>
    </div>
  );
}

function attachmentSizeLabel(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Number((size / 1024).toFixed(1))} KB`;
  return `${Number((size / (1024 * 1024)).toFixed(1))} MB`;
}

export function Round({ round, run, reveal, onRetryStop, onInspectProcess, onOpenSubtask, showUserMessage = true, isStreaming = false, preparing = false, disableMotion = false, userAttachments = [], sessionId, onPreviewAttachment, onOpenFileInPanel }: RoundProps) {
  // 解析用户消息，提取附件信息
  const { attachments, cleanContent } = parseMessageContent(round.user_message);

  const TERMINAL_STATUSES = new Set(['completed', 'failed', 'max_steps_reached', 'cancelled']);
  const isCompleted = TERMINAL_STATUSES.has(round.status);
  const effectiveStreaming = isStreaming && !isCompleted;
  const transcript = projectRoundTranscript(round, effectiveStreaming);

  return (
    <div className={`chat-round ${disableMotion ? '' : 'animate-fade-in'}`}>
      {/* ── 用户消息 ── */}
      {showUserMessage && <div className="chat-user-row">
        <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-claude-surface">
          <User size={20} className="text-claude-secondary" aria-hidden="true" />
        </div>
        <div className="chat-user-content">
          <p className="mb-1.5 text-xs font-medium text-claude-secondary">你</p>
          {((round.preferred_skills?.length || 0) > 0
            || (round.preferred_mcp_connections?.length || 0) > 0) && (
            <div
              className="mb-2.5 flex flex-wrap items-center gap-2"
              aria-label="本轮已选资源"
            >
              {round.preferred_skills?.map((skill, index) => {
                const label = skill.display_name?.trim() || skill.key;
                return (
                  <span
                    key={`${skill.key}-${index}`}
                    className="chat-user-chip"
                    title={skill.key}
                    aria-label={`Skill ${label}`}
                  >
                    <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-[#fff1e6] text-[#d97706]">
                      <BookOpenCheck className="h-3.5 w-3.5" aria-hidden="true" />
                    </span>
                    <span className="truncate">{label}</span>
                  </span>
                );
              })}
              {round.preferred_mcp_connections?.map((connection, index) => {
                const label = connection.display_name?.trim() || connection.server_id;
                return (
                  <span
                    key={`${connection.server_id}-${index}`}
                    className="chat-user-chip"
                    title={connection.server_id}
                    aria-label={`数据连接 ${label}`}
                  >
                    <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-[#eef7f0] text-[#4d795d]">
                      <Database className="h-3.5 w-3.5" aria-hidden="true" />
                    </span>
                    <span className="truncate">{label}</span>
                  </span>
                );
              })}
            </div>
          )}
          {/* 附件展示 */}
          {userAttachments.length > 0 ? (
            <div className="chat-user-attachments">
              {userAttachments.map((file, idx) => {
                const Icon = getFileIcon(file);
                const image = isImageFile(file);
                return (
                <button
                  key={`${file.path}-${idx}`}
                  data-reading-block={`user-file:${file.path}`}
                  type="button"
                  onClick={() => onPreviewAttachment?.(toFileInfo(file, sessionId), idx)}
                  className="chat-user-chip chat-user-file-card group"
                  title={`预览 ${file.name}\n${getFileExtLabel(file)}${!file.is_directory && file.size !== undefined ? ` · ${attachmentSizeLabel(file.size)}` : ''}`}
                  aria-label={`预览 ${file.name}`}
                >
                  <span className="flex h-6 w-6 shrink-0 items-center justify-center overflow-hidden rounded-md bg-claude-surface">
                    {image && (file.data_url || sessionId) ? (
                      <AuthenticatedImage
                        src={file.data_url || buildSandboxFileUrl(sessionId!, file.path)}
                        alt={file.name}
                        className="h-full w-full object-cover"
                        fallback={<Icon size={14} className={getFileIconClass(file)} aria-hidden="true" />}
                      />
                    ) : (
                      <Icon size={14} className={getFileIconClass(file)} aria-hidden="true" />
                    )}
                  </span>
                  <span className="min-w-0 truncate text-left">{file.name}</span>
                </button>
              ); })}
            </div>
          ) : attachments.length > 0 && (
            <div className="chat-user-attachments">
              {attachments.map((attachment, idx) => (
                <FileAttachment
                  key={idx}
                  filename={attachment.filename}
                  size={attachment.size}
                />
              ))}
            </div>
          )}
          {cleanContent && <div data-reading-block="user" className="chat-user-bubble whitespace-pre-wrap break-words">
            {cleanContent}
          </div>}
          <div className="chat-user-time">
            <MessageTimestamp value={round.created_at} label="消息发送时间" />
          </div>
        </div>
      </div>}

      <section className="chat-message-row chat-assistant" aria-label="助手回复">
        <div className="chat-message-avatar">
          <img src="/logo.jpg" alt="AI" className="h-full w-full object-cover" />
        </div>
        <div className="chat-message-content">
          <p className="chat-message-label text-xs font-medium text-claude-secondary">
            助手{round.model_display_name && <span aria-label={`本轮模型：${round.model_display_name}`}>{` · ${round.model_display_name}`}</span>}
          </p>
          {/* 推理面板 */}
          {preparing && (
            <div role="status" className="flex items-center gap-2 py-2 text-sm text-claude-secondary">
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              <span>正在准备请求...</span>
            </div>
          )}
          {(!preparing || transcript.nodes.length > 0 || Boolean(round.assistant_file_references?.length)) && <InlineRoundTranscript round={round} run={run} streaming={effectiveStreaming} reveal={reveal} onRetryStop={onRetryStop} onInspectProcess={onInspectProcess} onOpenSubtask={onOpenSubtask}
            onOpenFile={onOpenFileInPanel} renderFile={(reference) => <AssistantFileCard reference={reference} onOpen={onOpenFileInPanel} />} />}
          {transcript.error && (
            <details className="mt-2 text-sm text-claude-error">
              <summary className="cursor-pointer">错误详情</summary>
              <div className="mt-1 whitespace-pre-wrap break-words">{transcript.error}</div>
            </details>
          )}
          {transcript.notice && (
            <details className="mt-2 text-sm text-claude-secondary">
              <summary className="cursor-pointer">{transcript.notice.label}</summary>
              <div className="mt-1 whitespace-pre-wrap break-words">{transcript.notice.text}</div>
            </details>
          )}

          <div className="chat-response-footer">
            {transcript.copyText && <AssistantActions content={transcript.copyText} />}
          </div>
        </div>
      </section>

      {/* 分隔线 */}
      <div className="chat-round-separator" />
    </div>
  );
}
