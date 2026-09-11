import { describe, expect, it } from 'vitest';
import { chatRuntimeReducer } from '../../runtime/chatRuntimeReducer';
import { initialChatRuntimeState, type ChatRuntimeState } from '../../runtime/chatRuntimeTypes';
import { projectSubagentTaskRows } from '../../components/SubagentTaskGroup';
import { projectToolResult } from '../../runtime/toolResults';
import type { RoundData, SubagentTask } from '../../types';
import type { ToolGroupItem } from '../../utils/displayBlocks';

const task = (overrides: Partial<SubagentTask> = {}): SubagentTask => ({
  edge_id: 'edge-a', parent_run_id: 'parent', child_run_id: 'child-a', tool_call_id: 'tool-a',
  agent_name: 'audit', description: '审阅文件', agent_type: null, model_id: null,
  status: 'running', created_at: '2026-09-11T00:00:00', started_at: null, completed_at: null,
  ...overrides,
});
const parent = (overrides: Partial<RoundData> = {}): RoundData => ({
  round_id: 'parent', status: 'running', user_message: '审阅', final_response: null,
  steps: [], step_count: 0, created_at: '2026-09-11T00:00:00', ...overrides,
});
const history = (state: ChatRuntimeState, round: RoundData) => chatRuntimeReducer(state, {
  type: 'HISTORY_LOADED', sessionId: 'session', rounds: [round], loadedAt: Date.now(), source: 'history',
});
const update = (state: ChatRuntimeState, value: SubagentTask, sequence: number) => chatRuntimeReducer(state, {
  type: 'STREAM_EVENT', envelope: {
    ownerSessionId: 'session', clientRunKey: 'run:parent', transportEpoch: 1, connectionId: 'c',
    source: 'subscribe', receivedAt: Date.now(), sequence,
    event: { type: 'CUSTOM', name: 'subagent_run_updated', value },
  },
});

describe('subagent task projection', () => {
  it('keeps startup result separate from graph lifecycle through retry, completion, and legacy refresh', () => {
    const item = (result: ReturnType<typeof projectToolResult>): ToolGroupItem => ({
      id: 'tool-a', toolName: 'sub_agent', description: '审阅文件', input: {}, status: result.success === false ? 'failed' : 'unknown', result,
    });
    const failed = projectSubagentTaskRows([item(projectToolResult({
      success: false,
      content: '{"success":true,"error":"参数错误"}',
    }))], []);
    expect(failed[0]).toMatchObject({ status: 'startup_failed', task: undefined });
    expect(failed[0].item.result?.success).toBe(false);

    const retried = projectSubagentTaskRows([item(projectToolResult({ content: '{"success":true}' }))], []);
    expect(retried[0]).toMatchObject({ status: 'started_pending_sync', task: undefined });

    const running = projectSubagentTaskRows([item(projectToolResult({ content: '{"error":"not a result status"}' }))], [task({ child_run_id: null, status: 'running' })]);
    expect(running[0]).toMatchObject({ status: 'running' });

    const graphFailed = projectSubagentTaskRows([item(projectToolResult({ content: '{"success":true}' }))], [task({ child_run_id: null, status: 'failed' })]);
    expect(graphFailed[0]).toMatchObject({ status: 'startup_failed', task: { child_run_id: null } });

    const completed = projectSubagentTaskRows([item(projectToolResult({ content: '{"success":true}' }))], [task({ status: 'completed' })]);
    expect(completed[0]).toMatchObject({ status: 'completed', task: { child_run_id: 'child-a' } });

    const legacy = projectSubagentTaskRows([item(projectToolResult({ content: '{"error":"old failure text without a boolean"}' }))], []);
    expect(legacy[0]).toMatchObject({ status: 'unknown', task: undefined });
    expect(legacy[0].item.result?.success).toBeNull();
  });

  it('upserts exact edges and never changes parent admission or status', () => {
    let state = history(initialChatRuntimeState, parent());
    const active = state.sessions.session.activeRunKeys;
    state = update(state, task(), 1);
    state = update(state, task({ edge_id: 'edge-b', child_run_id: 'child-b' }), 2);
    state = update(state, task({ status: 'completed', completed_at: '2026-09-11T00:01:00' }), 3);
    state = update(state, task({ parent_run_id: 'another-parent', status: 'failed' }), 4);
    expect(state.sessions.session.rounds).toHaveLength(1);
    expect(state.sessions.session.rounds[0].subagent_tasks?.map((item) => [item.edge_id, item.status]))
      .toEqual([['edge-a', 'completed'], ['edge-b', 'running']]);
    expect(state.sessions.session.rounds[0].status).toBe('running');
    expect(state.sessions.session.activeRunKeys).toEqual(active);
    expect(state.runs['run:parent'].status).toBe('streaming');
  });

  it('stale history preserves terminal tasks and identities; newer history is authoritative', () => {
    let state = history(initialChatRuntimeState, parent());
    state = update(state, task({ status: 'completed', completed_at: '2026-09-11T00:01:00' }), 8);
    state = history(state, parent({ last_event_sequence: 2,
      subagent_tasks: [task({ status: 'requested', child_run_id: null, description: '旧标题' })] }));
    expect(state.sessions.session.rounds[0].subagent_tasks?.[0]).toMatchObject({
      status: 'completed', child_run_id: 'child-a', description: '审阅文件', completed_at: '2026-09-11T00:01:00',
    });
    state = history(state, parent({ last_event_sequence: 10,
      subagent_tasks: [task({ status: 'failed', description: '服务端修正', completed_at: '2026-09-11T00:01:01' })] }));
    expect(state.sessions.session.rounds[0].subagent_tasks?.[0]).toMatchObject({ status: 'failed', description: '服务端修正' });
    state = history(state, parent({ last_event_sequence: 3, subagent_tasks: [task({ status: 'completed' })] }));
    expect(state.sessions.session.rounds[0].subagent_tasks?.[0].status).toBe('failed');
  });

  it('terminal parent history keeps task facts even when an older snapshot omitted them', () => {
    let state = history(initialChatRuntimeState, parent({ status: 'completed', last_event_sequence: 8,
      subagent_tasks: [task({ status: 'completed' })] }));
    state = history(state, parent({ status: 'completed', last_event_sequence: 2 }));
    expect(state.sessions.session.rounds[0].subagent_tasks?.[0].status).toBe('completed');
  });

  it('graph facts newer than the event watermark survive older replayed CUSTOM events', () => {
    let state = history(initialChatRuntimeState, parent({ last_event_sequence: 3,
      subagent_tasks: [task({ status: 'cancelled', completed_at: '2026-09-11T00:02:00' })] }));
    state = update(state, task({ status: 'requested', child_run_id: null }), 4);
    state = update(state, task({ status: 'running' }), 5);
    state = update(state, task({ status: 'failed', completed_at: '2026-09-11T00:01:00' }), 6);
    expect(state.sessions.session.rounds[0].subagent_tasks?.[0]).toMatchObject({
      status: 'cancelled', child_run_id: 'child-a', completed_at: '2026-09-11T00:02:00',
    });
    expect(state.sessions.session.rounds[0].status).toBe('running');
  });
});
