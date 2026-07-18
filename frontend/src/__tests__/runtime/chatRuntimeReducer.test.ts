import { describe, expect, it } from 'vitest';

import { chatRuntimeReducer } from '../../runtime/chatRuntimeReducer';
import {
  ChatRuntimeState,
  StreamEnvelope,
  initialChatRuntimeState,
} from '../../runtime/chatRuntimeTypes';
import type { RoundData } from '../../types';

function round(overrides: Partial<RoundData> = {}): RoundData {
  return {
    round_id: 'temp-r1',
    user_message: 'hello',
    final_response: '',
    steps: [],
    step_count: 0,
    status: 'running',
    created_at: '2026-01-01T00:00:00.000Z',
    ...overrides,
  };
}

function startRun(
  state: ChatRuntimeState = initialChatRuntimeState,
  overrides: Partial<RoundData> = {},
): ChatRuntimeState {
  return chatRuntimeReducer(state, {
    type: 'LOCAL_RUN_STARTED',
    sessionId: 'sess-a',
    clientRunKey: 'run-a',
    tempRoundId: 'temp-r1',
    idempotencyKey: 'idem-a',
    source: 'direct',
    round: round(overrides),
  });
}

function envelope(event: any, overrides: Partial<StreamEnvelope> = {}): StreamEnvelope {
  return {
    ownerSessionId: 'sess-a',
    clientRunKey: 'run-a',
    transportEpoch: 1,
    connectionId: 'conn-a',
    event,
    source: 'direct',
    receivedAt: Date.parse('2026-01-01T00:00:01.000Z'),
    ...overrides,
  };
}

function stream(state: ChatRuntimeState, event: any, overrides: Partial<StreamEnvelope> = {}) {
  return chatRuntimeReducer(state, {
    type: 'STREAM_EVENT',
    envelope: envelope(event, overrides),
  });
}

describe('chatRuntimeReducer', () => {
  it('binds RUN_STARTED to the temp round and run maps', () => {
    let state = startRun(initialChatRuntimeState, {
      preferred_skills: [
        { key: 'pdf', display_name: 'pdf' },
        { key: 'removed', display_name: 'removed' },
      ],
    });

    state = stream(state, {
      type: 'RUN_STARTED',
      threadId: 'sess-a',
      runId: 'server-r1',
      preferredSkills: [{ key: 'pdf', display_name: 'PDF 文档' }],
    });

    expect(state.sessions['sess-a'].rounds[0].round_id).toBe('server-r1');
    expect(state.runs['run-a'].serverRunId).toBe('server-r1');
    expect(state.runs['run-a'].status).toBe('streaming');
    expect(state.serverRunIdToClientRunKey['server-r1']).toBe('run-a');
    expect(state.tempRoundIdToServerRoundId['temp-r1']).toBe('server-r1');
    expect(state.sessions['sess-a'].rounds[0].preferred_skills).toEqual([
      { key: 'pdf', display_name: 'PDF 文档' },
    ]);
  });

  it('clears optimistic Skill chips when RUN_STARTED resolves no valid Skill', () => {
    let state = startRun(initialChatRuntimeState, {
      preferred_skills: [{ key: 'removed', display_name: 'removed' }],
    });

    state = stream(state, {
      type: 'RUN_STARTED',
      threadId: 'sess-a',
      runId: 'server-r1',
    });
    state = stream(state, {
      type: 'RUN_STARTED',
      threadId: 'sess-a',
      runId: 'server-r1',
      preferredSkills: [],
    });

    expect(state.sessions['sess-a'].rounds[0].preferred_skills).toEqual([]);
  });

  it('appends sequenced text deltas and drops duplicate sequence events', () => {
    let state = startRun();

    state = stream(state, { type: 'TEXT_MESSAGE_START', messageId: 'm1', role: 'assistant' });
    state = stream(state, { type: 'TEXT_MESSAGE_CONTENT', messageId: 'm1', delta: 'hello' }, { sequence: 1 });
    state = stream(state, { type: 'TEXT_MESSAGE_CONTENT', messageId: 'm1', delta: ' duplicate' }, { sequence: 1 });
    state = stream(state, { type: 'TEXT_MESSAGE_CONTENT', messageId: 'm1', delta: ' world' }, { sequence: 2 });

    const step = state.sessions['sess-a'].rounds[0].steps[0];
    expect(step.assistant_content).toBe('hello world');
    expect(state.runs['run-a'].lastSequence).toBe(2);
  });

  it('replaces buffers for aggregate snapshots after deltas', () => {
    let state = startRun();

    state = stream(state, { type: 'TEXT_MESSAGE_CONTENT', messageId: 'm1', delta: 'partial' }, { sequence: 1 });
    state = stream(
      state,
      { type: 'TEXT_MESSAGE_CONTENT', messageId: 'm1', delta: 'snapshot content' },
      { sequence: 2, isAggregate: true },
    );

    const step = state.sessions['sess-a'].rounds[0].steps[0];
    expect(step.assistant_content).toBe('snapshot content');
    expect(state.runs['run-a'].buffers.textByMessageId.m1).toBe('snapshot content');
  });

  it('keeps tool argument buffers scoped by tool call id', () => {
    let state = startRun();

    state = stream(state, { type: 'STEP_STARTED', stepName: 'step_1' }, { sequence: 1 });
    state = stream(state, { type: 'TOOL_CALL_START', toolCallId: 'tool-a', toolCallName: 'search' }, { sequence: 2 });
    state = stream(state, { type: 'TOOL_CALL_ARGS', toolCallId: 'tool-a', delta: '{"query":' }, { sequence: 3 });
    state = stream(state, { type: 'TOOL_CALL_START', toolCallId: 'tool-b', toolCallName: 'read' }, { sequence: 4 });
    state = stream(state, { type: 'TOOL_CALL_ARGS', toolCallId: 'tool-b', delta: '{"path":"a.md"}' }, { sequence: 5 });
    state = stream(state, { type: 'TOOL_CALL_ARGS', toolCallId: 'tool-a', delta: '"capy"}' }, { sequence: 6 });

    expect(state.runs['run-a'].buffers.toolArgsByToolCallId['tool-a']).toBe('{"query":"capy"}');
    expect(state.runs['run-a'].buffers.toolArgsByToolCallId['tool-b']).toBe('{"path":"a.md"}');
    const calls = state.sessions['sess-a'].rounds[0].steps[0].tool_calls;
    expect(calls.find((call) => call.id === 'tool-a')?.input).toEqual({ query: 'capy' });
    expect(calls.find((call) => call.id === 'tool-b')?.input).toEqual({ path: 'a.md' });
  });

  it('keeps terminal events idempotent after a run has finished', () => {
    let state = startRun();

    state = stream(state, {
      type: 'RUN_FINISHED',
      threadId: 'sess-a',
      runId: 'server-r1',
      result: { finalResponse: 'done' },
      outcome: 'success',
    }, { sequence: 1 });
    state = stream(state, {
      type: 'RUN_ERROR',
      message: 'late failure',
    }, { sequence: 2 });

    expect(state.runs['run-a'].status).toBe('finished');
    expect(state.sessions['sess-a'].rounds[0].status).toBe('completed');
    expect(state.sessions['sess-a'].rounds[0].final_response).toBe('done');
    expect(state.sessions['sess-a'].error).toBe('');
  });

  it('drops visible deltas after local cancel', () => {
    let state = startRun();

    state = stream(state, { type: 'TEXT_MESSAGE_CONTENT', messageId: 'm1', delta: 'before cancel' }, { sequence: 1 });
    state = chatRuntimeReducer(state, { type: 'LOCAL_CANCELLED', sessionId: 'sess-a' });
    state = stream(state, { type: 'TEXT_MESSAGE_CONTENT', messageId: 'm1', delta: ' late token' }, { sequence: 2 });

    const roundState = state.sessions['sess-a'].rounds[0];
    expect(roundState.status).toBe('cancelled');
    expect(roundState.steps[0].assistant_content).toBe('before cancel');
    expect(state.runs['run-a'].debugMetadata?.droppedAfterCancel).toBe(1);
  });

  it('dedupes a temp round when history finds the idempotency round', () => {
    let state = startRun();

    state = chatRuntimeReducer(state, {
      type: 'HISTORY_LOADED',
      sessionId: 'sess-a',
      rounds: [
        round({
          round_id: 'server-r1',
          idempotency_key: 'idem-a',
          final_response: 'server final',
          status: 'completed',
          completed_at: '2026-01-01T00:00:02.000Z',
          last_event_sequence: 10,
        }),
      ],
      loadedAt: Date.parse('2026-01-01T00:00:03.000Z'),
      source: 'history',
    });

    expect(state.sessions['sess-a'].rounds.map((item) => item.round_id)).toEqual(['server-r1']);
    expect(state.sessions['sess-a'].rounds[0].final_response).toBe('server final');
    expect(state.sessions['sess-a'].activeRunKeys).toEqual([]);
    expect(state.runs['run-a'].serverRunId).toBe('server-r1');
    expect(state.idempotencyKeyToClientRunKey['idem-a']).toBe('run-a');
  });

  it('preserves an optimistic direct round when history has not seen it yet', () => {
    let state = startRun();

    state = stream(state, { type: 'TEXT_MESSAGE_CONTENT', messageId: 'm1', delta: 'partial' }, { sequence: 1 });
    state = chatRuntimeReducer(state, {
      type: 'HISTORY_LOADED',
      sessionId: 'sess-a',
      rounds: [
        round({
          round_id: 'old-r0',
          final_response: 'old final',
          status: 'completed',
          completed_at: '2026-01-01T00:00:02.000Z',
        }),
      ],
      loadedAt: Date.parse('2026-01-01T00:00:03.000Z'),
      source: 'history',
    });

    expect(state.sessions['sess-a'].rounds.map((item) => item.round_id)).toEqual(['old-r0', 'temp-r1']);
    expect(state.sessions['sess-a'].rounds[1].steps[0].assistant_content).toBe('partial');
    expect(state.sessions['sess-a'].activeRunKeys).toEqual(['run-a']);
  });

  it('preserves structured approval control metadata while history is catching up', () => {
    let state = startRun(initialChatRuntimeState, { control_kind: 'tool_approval' });

    state = chatRuntimeReducer(state, {
      type: 'HISTORY_LOADED',
      sessionId: 'sess-a',
      rounds: [
        round({
          round_id: 'server-r1',
          idempotency_key: 'idem-a',
          status: 'running',
        }),
      ],
      loadedAt: Date.parse('2026-01-01T00:00:03.000Z'),
      source: 'history',
    });

    expect(state.sessions['sess-a'].rounds[0].control_kind).toBe('tool_approval');
  });

  it('uses the server preferred Skill snapshot while merging a running history round', () => {
    let state = startRun(initialChatRuntimeState, {
      preferred_skills: [{ key: 'pdf', display_name: 'pdf' }],
    });

    state = chatRuntimeReducer(state, {
      type: 'HISTORY_LOADED',
      sessionId: 'sess-a',
      rounds: [
        round({
          round_id: 'server-r1',
          idempotency_key: 'idem-a',
          status: 'running',
          preferred_skills: [{ key: 'pdf', display_name: 'PDF 处理' }],
        }),
      ],
      loadedAt: Date.parse('2026-01-01T00:00:03.000Z'),
      source: 'history',
    });

    expect(state.sessions['sess-a'].rounds[0].preferred_skills).toEqual([
      { key: 'pdf', display_name: 'PDF 处理' },
    ]);
  });

  it('keeps the optimistic preferred Skill snapshot until history provides one', () => {
    let state = startRun(initialChatRuntimeState, {
      preferred_skills: [{ key: 'pdf', display_name: 'pdf' }],
    });

    state = chatRuntimeReducer(state, {
      type: 'HISTORY_LOADED',
      sessionId: 'sess-a',
      rounds: [
        round({
          round_id: 'server-r1',
          idempotency_key: 'idem-a',
          status: 'running',
        }),
      ],
      loadedAt: Date.parse('2026-01-01T00:00:03.000Z'),
      source: 'history',
    });

    expect(state.sessions['sess-a'].rounds[0].preferred_skills).toEqual([
      { key: 'pdf', display_name: 'pdf' },
    ]);
  });

  it('keeps the running round lastSequence when a subscribe run is (re)started', () => {
    let state = chatRuntimeReducer(initialChatRuntimeState, {
      type: 'HISTORY_LOADED',
      sessionId: 'sess-a',
      rounds: [
        round({
          round_id: 'server-r1',
          status: 'running',
          last_event_sequence: 7,
        }),
      ],
      loadedAt: Date.parse('2026-01-01T00:00:03.000Z'),
      source: 'history',
    });

    expect(state.runs['run:server-r1'].lastSequence).toBe(7);

    // startSubscribeForRound re-dispatches LOCAL_RUN_STARTED because dispatch
    // does not update the ref synchronously; the run already exists.
    state = chatRuntimeReducer(state, {
      type: 'LOCAL_RUN_STARTED',
      sessionId: 'sess-a',
      clientRunKey: 'run:server-r1',
      tempRoundId: 'server-r1',
      source: 'subscribe',
    });

    expect(state.runs['run:server-r1'].lastSequence).toBe(7);
    expect(state.runs['run:server-r1'].serverRunId).toBe('server-r1');
  });

  it('retains locally started direct runs when a running snapshot is empty', () => {
    let state = startRun();

    state = chatRuntimeReducer(state, {
      type: 'RUNNING_SESSIONS_SNAPSHOT',
      runningSessions: [],
      receivedAt: Date.parse('2026-01-01T00:00:05.000Z'),
    });

    expect(state.sessions['sess-a'].activeRunKeys).toEqual(['run-a']);
  });

  it('clears snapshot-owned placeholder runs when a running snapshot is empty', () => {
    let state = chatRuntimeReducer(initialChatRuntimeState, {
      type: 'RUNNING_SESSIONS_SNAPSHOT',
      runningSessions: [{ session_id: 'sess-a', round_id: 'server-r1' }],
      receivedAt: Date.parse('2026-01-01T00:00:04.000Z'),
    });

    expect(state.sessions['sess-a'].activeRunKeys).toEqual(['run:server-r1']);
    expect(state.runs['run:server-r1'].source).toBe('history');

    state = chatRuntimeReducer(state, {
      type: 'RUNNING_SESSIONS_SNAPSHOT',
      runningSessions: [],
      receivedAt: Date.parse('2026-01-01T00:00:06.000Z'),
    });

    expect(state.sessions['sess-a'].activeRunKeys).toEqual([]);
  });
});
