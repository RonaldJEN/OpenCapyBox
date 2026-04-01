import { useState, useEffect, useRef } from 'react';
import { StepData } from '../types';
import {
  ChevronDown,
  ChevronRight,
  Loader2,
  Lightbulb,
  Terminal,
  FileEdit,
  FilePlus,
  FileText,
  Search,
  Cpu,
  Pencil,
  TerminalSquare,
  BookOpen,
  CheckCircle2,
  Info,
  Zap,
} from 'lucide-react';
import {
  transformToDisplayBlocks,
  formatDuration,
  shortenPath,
  getToolCategory,
  type DisplayBlock,
  type ThinkingBlock,
  type ThinkingGroupBlock,
  type ToolGroupBlock,
  type ToolGroupItem,
} from '../utils/displayBlocks';

// ═══════════════════════════════════════════════════════════════════════════════
// ReasoningPanel — Claude 风格推理面板
// ═══════════════════════════════════════════════════════════════════════════════

interface ReasoningPanelProps {
  steps: StepData[];
  isStreaming?: boolean;
  /** @deprecated Currently unused — completion state is derived from blocks. Kept for caller compatibility. */
  isCompleted?: boolean;
  disableMotion?: boolean;
}

export function ReasoningPanel({ steps, isStreaming = false, isCompleted: _isCompleted = false, disableMotion = false }: ReasoningPanelProps) {
  if (steps.length === 0 && !isStreaming) {
    return null;
  }

  let blocks: DisplayBlock[];
  try {
    blocks = transformToDisplayBlocks(steps, isStreaming);
  } catch (err) {
    console.error('ReasoningPanel: transformToDisplayBlocks failed', err);
    return null;
  }

  // 正在流式传输但没有 blocks
  if (blocks.length === 0 && isStreaming) {
    return (
      <div className="flex items-center gap-2 py-3">
        <Loader2 size={14} className="text-claude-muted animate-spin" />
        <span className="text-sm text-claude-secondary">正在分析请求...</span>
      </div>
    );
  }

  return (
    <div className={`space-y-1 ${disableMotion ? '' : 'animate-fade-in'}`}>
      {blocks.map((block, idx) => (
        <BlockRenderer
          key={`${block.type}-${idx}`}
          block={block}
          isLast={idx === blocks.length - 1}
          isStreaming={isStreaming}
          disableMotion={disableMotion}
        />
      ))}
    </div>
  );
}

// ─── Block Dispatcher ────────────────────────────────────────────────────────

function BlockRenderer({ block, isLast, isStreaming, disableMotion }: {
  block: DisplayBlock;
  isLast: boolean;
  isStreaming: boolean;
  disableMotion: boolean;
}) {
  switch (block.type) {
    case 'thinking':
      return <ThinkingBlockView block={block} disableMotion={disableMotion} />;
    case 'thinkingGroup':
      return <ThinkingGroupBlockView block={block} disableMotion={disableMotion} />;
    case 'toolGroup':
      return <ToolGroupBlockView block={block} isLast={isLast} isStreaming={isStreaming} disableMotion={disableMotion} />;
    case 'narrative':
      return null; // Narrative rendered by Round.tsx via final_response
    default:
      return null;
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// ThinkingGroupBlock — 合并多个连续思考块，可折叠显示
// ═════════════════════════════════════════════════════════════════════════════

function ThinkingGroupBlockView({ block, disableMotion }: { block: ThinkingGroupBlock; disableMotion: boolean }) {
  const [isExpanded, setIsExpanded] = useState(false);
  const [liveDuration, setLiveDuration] = useState(0);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 实时计时器（针对组内最后一个正在流式的 item，1s 间隔足够秒级显示）
  const lastItem = block.items[block.items.length - 1];
  useEffect(() => {
    if (lastItem.isStreaming && lastItem.startTs) {
      const id = setInterval(() => {
        setLiveDuration(Date.now() - lastItem.startTs!);
      }, 1000);
      intervalRef.current = id;
      return () => { clearInterval(id); intervalRef.current = null; };
    }
    // 非流式时确保清理
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  }, [lastItem.isStreaming, lastItem.startTs]);

  const count = block.items.length;
  const totalMs = block.hasStreaming
    ? (block.totalDurationMs || 0) + liveDuration
    : block.totalDurationMs;
  const durationText = totalMs ? formatDuration(totalMs) : undefined;

  const headerText = block.hasStreaming
    ? `思考中 ${count}次${durationText ? ` (${durationText})` : ''}...`
    : `思考 ${count}次${durationText ? ` (${durationText})` : ''}`;

  return (
    <div className={disableMotion ? '' : 'animate-fade-in'}>
      <button
        type="button"
        onClick={() => setIsExpanded(!isExpanded)}
        className="inline-flex items-center gap-1.5 text-sm text-claude-secondary hover:text-claude-text transition-colors py-1 group"
      >
        {block.hasStreaming ? (
          <Loader2 size={13} className="text-claude-muted animate-spin flex-shrink-0" />
        ) : (
          <Lightbulb size={13} className="text-claude-muted flex-shrink-0" />
        )}
        <span className="font-medium">{headerText}</span>
        <ChevronRight
          size={12}
          className={`text-claude-muted transition-transform duration-200 ${isExpanded ? 'rotate-90' : ''}`}
        />
      </button>

      {isExpanded && (
        <div className={`ml-5 mt-0.5 space-y-0.5 ${disableMotion ? '' : 'animate-fade-in'}`}>
          {block.items.map((item, idx) => (
            <ThinkingBlockView key={idx} block={item} disableMotion={disableMotion} />
          ))}
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// ThinkingBlock — "思考 3s >"
// ═══════════════════════════════════════════════════════════════════════════════

function ThinkingBlockView({ block, disableMotion }: { block: ThinkingBlock; disableMotion: boolean }) {
  const [isExpanded, setIsExpanded] = useState(false);
  const [liveDuration, setLiveDuration] = useState(0);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // 实时计时器（1s 间隔足够秒级显示）
  useEffect(() => {
    if (block.isStreaming && block.startTs) {
      const id = setInterval(() => {
        setLiveDuration(Date.now() - block.startTs!);
      }, 1000);
      intervalRef.current = id;
      return () => { clearInterval(id); intervalRef.current = null; };
    }
    // 非流式时确保清理
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  }, [block.isStreaming, block.startTs]);

  const durationMs = block.durationMs || (block.isStreaming ? liveDuration : undefined);
  const durationText = durationMs ? formatDuration(durationMs) : undefined;

  const headerText = block.isStreaming
    ? `思考中${durationText ? ` ${durationText}` : ''}...`
    : `思考${durationText ? ` ${durationText}` : ''}`;

  return (
    <div className={disableMotion ? '' : 'animate-fade-in'}>
      <button
        type="button"
        onClick={() => setIsExpanded(!isExpanded)}
        className="inline-flex items-center gap-1.5 text-sm text-claude-secondary hover:text-claude-text transition-colors py-1 group"
      >
        {block.isStreaming ? (
          <Loader2 size={13} className="text-claude-muted animate-spin flex-shrink-0" />
        ) : (
          <Lightbulb size={13} className="text-claude-muted flex-shrink-0" />
        )}
        <span className="font-medium">{headerText}</span>
        <ChevronRight
          size={12}
          className={`text-claude-muted transition-transform duration-200 ${isExpanded ? 'rotate-90' : ''}`}
        />
      </button>

      {isExpanded && (
        <div className={`mt-1 mb-2 ml-5 ${disableMotion ? '' : 'animate-fade-in'}`}>
          <p className="text-sm text-claude-secondary leading-relaxed border-l-2 border-claude-accent/30 pl-3 py-1.5 whitespace-pre-wrap">
            {block.content}
          </p>
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// ToolGroupBlock — "Edited 2 files, read a file ▾"
// ═══════════════════════════════════════════════════════════════════════════════

function ToolGroupBlockView({ block, isLast: _isLast, isStreaming: _isStreaming, disableMotion }: {
  block: ToolGroupBlock;
  isLast: boolean;
  isStreaming: boolean;
  disableMotion: boolean;
}) {
  const [isExpanded, setIsExpanded] = useState(true);
  const isRunning = block.status === 'running';

  return (
    <div className={disableMotion ? '' : 'animate-fade-in'}>
      {/* Summary header */}
      <button
        type="button"
        onClick={() => setIsExpanded(!isExpanded)}
        className="inline-flex items-center gap-1.5 text-sm text-claude-secondary hover:text-claude-text transition-colors py-1 group"
      >
        {isRunning ? (
          <Loader2 size={13} className="text-claude-muted animate-spin flex-shrink-0" />
        ) : (
          <GroupIcon category={block.dominantCategory} />
        )}
        <span className="font-medium">{block.summary}</span>
        <ChevronDown
          size={12}
          className={`text-claude-muted transition-transform duration-200 ${isExpanded ? '' : '-rotate-90'}`}
        />
      </button>

      {/* Expanded items */}
      {isExpanded && (
        <div className={`ml-5 mt-0.5 space-y-0 ${disableMotion ? '' : 'animate-fade-in'}`}>
          {block.items.map((item, idx) => (
            <ToolItemView key={idx} item={item} disableMotion={disableMotion} />
          ))}
        </div>
      )}

      {/* Done marker */}
      {block.hasDone && !isRunning && <DoneMarker />}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// ToolItem — individual tool call with expandable detail
// ═══════════════════════════════════════════════════════════════════════════════

function ToolItemView({ item, disableMotion }: { item: ToolGroupItem; disableMotion: boolean }) {
  const [isExpanded, setIsExpanded] = useState(false);
  const isRunning = item.status === 'running';
  const isFailed = item.status === 'failed';

  const ToolIcon = getToolIcon(item.toolName);

  const observation = item.result?.content
    ? getObservation(item.toolName, item.result)
    : null;

  return (
    <div className={disableMotion ? '' : 'animate-fade-in'}>
      {/* Main row */}
      <button
        type="button"
        onClick={() => setIsExpanded(!isExpanded)}
        className="w-full text-left flex items-center gap-2 py-1 hover:bg-claude-hover/50 transition-colors rounded-md px-1 -mx-1 group"
      >
        {isRunning ? (
          <Loader2 size={12} className="text-claude-muted animate-spin flex-shrink-0" />
        ) : isFailed ? (
          <div className="w-3 h-3 rounded-full border-2 border-claude-error flex items-center justify-center flex-shrink-0">
            <span className="text-[8px] text-claude-error font-bold">!</span>
          </div>
        ) : (
          <ToolIcon size={12} className="text-claude-muted flex-shrink-0" />
        )}

        <span className={`text-sm font-semibold flex-1 min-w-0 ${isFailed ? 'text-claude-error' : 'text-claude-text'}`}>
          {item.description}
        </span>

        <ChevronRight
          size={11}
          className={`text-claude-muted flex-shrink-0 transition-transform duration-200 opacity-0 group-hover:opacity-100 ${isExpanded ? 'rotate-90 opacity-100' : ''}`}
        />
      </button>

      {/* Diff stats — separate line below description */}
      {item.diffStats && (
        <div className="ml-5 flex items-center gap-1.5 py-0.5 text-xs font-mono">
          {item.filePath && (
            <span className="text-claude-secondary">{shortenPath(item.filePath)}</span>
          )}
          <span className="text-green-600">+{item.diffStats.added}</span>
          <span className="text-red-500">-{item.diffStats.removed}</span>
        </div>
      )}

      {/* Observation — info icon, richer content */}
      {observation && !isExpanded && (
        <div className="ml-5 flex items-start gap-1.5 py-0.5">
          <Info size={11} className="text-claude-muted flex-shrink-0 mt-0.5" />
          <span className="text-xs text-claude-secondary whitespace-pre-line line-clamp-4">{observation}</span>
        </div>
      )}

      {/* Expanded detail */}
      {isExpanded && (
        <div className={`ml-5 mt-1 mb-2 space-y-2 ${disableMotion ? '' : 'animate-fade-in'}`}>
          {/* Input */}
          {item.input && Object.keys(item.input).length > 0 && (
            <div>
              <div className="flex items-center gap-1 text-xs text-claude-muted mb-1">
                <Terminal size={10} />
                <span>输入</span>
              </div>
              <TruncatedCodeBlock
                content={JSON.stringify(item.input, null, 2)}
                className="bg-[#1e1e1e] text-gray-300"
              />
            </div>
          )}

          {/* Result */}
          {item.result && (
            <div>
              <div className="flex items-center gap-1 text-xs text-claude-muted mb-1">
                <Cpu size={10} />
                <span>{item.result.success !== false ? '输出' : '错误'}</span>
                {item.executionTimeMs && (
                  <span className="text-claude-muted/70 ml-1">({formatDuration(item.executionTimeMs)})</span>
                )}
              </div>
              <TruncatedCodeBlock
                content={item.result.content || item.result.error || ''}
                className={item.result.success !== false
                  ? 'bg-claude-success/5 text-claude-text border border-claude-success/20'
                  : 'bg-claude-error/5 text-claude-error border border-claude-error/20'
                }
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// DoneMarker
// ═══════════════════════════════════════════════════════════════════════════════

function DoneMarker() {
  return (
    <div className="flex items-center gap-1.5 py-1 ml-0.5">
      <CheckCircle2 size={14} className="text-claude-success flex-shrink-0" />
      <span className="text-sm text-claude-muted font-medium">Done</span>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// TruncatedCodeBlock — 可折叠代码块
// ═══════════════════════════════════════════════════════════════════════════════

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
      <div className={`p-3 overflow-x-auto transition-all duration-300 ${isExpanded ? '' : 'max-h-[160px]'}`}>
        <pre className="whitespace-pre-wrap break-words">{content}</pre>
      </div>

      {!isExpanded && (
        <div className="absolute bottom-8 left-0 right-0 h-12 bg-gradient-to-t from-[#1e1e1e]/80 to-transparent pointer-events-none" />
      )}

      <button
        type="button"
        onClick={() => setIsExpanded(!isExpanded)}
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
    default:       return Terminal;
  }
}

function getObservation(toolName: string, result: { content: string; success?: boolean; error?: string }): string | null {
  if (result.success === false) {
    return result.error ? truncateObs(result.error) : 'Failed';
  }
  const content = result.content;
  if (!content || content.length < 5) return null;

  const category = getToolCategory(toolName);
  switch (category) {
    case 'edit':
    case 'create':
      return truncateObs(content);
    case 'bash': {
      const lines = content.split('\n').filter(Boolean);
      const preview = lines.slice(0, 4).join('\n');
      return preview ? truncateObs(preview) : null;
    }
    case 'search':
    case 'skill':
      return truncateObs(content);
    default:
      return null;
  }
}

function truncateObs(str: string): string {
  if (str.length <= 200) return str;
  return str.slice(0, 197) + '...';
}

/** 根据 dominantCategory 返回语义化分组图标 */
function GroupIcon({ category }: { category: string }) {
  switch (category) {
    case 'edit':   return <Pencil size={13} className="text-claude-muted flex-shrink-0" />;
    case 'create': return <FilePlus size={13} className="text-claude-muted flex-shrink-0" />;
    case 'read':   return <FileText size={13} className="text-claude-muted flex-shrink-0" />;
    case 'bash':   return <TerminalSquare size={13} className="text-claude-muted flex-shrink-0" />;
    case 'search': return <Search size={13} className="text-claude-muted flex-shrink-0" />;
    case 'skill':  return <BookOpen size={13} className="text-claude-muted flex-shrink-0" />;
    default:       return <Zap size={13} className="text-claude-muted flex-shrink-0" />;
  }
}
