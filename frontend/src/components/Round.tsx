import { RoundData, FileInfo, AttachmentInfo } from '../types';
import { ExternalLink, User } from 'lucide-react';
import { ReasoningPanel } from './ReasoningPanel';
import { FileAttachment } from './FileAttachment';
import { CodeBlock } from './CodeBlock';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { parseMessageContent } from '../utils/messageParser';
import { extractAssistantFiles } from '../utils/assistantFileRefs';
import { getFileIcon, getFileExtLabel, getFileCategoryLabel, getFileBadgeClass, getFileIconClass, toFileInfo, buildSandboxFileUrl, isImageFile } from '../utils/fileUtils';
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

function AssistantMarkdown({ content }: { content: string }) {
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
          if (imgSrc && imgSrc.startsWith('/api/')) {
            return (
              <AuthenticatedImage
                src={imgSrc}
                alt={imgAlt || ''}
                className="max-w-full rounded-lg my-2"
                {...imgRest}
              />
            );
          }
          return <img src={imgSrc} alt={imgAlt || ''} className="max-w-full rounded-lg my-2" {...imgRest} />;
        },
        a: ({ children, ...props }: any) => (
          <a
            className="text-blue-600 hover:underline underline-offset-2"
            target="_blank"
            rel="noopener noreferrer"
            {...props}
          >
            {children}
          </a>
        ),
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

function AssistantFileCard({ file, onOpen }: { file: FileInfo; onOpen?: (file: FileInfo) => void }) {
  const Icon = getFileIcon(file);
  const meta = file.size > 0
    ? `${getFileCategoryLabel(file)} · ${getFileExtLabel(file)} · ${formatAssistantFileSize(file.size)}`
    : `${getFileCategoryLabel(file)} · ${getFileExtLabel(file)}`;

  return (
    <button
      type="button"
      onClick={() => onOpen?.(file)}
      className="not-prose group flex w-full items-center gap-3 rounded-xl border border-claude-border bg-white px-4 py-3 text-left transition-colors hover:border-claude-border-strong hover:bg-claude-hover active:scale-[0.99]"
      aria-label={`查看 ${file.name}`}
      title={`查看 ${file.name}`}
    >
      <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-claude-surface">
        <Icon className={`h-5 w-5 ${getFileIconClass(file)}`} />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[15px] font-semibold leading-tight text-claude-text">
          {file.name}
        </span>
        <span className="mt-1 block text-[12px] text-claude-muted">
          {meta}
        </span>
      </span>
      <span className="inline-flex shrink-0 items-center gap-1 text-[12px] font-medium text-claude-accent transition-colors group-hover:text-claude-text">
        查看
        <ExternalLink size={13} />
      </span>
    </button>
  );
}

function formatAssistantFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function Round({ round, isStreaming = false, disableMotion = false, userAttachments = [], sessionId, assistantFileMatches, onPreviewAttachment, onOpenFileInPanel }: RoundProps) {
  // 解析用户消息，提取附件信息
  const { attachments, cleanContent } = parseMessageContent(round.user_message);

  const TERMINAL_STATUSES = new Set(['completed', 'failed', 'max_steps_reached', 'interrupted', 'resumed', 'cancelled']);
  const isCompleted = TERMINAL_STATUSES.has(round.status);
  const effectiveStreaming = isStreaming && !isCompleted;
  const latestStreamingContent = effectiveStreaming
    ? [...round.steps].reverse().find((step) => step.assistant_content)?.assistant_content
    : undefined;
  const assistantContent = round.final_response || latestStreamingContent;
  const assistantFiles = assistantContent
    ? extractAssistantFiles(assistantContent, sessionId)
    : [];
  const visibleAssistantFiles = assistantFileMatches
    ? assistantFiles.flatMap((file) => {
        if (!Object.prototype.hasOwnProperty.call(assistantFileMatches, file.path)) {
          return [file];
        }
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
        <div className="flex-1 min-w-0 pt-0.5">
          <p className="text-xs font-medium text-claude-secondary mb-1.5">你</p>
          <div className="text-[15px] text-claude-text leading-relaxed whitespace-pre-wrap break-words">
            {cleanContent}
          </div>
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
              <AssistantMarkdown content={assistantContent} />
              {/* 流式传输光标 */}
              {!round.final_response && effectiveStreaming && (
                <span className="inline-block w-0.5 h-5 bg-claude-muted ml-0.5 animate-blink align-middle" />
              )}
              {/* 底部文件卡片（去重） */}
              {visibleAssistantFiles.length > 0 && (
                <div className="not-prose mt-4 flex flex-col gap-2">
                  {visibleAssistantFiles.map((file) => (
                    <AssistantFileCard
                      key={`file-${file.path}`}
                      file={file}
                      onOpen={onOpenFileInPanel}
                    />
                  ))}
                </div>
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
        </div>
      </div>

      {/* 分隔线 */}
      <div className="border-b border-claude-border/50" />
    </div>
  );
}
