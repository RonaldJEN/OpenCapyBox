import { RoundData, FileInfo, AttachmentInfo } from '../types';
import { useState } from 'react';
import {
  AlignLeft,
  Archive,
  Check,
  Code2,
  Copy,
  File,
  Image,
  Presentation,
  Table2,
  User,
  type LucideIcon,
} from 'lucide-react';
import { ReasoningPanel } from './ReasoningPanel';
import { FileAttachment } from './FileAttachment';
import { CodeBlock } from './CodeBlock';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { parseMessageContent } from '../utils/messageParser';
import {
  createAssistantFileInfoFromHref,
  extractAssistantFiles,
} from '../utils/assistantFileRefs';
import { detectFileCategory, getFileIcon, getFileExtLabel, getFileBadgeClass, getFileIconClass, toFileInfo, buildSandboxFileUrl, isImageFile } from '../utils/fileUtils';
import { AuthenticatedImage } from './AuthenticatedImage';

interface RoundProps {
  round: RoundData;
  isStreaming?: boolean;
  disableMotion?: boolean;
  userAttachments?: AttachmentInfo[];
  sessionId?: string;
  assistantFileMatches?: Record<string, FileInfo | null | undefined>;
  onPreviewAttachment?: (file: FileInfo) => void;
  onOpenFileInPanel?: (file: FileInfo) => void;
}

const CANCELLED_RESPONSE_SENTINEL = 'Cancelled';

function isCancelledResponseSentinel(content: string | null | undefined, status: string): boolean {
  return status === 'cancelled'
    && typeof content === 'string'
    && content.normalize('NFKC').trim() === CANCELLED_RESPONSE_SENTINEL;
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

function AssistantMarkdown({
  content,
  sessionId,
  onOpenFile,
}: {
  content: string;
  sessionId?: string;
  onOpenFile?: (file: FileInfo) => void;
}) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        code: ({ className, children, ...props }: any) => {
          const match = /language-(\w+)/.exec(className || '');
          const language = match ? match[1] : '';
          const isInline = !match && !children?.toString().includes('\n');

          if (isInline) {
            return (
              <code
                className="px-1.5 py-0.5 bg-claude-surface text-orange-700 rounded-md text-[0.875em] font-mono border border-claude-border"
                {...props}
              >
                {children}
              </code>
            );
          }

          return (
            <CodeBlock
              language={language}
              value={String(children).replace(/\n$/, '')}
            />
          );
        },
        pre: ({ children, ...props }: any) => {
          if (children && typeof children === 'object' && 'props' in children) {
            return <>{children}</>;
          }
          return (
            <pre className="bg-[#1e1e1e] text-gray-300 rounded-2xl overflow-x-auto p-4 my-4 whitespace-pre-wrap break-words font-mono text-[13px]" {...props}>
              {children}
            </pre>
          );
        },
        img: ({ src: imgSrc, alt: imgAlt, ...imgRest }: any) => {
          if (typeof imgSrc !== 'string') {
            return null;
          }
          if (imgSrc.startsWith('/api/')) {
            return (
              <AuthenticatedImage
                src={imgSrc}
                alt={imgAlt || ''}
                className="max-w-full rounded-lg my-2"
                {...imgRest}
              />
            );
          }
          if (/^(https?:|data:image\/|blob:)/i.test(imgSrc)) {
            return <img src={imgSrc} alt={imgAlt || ''} className="max-w-full rounded-lg my-2" {...imgRest} />;
          }
          return null;
        },
        a: ({ children, href, ...props }: any) => {
          const localFile = typeof href === 'string'
            ? createAssistantFileInfoFromHref(href, sessionId)
            : null;
          if (localFile && onOpenFile) {
            return (
              <a
                href={href}
                className="text-blue-600 hover:underline underline-offset-2 cursor-pointer"
                onClick={(event) => {
                  event.preventDefault();
                  onOpenFile(localFile);
                }}
                {...props}
              >
                {children}
              </a>
            );
          }
          return (
            <a
              href={href}
              className="text-blue-600 hover:underline underline-offset-2"
              target="_blank"
              rel="noopener noreferrer"
              {...props}
            >
              {children}
            </a>
          );
        },
        blockquote: ({ children, ...props }: any) => (
          <blockquote
            className="border-l-2 border-claude-accent bg-claude-bg/60 pl-4 py-2 my-4 rounded-r-lg text-claude-secondary"
            {...props}
          >
            {children}
          </blockquote>
        ),
        ul: ({ children, ...props }: any) => (
          <ul className="space-y-1 my-2" {...props}>{children}</ul>
        ),
        ol: ({ children, ...props }: any) => (
          <ol className="space-y-1 my-2" {...props}>{children}</ol>
        ),
        li: ({ children, ...props }: any) => (
          <li className="text-claude-text" {...props}>{children}</li>
        ),
        h1: ({ children, ...props }: any) => (
          <h1 className="text-[1.5em] font-semibold text-claude-text tracking-tight mt-6 mb-3" {...props}>{children}</h1>
        ),
        h2: ({ children, ...props }: any) => (
          <h2 className="text-[1.25em] font-semibold text-claude-text tracking-tight mt-5 mb-2" {...props}>{children}</h2>
        ),
        h3: ({ children, ...props }: any) => (
          <h3 className="text-[1.1em] font-semibold text-claude-text tracking-tight mt-4 mb-2" {...props}>{children}</h3>
        ),
        table: ({ children, ...props }: any) => (
          <div className="overflow-x-auto my-4 rounded-xl border border-claude-border">
            <table className="min-w-full" {...props}>{children}</table>
          </div>
        ),
        thead: ({ children, ...props }: any) => (
          <thead className="bg-claude-surface" {...props}>{children}</thead>
        ),
        th: ({ children, ...props }: any) => (
          <th className="px-4 py-2 text-left text-[12px] font-semibold text-claude-secondary uppercase tracking-wider" {...props}>{children}</th>
        ),
        td: ({ children, ...props }: any) => (
          <td className="px-4 py-2 text-[14px] text-claude-text border-t border-claude-border/50" {...props}>{children}</td>
        ),
      }}
    >
      {content}
    </ReactMarkdown>
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
    <div className="not-prose -ml-1 mt-2 flex items-center gap-1">
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

function AssistantFileCard({ file, onOpen }: { file: FileInfo; onOpen?: (file: FileInfo) => void }) {
  const previewSessionId = file.session_id;
  const showImagePreview = isImageFile(file) && (file.data_url || (previewSessionId && file.path));

  return (
    <button
      type="button"
      onClick={() => onOpen?.(file)}
      className="not-prose group flex min-h-[52px] w-full max-w-[520px] items-center gap-2.5 rounded-[10px] border border-transparent bg-claude-surface px-2.5 py-2 text-left transition-[background-color,border-color,transform] hover:border-claude-border hover:bg-claude-hover active:scale-[0.995] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/35 sm:w-fit sm:min-w-[280px]"
      aria-label={`查看 ${file.name}`}
      title={`查看 ${file.name}`}
    >
      {showImagePreview ? (
        <span className="flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-[7px] bg-white/75">
          <AuthenticatedImage
            src={file.data_url || buildSandboxFileUrl(previewSessionId!, file.path, true)}
            alt={file.name}
            className="h-full w-full object-cover"
            fallback={<AssistantFileTypeIcon file={file} />}
          />
        </span>
      ) : (
        <AssistantFileTypeIcon file={file} />
      )}
      <span className="min-w-0 flex-1 truncate text-[15px] font-medium leading-6 text-claude-text sm:max-w-[430px] sm:flex-none">
        {file.name}
      </span>
    </button>
  );
}

export function Round({ round, isStreaming = false, disableMotion = false, userAttachments = [], sessionId, assistantFileMatches, onPreviewAttachment, onOpenFileInPanel }: RoundProps) {
  // 解析用户消息，提取附件信息
  const { attachments, cleanContent } = parseMessageContent(round.user_message);

  const TERMINAL_STATUSES = new Set(['completed', 'failed', 'max_steps_reached', 'cancelled']);
  const isCompleted = TERMINAL_STATUSES.has(round.status);
  const effectiveStreaming = isStreaming && !isCompleted;
  const latestStepContent = [...round.steps]
    .reverse()
    .find((step) => (
      step.assistant_content
      && !isCancelledResponseSentinel(step.assistant_content, round.status)
    ))
    ?.assistant_content;
  const visibleFinalResponse = isCancelledResponseSentinel(round.final_response, round.status)
    ? undefined
    : round.final_response;
  const visibleStepContent = latestStepContent || undefined;
  const assistantContent = visibleFinalResponse
    || ((effectiveStreaming || round.status === 'cancelled') ? visibleStepContent : undefined);
  const canCopyAssistantContent = round.status === 'completed' && !!round.final_response;
  const assistantFiles = assistantContent
    ? extractAssistantFiles(assistantContent, sessionId)
    : [];
  const visibleAssistantFiles = assistantFileMatches
    ? assistantFiles.flatMap((file) => {
        const matched = assistantFileMatches[file.path];
        return matched ? [{ ...file, ...matched, path: matched.path.replace(/^\/+/, '') }] : [];
      })
    : assistantFiles;

  return (
    <div className={`space-y-6 ${disableMotion ? '' : 'animate-fade-in'}`}>
      {/* ── 用户消息 ── */}
      <div className="flex items-start gap-3">
        <div className="w-7 h-7 rounded-full bg-claude-surface flex items-center justify-center flex-shrink-0 mt-0.5">
          <User size={14} className="text-claude-secondary" />
        </div>
        <div className="group/reply flex-1 min-w-0 pt-0.5">
          <p className="text-xs font-medium text-claude-secondary mb-1.5">你</p>
          <div className="text-[15px] text-claude-text leading-relaxed whitespace-pre-wrap break-words">
            {cleanContent}
          </div>
          {round.preferred_skills && round.preferred_skills.length > 0 && (
            <div className="mt-2.5 flex flex-wrap items-center gap-1.5" aria-label="本轮优先 Skill">
              <span className="mr-0.5 text-[11px] font-medium text-claude-muted">本轮优先 Skill</span>
              {round.preferred_skills.map((skill, index) => (
                <span
                  key={`${skill.key}-${index}`}
                  className="inline-flex max-w-full items-center rounded-full border border-claude-border bg-claude-surface px-2 py-0.5 text-[11px] font-medium text-claude-secondary"
                  title={skill.key}
                >
                  <span className="truncate">{skill.display_name?.trim() || skill.key}</span>
                </span>
              ))}
            </div>
          )}
          {/* 附件展示 */}
          {userAttachments.length > 0 ? (
            <div className="mt-3 flex flex-wrap gap-2">
              {userAttachments.map((file, idx) => (
                <button
                  key={`${file.path}-${idx}`}
                  type="button"
                  onClick={() => onPreviewAttachment?.(toFileInfo(file, sessionId))}
                  className="group relative w-24 h-20 rounded-xl overflow-hidden border border-claude-border bg-white hover:border-claude-border-strong transition-colors"
                  title={`预览 ${file.name}`}
                >
                  <div className={`absolute top-1.5 right-1.5 text-[9px] px-1.5 py-0.5 rounded-md uppercase tracking-wide z-10 ${getFileBadgeClass(file)}`}>
                    {getFileExtLabel(file)}
                  </div>
                  {isImageFile(file) && (file.data_url || sessionId) ? (
                    <AuthenticatedImage
                      src={file.data_url || buildSandboxFileUrl(sessionId!, file.path)}
                      alt={file.name}
                      className="w-full h-full object-cover transition-transform group-hover:scale-105"
                      fallback={
                        <div className="w-full h-full flex items-center justify-center bg-claude-surface">
                          {(() => {
                            const Icon = getFileIcon(file);
                            return <Icon className={`w-6 h-6 ${getFileIconClass(file)}`} />;
                          })()}
                        </div>
                      }
                    />
                  ) : (
                    <div className="w-full h-full flex items-center justify-center bg-claude-surface">
                      {(() => {
                        const Icon = getFileIcon(file);
                        return <Icon className={`w-6 h-6 ${getFileIconClass(file)}`} />;
                      })()}
                    </div>
                  )}
                  <div className="absolute inset-x-0 bottom-0 bg-black/55 text-white text-[10px] px-1.5 py-1 truncate">
                    {file.name}
                  </div>
                </button>
              ))}
            </div>
          ) : attachments.length > 0 && (
            <div className="mt-3 flex flex-wrap gap-2">
              {attachments.map((attachment, idx) => (
                <FileAttachment
                  key={idx}
                  filename={attachment.filename}
                  size={attachment.size}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      {/* ── 助手响应 ── */}
      <div className="flex items-start gap-3">
        <div className="w-7 h-7 rounded-full overflow-hidden flex-shrink-0 mt-0.5">
          <img src="/logo.jpg" alt="AI" className="w-full h-full object-cover" />
        </div>

        <div className="flex-1 min-w-0 pt-0.5">
          <p className="text-xs font-medium text-claude-secondary mb-1.5">助手</p>

          {/* 推理面板 */}
          {(round.steps.length > 0 || isStreaming) && (
            <ReasoningPanel
              steps={round.steps}
              isStreaming={effectiveStreaming}
              isCompleted={isCompleted && !!round.final_response}
              disableMotion={disableMotion}
            />
          )}

          {/* 最终答案 OR 流式传输中的答案 */}
          {assistantContent && (
            <div className="prose max-w-none mt-4">
              <AssistantMarkdown
                content={assistantContent}
                sessionId={sessionId}
                onOpenFile={onOpenFileInPanel}
              />
              {/* 流式传输光标 */}
              {!round.final_response && effectiveStreaming && (
                <span className="inline-block w-0.5 h-5 bg-claude-muted ml-0.5 animate-blink align-middle" />
              )}
              {/* 底部文件卡片（去重） */}
              {visibleAssistantFiles.length > 0 && (
                <div className="not-prose mt-3 flex max-w-[520px] flex-col items-stretch gap-1.5 sm:items-start">
                  {visibleAssistantFiles.map((file) => (
                    <AssistantFileCard
                      key={`file-${file.path}`}
                      file={file}
                      onOpen={onOpenFileInPanel}
                    />
                  ))}
                </div>
              )}
              {canCopyAssistantContent && (
                <AssistantActions content={round.final_response ?? ''} />
              )}
            </div>
          )}

          {/* 状态提示 */}
          {round.status === 'failed' && (
            <div className="text-xs text-claude-error font-medium mt-2">
              执行失败
            </div>
          )}
          {round.status === 'max_steps_reached' && (
            <div className="text-xs text-claude-warning font-medium mt-2">
              达到最大步数限制
            </div>
          )}
          {round.status === 'cancelled' && (
            <div className="text-xs text-claude-muted font-medium mt-2">
              已取消
            </div>
          )}
        </div>
      </div>

      {/* 分隔线 */}
      <div className="border-b border-claude-border/50" />
    </div>
  );
}
