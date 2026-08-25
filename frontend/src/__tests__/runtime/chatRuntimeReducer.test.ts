import { describe, expect, it } from 'vitest';

import { chatRuntimeReducer } from '../../runtime/chatRuntimeReducer';
import {
  ChatRuntimeState,
  RUNTIME_HISTORY_SNAPSHOT,
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

  it('reconciles a matching aggregate at the same cursor without accepting a shorter prefix', () => {
    let state = startRun();

    state = stream(state, {
      type: 'RUN_STARTED',
      threadId: 'sess-a',
      runId: 'server-r1',
    }, { sequence: 1 });
    state = stream(state, {
      type: 'TEXT_MESSAGE_START',
      messageId: 'm1',
      role: 'assistant',
    }, { sequence: 2 });
    state = stream(state, { type: 'TEXT_MESSAGE_CONTENT', messageId: 'm1', delta: 'partial' });
    state = stream(
      state,
      { type: 'TEXT_MESSAGE_CONTENT', messageId: 'm1', delta: 'part' },
      { sequence: 2, isAggregate: true },
    );

    expect(state.runs['run-a'].buffers.textByMessageId.m1).toBe('partial');

    state = stream(
      state,
      { type: 'TEXT_MESSAGE_CONTENT', messageId: 'm1', delta: 'partial content' },
      { sequence: 2, isAggregate: true },
    );

    const step = state.sessions['sess-a'].rounds[0].steps[0];
    expect(step.assistant_content).toBe('partial content');
    expect(state.runs['run-a'].buffers.textByMessageId.m1).toBe('partial content');
    expect(state.runs['run-a'].buffers.textSegmentStateByMessageId.m1).toEqual({
      open: true,
      dirty: false,
    });
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

  it('projects replayed tool lifecycle events once by tool call id', () => {
    let state = startRun();

    state = stream(state, { type: 'STEP_STARTED', stepName: 'step_1' }, { sequence: 1 });
    state = stream(state, {
      type: 'TOOL_CALL_START',
      toolCallId: 'tool-a',
      toolCallName: 'mcp_tool_search',
      timestamp: 1000,
    }, { sequence: 2 });
    state = stream(
      state,
      { type: 'TOOL_CALL_ARGS', toolCallId: 'tool-a', delta: '{"query":"capy"}' },
      { sequence: 3, isAggregate: true },
    );
    state = stream(state, {
      type: 'TOOL_CALL_END',
      toolCallId: 'tool-a',
      timestamp: 1500,
    }, { sequence: 4 });
    state = stream(state, {
      type: 'TOOL_CALL_RESULT',
      toolCallId: 'tool-a',
      content: '{"output":"first result"}',
      timestamp: 2000,
    }, { sequence: 5 });

    state = stream(state, { type: 'STEP_STARTED', stepName: 'step_2' }, { sequence: 6 });
    state = stream(state, {
      type: 'TOOL_CALL_START',
      toolCallId: 'tool-a',
      toolCallName: 'mcp_tool_search',
      timestamp: 1000,
    }, { sequence: 7 });
    state = stream(
      state,
      { type: 'TOOL_CALL_ARGS', toolCallId: 'tool-a', delta: '{"query":"capy"}' },
      { sequence: 8, isAggregate: true },
    );
    state = stream(state, {
      type: 'TOOL_CALL_END',
      toolCallId: 'tool-a',
      timestamp: 1500,
    }, { sequence: 9 });
    state = stream(state, {
      type: 'TOOL_CALL_RESULT',
      toolCallId: 'tool-a',
      content: '{"output":"first result"}',
      timestamp: 2000,
    }, { sequence: 10 });

    const steps = state.sessions['sess-a'].rounds[0].steps;
    expect(steps[0].tool_calls).toEqual([{
      id: 'tool-a',
      name: 'mcp_tool_search',
      input: { query: 'capy' },
      started_at_ts: 1000,
      ended_at_ts: 1500,
    }]);
    expect(steps[0].tool_results).toHaveLength(1);
    expect(steps[0].tool_results[0]).toMatchObject({
      tool_call_id: 'tool-a',
      success: true,
      content: 'first result',
    });
    expect(steps[1].tool_calls).toEqual([]);
    expect(steps[1].tool_results).toEqual([]);
    expect(state.runs['run-a'].buffers.toolArgsByToolCallId['tool-a']).toBe('{"query":"capy"}');
    expect(state.sessions['sess-a'].agentStateByRunKey['run-a'].toolLogs).toHaveLength(1);
    expect(state.sessions['sess-a'].agentStateByRunKey['run-a'].toolLogs[0].status).toBe('completed');
  });

  it('keeps distinct same-name tool calls as separate entities', () => {
    let state = startRun();

    state = stream(state, { type: 'STEP_STARTED', stepName: 'step_1' }, { sequence: 1 });
    state = stream(state, {
      type: 'TOOL_CALL_START',
      toolCallId: 'tool-a',
      toolCallName: 'mcp_tool_search',
    }, { sequence: 2 });
    state = stream(state, {
      type: 'TOOL_CALL_START',
      toolCallId: 'tool-b',
      toolCallName: 'mcp_tool_search',
    }, { sequence: 3 });

    const calls = state.sessions['sess-a'].rounds[0].steps[0].tool_calls;
    expect(calls.map((call) => call.id)).toEqual(['tool-a', 'tool-b']);
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

  it('keeps cancelled terminal state when completed and failed events arrive late', () => {
    let state = startRun();

    state = stream(state, { type: 'TEXT_MESSAGE_CONTENT', messageId: 'm1', delta: 'before cancel' }, { sequence: 1 });
    state = chatRuntimeReducer(state, { type: 'LOCAL_CANCELLED', sessionId: 'sess-a' });
    state = stream(state, {
      type: 'RUN_FINISHED',
      threadId: 'sess-a',
      runId: 'server-r1',
      result: { finalResponse: 'late completed response' },
      outcome: 'success',
    }, { sequence: 2 });
    state = stream(state, {
      type: 'RUN_ERROR',
      message: 'late failure',
    }, { sequence: 3 });

    const roundState = state.sessions['sess-a'].rounds[0];
    expect(state.runs['run-a'].status).toBe('cancelled');
    expect(roundState.status).toBe('cancelled');
    expect(roundState.final_response).toBe('');
    expect(roundState.steps[0].assistant_content).toBe('before cancel');
    expect(state.sessions['sess-a'].error).toBe('');
    expect(state.runs['run-a'].debugMetadata?.droppedAfterTerminal).toBe(2);
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

  it('reuses the direct run identity when the running-session snapshot sees its server round', () => {
    let state = startRun();

    state = stream(state, {
      type: 'RUN_STARTED',
      threadId: 'sess-a',
      runId: 'server-r1',
    }, { sequence: 1 });
    state = chatRuntimeReducer(state, {
      type: 'RUNNING_SESSIONS_SNAPSHOT',
      runningSessions: [{ session_id: 'sess-a', round_id: 'server-r1' }],
      receivedAt: Date.parse('2026-01-01T00:00:04.000Z'),
    });

    expect(state.sessions['sess-a'].activeRunKeys).toEqual(['run-a']);
    expect(state.runs['run:server-r1']).toBeUndefined();
    expect(state.serverRunIdToClientRunKey['server-r1']).toBe('run-a');
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

  it('returns the same state for an unchanged running-session snapshot', () => {
    const state = chatRuntimeReducer(initialChatRuntimeState, {
      type: 'RUNNING_SESSIONS_SNAPSHOT',
      runningSessions: [{ session_id: 'sess-a', round_id: 'server-r1' }],
      receivedAt: Date.parse('2026-01-01T00:00:04.000Z'),
    });

    const unchanged = chatRuntimeReducer(state, {
      type: 'RUNNING_SESSIONS_SNAPSHOT',
      runningSessions: [{ session_id: 'sess-a', round_id: 'server-r1' }],
      receivedAt: Date.parse('2026-01-01T00:00:09.000Z'),
    });

    expect(unchanged).toBe(state);
  });

  it('parks and resumes the same run for interaction events', () => {
    let state = startRun();
    state = stream(state, {
      type: 'RUN_STARTED',
      threadId: 'sess-a',
      runId: 'server-r1',
    });
    state = stream(state, {
      type: 'CUSTOM',
      name: 'interaction_requested',
      value: {
        interactionId: 'interaction-1',
        runId: 'server-r1',
        kind: 'user_input',
        toolCallId: 'tool-1',
        payload: { questions: [{ question: 'Continue?' }] },
      },
    });

    expect(state.runs['run-a'].status).toBe('waiting');
    expect(state.sessions['sess-a'].rounds).toHaveLength(1);
    expect(state.sessions['sess-a'].rounds[0].round_id).toBe('server-r1');
    expect(state.sessions['sess-a'].rounds[0].status).toBe('waiting_interaction');
    expect(state.sessions['sess-a'].pendingInterrupt?.id).toBe('interaction-1');
    expect(state.sessions['sess-a'].activeRunKeys).toEqual([]);

    state = stream(state, {
      type: 'CUSTOM',
      name: 'interaction_resolved',
      value: {
        interactionId: 'interaction-1',
        runId: 'server-r1',
        toolCallId: 'tool-1',
        toolResultContent: 'Continue?: Yes',
        resolution: 'answered',
      },
    }, { source: 'resume' });

    expect(state.runs['run-a'].status).toBe('streaming');
    expect(state.sessions['sess-a'].rounds).toHaveLength(1);
    expect(state.sessions['sess-a'].rounds[0].status).toBe('running');
    expect(state.sessions['sess-a'].pendingInterrupt).toBeNull();
    expect(state.sessions['sess-a'].activeRunKeys).toEqual(['run-a']);
  });

  it('ignores unsequenced RUN_ERROR after durable interaction boundaries', () => {
    let state = startRun();
    state = stream(state, {
      type: 'RUN_STARTED',
      threadId: 'sess-a',
      runId: 'server-r1',
    }, { sequence: 1 });
    state = stream(state, {
      type: 'CUSTOM',
      name: 'interaction_requested',
      value: {
        interactionId: 'interaction-1',
        runId: 'server-r1',
        kind: 'user_input',
        payload: { questions: [{ question: 'Continue?' }] },
      },
    }, { sequence: 2 });

    state = stream(state, {
      type: 'RUN_ERROR',
      runId: 'server-r1',
      message: 'adapter failed after waiting',
      code: 'INTERNAL_ERROR',
    });

    expect(state.runs['run-a']).toMatchObject({
      status: 'waiting',
      lastInteractionSequence: 2,
      debugMetadata: { droppedUnsequencedRunErrors: 1 },
    });
    expect(state.sessions['sess-a'].rounds[0].status).toBe('waiting_interaction');
    expect(state.sessions['sess-a'].pendingInterrupt?.id).toBe('interaction-1');
    expect(state.sessions['sess-a'].error).toBe('');

    state = stream(state, {
      type: 'CUSTOM',
      name: 'interaction_resolved',
      value: { interactionId: 'interaction-1', runId: 'server-r1' },
    }, { source: 'resume', sequence: 3 });
    state = stream(state, {
      type: 'RUN_ERROR',
      runId: 'server-r1',
      message: 'continuation setup failed',
      code: 'INTERNAL_ERROR',
    }, { source: 'resume' });

    expect(state.runs['run-a']).toMatchObject({
      status: 'streaming',
      lastInteractionSequence: 3,
      debugMetadata: { droppedUnsequencedRunErrors: 2 },
    });
    expect(state.sessions['sess-a'].rounds[0].status).toBe('running');
    expect(state.sessions['sess-a'].activeRunKeys).toEqual(['run-a']);
    expect(state.sessions['sess-a'].error).toBe('');

    state = stream(state, {
      type: 'RUN_ERROR',
      runId: 'server-r1',
      message: 'durable failure',
      code: 'RUN_FAILED',
      sequence: 4,
    }, { source: 'subscribe', sequence: 4, authoritativeRecovery: true });

    expect(state.runs['run-a'].status).toBe('error');
    expect(state.sessions['sess-a'].rounds[0].status).toBe('failed');
    expect(state.sessions['sess-a'].error).toBe('durable failure');
  });

  it('projects complete server steps before advancing a recovered history cursor', () => {
    let state = startRun();
    state = stream(state, {
      type: 'RUN_STARTED',
      threadId: 'sess-a',
      runId: 'server-r1',
    }, { sequence: 1 });
    state = stream(state, {
      type: 'TEXT_MESSAGE_CONTENT',
      messageId: 'm1',
      delta: 'local partial',
    }, { sequence: 2 });

    const persistedSteps = [{
      step_number: 1,
      thinking: 'persisted thinking',
      assistant_content: 'persisted complete text',
      tool_calls: [{ id: 'tool-1', name: 'search', input: { q: 'capy' } }],
      tool_results: [{
        tool_call_id: 'tool-1',
        success: true,
        content: 'persisted result',
      }],
      status: 'completed',
    }];
    state = stream(state, {
      type: RUNTIME_HISTORY_SNAPSHOT,
      rounds: [round({
        round_id: 'server-r1',
        idempotency_key: 'idem-a',
        status: 'waiting_interaction',
        steps: persistedSteps,
        step_count: 1,
        last_event_sequence: 7,
        interrupt: {
          id: 'interaction-1',
          reason: 'input_required',
          payload: { questions: [{ question: 'Continue?' }] },
        },
      })],
      sequence: 7,
    }, {
      source: 'subscribe',
      sequence: 7,
      authoritativeRecovery: true,
    });

    expect(state.runs['run-a']).toMatchObject({
      lastSequence: 7,
      status: 'waiting',
      buffers: {
        textByMessageId: {},
        thinkingByMessageId: {},
        toolArgsByToolCallId: {},
      },
    });
    expect(state.sessions['sess-a'].rounds[0].steps).toEqual(persistedSteps);
    expect(state.sessions['sess-a'].pendingInterrupt?.id).toBe('interaction-1');
  });

  it('preserves an unmaterialized text prefix across equal-cursor history', () => {
    let state = startRun();
    state = stream(state, {
      type: 'RUN_STARTED',
      threadId: 'sess-a',
      runId: 'server-r1',
    }, { sequence: 1 });
    state = stream(state, {
      type: 'TEXT_MESSAGE_START',
      messageId: 'm1',
      role: 'assistant',
    }, { sequence: 2 });
    state = stream(state, {
      type: 'TEXT_MESSAGE_CONTENT',
      messageId: 'm1',
      delta: 'hel',
    });

    state = chatRuntimeReducer(state, {
      type: 'HISTORY_LOADED',
      sessionId: 'sess-a',
      rounds: [round({
        round_id: 'server-r1',
        idempotency_key: 'idem-a',
        status: 'running',
        last_event_sequence: 2,
      })],
      loadedAt: Date.parse('2026-01-01T00:00:03.000Z'),
      source: 'history',
    });
    state = stream(state, {
      type: 'TEXT_MESSAGE_CONTENT',
      messageId: 'm1',
      delta: 'lo',
    });

    expect(state.sessions['sess-a'].rounds[0].steps[0].assistant_content).toBe('hello');
    expect(state.runs['run-a'].buffers.textByMessageId.m1).toBe('hello');
  });

  it('preserves unmaterialized thinking across equal-cursor history', () => {
    let state = startRun();
    state = stream(state, {
      type: 'RUN_STARTED',
      threadId: 'sess-a',
      runId: 'server-r1',
    }, { sequence: 1 });
    state = stream(state, { type: 'STEP_STARTED', stepName: 'step_1' }, { sequence: 2 });
    state = stream(state, {
      type: 'THINKING_TEXT_MESSAGE_START',
      messageId: 'thinking-1',
    }, { sequence: 3 });
    state = stream(state, {
      type: 'THINKING_TEXT_MESSAGE_CONTENT',
      messageId: 'thinking-1',
      delta: 'ana',
    });

    state = chatRuntimeReducer(state, {
      type: 'HISTORY_LOADED',
      sessionId: 'sess-a',
      rounds: [round({
        round_id: 'server-r1',
        idempotency_key: 'idem-a',
        status: 'running',
        last_event_sequence: 3,
      })],
      loadedAt: Date.parse('2026-01-01T00:00:03.000Z'),
      source: 'history',
    });
    state = stream(state, {
      type: 'THINKING_TEXT_MESSAGE_CONTENT',
      messageId: 'thinking-1',
      delta: 'lysis',
    });

    expect(state.sessions['sess-a'].rounds[0].steps[0].thinking).toBe('analysis');
    expect(state.runs['run-a'].buffers.thinkingByMessageId['thinking-1']).toBe('analysis');
    expect(state.runs['run-a'].lastSequence).toBe(3);
  });

  it('preserves a dirty thinking prefix when an unrelated durable event advances history', () => {
    let state = startRun();
    state = stream(state, {
      type: 'RUN_STARTED',
      threadId: 'sess-a',
      runId: 'server-r1',
    }, { sequence: 1 });
    state = stream(state, { type: 'STEP_STARTED', stepName: 'step_1' }, { sequence: 2 });
    state = stream(state, {
      type: 'THINKING_TEXT_MESSAGE_START',
      messageId: 'thinking-1',
    }, { sequence: 3 });
    state = stream(state, {
      type: 'THINKING_TEXT_MESSAGE_CONTENT',
      messageId: 'thinking-1',
      delta: 'ana',
    });

    state = chatRuntimeReducer(state, {
      type: 'HISTORY_LOADED',
      sessionId: 'sess-a',
      rounds: [round({
        round_id: 'server-r1',
        idempotency_key: 'idem-a',
        status: 'running',
        steps: [
          {
            step_number: 1,
            thinking: 'analysis from a prior step',
            assistant_content: '',
            tool_calls: [],
            tool_results: [],
            status: 'completed',
          },
          {
            step_number: 2,
            thinking: '',
            assistant_content: '',
            tool_calls: [],
            tool_results: [],
            status: 'running',
          },
        ],
        last_event_sequence: 4,
      })],
      loadedAt: Date.parse('2026-01-01T00:00:03.000Z'),
      source: 'history',
    });
    state = stream(state, {
      type: 'THINKING_TEXT_MESSAGE_CONTENT',
      messageId: 'thinking-1',
      delta: 'lysis',
    });

    expect(state.sessions['sess-a'].rounds[0].steps[0].thinking).toBe('analysis');
    expect(state.runs['run-a'].buffers.thinkingByMessageId['thinking-1']).toBe('analysis');
    expect(state.runs['run-a'].lastSequence).toBe(3);
  });

  it('accepts higher-cursor history only when it materializes the dirty prefix', () => {
    let state = startRun();
    state = stream(state, {
      type: 'RUN_STARTED',
      threadId: 'sess-a',
      runId: 'server-r1',
    }, { sequence: 1 });
    state = stream(state, { type: 'STEP_STARTED', stepName: 'step_1' }, { sequence: 2 });
    state = stream(state, {
      type: 'THINKING_TEXT_MESSAGE_START',
      messageId: 'thinking-1',
    }, { sequence: 3 });
    state = stream(state, {
      type: 'THINKING_TEXT_MESSAGE_CONTENT',
      messageId: 'thinking-1',
      delta: 'ana',
    });

    state = chatRuntimeReducer(state, {
      type: 'HISTORY_LOADED',
      sessionId: 'sess-a',
      rounds: [round({
        round_id: 'server-r1',
        idempotency_key: 'idem-a',
        status: 'running',
        steps: [{
          step_number: 1,
          thinking: 'analysis complete',
          assistant_content: '',
          tool_calls: [],
          tool_results: [],
          status: 'completed',
        }],
        last_event_sequence: 5,
      })],
      loadedAt: Date.parse('2026-01-01T00:00:03.000Z'),
      source: 'history',
    });

    expect(state.sessions['sess-a'].rounds[0].steps[0].thinking).toBe('analysis complete');
    expect(state.runs['run-a'].buffers.thinkingByMessageId).toEqual({});
    expect(state.runs['run-a'].lastSequence).toBe(5);
  });

  it('preserves unmaterialized tool arguments across equal-cursor history', () => {
    let state = startRun();
    state = stream(state, {
      type: 'RUN_STARTED',
      threadId: 'sess-a',
      runId: 'server-r1',
    }, { sequence: 1 });
    state = stream(state, { type: 'STEP_STARTED', stepName: 'step_1' }, { sequence: 2 });
    state = stream(state, {
      type: 'TOOL_CALL_START',
      toolCallId: 'tool-1',
      toolCallName: 'search',
    }, { sequence: 3 });
    state = stream(state, {
      type: 'TOOL_CALL_ARGS',
      toolCallId: 'tool-1',
      delta: '{"q":"cap',
    });

    state = chatRuntimeReducer(state, {
      type: 'HISTORY_LOADED',
      sessionId: 'sess-a',
      rounds: [round({
        round_id: 'server-r1',
        idempotency_key: 'idem-a',
        status: 'running',
        last_event_sequence: 3,
      })],
      loadedAt: Date.parse('2026-01-01T00:00:03.000Z'),
      source: 'history',
    });
    state = stream(state, {
      type: 'TOOL_CALL_ARGS',
      toolCallId: 'tool-1',
      delta: 'y"}',
    });

    expect(state.runs['run-a'].buffers.toolArgsByToolCallId['tool-1']).toBe('{"q":"capy"}');
    expect(state.sessions['sess-a'].rounds[0].steps[0].tool_calls[0].input).toEqual({ q: 'capy' });
  });

  it('projects higher-cursor history after closed dirty text and thinking before applying tool result', () => {
    let state = startRun();
    state = stream(state, {
      type: 'RUN_STARTED',
      threadId: 'sess-a',
      runId: 'server-r1',
    }, { sequence: 1 });
    state = stream(state, { type: 'STEP_STARTED', stepName: 'step_1' }, { sequence: 2 });
    state = stream(state, {
      type: 'THINKING_TEXT_MESSAGE_START',
      messageId: 'thinking-1',
    }, { sequence: 3 });
    state = stream(state, {
      type: 'THINKING_TEXT_MESSAGE_CONTENT',
      messageId: 'thinking-1',
      delta: 'analysis',
    });
    state = stream(state, {
      type: 'THINKING_TEXT_MESSAGE_END',
      messageId: 'thinking-1',
    }, { sequence: 5 });
    state = stream(state, {
      type: 'TEXT_MESSAGE_START',
      messageId: 'message-1',
      role: 'assistant',
    }, { sequence: 6 });
    state = stream(state, {
      type: 'TEXT_MESSAGE_CONTENT',
      messageId: 'message-1',
      delta: 'hello',
    });
    state = stream(state, {
      type: 'TEXT_MESSAGE_END',
      messageId: 'message-1',
    }, { sequence: 8 });

    state = chatRuntimeReducer(state, {
      type: 'HISTORY_LOADED',
      sessionId: 'sess-a',
      rounds: [round({
        round_id: 'server-r1',
        idempotency_key: 'idem-a',
        status: 'running',
        steps: [{
          step_number: 1,
          thinking: 'analysis',
          assistant_content: 'hello',
          tool_calls: [{ id: 'tool-1', name: 'search', input: { q: 'capy' } }],
          tool_results: [],
          status: 'running',
        }],
        step_count: 1,
        last_event_sequence: 11,
      })],
      loadedAt: Date.parse('2026-01-01T00:00:09.000Z'),
      source: 'history',
    });

    expect(state.runs['run-a'].lastSequence).toBe(11);
    expect(state.runs['run-a'].buffers.textByMessageId).toEqual({});
    expect(state.runs['run-a'].buffers.thinkingByMessageId).toEqual({});
    expect(state.sessions['sess-a'].rounds[0].steps[0].tool_calls[0]).toMatchObject({
      id: 'tool-1',
      input: { q: 'capy' },
    });

    state = stream(state, {
      type: 'TOOL_CALL_RESULT',
      toolCallId: 'tool-1',
      content: '{"output":"found"}',
    }, { sequence: 12 });

    expect(state.sessions['sess-a'].rounds[0].steps[0].tool_results).toEqual([
      expect.objectContaining({
        tool_call_id: 'tool-1',
        content: 'found',
      }),
    ]);
  });

  it('restores a waiting interaction from history without marking it active', () => {
    const interrupt = {
      id: 'interaction-1',
      reason: 'input_required',
      payload: { questions: [{ question: 'Continue?' }], run_id: 'server-r1' },
    };
    const state = chatRuntimeReducer(initialChatRuntimeState, {
      type: 'HISTORY_LOADED',
      sessionId: 'sess-a',
      rounds: [round({
        round_id: 'server-r1',
        status: 'waiting_interaction',
        interrupt,
      })],
      loadedAt: Date.parse('2026-01-01T00:00:09.000Z'),
      source: 'history',
    });

    expect(state.sessions['sess-a'].pendingInterrupt?.id).toBe('interaction-1');
    expect(state.sessions['sess-a'].activeRunKeys).toEqual([]);
    expect(state.runs['run:server-r1'].status).toBe('waiting');
  });

  it('keeps round, run and active keys coherent when history parks a running round', () => {
    const interrupt = {
      id: 'interaction-1',
      reason: 'input_required',
      payload: { questions: [{ question: 'Continue?' }], run_id: 'server-r1' },
    };
    let state = startRun();
    state = stream(state, {
      type: 'RUN_STARTED',
      threadId: 'sess-a',
      runId: 'server-r1',
    }, { sequence: 1 });

    state = chatRuntimeReducer(state, {
      type: 'HISTORY_LOADED',
      sessionId: 'sess-a',
      rounds: [round({
        round_id: 'server-r1',
        status: 'waiting_interaction',
        interrupt,
        last_event_sequence: 1,
      })],
      loadedAt: Date.parse('2026-01-01T00:00:09.000Z'),
      source: 'history',
    });

    expect(state.sessions['sess-a'].rounds[0].status).toBe('waiting_interaction');
    expect(state.runs['run-a'].status).toBe('waiting');
    expect(state.sessions['sess-a'].activeRunKeys).toEqual([]);
    expect(state.sessions['sess-a'].pendingInterrupt?.id).toBe('interaction-1');
  });

  it('clears a stale pending interaction when terminal history replaces the waiting round', () => {
    const interrupt = {
      id: 'interaction-1',
      reason: 'input_required',
      payload: { questions: [{ question: 'Continue?' }] },
    };
    let state = chatRuntimeReducer(initialChatRuntimeState, {
      type: 'HISTORY_LOADED',
      sessionId: 'sess-a',
      rounds: [round({
        round_id: 'server-r1',
        status: 'waiting_interaction',
        interrupt,
      })],
      loadedAt: Date.parse('2026-01-01T00:00:01.000Z'),
      source: 'history',
    });

    state = chatRuntimeReducer(state, {
      type: 'HISTORY_LOADED',
      sessionId: 'sess-a',
      rounds: [round({
        round_id: 'server-r1',
        status: 'completed',
        interrupt: undefined,
        final_response: 'done',
        completed_at: '2026-01-01T00:00:03.000Z',
      })],
      loadedAt: Date.parse('2026-01-01T00:00:04.000Z'),
      source: 'history',
    });

    expect(state.sessions['sess-a'].pendingInterrupt).toBeNull();
    expect(state.sessions['sess-a'].rounds[0].status).toBe('completed');
    expect(state.runs['run:server-r1'].status).toBe('finished');
  });

  it('accepts an authoritative recovered interaction at the current sequence', () => {
    let state = startRun();
    state = stream(state, {
      type: 'RUN_STARTED',
      threadId: 'sess-a',
      runId: 'server-r1',
    });
    state = stream(state, { type: 'CUSTOM', name: 'heartbeat', value: {} }, { sequence: 7 });

    state = stream(state, {
      type: 'CUSTOM',
      name: 'interaction_requested',
      value: {
        interactionId: 'interaction-reparked',
        runId: 'server-r1',
        kind: 'user_input',
        payload: { questions: [{ question: 'Try again?' }] },
      },
    }, {
      source: 'subscribe',
      sequence: 7,
      authoritativeRecovery: true,
    });

    expect(state.runs['run-a'].lastSequence).toBe(7);
    expect(state.runs['run-a'].status).toBe('waiting');
    expect(state.sessions['sess-a'].pendingInterrupt?.id).toBe('interaction-reparked');
    expect(state.sessions['sess-a'].activeRunKeys).toEqual([]);
  });

  it('accepts an authoritative recovered terminal at the current sequence', () => {
    let state = startRun();
    state = stream(state, {
      type: 'RUN_STARTED',
      threadId: 'sess-a',
      runId: 'server-r1',
    });
    state = stream(state, { type: 'CUSTOM', name: 'heartbeat', value: {} }, { sequence: 9 });

    state = stream(state, {
      type: 'RUN_FINISHED',
      threadId: 'sess-a',
      runId: 'server-r1',
      outcome: 'success',
      result: { finalResponse: 'done' },
    }, {
      source: 'subscribe',
      sequence: 9,
      authoritativeRecovery: true,
    });

    expect(state.runs['run-a'].lastSequence).toBe(9);
    expect(state.runs['run-a'].status).toBe('finished');
    expect(state.sessions['sess-a'].rounds[0].status).toBe('completed');
    expect(state.sessions['sess-a'].activeRunKeys).toEqual([]);
  });
});
