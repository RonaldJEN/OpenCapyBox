import { describe, expect, it } from 'vitest';
import { chatRuntimeReducer } from '../../runtime/chatRuntimeReducer';
import { initialChatRuntimeState, type ChatRuntimeState } from '../../runtime/chatRuntimeTypes';
import { selectLiveThinking } from '../../transcript/liveThinking';
import type { RoundData } from '../../types';

const originalRound: RoundData = {
  round_id: 'round', user_message: 'hello', final_response: '', steps: [],
  step_count: 0, status: 'running', created_at: '2026-09-11T00:00:00Z',
};
const send = (state: ChatRuntimeState, event: Record<string, unknown>) => chatRuntimeReducer(state, {
  type: 'STREAM_EVENT', envelope: {
    ownerSessionId: 'session', clientRunKey: 'run', transportEpoch: 1,
    connectionId: 'connection', source: 'direct', receivedAt: 1000, event,
  },
});
function startThinking() {
  let state = chatRuntimeReducer(initialChatRuntimeState, {
    type: 'LOCAL_RUN_STARTED', sessionId: 'session', clientRunKey: 'run',
    tempRoundId: 'round', source: 'direct', round: originalRound,
  });
  state = send(state, { type: 'RUN_STARTED', threadId: 'session', runId: 'round' });
  state = send(state, { type: 'STEP_STARTED', stepName: 'step_1' });
  return send(state, { type: 'THINKING_TEXT_MESSAGE_START', messageId: 'old' });
}
const currentRound = (state: ChatRuntimeState) => state.sessions.session.rounds[0];
const preview = (state: ChatRuntimeState) => selectLiveThinking(currentRound(state), state.runs.run);

describe('live thinking status preview', () => {
  it('prioritizes an open text segment after its first content without ending thinking', () => {
    let state = send(startThinking(), { type: 'THINKING_TEXT_MESSAGE_CONTENT', messageId: 'old', delta: '分析中' });
    state = send(state, { type: 'TEXT_MESSAGE_START', messageId: 'answer', role: 'assistant' });
    expect(preview(state)).toEqual({ id: 'old', text: '分析中' });
    state = send(state, { type: 'TEXT_MESSAGE_CONTENT', messageId: 'answer', delta: '已经开始回答' });
    expect(preview(state)).toBeNull();
    expect(state.runs.run.buffers.currentThinkingMessageId).toBe('old');
    expect(state.runs.run.buffers.thinkingSegmentStateByMessageId.old.open).toBe(true);
    state = send(state, { type: 'TEXT_MESSAGE_END', messageId: 'answer' });
    expect(preview(state)).toEqual({ id: 'old', text: '分析中' });
  });

  it('uses the live segment verbatim and closes on END without a timestamp or ID', () => {
    let state = startThinking();
    expect(preview(state)).toBeNull();
    state = send(state, { type: 'THINKING_TEXT_MESSAGE_CONTENT', messageId: 'old', delta: '  核对\n资料  ' });
    expect(preview(state)).toEqual({ id: 'old', text: '  核对\n资料  ' });
    state = send(state, { type: 'THINKING_TEXT_MESSAGE_END' });
    expect(preview(state)).toBeNull();
    expect(state.runs.run.buffers.thinkingSegmentStateByMessageId.old).toEqual({ open: false, dirty: true });
    expect(currentRound(state).steps[0].thinking).toBe('  核对\n资料  ');
    expect(selectLiveThinking(currentRound(state))).toBeNull();
  });

  it('does not flash a prior attempt while a new same-step segment starts, or let its late END hide the new one', () => {
    let state = send(startThinking(), { type: 'THINKING_TEXT_MESSAGE_CONTENT', messageId: 'old', delta: '旧尝试' });
    state = send(state, { type: 'THINKING_TEXT_MESSAGE_END', messageId: 'old', timestamp: 100 });
    state = send(state, { type: 'THINKING_TEXT_MESSAGE_START', messageId: 'new', timestamp: 200 });
    expect(preview(state)).toBeNull();
    state = send(state, { type: 'THINKING_TEXT_MESSAGE_CONTENT', messageId: 'new', delta: '新尝试' });
    state = send(state, { type: 'THINKING_TEXT_MESSAGE_END', messageId: 'old', timestamp: 300 });
    expect(preview(state)).toEqual({ id: 'new', text: '新尝试' });
    expect(currentRound(state).steps[0].thinking_end_ts).toBe(100);
    state = send(state, { type: 'THINKING_TEXT_MESSAGE_END', messageId: 'new' });
    expect(preview(state)).toBeNull();
  });

  it.each(['STEP_STARTED', 'STEP_FINISHED'])('%s removes the live pointer while preserving recovery buffers', (type) => {
    let state = send(startThinking(), { type: 'THINKING_TEXT_MESSAGE_CONTENT', messageId: 'old', delta: '尚未聚合的文本' });
    const buffers = state.runs.run.buffers;
    state = send(state, { type, stepName: type === 'STEP_STARTED' ? 'step_2' : 'step_1' });
    expect(preview(state)).toBeNull();
    expect(state.runs.run.buffers.thinkingByMessageId).toBe(buffers.thinkingByMessageId);
    expect(state.runs.run.buffers.thinkingSegmentStateByMessageId).toBe(buffers.thinkingSegmentStateByMessageId);
    state = send(state, { type: 'THINKING_TEXT_MESSAGE_START', messageId: 'new' });
    state = send(state, { type: 'THINKING_TEXT_MESSAGE_CONTENT', messageId: 'new', delta: '当前步骤' });
    expect(preview(state)).toEqual({ id: 'new', text: '当前步骤' });
  });

  it('prioritizes explicit run status, local stop and final-answer presentation', () => {
    let state = send(startThinking(), { type: 'THINKING_TEXT_MESSAGE_CONTENT', messageId: 'old', delta: '分析中' });
    const round = currentRound(state);
    const run = state.runs.run;
    expect(selectLiveThinking(round, run, true)).toBeNull();
    for (const status of ['starting', 'waiting', 'cancelled', 'finished', 'error', 'stale'] as const) {
      expect(selectLiveThinking(round, { ...run, status })).toBeNull();
    }
    for (const status of ['waiting_interaction', 'completed', 'failed', 'cancelled', 'max_steps_reached']) {
      expect(selectLiveThinking({ ...round, status }, run)).toBeNull();
    }
    state = chatRuntimeReducer(state, { type: 'LOCAL_STOP_REQUESTED', sessionId: 'session', runKeys: ['run'] });
    expect(preview(state)).toBeNull();
    state = send(state, { type: 'THINKING_TEXT_MESSAGE_CONTENT', messageId: 'old', delta: '不应继续出现' });
    expect(state.runs.run.buffers.thinkingByMessageId.old).toBe('分析中');
  });

  it('does not replay persisted thinking after an accepted history snapshot', () => {
    let state = send(startThinking(), { type: 'THINKING_TEXT_MESSAGE_CONTENT', messageId: 'old', delta: '已保存的思考' });
    state = send(state, { type: 'THINKING_TEXT_MESSAGE_END', messageId: 'old' });
    state = chatRuntimeReducer(state, {
      type: 'HISTORY_LOADED', sessionId: 'session', source: 'history', loadedAt: 2000,
      rounds: [{ ...currentRound(state), last_event_sequence: 20 }],
    });
    expect(state.runs.run.buffers.thinkingByMessageId).toEqual({});
    expect(currentRound(state).steps[0].thinking).toBe('已保存的思考');
    expect(preview(state)).toBeNull();
    state = send(state, { type: 'STEP_STARTED', stepName: 'step_2' });
    expect(preview(state)).toBeNull();
    state = send(state, { type: 'THINKING_TEXT_MESSAGE_CONTENT', messageId: 'new', delta: '恢复后的真实增量' });
    expect(preview(state)).toEqual({ id: 'new', text: '恢复后的真实增量' });
  });
});
