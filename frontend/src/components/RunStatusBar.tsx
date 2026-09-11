import { useEffect, useState } from 'react';
import { Atom, ChevronRight, Loader2 } from 'lucide-react';
import type { RoundData } from '../types';
import type { ChatRunRuntimeState } from '../runtime/chatRuntimeTypes';
import { selectLiveThinking } from '../transcript/liveThinking';
import { roundRenderKey } from '../transcript/projectRoundTranscript';
import { LiveThinkingPreview } from './LiveThinkingPreview';
import { ActivityIcon } from './ActivityIcon';
function elapsedLabel(ms: number): string {
  const seconds = Math.max(1, Math.round(ms / 1000));
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

export function RunStatusBar({ round, run, answering = false, expanded, controls, onToggle, onRetryStop }: {
  round: RoundData; run?: ChatRunRuntimeState; expanded: boolean; controls?: string;
  answering?: boolean;
  onToggle?: () => void; onRetryStop?: () => void;
}) {
  const [now, setNow] = useState(Date.now);
  const running = round.status === 'running' || round.status === 'waiting_interaction';
  useEffect(() => {
    if (!running || round.started_at_ts == null || run?.localOutputStopped) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running, round.started_at_ts, run?.localOutputStopped]);
  const end = running ? now : round.finished_at_ts;
  const elapsed = round.started_at_ts != null && end != null && end >= round.started_at_ts ? elapsedLabel(end - round.started_at_ts) : undefined;
  const stopping = run?.localOutputStopped && run.cancelRequest !== 'confirmed';
  const resumingInteraction = round.status === 'waiting_interaction' && run?.source === 'resume'
    && (run.status === 'starting' || run.status === 'streaming') && !run.localOutputStopped;
  const thinking = selectLiveThinking(round, run, answering);
  const status = stopping ? (run.cancelRequest === 'pending' ? '正在确认停止结果' : '停止结果待确认')
    : round.status === 'failed' ? '执行失败' : round.status === 'cancelled' ? '已停止'
      : round.status === 'max_steps_reached' ? '达到步数限制'
        : resumingInteraction ? '正在继续'
          : round.status === 'waiting_interaction' ? (round.interrupt?.reason === 'human_approval' ? '等待审批' : '等待回答') : '';
  const label = status || (running ? answering ? '正在回答' : thinking ? '思考中' : '运行中' : elapsed ? `处理了 ${elapsed}` : onToggle ? '处理过程' : '已完成');
  const content = <>
    {(round.status === 'running' || resumingInteraction) && !stopping && <ActivityIcon icon={thinking ? Atom : Loader2} spinning={!thinking} />}
    <span className="chat-process-label">{label}</span>
    {thinking && <><span aria-hidden="true" className="chat-thinking-separator">·</span><LiveThinkingPreview key={`${roundRenderKey(round)}:${thinking.id}`} text={thinking.text} /></>}
    {onToggle && <ChevronRight size={14} strokeWidth={1.75} aria-hidden="true" className={`shrink-0 transition-transform motion-reduce:transition-none ${expanded ? 'rotate-90' : ''}`} />}
  </>;
  return <div className="chat-process-bar">
    {onToggle ? <button type="button" onClick={onToggle} aria-expanded={expanded} aria-controls={controls}
      data-process-summary data-reading-block={`process:${round.idempotency_key || round.round_id}`}
      className={`chat-process-toggle ${thinking ? 'is-thinking' : ''}`}>{content}</button>
      : <span tabIndex={-1} data-process-summary data-reading-block={`process:${round.idempotency_key || round.round_id}`} className={`chat-process-idle inline-flex items-center gap-1.5 ${thinking ? 'is-thinking' : ''}`}>{content}</span>}
    {stopping && run.cancelRequest !== 'pending' && onRetryStop && <button type="button" onClick={onRetryStop} className="text-xs underline underline-offset-4">重新确认停止</button>}
  </div>;
}
