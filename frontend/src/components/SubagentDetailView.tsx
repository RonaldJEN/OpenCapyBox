import { useLayoutEffect, useRef } from 'react';
import { ArrowDown, ArrowLeft, Loader2 } from 'lucide-react';
import type { FileInfo, SubagentTask } from '../types';
import { Round } from './Round';
import FeedbackMessage from './FeedbackMessage';
import { subagentTitle } from './SubagentTaskGroup';
import { useSubagentRound } from './useSubagentRound';
import { useChatReadingPosition } from './useChatReadingPosition';
import { isWorkspaceEntryDeleted } from '../services/workspaceEvents';
import './subagent.css';

export function SubagentDetailView({ sessionId, task, active, onBack, backLabel = '返回主对话', onOpenFile, onOpenSubtask }: {
  sessionId: string; task: SubagentTask; active: boolean; onBack: () => void;
  backLabel?: string;
  onOpenFile?: (file: FileInfo) => void; onOpenSubtask?: (task: SubagentTask) => void;
}) {
  const { round, loading, error, retry } = useSubagentRound(sessionId, task, active);
  const visibleRound = round ? { ...round, assistant_file_references: round.assistant_file_references?.filter(file => file.source !== 'workspace' || !isWorkspaceEntryDeleted(file.entry_id)) } : null;
  const containerRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const backRef = useRef<HTMLButtonElement>(null);
  const reader = useChatReadingPosition({ sessionId: `subtask:${task.child_run_id || task.edge_id}`,
    containerRef, contentRef, hidden: !active, loading: loading && !round,
    layoutKey: 'subtask', contentVersion: round, hasContent: !!round });
  useLayoutEffect(() => {
    if (active) backRef.current?.focus({ preventScroll: true });
  }, [active]);
  const openChild = (child: SubagentTask) => { reader.beginReading(); onOpenSubtask?.(child); };
  return <section className="chat-subtask-detail" style={{ display: active ? undefined : 'none' }} aria-label={`子任务：${subagentTitle(task)}`}>
    <header className="chat-subtask-header">
      <button ref={backRef} type="button" onClick={onBack} className="chat-subtask-back" aria-label={backLabel} title={backLabel}><ArrowLeft size={18} aria-hidden="true" /><span>{backLabel}</span></button>
      <div className="min-w-0"><p className="chat-subtask-header-caption">子任务</p><h2 className="chat-subtask-header-title">{subagentTitle(task)}</h2></div>
    </header>
    <div ref={containerRef} className="chat-subtask-scroll" style={{ overflowAnchor: 'none' }} tabIndex={0}>
      <div ref={contentRef} className="chat-column py-6" data-round-id={round?.round_id || task.child_run_id}>
        {task.prompt && <details className="chat-subtask-instructions"><summary>任务说明</summary><p>{task.prompt}</p></details>}
        {loading && !round && <div role="status" className="flex items-center gap-2 text-sm text-claude-secondary"><Loader2 size={15} className="animate-spin" aria-hidden="true" />正在载入子任务…</div>}
        {error && <div className="my-3 text-[13px] text-claude-error"><FeedbackMessage tone="error">{error}</FeedbackMessage><button type="button" className="mt-1.5 underline underline-offset-4" onClick={retry}>重新连接</button></div>}
        {visibleRound && <Round round={visibleRound} showUserMessage={false} isStreaming={visibleRound.status === 'running'} disableMotion
          sessionId={sessionId} onInspectProcess={reader.beginReading} onOpenSubtask={openChild} onOpenFileInPanel={onOpenFile} />}
        {!loading && !error && !round && <p className="text-sm text-claude-secondary">{task.error || '子任务正在准备，暂时还没有输出。'}</p>}
      </div>
    </div>
    {reader.showScrollButton && <div className="chat-subtask-scroll-control"><button type="button" onClick={reader.scrollToBottom} aria-label="回到子任务最新消息">回到最新消息<ArrowDown size={16} aria-hidden="true" /></button></div>}
  </section>;
}
