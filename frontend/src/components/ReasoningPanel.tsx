import { useState, useEffect } from 'react';
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
  X,
  Zap,
} from 'lucide-react';
import {
  transformToDisplayBlocks,
  formatDuration,
  shortenPath,
  getToolCategory,
  type DisplayBlock,
  type ThinkingBlock,
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

  const activityBlocks = blocks.filter((block) => block.type !== 'narrative');

  if (activityBlocks.length === 0) {
    return null;
  }

  return (
    <div className={`space-y-1 ${disableMotion ? '' : 'animate-fade-in'}`}>
      <ActivityOverview
        blocks={activityBlocks}
        lastDisplayBlock={blocks[blocks.length - 1]}
        isStreaming={isStreaming}
        disableMotion={disableMotion}
      />
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// ActivityOverview — 主聊天区只渲染一个活动入口
// ═════════════════════════════════════════════════════════════════════════════

function ActivityOverview({ blocks, lastDisplayBlock, isStreaming, disableMotion }: {
  blocks: DisplayBlock[];
  lastDisplayBlock: DisplayBlock;
  isStreaming: boolean;
  disableMotion: boolean;
}) {
  const [isActivityOpen, setIsActivityOpen] = useState(false);
  const items = collectThinkingItems(blocks);
  const activeItem = [...items].reverse().find((item) => item.isStreaming);
  const activeToolGroup = isStreaming ? [...blocks].reverse().find(
    (block): block is ToolGroupBlock => block.type === 'toolGroup' && block.status === 'running'
  ) : undefined;
  const waitingToolGroup = isStreaming && !activeItem && !activeToolGroup && lastDisplayBlock.type === 'toolGroup'
    ? lastDisplayBlock
    : undefined;
  const previewItem = activeItem || items[items.length - 1];
  const activeToolStartTs = activeToolGroup ? getRunningToolStartTs(activeToolGroup) : undefined;
  const waitingStartTs = waitingToolGroup ? getLastToolCompletionTs(waitingToolGroup) : undefined;
  const liveDuration = useLiveDuration(!!activeItem, activeItem?.startTs);
  const liveToolDuration = useLiveDuration(!activeItem && !!activeToolGroup, activeToolStartTs);
  const liveWaitingDuration = useLiveDuration(!!waitingToolGroup, waitingStartTs);

  const totalDurationMs = getActivityDurationMs(blocks);
  const totalMs = activeItem
    ? totalDurationMs + liveDuration
    : activeToolGroup
      ? totalDurationMs + liveToolDuration
      : waitingToolGroup
        ? totalDurationMs + liveWaitingDuration
      : totalDurationMs;
  const durationText = totalMs ? formatDuration(totalMs) : undefined;
  const stepCount = countActivitySteps(blocks);

  if (activeItem) {
    return (
      <>
        <ActiveThinkingCard
          content={previewItem.content}
          durationText={durationText}
          onOpenActivity={() => setIsActivityOpen(true)}
          disableMotion={disableMotion}
        />
        <ActivityDrawer
          isOpen={isActivityOpen}
          onClose={() => setIsActivityOpen(false)}
          blocks={blocks}
          stepCount={stepCount}
          durationText={durationText}
          disableMotion={disableMotion}
        />
      </>
    );
  }

  if (activeToolGroup) {
    return (
      <>
        <ActiveToolCard
          summary={activeToolGroup.summary}
          durationText={durationText}
          onOpenActivity={() => setIsActivityOpen(true)}
          disableMotion={disableMotion}
        />
        <ActivityDrawer
          isOpen={isActivityOpen}
          onClose={() => setIsActivityOpen(false)}
          blocks={blocks}
          stepCount={stepCount}
          durationText={durationText}
          disableMotion={disableMotion}
        />
      </>
    );
  }

  if (waitingToolGroup) {
    return (
      <>
        <ActiveWaitingCard
          summary={waitingToolGroup.summary}
          durationText={durationText}
          onOpenActivity={() => setIsActivityOpen(true)}
          disableMotion={disableMotion}
        />
        <ActivityDrawer
          isOpen={isActivityOpen}
          onClose={() => setIsActivityOpen(false)}
          blocks={blocks}
          stepCount={stepCount}
          durationText={durationText}
          disableMotion={disableMotion}
        />
      </>
    );
  }

  return (
    <>
      <button
        type="button"
        onClick={() => setIsActivityOpen(true)}
        className="inline-flex items-center gap-1.5 rounded-full border border-claude-border bg-white/70 px-3 py-1 text-sm text-claude-secondary shadow-sm transition-colors hover:border-claude-border-strong hover:bg-white hover:text-claude-text group"
      >
        <Lightbulb size={13} className="text-claude-muted flex-shrink-0" />
        <span className="font-medium">{getCompletedActivityLabel(items.length > 0, durationText)}</span>
        <ChevronRight
          size={12}
          className="text-claude-muted transition-transform duration-200 group-hover:translate-x-0.5"
        />
      </button>
      <ActivityDrawer
        isOpen={isActivityOpen}
        onClose={() => setIsActivityOpen(false)}
        blocks={blocks}
        stepCount={stepCount}
        durationText={durationText}
        disableMotion={disableMotion}
      />
    </>
  );
}

function ActiveWaitingCard({ summary, durationText, onOpenActivity, disableMotion }: {
  summary: string;
  durationText?: string;
  onOpenActivity: () => void;
  disableMotion: boolean;
}) {
  return (
    <div className={`my-2 rounded-xl border border-claude-border bg-white/75 px-4 py-3 shadow-sm ${disableMotion ? '' : 'animate-fade-in'}`}>
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2 text-sm">
          <Loader2 size={14} className="shrink-0 animate-spin text-claude-muted" />
          <span className="font-medium text-claude-text">正在处理工具结果</span>
          {durationText && <span className="text-claude-muted">{durationText}</span>}
        </div>

        <button
          type="button"
          onClick={onOpenActivity}
          className="inline-flex shrink-0 items-center gap-1 rounded-full border border-claude-border bg-claude-bg px-2.5 py-1 text-xs font-medium text-claude-secondary transition-colors hover:border-claude-border-strong hover:bg-white hover:text-claude-text"
        >
          <span>查看活动</span>
          <ChevronRight size={12} />
        </button>
      </div>

      <div className="mt-3 border-l-2 border-claude-border pl-3 text-[15px] leading-relaxed text-claude-text">
        {summary || '等待模型继续'}
        <span className={`ml-1 inline-block text-claude-accent ${disableMotion ? '' : 'animate-pulse'}`}>_</span>
      </div>
    </div>
  );
}

function ActiveToolCard({ summary, durationText, onOpenActivity, disableMotion }: {
  summary: string;
  durationText?: string;
  onOpenActivity: () => void;
  disableMotion: boolean;
}) {
  return (
    <div className={`my-2 rounded-xl border border-claude-border bg-white/75 px-4 py-3 shadow-sm ${disableMotion ? '' : 'animate-fade-in'}`}>
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2 text-sm">
          <Loader2 size={14} className="shrink-0 animate-spin text-claude-muted" />
          <span className="font-medium text-claude-text">正在调用工具</span>
          {durationText && <span className="text-claude-muted">{durationText}</span>}
        </div>

        <button
          type="button"
          onClick={onOpenActivity}
          className="inline-flex shrink-0 items-center gap-1 rounded-full border border-claude-border bg-claude-bg px-2.5 py-1 text-xs font-medium text-claude-secondary transition-colors hover:border-claude-border-strong hover:bg-white hover:text-claude-text"
        >
          <span>查看活动</span>
          <ChevronRight size={12} />
        </button>
      </div>

      <div className="mt-3 border-l-2 border-claude-border pl-3 text-[15px] leading-relaxed text-claude-text">
        {summary || '等待工具返回'}
        <span className={`ml-1 inline-block text-claude-accent ${disableMotion ? '' : 'animate-pulse'}`}>_</span>
      </div>
    </div>
  );
}

function ActiveThinkingCard({ content, durationText, onOpenActivity, disableMotion }: {
  content: string;
  durationText?: string;
  onOpenActivity: () => void;
  disableMotion: boolean;
}) {
  return (
    <div className={`my-2 rounded-xl border border-claude-border bg-white/75 px-4 py-3 shadow-sm ${disableMotion ? '' : 'animate-fade-in'}`}>
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2 text-sm">
          <span className={`h-2 w-2 rounded-full bg-claude-accent ${disableMotion ? '' : 'animate-dot-pulse'}`} />
          <span className="font-medium text-claude-text">正在思考</span>
          {durationText && <span className="text-claude-muted">{durationText}</span>}
        </div>

        <button
          type="button"
          onClick={onOpenActivity}
          className="inline-flex shrink-0 items-center gap-1 rounded-full border border-claude-border bg-claude-bg px-2.5 py-1 text-xs font-medium text-claude-secondary transition-colors hover:border-claude-border-strong hover:bg-white hover:text-claude-text"
        >
          <span>查看活动</span>
          <ChevronRight size={12} />
        </button>
      </div>

      <div className="mt-3 border-l-2 border-claude-accent/40 pl-3 text-[15px] leading-relaxed text-claude-text whitespace-pre-wrap">
        {content}
        <span className={`ml-1 inline-block text-claude-accent ${disableMotion ? '' : 'animate-pulse'}`}>_</span>
      </div>
    </div>
  );
}

function ActivityDrawer({ isOpen, onClose, blocks, stepCount, durationText, disableMotion }: {
  isOpen: boolean;
  onClose: () => void;
  blocks: DisplayBlock[];
  stepCount: number;
  durationText?: string;
  disableMotion: boolean;
}) {
  if (!isOpen) return null;

  return (
    <>
      <div
        className="fixed inset-0 z-30 bg-black/20 backdrop-blur-[2px]"
        onClick={onClose}
        aria-hidden="true"
      />
      <aside className={`fixed right-0 top-0 bottom-0 z-40 w-[460px] max-w-[calc(100vw-24px)] border-l border-claude-border bg-claude-bg shadow-2xl ${disableMotion ? '' : 'animate-slide-in-right'}`}>
        <div className="flex h-full flex-col">
          <div className="flex items-center justify-between border-b border-claude-border px-5 py-4">
            <div className="min-w-0 text-sm text-claude-secondary">
              <span className="font-semibold text-claude-text">活动</span>
              <span className="mx-1">·</span>
              <span>共 {stepCount} 步</span>
              {durationText && <span> · {durationText}</span>}
            </div>
            <button
              type="button"
              onClick={onClose}
              className="rounded-full p-1.5 text-claude-muted transition-colors hover:bg-claude-hover hover:text-claude-text"
              aria-label="关闭活动"
            >
              <X size={16} />
            </button>
          </div>

          <div className="flex-1 overflow-y-auto px-5 py-4">
            <ActivityBlockList blocks={blocks} disableMotion={disableMotion} />
          </div>
        </div>
      </aside>
    </>
  );
}

function ActivityBlockList({ blocks, disableMotion }: { blocks: DisplayBlock[]; disableMotion: boolean }) {
  return (
    <div className="space-y-4">
      {blocks.map((block, index) => {
        if (block.type === 'thinking') {
          return <ActivityThinkingItem key={`thinking-${index}`} item={block} />;
        }
        if (block.type === 'thinkingGroup') {
          return block.items.map((item, itemIndex) => (
            <ActivityThinkingItem key={`thinking-group-${index}-${itemIndex}`} item={item} />
          ));
        }
        if (block.type === 'toolGroup') {
          return <ToolGroupBlockView key={`tool-${index}`} block={block} isLast={false} isStreaming={false} disableMotion={disableMotion} />;
        }
        return null;
      })}
    </div>
  );
}

function ActivityThinkingItem({ item }: { item: ThinkingBlock }) {
  const title = getThinkingTitle(item.content);
  const durationText = item.durationMs ? formatDuration(item.durationMs) : undefined;

  return (
    <div className="flex gap-3">
      <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-claude-accent" />
      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-3 text-sm">
          <span className="font-medium text-claude-text truncate">{title}</span>
          {durationText && <span className="shrink-0 text-xs text-claude-muted">{durationText}</span>}
        </div>
        <p className="mt-1 text-sm leading-relaxed text-claude-secondary whitespace-pre-wrap">
          {item.content}
        </p>
      </div>
    </div>
  );
}

function useLiveDuration(isStreaming: boolean, startTs?: number): number {
  const [liveDuration, setLiveDuration] = useState(0);

  useEffect(() => {
    if (!isStreaming || !startTs) {
      setLiveDuration(0);
      return;
    }

    const updateLiveDuration = () => setLiveDuration(Date.now() - startTs);
    updateLiveDuration();
    const intervalId = setInterval(updateLiveDuration, 1000);
    return () => clearInterval(intervalId);
  }, [isStreaming, startTs]);

  return liveDuration;
}

function collectThinkingItems(blocks: DisplayBlock[]): ThinkingBlock[] {
  const items: ThinkingBlock[] = [];
  for (const block of blocks) {
    if (block.type === 'thinking') {
      items.push(block);
    } else if (block.type === 'thinkingGroup') {
      items.push(...block.items);
    }
  }
  return items;
}

function countActivitySteps(blocks: DisplayBlock[]): number {
  let count = 0;
  for (const block of blocks) {
    if (block.type === 'thinking') {
      count += 1;
    } else if (block.type === 'thinkingGroup') {
      count += block.items.length;
    } else if (block.type === 'toolGroup') {
      count += block.items.length;
    }
  }
  return count;
}

function getActivityDurationMs(blocks: DisplayBlock[]): number {
  let totalMs = 0;
  for (const block of blocks) {
    if (block.type === 'thinking') {
      totalMs += block.durationMs || 0;
    } else if (block.type === 'thinkingGroup') {
      totalMs += block.items.reduce((sum, item) => sum + (item.durationMs || 0), 0);
    } else if (block.type === 'toolGroup') {
      totalMs += block.items.reduce((sum, item) => sum + (item.executionTimeMs || 0), 0);
    }
  }
  return totalMs;
}

function getRunningToolStartTs(block: ToolGroupBlock): number | undefined {
  return [...block.items].reverse().find((item) => item.status === 'running' && item.startTs)?.startTs;
}

function getLastToolCompletionTs(block: ToolGroupBlock): number | undefined {
  return [...block.items].reverse().find((item) => item.result?.received_at_ts)?.result?.received_at_ts;
}

function getThinkingTitle(content: string): string {
  const firstLine = content.split('\n').map((line) => line.trim()).find(Boolean) || '思考中';
  const punctuationIndex = firstLine.search(/[，。！？；：,.!?;:]/);
  const title = punctuationIndex > 0 ? firstLine.slice(0, punctuationIndex) : firstLine;
  return title.length > 28 ? `${title.slice(0, 28)}...` : title;
}

function getCompletedActivityLabel(hasThinking: boolean, durationText?: string): string {
  const durationPart = durationText ? ` ${durationText}` : '';
  return `${hasThinking ? '已完成思考' : '已完成活动'}${durationPart}`;
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
