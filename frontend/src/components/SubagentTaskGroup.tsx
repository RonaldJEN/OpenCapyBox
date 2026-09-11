import { useId, useState } from 'react';
import { Check, ChevronRight, GitBranch, Loader2, AlertCircle } from 'lucide-react';
import type { SubagentTask } from '../types';
import type { ToolGroupItem } from '../utils/displayBlocks';
import { ActivityIcon } from './ActivityIcon';
import './subagent.css';

export function subagentTitle(task?: SubagentTask, item?: ToolGroupItem): string {
  return task?.description?.trim() || task?.agent_name?.trim()
    || (typeof item?.input?.description === 'string' ? item.input.description.trim() : '') || '子任务';
}

export type SubagentTaskRowStatus = SubagentTask['status'] | 'startup_failed' | 'started_pending_sync' | 'unknown';

export function projectSubagentTaskRows(items: ToolGroupItem[], tasks: SubagentTask[]) {
  return items.map((item) => {
    const task = tasks.find((candidate) => candidate.tool_call_id === item.id);
    const hasChild = Boolean(task?.child_run_id);
    const status: SubagentTaskRowStatus = hasChild
      ? task!.status
      : item.result?.success === false || task?.status === 'failed'
        ? 'startup_failed'
        : item.result?.success === true
          ? 'started_pending_sync'
          : task?.status === 'requested' || task?.status === 'running' || task?.status === 'cancelled'
            ? task.status
            : 'unknown';
    return { item, task, status, title: subagentTitle(task, item) };
  });
}

const statusLabel: Record<SubagentTaskRowStatus, string> = {
  requested: '准备中', running: '进行中', completed: '已完成', failed: '失败', cancelled: '已停止',
  startup_failed: '启动失败', started_pending_sync: '已发起，等待状态同步', unknown: '状态未知',
};

export function SubagentTaskGroup({ items, tasks, onOpen }: {
  items: ToolGroupItem[]; tasks: SubagentTask[]; onOpen?: (task: SubagentTask) => void;
}) {
  const resultIdPrefix = useId();
  const [expandedResults, setExpandedResults] = useState<Record<string, boolean>>({});
  const rows = projectSubagentTaskRows(items, tasks);
  const completed = rows.filter(row => row.status === 'completed').length;
  const failed = rows.filter(row => row.status === 'failed' || row.status === 'startup_failed').length;
  return <div className="chat-subtasks" role="group" aria-label="子任务">
    <div className="chat-subtasks-summary">{items.length} 个子任务 · {completed} 个已完成{failed > 0 && ` · ${failed} 个失败`}</div>
    {rows.map(({ item, task, status, title }) => {
      const key = item.id || item.toolName;
      const hasChild = Boolean(task?.child_run_id);
      const expanded = !hasChild && Boolean(expandedResults[key]);
      const resultId = `${resultIdPrefix}-${key}`;
      const result = item.result?.content || item.result?.error || '没有返回内容';
      const displayedResult = status === 'startup_failed'
        ? (item.result?.error || result).split(/\r?\n/, 1)[0].replace(/^(?:Error:\s*)?(?:Tool execution failed:\s*)?/, '')
        : result;
      return <div key={key}>
      <button type="button" className="chat-subtask-row" disabled={hasChild ? !onOpen : !item.result}
        data-subtask-id={task?.child_run_id || undefined} title={title}
        aria-expanded={!hasChild && item.result ? expanded : undefined}
        aria-controls={!hasChild && item.result ? resultId : undefined}
        onClick={() => {
          if (hasChild && task) onOpen?.({ ...task,
            prompt: typeof item.input?.prompt === 'string' ? item.input.prompt : task.prompt,
            error: item.result?.error || task.error,
          });
          else setExpandedResults(current => ({ ...current, [key]: !current[key] }));
        }}>
        <ActivityIcon icon={status === 'running' || status === 'requested' || status === 'started_pending_sync' ? Loader2
          : status === 'completed' ? Check : status === 'failed' || status === 'startup_failed' ? AlertCircle : GitBranch}
          spinning={status === 'running' || status === 'requested' || status === 'started_pending_sync'}
          className={status === 'failed' || status === 'startup_failed' ? 'text-claude-error' : ''} />
        <span className="chat-subtask-title">{title}</span>
        <span className={status === 'failed' || status === 'startup_failed' ? 'chat-subtask-status text-claude-error' : 'chat-subtask-status'}>
          {statusLabel[status]}
        </span>
        <ChevronRight size={14} strokeWidth={1.75} className={`shrink-0 transition-transform motion-reduce:transition-none ${expanded ? 'rotate-90' : ''}`} aria-hidden="true" />
      </button>
      {expanded && <pre id={resultId} className="chat-subtask-result" tabIndex={0} aria-label={`${title}的调用结果`}>
        {displayedResult}
      </pre>}
    </div>; })}
  </div>;
}
