import { describe, it, expect } from 'vitest';
import { projectToolItems } from '../../utils/displayBlocks';
import type { StepData } from '../../types';

describe('projectToolItems', () => {
  const makeStep = (partial: Partial<StepData>): StepData => ({
    step_number: 1, thinking: '', assistant_content: '',
    tool_calls: [], tool_results: [], status: 'completed', ...partial,
  });

  it('按调用身份关联乱序结果，跨正文和思考保留工具顺序与来源', () => {
    const display = { provider: 'mcp', server_name: '资料服务', tool_title: '检索研报', tool_name: 'lookup' };
    const items = projectToolItems([
      makeStep({ thinking: '思考', assistant_content: '进展', tool_calls: [
        { id: 'a', name: 'mcp__123__lookup', tool_display: display, input: { query: 'alpha' }, sequence: 3 },
        { id: 'b', name: 'mcp__123__lookup', input: { query: 'beta' }, sequence: 4 },
      ], tool_results: [
        { tool_call_id: 'b', success: false, content: 'beta failure' },
        { tool_call_id: 'a', success: true, content: 'alpha result', execution_time_ms: 1200 },
      ] }),
      makeStep({ step_number: 2, assistant_content: '答复' }),
    ]);
    expect(items.map(item => [item.id, item.sequence, item.stepNumber, item.result?.content, item.status]))
      .toEqual([['a', 3, 1, 'alpha result', 'completed'], ['b', 4, 1, 'beta failure', 'failed']]);
    expect(items[0]).toMatchObject({ toolDisplay: display, description: '资料服务 · 检索研报', executionTimeMs: 1200 });
  });

  it('终态缺失结果和未知布尔保持未知，只有当前运行工具显示进行中', () => {
    const earlier = makeStep({ tool_calls: [{ id: 'old', name: 'bash', input: {} }] });
    const current = makeStep({ step_number: 2, status: 'streaming', tool_calls: [
      { id: 'pending', name: 'bash', input: {} }, { id: 'unknown', name: 'bash', input: {} },
    ], tool_results: [{ tool_call_id: 'unknown', content: 'not evidence of success', success: null }] });
    expect(projectToolItems([earlier, current], true).map(item => item.status)).toEqual(['unknown', 'running', 'unknown']);
    expect(projectToolItems([earlier, current]).map(item => item.status)).toEqual(['unknown', 'unknown', 'unknown']);
  });

  it('旧无调用ID记录按位置保留结果及编辑信息，不占用新调用的身份', () => {
    const items = projectToolItems([makeStep({ tool_calls: [
      { name: 'edit_file', input: { path: 'a.ts' } }, { id: 'new', name: 'read_file', input: {} },
    ], tool_results: [{ success: true, content: 'Edited a.ts +10 -5' }, { success: true, content: 'legacy output' }] })]);
    expect(items[0]).toMatchObject({ id: 'legacy-tool:1:0', filePath: 'a.ts', diffStats: { added: 10, removed: 5 }, status: 'completed' });
    expect(items[1]).toMatchObject({ id: 'new', status: 'unknown' });
    expect(items[1].result).toBeUndefined();
  });
});
