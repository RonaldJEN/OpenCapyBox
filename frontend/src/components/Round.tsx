import { RoundData, FileInfo, AttachmentInfo } from '../types';
import { User } from 'lucide-react';
import { ReasoningPanel } from './ReasoningPanel';
import { FileAttachment } from './FileAttachment';
import { CodeBlock } from './CodeBlock';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { parseMessageContent } from '../utils/messageParser';
import { getFileIcon, getFileExtLabel, getFileBadgeClass, getFileIconClass, toFileInfo, buildSandboxFileUrl, isImageFile } from '../utils/fileUtils';

interface RoundProps {
  round: RoundData;
  isStreaming?: boolean;
  disableMotion?: boolean;
  userAttachments?: AttachmentInfo[];
  sessionId?: string;
  /** 用戶認證 session ID（用於構建沙箱文件 URL） */
  authSessionId?: string;
  onPreviewAttachment?: (file: FileInfo) => void;
}

export function Round({ round, isStreaming = false, disableMotion = false, userAttachments = [], sessionId, authSessionId, onPreviewAttachment }: RoundProps) {
  // 解析用户消息，提取附件信息
  const { attachments, cleanContent } = parseMessageContent(round.user_message);

  const isCompleted = round.status === 'completed' || round.status === 'failed' || round.status === 'max_steps_reached';

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
                  {isImageFile(file) && (file.data_url || (sessionId && authSessionId)) ? (
                    <img
                      src={file.data_url || buildSandboxFileUrl(sessionId!, file.path, authSessionId!)}
                      alt={file.name}
                      className="w-full h-full object-cover transition-transform group-hover:scale-105"
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
              isStreaming={isStreaming}
              isCompleted={isCompleted && !!round.final_response}
              disableMotion={disableMotion}
            />
          )}

          {/* 最终答案 OR 流式传输中的答案 */}
          {(round.final_response || (isStreaming && round.steps.length > 0 && !round.steps[round.steps.length - 1].tool_calls.length && round.steps[round.steps.length - 1].assistant_content)) && (
            <div className="prose max-w-none mt-4">
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
                {round.final_response || round.steps[round.steps.length - 1].assistant_content}
              </ReactMarkdown>
              {/* 流式传输光标 */}
              {!round.final_response && isStreaming && (
                <span className="inline-block w-0.5 h-5 bg-claude-muted ml-0.5 animate-blink align-middle" />
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
