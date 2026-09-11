/** 工具展示投影；正文与实时思考分别由 transcript 和状态栏负责。 */

import type { StepData, ToolDisplayMetadata, ToolResult } from '../types';

// ─── 工具分类 ────────────────────────────────────────────────────────────────

export type ToolCategory = 'edit' | 'create' | 'read' | 'bash' | 'search' | 'skill' | 'note' | 'subagent' | 'other';

/** 统一的工具分类函数 — 所有分类逻辑的唯一来源 */
export function getToolCategory(name: string): ToolCategory {
  const lower = name.toLowerCase();
  if (lower === 'apply_patch' || lower === 'edit_file' || lower === 'edittool') return 'edit';
  if (lower === 'write_file' || lower === 'writetool') return 'create';
  if (lower === 'read_file' || lower === 'readtool') return 'read';
  if (lower === 'bash' || lower === 'bashtool' || lower === 'shell' || lower === 'bash_output' || lower === 'bashoutputtool' || lower === 'bash_kill' || lower === 'bashkilltool') return 'bash';
  if (['glm_search', 'glm_batch_search', 'mcp_tool_search', 'search_memory'].includes(lower)) return 'search';
  if (lower === 'get_skill') return 'skill';
  if (lower === 'session_note' || lower === 'note') return 'note';
  if (lower === 'sub_agent' || lower === 'subagenttool') return 'subagent';
  return 'other';
}

/** 判断工具是否为文件操作类 */
function isFileToolCategory(cat: ToolCategory): boolean {
  return cat === 'edit' || cat === 'create' || cat === 'read';
}

// ─── Display Block 类型 ─────────────────────────────────────────────────────

export interface ToolGroupItem {
  id?: string;
  sequence?: number;
  stepNumber?: number;
  description: string;          // 智能描述，如 "读取 src/app.py"
  toolName: string;
  toolDisplay?: ToolDisplayMetadata | null;
  input?: Record<string, unknown>;
  result?: ToolResult;
  filePath?: string;            // 文件操作的路径
  diffStats?: { added: number; removed: number };
  executionTimeMs?: number;
  startTs?: number;
  status: 'running' | 'completed' | 'failed' | 'unknown';
}

// ─── 工具描述生成器 ─────────────────────────────────────────────────────────

/**
 * 智能生成工具描述（1 行中文/英文混合文字）
 */
export function getToolDescription(name: string, input: Record<string, unknown>, display?: ToolDisplayMetadata | null): string {
  if (display?.provider === 'mcp') {
    const title = display.tool_title?.trim() || display.tool_name?.trim() || name;
    const server = display.server_name?.trim();
    return server ? `${server} · ${title}` : title;
  }
  const category = getToolCategory(name);
  const lowerName = name.toLowerCase();

  if (lowerName === 'present_files') {
    const paths = Array.isArray(input.paths)
      ? input.paths.filter((path): path is string => typeof path === 'string')
      : [];
    if (paths.length === 1) return `展示附件 ${shortenPath(paths[0])}`;
    return paths.length > 1 ? `展示 ${paths.length} 个附件` : '展示附件';
  }

  switch (category) {
    case 'read': {
      const path = getStringField(input, 'path', 'file_path');
      return path ? `读取 ${shortenPath(path)}` : '读取文件';
    }
    case 'create': {
      const path = getStringField(input, 'path', 'file_path');
      return path ? `创建 ${shortenPath(path)}` : '创建文件';
    }
    case 'edit': {
      if (lowerName === 'apply_patch') {
        const paths = extractPatchPaths(input);
        if (paths.length === 1) return `更新 ${shortenPath(paths[0])}`;
        if (paths.length > 1) return `更新 ${paths.length} 个文件`;
      }
      const path = getStringField(input, 'path', 'file_path');
      return path ? `更新 ${shortenPath(path)}` : '编辑文件';
    }
    case 'bash': {
      // 区分子类型
      if (lowerName === 'bash_output' || lowerName === 'bashoutputtool') return '读取命令输出';
      if (lowerName === 'bash_kill' || lowerName === 'bashkilltool') return '停止进程';
      return '运行命令';
    }
    case 'search': {
      const query = getStringField(input, 'query', 'q');
      return query ? `搜索 "${truncate(query, 40)}"` : '搜索';
    }
    case 'skill': {
      const skillName = getStringField(input, 'skill_name', 'name');
      return skillName ? `加载技能：${skillName}` : '加载技能';
    }
    case 'note':
      return '保存笔记';
    case 'subagent': {
      const description = typeof input.description === 'string' ? input.description.trim() : '';
      const prompt = typeof input.prompt === 'string' ? input.prompt.trim() : '';
      const title = description || firstMeaningfulLine(prompt);
      return title ? `委派子任务 ${truncate(title, 42)}` : '委派子任务';
    }
    default:
      return name;
  }
}

/**
 * 从工具调用中提取文件路径（如有）
 */
export function extractFilePath(name: string, input: Record<string, unknown>): string | undefined {
  if (name.toLowerCase() === 'present_files') {
    const paths = Array.isArray(input.paths)
      ? input.paths.filter((path): path is string => typeof path === 'string')
      : [];
    return paths.length === 1 ? paths[0] : paths.length > 1 ? `${paths.length} files` : undefined;
  }
  if (isFileToolCategory(getToolCategory(name))) {
    if (name.toLowerCase() === 'apply_patch') {
      const paths = extractPatchPaths(input);
      return paths.length === 1 ? paths[0] : paths.length > 1 ? `${paths.length} files` : undefined;
    }
    return getStringField(input, 'path', 'file_path') || undefined;
  }
  return undefined;
}

function extractPatchPaths(input: Record<string, unknown>): string[] {
  if (typeof input.patch !== 'string') return [];
  return [...input.patch.matchAll(/^\*\*\* (?:Add File|Delete File|Update File|Move to):\s*(.+)$/gm)]
    .map((match) => match[1].trim())
    .filter((path, index, paths) => path.length > 0 && paths.indexOf(path) === index);
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

/** Preserve tool order, invocation identity and explicit outcomes for the transcript. */
export function projectToolItems(steps: StepData[], isStreaming = false): ToolGroupItem[] {
  return steps.flatMap((step, index) => {
    const isStepStreaming = isStreaming && index === steps.length - 1 && step.status !== 'completed';
    return step.tool_calls.map((call, callIndex) => {
      const result = call.id
        ? step.tool_results.find((item) => item.tool_call_id === call.id)
        : step.tool_results[callIndex];
      return {
        id: call.id || `legacy-tool:${step.step_number}:${callIndex}`,
        sequence: call.sequence ?? undefined,
        stepNumber: step.step_number,
        description: getToolDescription(call.name, call.input, call.tool_display),
        toolName: call.name,
        toolDisplay: call.tool_display,
        input: call.input,
        result,
        filePath: extractFilePath(call.name, call.input),
        diffStats: extractDiffStats(call.name, result),
        executionTimeMs: result?.execution_time_ms,
        startTs: call.started_at_ts,
        status: result
          ? result.success === false ? 'failed' : result.success === true ? 'completed' : 'unknown'
          : isStepStreaming ? 'running' : 'unknown',
      };
    });
  });
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

function firstMeaningfulLine(str: string): string {
  return str
    .split('\n')
    .map(line => line.trim())
    .find(Boolean) || '';
}

function getStringField(input: Record<string, unknown>, ...keys: string[]): string {
  for (const key of keys) {
    const value = input[key];
    if (typeof value === 'string') return value;
  }
  return '';
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
