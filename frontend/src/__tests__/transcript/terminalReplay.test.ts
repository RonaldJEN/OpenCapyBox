import { describe, expect, it } from 'vitest';
import fixtures from '../fixtures/transcript-legacy.json';
import { chatRuntimeReducer } from '../../runtime/chatRuntimeReducer';
import { initialChatRuntimeState } from '../../runtime/chatRuntimeTypes';
import { projectRoundTranscript } from '../../transcript/projectRoundTranscript';
import type { RoundData } from '../../types';

describe('legacy EventBus/history fixtures', () => {
  it.each(['A-normal', 'A-error'])('preserves the same reply copy across direct events and committed history (%s)', (id) => {
    const fixture = fixtures.cases.find((item) => item.id === id)!;
    const history = fixture.history as unknown as RoundData;
    let state = chatRuntimeReducer(initialChatRuntimeState, {
      type: 'LOCAL_RUN_STARTED', sessionId: 'fixture-session', clientRunKey: 'local-run', tempRoundId: 'temp',
      source: 'direct', round: { ...history, round_id: 'temp', steps: [], final_response: '', status: 'running', terminal_presentation: null },
    });
    for (const event of fixture.direct) {
      state = chatRuntimeReducer(state, { type: 'STREAM_EVENT', envelope: {
        ownerSessionId: 'fixture-session', clientRunKey: 'local-run', source: 'direct', transportEpoch: 1,
        connectionId: 'fixture-connection', receivedAt: 1, event, sequence: (event as { sequence?: number }).sequence,
      } });
    }
    const live = state.sessions['fixture-session'].rounds[0];
    expect(projectRoundTranscript(live).copyText).toBe(projectRoundTranscript(history).copyText);
    expect(projectRoundTranscript(live).error).toBe(projectRoundTranscript(history).error);
    if (id === 'A-error') expect(projectRoundTranscript(live).copyText).not.toContain('Provider unavailable');
    else expect(live.assistant_file_references?.map((file) => file.ref_id)).toEqual(history.assistant_file_references?.map((file) => file.ref_id));
  });

  it('keeps a local uncommitted tail through cancellation without claiming it exists in history', () => {
    const fixture = fixtures.cases.find((item) => item.id === 'F4')!;
    const history = fixture.history as unknown as RoundData;
    let state = chatRuntimeReducer(initialChatRuntimeState, { type: 'LOCAL_RUN_STARTED', sessionId: 's', clientRunKey: 'r', tempRoundId: 'temp', source: 'direct', round: { ...history, round_id: 'temp' } });
    for (const event of fixture.direct) {
      state = chatRuntimeReducer(state, { type: 'STREAM_EVENT', envelope: { ownerSessionId: 's', clientRunKey: 'r', source: 'direct', transportEpoch: 1, connectionId: 'c', receivedAt: 1, event, sequence: (event as { sequence?: number }).sequence } });
    }
    state = chatRuntimeReducer(state, { type: 'LOCAL_CANCELLED', sessionId: 's' });
    expect(projectRoundTranscript(state.sessions.s.rounds[0]).nodes.map(node => node.text)).toEqual(['尚未结束的正文尾段']);
    expect(projectRoundTranscript(state.sessions.s.rounds[0]).copyText).toBe('');
    expect(projectRoundTranscript(history).copyText).toBe('');
  });
});
