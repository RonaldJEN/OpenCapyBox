import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  startSendStream,
  startSubscribeStream,
} from '../../services/chatStreamClient';
import { apiService } from '../../services/api';
import type { StreamEnvelope } from '../../runtime/chatRuntimeTypes';

vi.mock('../../services/api', () => ({
  apiService: {
    getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer token-1' })),
    getSessionHistoryV2: vi.fn(),
  },
}));

function responseWithChunks(chunks: string[]) {
  const encoder = new TextEncoder();
  const reader = {
    read: vi.fn()
      .mockImplementationOnce(async () => ({ done: false, value: encoder.encode(chunks.join('')) }))
      .mockImplementationOnce(async () => ({ done: true })),
  };
  return {
    ok: true,
    body: {
      getReader: () => reader,
    },
  };
}

function responseWithChunkThenError(chunk: string, error: Error) {
  const encoder = new TextEncoder();
  const reader = {
    read: vi.fn()
      .mockResolvedValueOnce({ done: false, value: encoder.encode(chunk) })
      .mockRejectedValueOnce(error),
  };
  return {
    ok: true,
    body: {
      getReader: () => reader,
    },
  };
}

function identity(overrides: Record<string, any> = {}) {
  return {
    ownerSessionId: 'sess-a',
    clientRunKey: 'run-a',
    transportEpoch: 1,
    connectionId: 'conn-a',
    source: 'direct' as const,
    ...overrides,
  };
}

describe('chatStreamClient', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('POST stream parses SSE into owner/run scoped envelopes', async () => {
    const envelopes: StreamEnvelope[] = [];
    const fetchMock = vi.fn().mockResolvedValue(responseWithChunks([
      'data: {"type":"RUN_STARTED","threadId":"sess-a","runId":"server-r1","sequence":1}\n',
      'data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"m1","delta":"hi","sequence":2}\n',
      'data: {"type":"RUN_FINISHED","threadId":"sess-a","runId":"server-r1","result":{"finalResponse":"hi"},"outcome":"success","sequence":3}\n',
    ]));
    vi.stubGlobal('fetch', fetchMock);

    const subscription = startSendStream({
      ...identity(),
      content: [{ type: 'text', text: 'hello' }],
      idempotencyKey: 'idem-a',
      preferredSkillKeys: ['pdf', 'data_analysis'],
      reasoning: { mode: 'enabled', effort: 'max' },
      onEnvelope: (envelope) => envelopes.push(envelope),
    });

    await subscription.promise;

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/chat/sess-a/message/stream',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          content: [{ type: 'text', text: 'hello' }],
          idempotency_key: 'idem-a',
          preferred_skill_keys: ['pdf', 'data_analysis'],
          thinking_mode: 'enabled',
          reasoning_effort: 'max',
        }),
      }),
    );
    expect(envelopes.map((item) => item.event.type)).toEqual([
      'CUSTOM',
      'RUN_STARTED',
      'TEXT_MESSAGE_CONTENT',
      'RUN_FINISHED',
    ]);
    expect(envelopes[2]).toMatchObject({
      ownerSessionId: 'sess-a',
      clientRunKey: 'run-a',
      transportEpoch: 1,
      connectionId: 'conn-a',
      sequence: 2,
      messageId: 'm1',
    });
  });

  it('direct stream reconnects through subscribe using the consumed sequence', async () => {
    vi.useFakeTimers();
    const envelopes: StreamEnvelope[] = [];
    const encoder = new TextEncoder();
    const postReader = {
      read: vi.fn()
        .mockResolvedValueOnce({
          done: false,
          value: encoder.encode([
            'data: {"type":"RUN_STARTED","threadId":"sess-a","runId":"server-r1","sequence":1}',
            'data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"m1","delta":"hel","sequence":2}',
            '',
          ].join('\n')),
        })
        .mockRejectedValueOnce(new Error('stream dropped')),
    };
    const subscribeReader = {
      read: vi.fn().mockResolvedValueOnce({
        done: false,
        value: encoder.encode('data: {"type":"RUN_FINISHED","threadId":"sess-a","runId":"server-r1","result":{"finalResponse":"hello"},"outcome":"success","sequence":3}\n'),
      }),
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, body: { getReader: () => postReader } })
      .mockResolvedValueOnce({ ok: true, body: { getReader: () => subscribeReader } });
    vi.stubGlobal('fetch', fetchMock);

    const subscription = startSendStream({
      ...identity(),
      content: [{ type: 'text', text: 'hello' }],
      idempotencyKey: 'idem-a',
      onEnvelope: (envelope) => envelopes.push(envelope),
    });

    await vi.runAllTimersAsync();
    await subscription.promise;

    expect(fetchMock.mock.calls[1][0]).toBe('/api/chat/sess-a/round/server-r1/subscribe?last_sequence=2');
    expect(envelopes.find((item) => item.event.type === 'RUN_FINISHED')?.source).toBe('subscribe');
  });

  it('recovers a completed round when the POST response was lost before acceptance', async () => {
    vi.useFakeTimers();
    const envelopes: StreamEnvelope[] = [];
    const onRejectedBeforeAccept = vi.fn();
    const fetchMock = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));
    vi.stubGlobal('fetch', fetchMock);
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [{
        round_id: 'server-r1',
        idempotency_key: 'idem-a',
        status: 'completed',
        final_response: 'done',
        preferred_skills: [{ key: 'pdf', display_name: 'PDF 文档' }],
        created_at: '2026-07-17T01:00:00Z',
        last_event_sequence: 4,
      }],
    } as any);

    const subscription = startSendStream({
      ...identity(),
      content: [{ type: 'text', text: 'hello' }],
      idempotencyKey: 'idem-a',
      onEnvelope: (envelope) => envelopes.push(envelope),
      onRejectedBeforeAccept,
    });

    await vi.runAllTimersAsync();
    await subscription.promise;

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onRejectedBeforeAccept).not.toHaveBeenCalled();
    expect(envelopes.map((item) => item.event.type)).toEqual([
      'CUSTOM',
      'RUN_STARTED',
      'RUN_FINISHED',
    ]);
    expect(envelopes[0].event).toMatchObject({ name: 'stream_accepted' });
    expect(envelopes[1].event).toMatchObject({
      runId: 'server-r1',
      preferredSkills: [{ key: 'pdf', display_name: 'PDF 文档' }],
    });
    expect(envelopes[2].event).toMatchObject({
      runId: 'server-r1',
      result: { finalResponse: 'done' },
    });
  });

  it('preserves the server identity when history recovers a failed round', async () => {
    vi.useFakeTimers();
    const envelopes: StreamEnvelope[] = [];
    const onRejectedBeforeAccept = vi.fn();
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [{
        round_id: 'server-failed-r1',
        idempotency_key: 'idem-a',
        status: 'failed',
        final_response: 'provider failed',
        preferred_skills: [],
        created_at: '2026-07-17T01:00:00Z',
        last_event_sequence: 7,
      }],
    } as any);

    const subscription = startSendStream({
      ...identity(),
      content: [{ type: 'text', text: 'hello' }],
      idempotencyKey: 'idem-a',
      onEnvelope: (envelope) => envelopes.push(envelope),
      onRejectedBeforeAccept,
    });

    await vi.runAllTimersAsync();
    await subscription.promise;

    expect(onRejectedBeforeAccept).not.toHaveBeenCalled();
    expect(envelopes.map((item) => item.event.type)).toEqual([
      'CUSTOM',
      'RUN_STARTED',
      'RUN_ERROR',
    ]);
    expect(envelopes[1].event).toMatchObject({
      runId: 'server-failed-r1',
      preferredSkills: [],
    });
    expect(envelopes[2]).toMatchObject({
      ownerSessionId: 'sess-a',
      serverRunId: 'server-failed-r1',
      sequence: 7,
      event: {
        type: 'RUN_ERROR',
        threadId: 'sess-a',
        runId: 'server-failed-r1',
        message: 'provider failed',
        code: 'RUN_FAILED',
        sequence: 7,
      },
    });
  });

  it('reconciles Skill snapshots when subscribe falls back to terminal history', async () => {
    const envelopes: StreamEnvelope[] = [];
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('subscribe failed')));
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [{
        round_id: 'server-r1',
        status: 'completed',
        final_response: 'done',
        preferred_skills: [{ key: 'pdf', display_name: 'PDF 文档' }],
        last_event_sequence: 8,
      }],
    } as any);

    const subscription = startSubscribeStream({
      ...identity({ source: 'subscribe' }),
      serverRunId: 'server-r1',
      onEnvelope: (envelope) => envelopes.push(envelope),
    });
    await subscription.promise;

    expect(envelopes.map((item) => item.event.type)).toEqual([
      'RUN_STARTED',
      'RUN_FINISHED',
    ]);
    expect(envelopes[0].event.preferredSkills).toEqual([
      { key: 'pdf', display_name: 'PDF 文档' },
    ]);
  });

  it('subscribes immediately when the final history retry finds a running round', async () => {
    vi.useFakeTimers();
    const envelopes: StreamEnvelope[] = [];
    const onRejectedBeforeAccept = vi.fn();
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(responseWithChunks([
        'data: {"type":"RUN_FINISHED","threadId":"sess-a","runId":"server-r1","result":{"finalResponse":"done"},"outcome":"success","sequence":1}\n',
      ]));
    vi.stubGlobal('fetch', fetchMock);
    vi.mocked(apiService.getSessionHistoryV2)
      .mockResolvedValueOnce({ rounds: [] } as any)
      .mockResolvedValueOnce({ rounds: [] } as any)
      .mockResolvedValueOnce({
        rounds: [{
          round_id: 'server-r1',
          idempotency_key: 'idem-a',
          status: 'running',
          created_at: '2026-07-17T01:00:00Z',
        }],
      } as any);

    const subscription = startSendStream({
      ...identity(),
      content: [{ type: 'text', text: 'hello' }],
      idempotencyKey: 'idem-a',
      onEnvelope: (envelope) => envelopes.push(envelope),
      onRejectedBeforeAccept,
    });

    await vi.runAllTimersAsync();
    await subscription.promise;

    expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(3);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toBe('/api/chat/sess-a/round/server-r1/subscribe?last_sequence=0');
    expect(onRejectedBeforeAccept).not.toHaveBeenCalled();
    expect(envelopes.filter((item) => item.event.type === 'CUSTOM')).toHaveLength(1);
    expect(envelopes.some((item) => item.event.type === 'RUN_FINISHED')).toBe(true);
  });

  it('restores the optimistic draft once after history retries confirm no matching round', async () => {
    vi.useFakeTimers();
    const envelopes: StreamEnvelope[] = [];
    const onRejectedBeforeAccept = vi.fn();
    const fetchMock = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));
    vi.stubGlobal('fetch', fetchMock);
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [{
        round_id: 'other-round',
        idempotency_key: 'other-idempotency-key',
        status: 'running',
        created_at: '2026-07-17T01:00:00Z',
      }],
    } as any);

    const subscription = startSendStream({
      ...identity(),
      content: [{ type: 'text', text: 'hello' }],
      idempotencyKey: 'idem-a',
      onEnvelope: (envelope) => envelopes.push(envelope),
      onRejectedBeforeAccept,
    });

    await vi.runAllTimersAsync();
    await subscription.promise;

    expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(3);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onRejectedBeforeAccept).toHaveBeenCalledTimes(1);
    expect(envelopes.filter((item) => item.event.type === 'RUN_ERROR')).toHaveLength(1);
    expect(envelopes.find((item) => item.event.type === 'RUN_ERROR')?.event).toMatchObject({
      code: 'REQUEST_FAILED',
    });
  });

  it('keeps the draft cleared when every history confirmation attempt fails', async () => {
    vi.useFakeTimers();
    const envelopes: StreamEnvelope[] = [];
    const onRejectedBeforeAccept = vi.fn();
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    vi.mocked(apiService.getSessionHistoryV2).mockRejectedValue(new Error('history unavailable'));

    const subscription = startSendStream({
      ...identity(),
      content: [{ type: 'text', text: 'hello' }],
      idempotencyKey: 'idem-a',
      onEnvelope: (envelope) => envelopes.push(envelope),
      onRejectedBeforeAccept,
    });

    await vi.runAllTimersAsync();
    await subscription.promise;

    expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(3);
    expect(onRejectedBeforeAccept).not.toHaveBeenCalled();
    expect(envelopes).toHaveLength(1);
    expect(envelopes[0].event).toMatchObject({
      type: 'RUN_ERROR',
      code: 'REQUEST_STATUS_UNKNOWN',
    });
    expect(envelopes[0].event.message).toContain('无法确认请求是否已受理');
    expect(envelopes[0].event.message).not.toContain('重新发送');
  });

  it('does not restore after a mixture of failed and empty history checks', async () => {
    vi.useFakeTimers();
    const envelopes: StreamEnvelope[] = [];
    const onRejectedBeforeAccept = vi.fn();
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    vi.mocked(apiService.getSessionHistoryV2)
      .mockRejectedValueOnce(new Error('history unavailable'))
      .mockResolvedValueOnce({ session_id: 'sess-a', rounds: [], total: 0 })
      .mockResolvedValueOnce({ session_id: 'sess-a', rounds: [], total: 0 });

    const subscription = startSendStream({
      ...identity(),
      content: [{ type: 'text', text: 'hello' }],
      idempotencyKey: 'idem-a',
      onEnvelope: (envelope) => envelopes.push(envelope),
      onRejectedBeforeAccept,
    });

    await vi.runAllTimersAsync();
    await subscription.promise;

    expect(onRejectedBeforeAccept).not.toHaveBeenCalled();
    expect(envelopes[0].event).toMatchObject({ code: 'REQUEST_STATUS_UNKNOWN' });
  });

  it('cancels cleanly before POST response headers without restoring or checking history', async () => {
    const envelopes: StreamEnvelope[] = [];
    const onRejectedBeforeAccept = vi.fn();
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => new Promise((_resolve, reject) => {
      const rejectAbort = () => {
        const error = new Error('aborted');
        error.name = 'AbortError';
        reject(error);
      };
      const signal = init?.signal;
      if (signal?.aborted) rejectAbort();
      else signal?.addEventListener('abort', rejectAbort, { once: true });
    }));
    vi.stubGlobal('fetch', fetchMock);

    const subscription = startSendStream({
      ...identity(),
      content: [{ type: 'text', text: 'hello' }],
      idempotencyKey: 'idem-a',
      onEnvelope: (envelope) => envelopes.push(envelope),
      onRejectedBeforeAccept,
    });
    subscription.abort();
    await subscription.promise;

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(apiService.getSessionHistoryV2).not.toHaveBeenCalled();
    expect(onRejectedBeforeAccept).not.toHaveBeenCalled();
    expect(envelopes).toEqual([]);
  });

  it('does not subscribe when cancelled while waiting for history confirmation', async () => {
    vi.useFakeTimers();
    let historySignal: AbortSignal | undefined;
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(responseWithChunks([
        'data: {"type":"RUN_FINISHED","threadId":"sess-a","runId":"server-r1","result":{"finalResponse":"done"},"outcome":"success","sequence":1}\n',
      ]));
    vi.stubGlobal('fetch', fetchMock);
    vi.mocked(apiService.getSessionHistoryV2).mockImplementation((_sessionId, signal) => {
      historySignal = signal;
      return new Promise((_resolve, reject) => {
        const rejectAbort = () => {
          const error = new Error('cancelled');
          error.name = 'CanceledError';
          reject(error);
        };
        if (signal?.aborted) rejectAbort();
        else signal?.addEventListener('abort', rejectAbort, { once: true });
      });
    });

    const subscription = startSendStream({
      ...identity(),
      content: [{ type: 'text', text: 'hello' }],
      idempotencyKey: 'idem-a',
      onEnvelope: vi.fn(),
      onRejectedBeforeAccept: vi.fn(),
    });

    await vi.advanceTimersByTimeAsync(1000);
    expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(1);
    expect(historySignal).toBeInstanceOf(AbortSignal);
    subscription.abort();
    await subscription.promise;

    expect(historySignal?.aborted).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('settles immediately when cancelled during a retry delay', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));
    vi.stubGlobal('fetch', fetchMock);

    const subscription = startSendStream({
      ...identity(),
      content: [{ type: 'text', text: 'hello' }],
      idempotencyKey: 'idem-a',
      onEnvelope: vi.fn(),
    });

    await vi.advanceTimersByTimeAsync(0);
    expect(vi.getTimerCount()).toBe(1);
    subscription.abort();
    await subscription.promise;

    expect(vi.getTimerCount()).toBe(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(apiService.getSessionHistoryV2).not.toHaveBeenCalled();
  });

  it('cancels final known-round recovery without emitting a late disconnect terminal', async () => {
    vi.useFakeTimers();
    const envelopes: StreamEnvelope[] = [];
    let finalRecoverySignal: AbortSignal | undefined;
    const emptyHistory = { session_id: 'sess-a', rounds: [], total: 0 };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(responseWithChunkThenError(
        'data: {"type":"RUN_STARTED","threadId":"sess-a","runId":"server-r1","sequence":1}\n\n',
        new Error('stream dropped'),
      ))
      .mockRejectedValue(new TypeError('subscribe failed'));
    vi.stubGlobal('fetch', fetchMock);
    vi.mocked(apiService.getSessionHistoryV2)
      .mockResolvedValueOnce(emptyHistory)
      .mockResolvedValueOnce(emptyHistory)
      .mockResolvedValueOnce(emptyHistory)
      .mockImplementationOnce((_sessionId, signal) => {
        finalRecoverySignal = signal;
        return new Promise((_resolve, reject) => {
          const rejectAbort = () => reject(new Error('cancelled'));
          if (signal?.aborted) rejectAbort();
          else signal?.addEventListener('abort', rejectAbort, { once: true });
        });
      });

    const subscription = startSendStream({
      ...identity(),
      content: [{ type: 'text', text: 'hello' }],
      idempotencyKey: 'idem-a',
      onEnvelope: (envelope) => envelopes.push(envelope),
    });

    await vi.advanceTimersByTimeAsync(6000);
    expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(4);
    expect(finalRecoverySignal).toBeInstanceOf(AbortSignal);
    subscription.abort();
    await subscription.promise;

    expect(finalRecoverySignal?.aborted).toBe(true);
    expect(envelopes.some((item) => (
      item.event.type === 'RUN_ERROR' && item.event.code === 'SSE_DISCONNECTED'
    ))).toBe(false);
  });

  it('cancels subscribe recovery without emitting a late terminal or onError', async () => {
    const envelopes: StreamEnvelope[] = [];
    const onError = vi.fn();
    let historySignal: AbortSignal | undefined;
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('subscribe failed')));
    vi.mocked(apiService.getSessionHistoryV2).mockImplementation((_sessionId, signal) => {
      historySignal = signal;
      return new Promise((_resolve, reject) => {
        const rejectAbort = () => reject(new Error('cancelled'));
        if (signal?.aborted) rejectAbort();
        else signal?.addEventListener('abort', rejectAbort, { once: true });
      });
    });

    const subscription = startSubscribeStream({
      ...identity({ source: 'subscribe' }),
      serverRunId: 'server-r1',
      onEnvelope: (envelope) => envelopes.push(envelope),
      onError,
    });

    await vi.waitFor(() => expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(1));
    expect(historySignal).toBeInstanceOf(AbortSignal);
    subscription.abort();
    await subscription.promise;

    expect(historySignal?.aborted).toBe(true);
    expect(envelopes).toEqual([]);
    expect(onError).not.toHaveBeenCalled();
  });

  it('reports a pre-accept rejection so optimistic Skill drafts can be restored', async () => {
    const envelopes: StreamEnvelope[] = [];
    const onRejectedBeforeAccept = vi.fn();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 422,
      text: async () => JSON.stringify({
        detail: [{
          type: 'too_long',
          loc: ['body', 'preferred_skill_keys'],
          msg: 'Request validation failed',
          ctx: { max_length: 50 },
        }],
      }),
    }));

    const subscription = startSendStream({
      ...identity(),
      content: [{ type: 'text', text: 'hello' }],
      preferredSkillKeys: ['pdf'],
      onEnvelope: (envelope) => envelopes.push(envelope),
      onRejectedBeforeAccept,
    });

    await subscription.promise;

    expect(onRejectedBeforeAccept).toHaveBeenCalledTimes(1);
    expect(envelopes.find((item) => item.event.type === 'RUN_ERROR')?.event).toMatchObject({
      code: 'HTTP_CLIENT_ERROR',
      message: '优先 Skill：最多 50 项',
    });
    expect(envelopes.find((item) => item.event.type === 'RUN_ERROR')?.event).not.toMatchObject({
      message: expect.stringContaining('消息太长'),
    });
  });

  it('subscribe marks sequenced content as aggregate replay envelopes', async () => {
    const envelopes: StreamEnvelope[] = [];
    const fetchMock = vi.fn().mockResolvedValue(responseWithChunks([
      'data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"m1","delta":"hello","sequence":7}\n',
      'data: {"type":"RUN_FINISHED","threadId":"sess-a","runId":"server-r1","result":{"finalResponse":"hello"},"outcome":"success","sequence":8}\n',
    ]));
    vi.stubGlobal('fetch', fetchMock);

    const subscription = startSubscribeStream({
      ...identity({ source: 'subscribe' }),
      serverRunId: 'server-r1',
      lastSequence: 6,
      onEnvelope: (envelope) => envelopes.push(envelope),
    });

    await subscription.promise;

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/chat/sess-a/round/server-r1/subscribe?last_sequence=6',
      expect.objectContaining({ method: 'GET' }),
    );
    expect(envelopes[0]).toMatchObject({
      event: { type: 'TEXT_MESSAGE_CONTENT', delta: 'hello' },
      sequence: 7,
      isAggregate: true,
      source: 'subscribe',
    });
    expect(subscription.getLatestSequence?.()).toBe(8);
  });
});
