/**
 * Display Blocks 转换层
 *
 * 将扁平的 StepData[] 聚合为 Claude 官方风格的 DisplayBlock[]，
 * 支持跨 Step 分组、智能工具描述、thinking 计时、diff 统计等。
 */

import type { StepData, ToolResult } from '../types';

// ─── 工具分类 ────────────────────────────────────────────────────────────────

export type ToolCategory = 'edit' | 'create' | 'read' | 'bash' | 'search' | 'skill' | 'note' | 'other';

/** 统一的工具分类函数 — 所有分类逻辑的唯一来源 */
export function getToolCategory(name: string): ToolCategory {
  const lower = name.toLowerCase();
  if (lower === 'edit_file' || lower === 'edittool') return 'edit';
  if (lower === 'write_file' || lower === 'writetool' || lower === 'create_file') return 'create';
  if (lower === 'read_file' || lower === 'readtool') return 'read';
  if (lower === 'bash' || lower === 'bashtool' || lower === 'shell' || lower === 'bash_output' || lower === 'bashoutputtool' || lower === 'bash_kill' || lower === 'bashkilltool') return 'bash';
  if (lower.includes('search')) return 'search';
  if (lower === 'get_skill') return 'skill';
  if (lower === 'session_note' || lower === 'note') return 'note';
  return 'other';
}

/** 判断工具是否为文件操作类 */
function isFileToolCategory(cat: ToolCategory): boolean {
  return cat === 'edit' || cat === 'create' || cat === 'read';
}

// ─── Display Block 类型 ─────────────────────────────────────────────────────

export interface ThinkingBlock {
  type: 'thinking';
  content: string;
  durationMs?: number;          // thinking_end_ts - thinking_start_ts
  isStreaming: boolean;          // 正在接收中
  startTs?: number;             // 用于实时计时器
}

export interface ToolGroupItem {
  description: string;          // 智能描述，如 "Read src/app.py"
  toolName: string;
  input?: Record<string, any>;
  result?: ToolResult;
  filePath?: string;            // 文件操作的路径
  diffStats?: { added: number; removed: number };
  executionTimeMs?: number;
  status: 'running' | 'completed' | 'failed';
}

export interface ToolGroupBlock {
  type: 'toolGroup';
  summary: string;              // "Edited 2 files, read a file"
  items: ToolGroupItem[];
  status: 'running' | 'completed' | 'failed';
  hasDone: boolean;             // 是否显示 Done 标记
  dominantCategory: string;     // 主要工具类别，用于选择分组图标
}

export interface NarrativeBlock {
  type: 'narrative';
  content: string;
  isStreaming: boolean;
}

export interface ThinkingGroupBlock {
  type: 'thinkingGroup';
  items: ThinkingBlock[];       // 包含的所有 thinking 条目
  totalDurationMs?: number;     // 所有 thinking 时长之和
  hasStreaming: boolean;         // 组内是否有正在流式的条目
}

export type DisplayBlock = ThinkingBlock | ThinkingGroupBlock | ToolGroupBlock | NarrativeBlock;

// ─── 工具描述生成器 ─────────────────────────────────────────────────────────

/**
 * 智能生成工具描述（1 行中文/英文混合文字）
 */
export function getToolDescription(name: string, input: Record<string, any>): string {
  const category = getToolCategory(name);
  const lowerName = name.toLowerCase();

  switch (category) {
    case 'read': {
      const path = input.path || input.file_path || '';
      return path ? `Read ${shortenPath(path)}` : 'Read file';
    }
    case 'create': {
      const path = input.path || input.file_path || '';
      return path ? `Create ${shortenPath(path)}` : 'Create file';
    }
    case 'edit': {
      const path = input.path || input.file_path || '';
      return path ? `Update ${shortenPath(path)}` : 'Edit file';
    }
    case 'bash': {
      // 区分子类型
      if (lowerName === 'bash_output' || lowerName === 'bashoutputtool') return 'Read command output';
      if (lowerName === 'bash_kill' || lowerName === 'bashkilltool') return 'Stop process';
      const cmd = input.command || input.cmd || '';
      if (cmd) {
        const firstCmd = cmd.split(/[;&|]/).map((s: string) => s.trim()).filter(Boolean)[0] || cmd;
        if (firstCmd.length > 60) return 'Run command';
        return `Run ${firstCmd}`;
      }
      return 'Run command';
    }
    case 'search': {
      const query = input.query || input.q || '';
      return query ? `Search "${truncate(query, 40)}"` : 'Search';
    }
    case 'skill': {
      const skillName = input.skill_name || input.name || '';
      return skillName ? `Load skill: ${skillName}` : 'Load skill';
    }
    case 'note':
      return 'Save note';
    default:
      return name;
  }
}

/**
 * 从工具调用中提取文件路径（如有）
 */
export function extractFilePath(name: string, input: Record<string, any>): string | undefined {
  if (isFileToolCategory(getToolCategory(name))) {
    return input.path || input.file_path;
  }
  return undefined;
}

/**
 * 从工具结果中提取 diff 统计（如有）
 */
export function extractDiffStats(name: string, result?: ToolResult): { added: number; removed: number } | undefined {
  if (!result?.content) return undefined;
  if (getToolCategory(name) === 'edit') {
    // 匹配末尾的 " +X -Y" 格式（后端 EditTool 输出格式），前置空格防止文件名含 + 的误匹配
    const match = result.content.match(/\s\+(\d+)\s+-(\d+)\s*$/);
    if (match) {
      return { added: parseInt(match[1], 10), removed: parseInt(match[2], 10) };
    }
  }
  return undefined;
}

// ─── 分组摘要生成器 ─────────────────────────────────────────────────────────

/**
 * 生成工具分组摘要（如 "Edited 2 files, read a file"）
 */
export function getGroupSummary(items: ToolGroupItem[]): string {
  const counts: Record<string, number> = {};
  const fileNames: Record<string, Set<string>> = {};

  for (const item of items) {
    const category = categorizeToolAction(item.toolName);
    counts[category] = (counts[category] || 0) + 1;
    if (item.filePath) {
      if (!fileNames[category]) fileNames[category] = new Set();
      fileNames[category].add(shortenPath(item.filePath));
    }
  }

  const parts: string[] = [];

  // 按优先级排序：edit > create > read > bash > search > other
  const priorityOrder = ['edit', 'create', 'read', 'bash', 'search', 'skill', 'other'];

  for (const cat of priorityOrder) {
    const count = counts[cat];
    if (!count) continue;

    switch (cat) {
      case 'edit': {
        const files = fileNames[cat]?.size || count;
        parts.push(files === 1 ? `Edited ${[...(fileNames[cat] || [])][0] || 'a file'}` : `Edited ${files} files`);
        break;
      }
      case 'create': {
        const files = fileNames[cat]?.size || count;
        parts.push(files === 1 ? `Created ${[...(fileNames[cat] || [])][0] || 'a file'}` : `Created ${files} files`);
        break;
      }
      case 'read': {
        const files = fileNames[cat]?.size || count;
        parts.push(files === 1 ? `Read ${[...(fileNames[cat] || [])][0] || 'a file'}` : `Read ${files} files`);
        break;
      }
      case 'bash':
        parts.push(count === 1 ? `Ran a command` : `Ran ${count} commands`);
        break;
      case 'search':
        parts.push(count === 1 ? `Searched` : `Searched ${count} times`);
        break;
      case 'skill':
        parts.push(count === 1 ? `Loaded a skill` : `Loaded ${count} skills`);
        break;
      case 'other':
        parts.push(count === 1 ? `Used a tool` : `Used ${count} tools`);
        break;
    }
  }

  return parts.join(', ') || 'Processing';
}

/** 分组摘要专用分类：将 note 归入 other 以保持摘要简洁 */
function categorizeToolAction(name: string): string {
  const cat = getToolCategory(name);
  // note 类归入 other 以保持分组摘要行为不变
  return cat === 'note' ? 'other' : cat;
}

// ─── 核心转换函数 ────────────────────────────────────────────────────────────

/**
 * 将 StepData[] 转换为 Claude 风格的 DisplayBlock[]
 *
 * 规则：
 * 1. 有 thinking 的 step → ThinkingBlock
 * 2. 连续的含 tool_calls 的 steps → 合并为一个 ToolGroupBlock
 * 3. 有 assistant_content 且无 tool_calls → NarrativeBlock
 * 4. ToolGroupBlock 完成后生成 Done 标记
 */
export function transformToDisplayBlocks(
  steps: StepData[],
  isStreaming: boolean = false
): DisplayBlock[] {
  const blocks: DisplayBlock[] = [];
  let pendingToolItems: ToolGroupItem[] = [];
  let pendingToolStepsStatus: string[] = [];

  const flushToolGroup = (isDone: boolean) => {
    if (pendingToolItems.length === 0) return;

    const allCompleted = pendingToolStepsStatus.every(s => s === 'completed');
    const anyFailed = pendingToolStepsStatus.some(s => s === 'failed');
    const status: 'running' | 'completed' | 'failed' = anyFailed ? 'failed' : allCompleted ? 'completed' : 'running';

    // 计算主要工具类别（出现次数最多的类别，平局按优先级）
    const catCounts: Record<string, number> = {};
    for (const item of pendingToolItems) {
      const cat = categorizeToolAction(item.toolName);
      catCounts[cat] = (catCounts[cat] || 0) + 1;
    }
    const catPriority = ['edit', 'bash', 'create', 'read', 'search', 'skill', 'other'];
    let dominantCategory = 'other';
    let maxCount = 0;
    for (const cat of catPriority) {
      if ((catCounts[cat] || 0) > maxCount) {
        maxCount = catCounts[cat];
        dominantCategory = cat;
      }
    }

    blocks.push({
      type: 'toolGroup',
      summary: getGroupSummary(pendingToolItems),
      items: [...pendingToolItems],
      status,
      hasDone: isDone && status !== 'running',
      dominantCategory,
    });
    pendingToolItems = [];
    pendingToolStepsStatus = [];
  };

  for (let i = 0; i < steps.length; i++) {
    const step = steps[i];
    const isLastStep = i === steps.length - 1;
    const isStepStreaming = isStreaming && isLastStep && step.status !== 'completed';

    // 1. Thinking block
    if (step.thinking) {
      // 如果之前累积了工具组，先 flush
      const nextStepHasTools = step.tool_calls.length > 0;
      if (!nextStepHasTools) {
        flushToolGroup(true);
      }

      const durationMs = (step.thinking_start_ts && step.thinking_end_ts)
        ? step.thinking_end_ts - step.thinking_start_ts
        : undefined;

      blocks.push({
        type: 'thinking',
        content: step.thinking,
        durationMs,
        isStreaming: isStepStreaming && !step.thinking_end_ts,
        startTs: step.thinking_start_ts,
      });
    }

    // 2. Tool calls → accumulate into pending group
    if (step.tool_calls.length > 0) {
      for (let j = 0; j < step.tool_calls.length; j++) {
        const tc = step.tool_calls[j];
        const tr = step.tool_results[j];
        const filePath = extractFilePath(tc.name, tc.input);
        const diffStats = extractDiffStats(tc.name, tr);

        const itemStatus: 'running' | 'completed' | 'failed' = 
          tr ? (tr.success === false ? 'failed' : 'completed') : 'running';

        pendingToolItems.push({
          description: getToolDescription(tc.name, tc.input),
          toolName: tc.name,
          input: tc.input,
          result: tr,
          filePath,
          diffStats,
          executionTimeMs: tr?.execution_time_ms,
          status: itemStatus,
        });
      }
      pendingToolStepsStatus.push(step.status);
    }

    // 3. Narrative (assistant_content with no tool_calls)
    if (step.assistant_content && step.tool_calls.length === 0) {
      flushToolGroup(true);
      // Narrative blocks are typically absorbed into final_response by Round.tsx.
      // We only emit them if streaming (preview of in-progress text).
      if (isStepStreaming) {
        blocks.push({
          type: 'narrative',
          content: step.assistant_content,
          isStreaming: true,
        });
      }
    }

    // 4. If a non-tool step follows tool steps, or this is the last step, flush
    const nextStep = steps[i + 1];
    if (pendingToolItems.length > 0) {
      const nextHasTools = nextStep && nextStep.tool_calls.length > 0;
      // Also check: next step only has thinking (no tools) – should flush
      const nextHasOnlyThinking = nextStep && !nextStep.tool_calls.length && nextStep.thinking;

      if (!nextHasTools || nextHasOnlyThinking || isLastStep) {
        flushToolGroup(!isStepStreaming);
      }
    }
  }

  // Final flush for any remaining accumulated tool items
  flushToolGroup(!isStreaming);

  // Post-process: merge consecutive ThinkingBlocks into ThinkingGroupBlock
  return mergeConsecutiveThinking(blocks);
}

/**
 * 将连续的 ThinkingBlock 合并为 ThinkingGroupBlock
 * 单个 ThinkingBlock 保持不变；2+ 个合并为 ThinkingGroupBlock
 */
function mergeConsecutiveThinking(blocks: DisplayBlock[]): DisplayBlock[] {
  const result: DisplayBlock[] = [];
  let thinkingAccum: ThinkingBlock[] = [];

  const flushThinking = () => {
    if (thinkingAccum.length === 0) return;
    if (thinkingAccum.length === 1) {
      // 单条思考不合并，直接保留
      result.push(thinkingAccum[0]);
    } else {
      // 2+ 条合并为 ThinkingGroupBlock
      let totalMs = 0;
      let hasStreaming = false;
      for (const t of thinkingAccum) {
        if (t.durationMs) totalMs += t.durationMs;
        if (t.isStreaming) hasStreaming = true;
      }
      result.push({
        type: 'thinkingGroup',
        items: [...thinkingAccum],
        totalDurationMs: totalMs > 0 ? totalMs : undefined,
        hasStreaming,
      });
    }
    thinkingAccum = [];
  };

  for (const block of blocks) {
    if (block.type === 'thinking') {
      thinkingAccum.push(block);
    } else {
      flushThinking();
      result.push(block);
    }
  }
  flushThinking();

  return result;
}

// ─── 工具函数 ─────────────────────────────────────────────────────────────

export function shortenPath(path: string): string {
  if (!path) return '';
  // 取文件名或最后两级路径
  const parts = path.replace(/\\/g, '/').split('/').filter(Boolean);
  if (parts.length <= 2) return parts.join('/');
  return parts.slice(-2).join('/');
}

function truncate(str: string, maxLen: number): string {
  if (str.length <= maxLen) return str;
  return str.slice(0, maxLen) + '...';
}

/**
 * 格式化毫秒为人类可读时长
 */
export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  return remainingSeconds > 0 ? `${minutes}m ${remainingSeconds}s` : `${minutes}m`;
}
