import { useEffect, useId, useState } from 'react';
import { Check, ChevronRight, Copy, Loader2, Terminal } from 'lucide-react';
import { formatDuration, type ToolGroupItem } from '../utils/displayBlocks';
import { useActivityDisclosure } from './useActivityDisclosure';
import { ActivityIcon } from './ActivityIcon';
import FeedbackMessage from './FeedbackMessage';

function CopyCommandText({ value, label }: { value: string; label: string }) {
  const [feedback, setFeedback] = useState<'idle' | 'copied' | 'failed'>('idle');
  useEffect(() => {
    if (feedback !== 'copied') return;
    const timeout = window.setTimeout(() => setFeedback('idle'), 2000);
    return () => window.clearTimeout(timeout);
  }, [feedback]);

  const copy = async () => {
    setFeedback('idle');
    try {
      await navigator.clipboard.writeText(value);
      setFeedback('copied');
    } catch { setFeedback('failed'); }
  };

  return <div className="chat-command-copy-control">
    <span className="chat-command-copy-feedback" role="status">
      {feedback === 'copied' ? '已复制' : ''}
    </span>
    {feedback === 'failed' && <FeedbackMessage tone="error" onDismiss={() => setFeedback('idle')} className="text-xs text-claude-error">复制失败，请重试</FeedbackMessage>}
    <button type="button" className="chat-command-copy" onClick={copy} aria-label={label}
      title={feedback === 'failed' ? '复制失败，请重试' : feedback === 'copied' ? '已复制' : label}>
      {feedback === 'copied' ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
    </button>
  </div>;
}

/** A command and its result share one disclosure; only the preview normalizes whitespace. */
export function CommandToolItem({ item, disableMotion }: { item: ToolGroupItem; disableMotion: boolean }) {
  const [open, setOpen] = useActivityDisclosure(item.id || item.toolName, false);
  const detailId = useId();
  const command = [item.input?.command, item.input?.cmd]
    .find((value): value is string => typeof value === 'string' && value.trim().length > 0);
  const preview = command ? command.replace(/\s+/g, ' ').trim() : item.description;
  const status = item.status === 'running' ? '正在执行' : item.status === 'failed' ? '执行失败'
    : item.status === 'unknown' ? '结果未确认' : '已执行';
  const parameters = !command && item.input && Object.keys(item.input).length > 0
    ? JSON.stringify(item.input, null, 2) : undefined;
  const output = item.result?.content || item.result?.error || '';
  const separateError = item.result?.content && item.result.error && item.result.error !== item.result.content
    ? item.result.error : undefined;
  const duration = item.executionTimeMs !== undefined ? formatDuration(item.executionTimeMs) : undefined;

  return <div className={`chat-command-tool ${disableMotion ? '' : 'animate-fade-in'}`}>
    <button type="button" className="chat-tool-row chat-command-row" aria-expanded={open}
      aria-controls={detailId} title={`${status} ${preview}`} onClick={() => setOpen(!open)}>
      <ActivityIcon icon={item.status === 'running' ? Loader2 : Terminal} spinning={item.status === 'running'} />
      <span className={`chat-command-status ${item.status === 'failed' ? 'text-claude-error' : ''}`}>{status}</span>
      <span className="chat-command-preview">{preview}</span>
      <ChevronRight size={14} strokeWidth={1.75} aria-hidden="true" className={`shrink-0 transition-transform motion-reduce:transition-none ${open ? 'rotate-90' : ''}`} />
    </button>
    {open && <section id={detailId} className="chat-command-panel" aria-label={`${preview} 的命令详情`}>
      <div className="chat-command-toolbar"><span>Shell</span>{duration && <span>{duration}</span>}</div>
      <div className="chat-command-scroll" tabIndex={0} aria-label="命令与输出">
        {(command || parameters) && <section className="chat-command-section chat-command-command-section">
          <div className="chat-command-section-heading">
            <span>{command ? '命令' : '参数'}</span>
            <CopyCommandText value={command || parameters!} label={command ? '复制命令' : '复制参数'} />
          </div>
          <pre className="chat-command-command">{command && <span className="chat-command-prompt" aria-hidden="true">$ </span>}<code>{command || parameters}</code></pre>
        </section>}
        <section className="chat-command-section chat-command-output-section">
          <div className="chat-command-section-heading">
            <span>{item.status === 'failed' ? '错误输出' : '输出'}</span>
            {output && <CopyCommandText value={output} label="复制输出" />}
          </div>
          <pre className="chat-command-output"><code>{output || (item.result ? '没有返回内容' : item.status === 'running' ? '等待命令输出…' : '尚未收到输出')}</code></pre>
        </section>
        {separateError && <section className="chat-command-section chat-command-output-section">
          <div className="chat-command-section-heading"><span>错误</span><CopyCommandText value={separateError} label="复制错误" /></div>
          <pre className="chat-command-output text-claude-error"><code>{separateError}</code></pre>
        </section>}
      </div>
    </section>}
  </div>;
}
