import { useState } from 'react';
import { ChevronDown, ChevronRight, Loader2, Terminal, FileEdit, FilePlus, FileText, Search, Zap, Plug, Wrench } from 'lucide-react';
import { useActivityDisclosure } from './useActivityDisclosure';
import { WebToolItem } from './WebToolItem';
import { CommandToolItem } from './CommandToolItem';
import { ActivityIcon } from './ActivityIcon';
import { isWebSearchTool } from '../utils/webToolSources';
import { CodeBlock } from './CodeBlock';
import { formatDuration, shortenPath, getToolCategory, type ToolGroupItem } from '../utils/displayBlocks';

// Inline tool details. Round owns the process disclosure and content ordering.
export function ToolItemView({ item, disableMotion }: { item: ToolGroupItem; disableMotion: boolean }) {
  if (isWebSearchTool(item.toolName)) return <WebToolItem item={item} />;
  if (item.toolDisplay?.provider !== 'mcp' && getToolCategory(item.toolName) === 'bash') {
    return <CommandToolItem item={item} disableMotion={disableMotion} />;
  }

  return <DefaultToolItemView item={item} disableMotion={disableMotion} />;
}

function DefaultToolItemView({ item, disableMotion }: { item: ToolGroupItem; disableMotion: boolean }) {
  const [isExpanded, setIsExpanded] = useActivityDisclosure(item.id || item.toolName, false);
  const isRunning = item.status === 'running';
  const isFailed = item.status === 'failed';
  const ToolIcon = item.toolDisplay?.provider === 'mcp' ? Plug : getToolIcon(item.toolName);
  const showIdentity = item.toolDisplay?.provider === 'mcp' || getToolCategory(item.toolName) === 'other';
  const command = getToolCategory(item.toolName) === 'bash'
    ? [item.input?.command, item.input?.cmd].find((value): value is string => typeof value === 'string') : undefined;
  const status = isFailed ? '失败' : isRunning ? '执行中' : item.status === 'unknown' ? '结果未确认' : '';
  return <div className={disableMotion ? '' : 'animate-fade-in'}>
    <button type="button" onClick={() => setIsExpanded(!isExpanded)} aria-expanded={isExpanded}
      title={item.description} className="chat-tool-row">
      <ActivityIcon icon={isRunning ? Loader2 : ToolIcon} spinning={isRunning} />
      <span className="chat-tool-caption">{item.description}</span>
      {status && <span className={`chat-tool-status ${isFailed ? 'text-claude-error' : ''}`}>{status}</span>}
      <ChevronRight size={14} strokeWidth={1.75} aria-hidden="true" className={`shrink-0 transition-transform motion-reduce:transition-none ${isExpanded ? 'rotate-90' : ''}`} />
    </button>
    {isExpanded && <div className="chat-tool-details">
      {showIdentity && <dl className="mb-3 space-y-1 text-xs text-claude-muted">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <dt>工具标识</dt><dd className="min-w-0 break-all font-mono">{item.toolName}</dd>
        </div>
        {item.toolDisplay?.tool_name && item.toolDisplay.tool_name !== item.toolName && <div className="flex flex-wrap items-baseline gap-x-2">
          <dt>原工具名</dt><dd className="min-w-0 break-all font-mono">{item.toolDisplay.tool_name}</dd>
        </div>}
      </dl>}
      {item.diffStats && <div className="flex gap-2 text-sm font-mono">
        {item.filePath && <span>{shortenPath(item.filePath)}</span>}
        <span className="text-green-700">+{item.diffStats.added}</span><span className="text-red-600">−{item.diffStats.removed}</span>
      </div>}
      {command ? <CodeBlock language="bash" value={command} />
        : item.input && Object.keys(item.input).length > 0 && <CodeBlock language="json" value={JSON.stringify(item.input, null, 2)} />}
      {item.result && <details className="chat-tool-output" open>
        <summary>{isFailed ? '错误输出' : '输出'}{item.executionTimeMs ? ` · ${formatDuration(item.executionTimeMs)}` : ''}</summary>
        <TruncatedCodeBlock content={item.result.content || item.result.error || '没有返回内容'} />
      </details>}
    </div>}
  </div>;
}

interface TruncatedCodeBlockProps {
  content: string;
  className?: string;
}

function TruncatedCodeBlock({ content, className = '' }: TruncatedCodeBlockProps) {
  const [isExpanded, setIsExpanded] = useState(false);

  const lines = content.split('\n');
  const isLongContent = lines.length > 20 || content.length > 800;

  if (!isLongContent) {
    return (
      <div className={`p-3 rounded-lg text-xs font-mono overflow-x-auto ${className}`}>
        <pre className="whitespace-pre-wrap break-words">{content}</pre>
      </div>
    );
  }

  return (
    <div className={`rounded-lg text-xs font-mono overflow-hidden relative ${className}`}>
      <div className={`p-3 overflow-x-auto ${isExpanded ? '' : 'max-h-[240px] overflow-y-hidden'}`}>
        <pre className="whitespace-pre-wrap break-words">{content}</pre>
      </div>



      <button
        type="button"
        onClick={() => setIsExpanded(!isExpanded)}
        aria-expanded={isExpanded}
        className="w-full py-1.5 text-xs text-center text-claude-muted hover:text-claude-secondary transition-colors flex items-center justify-center gap-1"
      >
        <span>{isExpanded ? '收起' : '展开全部'}</span>
        <ChevronDown size={12} className={`transition-transform duration-200 ${isExpanded ? 'rotate-180' : ''}`} />
      </button>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// Helper 函数
// ═══════════════════════════════════════════════════════════════════════════════

function getToolIcon(toolName: string): typeof Terminal {
  const category = getToolCategory(toolName);
  switch (category) {
    case 'edit':   return FileEdit;
    case 'create': return FilePlus;
    case 'read':   return FileText;
    case 'search': return Search;
    case 'subagent': return Zap;
    case 'bash': return Terminal;
    default:       return Wrench;
  }
}
