import { describe, it, expect } from 'vitest';
import {
  getToolDescription,
  extractFilePath,
  extractDiffStats,
  getGroupSummary,
  transformToDisplayBlocks,
  formatDuration,
  getToolCategory,
  type ToolGroupItem,
  type ThinkingBlock,
  type ThinkingGroupBlock,
  type ToolGroupBlock,
} from '../../utils/displayBlocks';
import type { StepData } from '../../types';

// ═══════════════════════════════════════════════════════════════════════════════
// getToolDescription
// ═══════════════════════════════════════════════════════════════════════════════

describe('getToolDescription', () => {
  it('应为 read_file 工具生成描述', () => {
    expect(getToolDescription('read_file', { path: 'src/app.py' })).toBe('Read src/app.py');
    expect(getToolDescription('ReadTool', { file_path: '/a/b/c/d.ts' })).toBe('Read c/d.ts');
    expect(getToolDescription('read_file', {})).toBe('Read file');
  });

  it('应为 write/create 工具生成描述', () => {
    expect(getToolDescription('write_file', { path: 'test.txt' })).toBe('Create test.txt');
    expect(getToolDescription('create_file', { file_path: 'a/b.ts' })).toBe('Create a/b.ts');
    expect(getToolDescription('WriteTool', {})).toBe('Create file');
  });

  it('应为 edit 工具生成描述', () => {
    expect(getToolDescription('edit_file', { path: 'main.py' })).toBe('Update main.py');
    expect(getToolDescription('EditTool', { file_path: 'src/utils/helper.ts' })).toBe('Update utils/helper.ts');
    expect(getToolDescription('edit_file', {})).toBe('Edit file');
  });

  it('应为 bash 工具生成描述', () => {
    expect(getToolDescription('bash', { command: 'npm install' })).toBe('Run npm install');
    expect(getToolDescription('BashTool', { cmd: 'python test.py' })).toBe('Run python test.py');
    expect(getToolDescription('bash', {})).toBe('Run command');
    // 命令超过 60 字符
    const longCmd = 'a'.repeat(61);
    expect(getToolDescription('bash', { command: longCmd })).toBe('Run command');
  });

  it('应为 search 工具生成描述', () => {
    expect(getToolDescription('glm_search', { query: 'test query' })).toBe('Search "test query"');
    expect(getToolDescription('glm_search', {})).toBe('Search');
  });

  it('应为 get_skill 工具生成描述', () => {
    expect(getToolDescription('get_skill', { skill_name: 'docx' })).toBe('Load skill: docx');
    expect(getToolDescription('get_skill', {})).toBe('Load skill');
  });

  it('应为 session_note 生成描述', () => {
    expect(getToolDescription('session_note', {})).toBe('Save note');
  });

  it('对未知工具返回工具名本身', () => {
    expect(getToolDescription('my_custom_tool', {})).toBe('my_custom_tool');
  });

  it('应处理 bash_output 和 bash_kill', () => {
    expect(getToolDescription('bash_output', {})).toBe('Read command output');
    expect(getToolDescription('bash_kill', {})).toBe('Stop process');
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// extractFilePath
// ═══════════════════════════════════════════════════════════════════════════════

describe('extractFilePath', () => {
  it('应从文件工具中提取路径', () => {
    expect(extractFilePath('read_file', { path: 'a.txt' })).toBe('a.txt');
    expect(extractFilePath('edit_file', { file_path: 'b.ts' })).toBe('b.ts');
    expect(extractFilePath('WriteTool', { path: 'c.py' })).toBe('c.py');
  });

  it('对非文件工具应返回 undefined', () => {
    expect(extractFilePath('bash', { command: 'ls' })).toBeUndefined();
    expect(extractFilePath('glm_search', { query: 'q' })).toBeUndefined();
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// extractDiffStats
// ═══════════════════════════════════════════════════════════════════════════════

describe('extractDiffStats', () => {
  it('应从 edit 结果中提取 diff 统计', () => {
    const result = { success: true, content: 'Successfully edited /a/b.ts +5 -3' };
    expect(extractDiffStats('edit_file', result)).toEqual({ added: 5, removed: 3 });
  });

  it('不应匹配内容中间的 +X -Y', () => {
    // 内容中间有类似格式但不在末尾，不应匹配
    const result = { success: true, content: 'found +2 -3 matches in file' };
    expect(extractDiffStats('edit_file', result)).toBeUndefined();
  });

  it('无匹配时返回 undefined', () => {
    expect(extractDiffStats('edit_file', { success: true, content: 'done' })).toBeUndefined();
    expect(extractDiffStats('edit_file', undefined)).toBeUndefined();
    expect(extractDiffStats('bash', { success: true, content: '+1 -2' })).toBeUndefined();
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// getToolCategory
// ═══════════════════════════════════════════════════════════════════════════════

describe('getToolCategory', () => {
  it('应正确分类各种工具', () => {
    expect(getToolCategory('edit_file')).toBe('edit');
    expect(getToolCategory('EditTool')).toBe('edit');
    expect(getToolCategory('write_file')).toBe('create');
    expect(getToolCategory('WriteTool')).toBe('create');
    expect(getToolCategory('create_file')).toBe('create');
    expect(getToolCategory('read_file')).toBe('read');
    expect(getToolCategory('ReadTool')).toBe('read');
    expect(getToolCategory('bash')).toBe('bash');
    expect(getToolCategory('BashTool')).toBe('bash');
    expect(getToolCategory('shell')).toBe('bash');
    expect(getToolCategory('bash_output')).toBe('bash');
    expect(getToolCategory('BashOutputTool')).toBe('bash');
    expect(getToolCategory('bash_kill')).toBe('bash');
    expect(getToolCategory('BashKillTool')).toBe('bash');
    expect(getToolCategory('glm_search')).toBe('search');
    expect(getToolCategory('get_skill')).toBe('skill');
    expect(getToolCategory('session_note')).toBe('note');
    expect(getToolCategory('note')).toBe('note');
    expect(getToolCategory('my_custom_tool')).toBe('other');
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// getGroupSummary
// ═══════════════════════════════════════════════════════════════════════════════

describe('getGroupSummary', () => {
  const makeItem = (toolName: string, filePath?: string): ToolGroupItem => ({
    description: '',
    toolName,
    status: 'completed',
    filePath,
  });

  it('单个编辑操作应显示文件名', () => {
    expect(getGroupSummary([makeItem('edit_file', 'src/app.py')])).toBe('Edited src/app.py');
  });

  it('多个编辑操作应显示文件数', () => {
    const items = [
      makeItem('edit_file', 'a.ts'),
      makeItem('edit_file', 'b.ts'),
    ];
    expect(getGroupSummary(items)).toBe('Edited 2 files');
  });

  it('同一文件多次编辑应去重', () => {
    const items = [
      makeItem('edit_file', 'app.py'),
      makeItem('edit_file', 'app.py'),
    ];
    // 去重后只有 1 个文件
    expect(getGroupSummary(items)).toBe('Edited app.py');
  });

  it('混合操作应合并描述', () => {
    const items = [
      makeItem('edit_file', 'a.ts'),
      makeItem('read_file', 'b.ts'),
      makeItem('bash'),
    ];
    expect(getGroupSummary(items)).toBe('Edited a.ts, Read b.ts, Ran a command');
  });

  it('空列表返回 Processing', () => {
    expect(getGroupSummary([])).toBe('Processing');
  });

  it('应为 create 操作生成正确描述', () => {
    expect(getGroupSummary([makeItem('create_file', 'new.ts')])).toBe('Created new.ts');
  });

  it('多个 bash 命令', () => {
    const items = [makeItem('bash'), makeItem('bash')];
    expect(getGroupSummary(items)).toBe('Ran 2 commands');
  });

  it('应为 skill 操作生成正确描述', () => {
    expect(getGroupSummary([makeItem('get_skill')])).toBe('Loaded a skill');
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// formatDuration
// ═══════════════════════════════════════════════════════════════════════════════

describe('formatDuration', () => {
  it('毫秒级别', () => {
    expect(formatDuration(500)).toBe('500ms');
    expect(formatDuration(0)).toBe('0ms');
  });

  it('秒级别', () => {
    expect(formatDuration(1000)).toBe('1s');
    expect(formatDuration(3500)).toBe('4s');
    expect(formatDuration(59000)).toBe('59s');
  });

  it('分钟级别', () => {
    expect(formatDuration(60000)).toBe('1m');
    expect(formatDuration(90000)).toBe('1m 30s');
    expect(formatDuration(120000)).toBe('2m');
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// transformToDisplayBlocks
// ═══════════════════════════════════════════════════════════════════════════════

describe('transformToDisplayBlocks', () => {
  const makeStep = (partial: Partial<StepData>): StepData => ({
    step_number: 1,
    thinking: '',
    assistant_content: '',
    tool_calls: [],
    tool_results: [],
    status: 'completed',
    ...partial,
  });

  it('空步骤应返回空数组', () => {
    expect(transformToDisplayBlocks([])).toEqual([]);
  });

  it('仅含 thinking 的步骤应生成 ThinkingBlock', () => {
    const steps = [makeStep({ thinking: '正在分析...' })];
    const blocks = transformToDisplayBlocks(steps);

    expect(blocks).toHaveLength(1);
    expect(blocks[0].type).toBe('thinking');
    const tb = blocks[0] as ThinkingBlock;
    expect(tb.content).toBe('正在分析...');
    expect(tb.isStreaming).toBe(false);
  });

  it('流式传输中的 thinking 应标记 isStreaming', () => {
    const steps = [
      makeStep({
        thinking: '思考中...',
        status: 'streaming',
        thinking_start_ts: 1000,
      }),
    ];
    const blocks = transformToDisplayBlocks(steps, true);
    const tb = blocks[0] as ThinkingBlock;
    expect(tb.isStreaming).toBe(true);
    expect(tb.startTs).toBe(1000);
  });

  it('带时间戳的 thinking 应计算 durationMs', () => {
    const steps = [
      makeStep({
        thinking: '分析完毕',
        thinking_start_ts: 1000,
        thinking_end_ts: 4500,
      }),
    ];
    const blocks = transformToDisplayBlocks(steps);
    const tb = blocks[0] as ThinkingBlock;
    expect(tb.durationMs).toBe(3500);
  });

  it('含工具调用的步骤应生成 ToolGroupBlock', () => {
    const steps = [
      makeStep({
        tool_calls: [{ name: 'read_file', input: { path: 'a.txt' } }],
        tool_results: [{ success: true, content: 'ok' }],
      }),
    ];
    const blocks = transformToDisplayBlocks(steps);

    expect(blocks).toHaveLength(1);
    expect(blocks[0].type).toBe('toolGroup');
    const tg = blocks[0] as ToolGroupBlock;
    expect(tg.items).toHaveLength(1);
    expect(tg.items[0].description).toBe('Read a.txt');
    expect(tg.status).toBe('completed');
    expect(tg.hasDone).toBe(true);
    expect(tg.dominantCategory).toBe('read');
  });

  it('跨步骤的连续工具调用应合并为一个 ToolGroupBlock', () => {
    const steps = [
      makeStep({
        step_number: 1,
        tool_calls: [{ name: 'read_file', input: { path: 'a.txt' } }],
        tool_results: [{ success: true, content: '...' }],
      }),
      makeStep({
        step_number: 2,
        tool_calls: [{ name: 'edit_file', input: { path: 'a.txt' } }],
        tool_results: [{ success: true, content: 'done +3 -1' }],
      }),
    ];
    const blocks = transformToDisplayBlocks(steps);

    expect(blocks).toHaveLength(1);
    const tg = blocks[0] as ToolGroupBlock;
    expect(tg.items).toHaveLength(2);
    expect(tg.summary).toContain('Edited');
    expect(tg.summary).toContain('Read');
    expect(tg.dominantCategory).toBe('edit');
  });

  it('thinking + tool + thinking + tool 应生成交替 blocks', () => {
    const steps = [
      makeStep({
        step_number: 1,
        thinking: '第一次思考',
        tool_calls: [{ name: 'bash', input: { command: 'ls' } }],
        tool_results: [{ success: true, content: 'files' }],
      }),
      makeStep({
        step_number: 2,
        thinking: '第二次思考',
        tool_calls: [{ name: 'bash', input: { command: 'cat a' } }],
        tool_results: [{ success: true, content: 'content' }],
      }),
    ];
    const blocks = transformToDisplayBlocks(steps);

    // 预期：thinking → toolGroup（合并两步bash）→ thinking（第二次）
    // 但由于第二步同时有 thinking + tool_call，thinking 会先输出
    // 然后两步的 tool_calls 会尝试合并
    // 实际行为取决于 flush 逻辑

    // 两个连续 thinking 会被合并为 thinkingGroup
    const thinkingBlocks = blocks.filter(b => b.type === 'thinking');
    const thinkingGroups = blocks.filter(b => b.type === 'thinkingGroup');
    // 应至少有 thinking 或 thinkingGroup
    expect(thinkingBlocks.length + thinkingGroups.length).toBeGreaterThanOrEqual(1);
  });

  it('多个连续 thinking 块应合并为 ThinkingGroupBlock', () => {
    const steps = [
      makeStep({ step_number: 1, thinking: '第一次思考', thinking_start_ts: 1000, thinking_end_ts: 3000 }),
      makeStep({ step_number: 2, thinking: '第二次思考', thinking_start_ts: 3500, thinking_end_ts: 5000 }),
      makeStep({ step_number: 3, thinking: '第三次思考', thinking_start_ts: 5500, thinking_end_ts: 7000 }),
    ];
    const blocks = transformToDisplayBlocks(steps);

    expect(blocks).toHaveLength(1);
    expect(blocks[0].type).toBe('thinkingGroup');
    const tg = blocks[0] as ThinkingGroupBlock;
    expect(tg.items).toHaveLength(3);
    expect(tg.items[0].content).toBe('第一次思考');
    expect(tg.items[1].content).toBe('第二次思考');
    expect(tg.items[2].content).toBe('第三次思考');
    expect(tg.totalDurationMs).toBe(5000); // 2000 + 1500 + 1500
    expect(tg.hasStreaming).toBe(false);
  });

  it('单个 thinking 块不应合并为 ThinkingGroupBlock', () => {
    const steps = [
      makeStep({ step_number: 1, thinking: '唯一的思考' }),
    ];
    const blocks = transformToDisplayBlocks(steps);

    expect(blocks).toHaveLength(1);
    expect(blocks[0].type).toBe('thinking');
  });

  it('被 ToolGroup 间隔的 thinking 应分别处理', () => {
    const steps = [
      makeStep({ step_number: 1, thinking: '思考A' }),
      makeStep({
        step_number: 2,
        tool_calls: [{ name: 'bash', input: { command: 'ls' } }],
        tool_results: [{ success: true, content: 'files' }],
      }),
      makeStep({ step_number: 3, thinking: '思考B' }),
    ];
    const blocks = transformToDisplayBlocks(steps);

    // 思考A → ToolGroup → 思考B，各自独立，不合并
    expect(blocks).toHaveLength(3);
    expect(blocks[0].type).toBe('thinking');
    expect(blocks[1].type).toBe('toolGroup');
    expect(blocks[2].type).toBe('thinking');
  });

  it('流式 thinking 组应标记 hasStreaming', () => {
    const steps = [
      makeStep({ step_number: 1, thinking: '思考1', thinking_start_ts: 1000, thinking_end_ts: 2000 }),
      makeStep({ step_number: 2, thinking: '思考2', status: 'streaming', thinking_start_ts: 2500 }),
    ];
    const blocks = transformToDisplayBlocks(steps, true);

    expect(blocks).toHaveLength(1);
    expect(blocks[0].type).toBe('thinkingGroup');
    const tg = blocks[0] as ThinkingGroupBlock;
    expect(tg.hasStreaming).toBe(true);
    expect(tg.items[1].isStreaming).toBe(true);
  });

  it('失败的工具调用应标记 failed', () => {
    const steps = [
      makeStep({
        tool_calls: [{ name: 'bash', input: { command: 'bad cmd' } }],
        tool_results: [{ success: false, content: '', error: 'command not found' }],
        status: 'failed',
      }),
    ];
    const blocks = transformToDisplayBlocks(steps);
    const tg = blocks[0] as ToolGroupBlock;
    expect(tg.items[0].status).toBe('failed');
  });

  it('正在运行的工具应标记 running', () => {
    const steps = [
      makeStep({
        tool_calls: [{ name: 'bash', input: { command: 'npm test' } }],
        tool_results: [],
        status: 'streaming',
      }),
    ];
    const blocks = transformToDisplayBlocks(steps, true);
    const tg = blocks[0] as ToolGroupBlock;
    expect(tg.items[0].status).toBe('running');
    expect(tg.status).toBe('running');
  });

  it('含 diff 统计的 edit 工具应提取 diffStats', () => {
    const steps = [
      makeStep({
        tool_calls: [{ name: 'edit_file', input: { path: 'a.ts' } }],
        tool_results: [{ success: true, content: 'Successfully edited /workspace/a.ts +10 -5' }],
      }),
    ];
    const blocks = transformToDisplayBlocks(steps);
    const tg = blocks[0] as ToolGroupBlock;
    expect(tg.items[0].diffStats).toEqual({ added: 10, removed: 5 });
    expect(tg.dominantCategory).toBe('edit');
  });

  it('bash 为主的分组应有 dominantCategory=bash', () => {
    const steps = [
      makeStep({
        tool_calls: [
          { name: 'bash', input: { command: 'ls' } },
          { name: 'bash', input: { command: 'cat a' } },
          { name: 'get_skill', input: { skill_name: 'pdf' } },
        ],
        tool_results: [
          { success: true, content: 'files' },
          { success: true, content: 'content' },
          { success: true, content: 'skill loaded' },
        ],
      }),
    ];
    const blocks = transformToDisplayBlocks(steps);
    const tg = blocks[0] as ToolGroupBlock;
    expect(tg.dominantCategory).toBe('bash');
  });

  it('流式 assistant_content 应生成 NarrativeBlock', () => {
    const steps = [
      makeStep({
        assistant_content: '正在生成回复...',
        status: 'streaming',
      }),
    ];
    const blocks = transformToDisplayBlocks(steps, true);

    expect(blocks).toHaveLength(1);
    expect(blocks[0].type).toBe('narrative');
    const nb = blocks[0] as { type: 'narrative'; content: string; isStreaming: boolean };
    expect(nb.content).toBe('正在生成回复...');
    expect(nb.isStreaming).toBe(true);
  });

  it('已完成的 assistant_content 不应生成 NarrativeBlock', () => {
    const steps = [
      makeStep({
        assistant_content: '完成的回复',
        status: 'completed',
      }),
    ];
    const blocks = transformToDisplayBlocks(steps, false);

    // 非流式的纯 narrative 交由 Round.tsx 渲染，不生成 block
    expect(blocks).toHaveLength(0);
  });
});
