import { describe, expect, it } from 'vitest';
import fixtures from '../fixtures/transcript-legacy.json';
import { chatRuntimeReducer } from '../../runtime/chatRuntimeReducer';
import { initialChatRuntimeState } from '../../runtime/chatRuntimeTypes';
import { projectRoundTranscript } from '../../transcript/projectRoundTranscript';
import { preserveUncommittedMessages } from '../../transcript/mergeMessageSnapshot';
import type { RoundData } from '../../types';

function replay(id: string, source: 'direct' | 'durable') {
  const sample = fixtures.cases.find((item) => item.id === id)!;
  const round = { ...sample.history, round_id: 'temp', status: 'running', final_response: '', steps: [] } as unknown as RoundData;
  let state = chatRuntimeReducer(initialChatRuntimeState, { type: 'LOCAL_RUN_STARTED', sessionId: 's', clientRunKey: 'r', tempRoundId: 'temp', source: 'direct', round });
  for (const event of sample[source]) {
    state = chatRuntimeReducer(state, { type: 'STREAM_EVENT', envelope: { ownerSessionId: 's', clientRunKey: 'r', connectionId: 'c', transportEpoch: 1, receivedAt: 0,
      source: source === 'direct' ? 'direct' : 'subscribe', event,
      sequence: (event as { sequence?: number }).sequence,
      isAggregate: source === 'durable' && event.type === 'TEXT_MESSAGE_CONTENT',
    } });
  }
  return state.sessions.s.rounds[0];
}

describe('message replay', () => {
  it('preserves native phases, interrupted corrections and a multi-message final alias', () => {
    const round: RoundData = { round_id: 'temp', user_message: 'question', status: 'running', final_response: '', steps: [], step_count: 0, created_at: '' };
    let state = chatRuntimeReducer(initialChatRuntimeState, { type: 'LOCAL_RUN_STARTED', sessionId: 's', clientRunKey: 'r', tempRoundId: 'temp', source: 'direct', round });
    const events = [
      { type: 'RUN_STARTED', threadId: 's', runId: 'native' },
      { type: 'STEP_STARTED', stepName: 'step_1' },
      { type: 'TEXT_MESSAGE_START', messageId: 'old', role: 'assistant', phase: 'final_answer' },
      { type: 'TEXT_MESSAGE_CONTENT', messageId: 'old', delta: '失败尝试' },
      { type: 'TEXT_MESSAGE_END', messageId: 'old', interrupted: true },
      { type: 'TEXT_MESSAGE_START', messageId: 'answer-1', role: 'assistant', phase: 'final_answer' },
      { type: 'TEXT_MESSAGE_CONTENT', messageId: 'answer-1', delta: '正文一' },
      { type: 'TEXT_MESSAGE_END', messageId: 'answer-1' },
      { type: 'TEXT_MESSAGE_START', messageId: 'answer-2', role: 'assistant' },
      { type: 'TEXT_MESSAGE_CONTENT', messageId: 'answer-2', delta: '正文二' },
      { type: 'TEXT_MESSAGE_END', messageId: 'answer-2', phase: 'final_answer' },
      { type: 'RUN_FINISHED', outcome: 'success', finalMessageIds: ['answer-1', 'answer-2'], result: { finalResponse: '正文一\n\n正文二' } },
    ];
    events.forEach((event, index) => {
      state = chatRuntimeReducer(state, { type: 'STREAM_EVENT', envelope: {
        ownerSessionId: 's', clientRunKey: 'r', connectionId: 'c', transportEpoch: 1, receivedAt: 0, source: 'direct', event, sequence: index + 1, isAggregate: false,
      } });
    });
    const result = state.sessions.s.rounds[0];
    expect(result.assistant_messages?.map(message => [message.phase, message.state])).toEqual([
      ['final_answer', 'interrupted'], ['final_answer', 'complete'], ['final_answer', 'complete'],
    ]);
    expect(result.final_message_ids).toEqual(['answer-1', 'answer-2']);
    expect(projectRoundTranscript(result).nodes.map(node => node.text)).toEqual(['失败尝试', '正文一', '正文二']);
    expect(projectRoundTranscript(result).copyText).toBe('正文一\n\n正文二');
  });

  it.each(['F1', 'F2', 'F3'])('direct and durable replay preserve independent messages (%s)', (id) => {
    const sample = fixtures.cases.find((item) => item.id === id)!;
    for (const source of ['direct', 'durable'] as const) {
      const result = replay(id, source);
      expect(result.assistant_messages?.map((message) => ({ messageId: message.message_id, text: message.content, state: message.state, committed: message.content_committed }))).toEqual(sample.expected_b1);
      expect(projectRoundTranscript(result).nodes.map((node) => node.text)).toEqual(sample.expected_b1.map((message) => message.text));
    }
  });
  it('does not erase an uncommitted prefix using a later unrelated watermark', () => {
    const local = replay('F5', 'direct');
    const server = { ...local, status: 'failed', last_event_sequence: 100,
      assistant_messages: local.assistant_messages!.map((message) => ({ ...message, content: '', content_committed: true })),
    };
    const merged = preserveUncommittedMessages(local, server);
    expect(projectRoundTranscript(merged).nodes.map(node => node.text)).toEqual(['尚未结束的正文尾段']);
    expect(projectRoundTranscript(merged).copyText).toBe('');
    expect(merged.assistant_messages![0]).toMatchObject({ content_committed: false, state: 'interrupted' });
  });
});
