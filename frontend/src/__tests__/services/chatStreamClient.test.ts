import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  NonTerminalStreamError,
  startResumeStream,
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

  it.each([
    {
      status: 'waiting_interaction',
      expectedHandoff: 'waiting_interaction',
      interrupt: {
        id: 'interaction-1',
        reason: 'input_required',
        payload: { questions: [{ question: 'Continue?' }] },
      },
    },
    {
      status: 'running',
      expectedHandoff: 'running',
      interrupt: undefined,
    },
  ])('hands an accepted direct run back to Provider when nested subscribe recovers $status', async ({
    status,
    expectedHandoff,
    interrupt,
  }) => {
    vi.useFakeTimers();
    const envelopes: StreamEnvelope[] = [];
    const onError = vi.fn();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(responseWithChunkThenError(
        'data: {"type":"RUN_STARTED","threadId":"sess-a","runId":"server-r1","sequence":1}\n\n',
        new Error('direct stream dropped'),
      ))
      .mockRejectedValueOnce(new Error('nested subscribe dropped'));
    vi.stubGlobal('fetch', fetchMock);
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [{
        round_id: 'server-r1',
        status,
        final_response: '',
        steps: [],
        step_count: 0,
        created_at: '2026-07-17T01:00:00Z',
        last_event_sequence: 5,
        interrupt,
      }],
    } as any);

    const subscription = startSendStream({
      ...identity(),
      content: [{ type: 'text', text: 'hello' }],
      idempotencyKey: 'idem-a',
      onEnvelope: (envelope) => envelopes.push(envelope),
      onError,
    });

    await vi.runAllTimersAsync();
    await expect(subscription.promise).resolves.toBeUndefined();

    expect(envelopes.some((item) => item.event.type === 'RUN_ERROR')).toBe(false);
    expect(subscription.getHandoff?.()).toEqual({
      serverRunId: 'server-r1',
      status: expectedHandoff,
      lastSequence: 5,
    });
    expect(onError).not.toHaveBeenCalled();
    expect(envelopes.some((item) => (
      item.event.type === 'CUSTOM' && item.event.name === 'interaction_requested'
    ))).toBe(status === 'waiting_interaction');
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
      'RUNTIME_HISTORY_SNAPSHOT',
      'RUN_FINISHED',
    ]);
    expect(envelopes[0].event).toMatchObject({ name: 'stream_accepted' });
    expect(envelopes[1].event).toMatchObject({
      rounds: [{
        round_id: 'server-r1',
        preferred_skills: [{ key: 'pdf', display_name: 'PDF 文档' }],
      }],
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
      'RUNTIME_HISTORY_SNAPSHOT',
      'RUN_ERROR',
    ]);
    expect(envelopes[1].event).toMatchObject({
      rounds: [{ round_id: 'server-failed-r1', preferred_skills: [] }],
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
      'RUNTIME_HISTORY_SNAPSHOT',
      'RUN_FINISHED',
    ]);
    expect(envelopes[0].event.rounds[0].preferred_skills).toEqual([
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
    const onError = vi.fn();
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    vi.mocked(apiService.getSessionHistoryV2).mockRejectedValue(new Error('history unavailable'));

    const subscription = startSendStream({
      ...identity(),
      content: [{ type: 'text', text: 'hello' }],
      idempotencyKey: 'idem-a',
      onEnvelope: (envelope) => envelopes.push(envelope),
      onRejectedBeforeAccept,
      onError,
    });

    await vi.runAllTimersAsync();
    await subscription.promise;

    expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(3);
    expect(onRejectedBeforeAccept).not.toHaveBeenCalled();
    expect(envelopes).toHaveLength(0);
    expect(onError).toHaveBeenCalledWith(
      expect.stringContaining('无法确认请求是否已受理'),
      'REQUEST_STATUS_UNKNOWN',
    );
    expect(onError.mock.calls[0][0]).not.toContain('重新发送');
  });

  it('does not restore after a mixture of failed and empty history checks', async () => {
    vi.useFakeTimers();
    const envelopes: StreamEnvelope[] = [];
    const onRejectedBeforeAccept = vi.fn();
    const onError = vi.fn();
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
      onError,
    });

    await vi.runAllTimersAsync();
    await subscription.promise;

    expect(onRejectedBeforeAccept).not.toHaveBeenCalled();
    expect(envelopes).toHaveLength(0);
    expect(onError).toHaveBeenCalledWith(
      expect.any(String),
      'REQUEST_STATUS_UNKNOWN',
    );
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

  it('rejects a non-terminal waiting subscribe after projecting the full history snapshot', async () => {
    const envelopes: StreamEnvelope[] = [];
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(responseWithChunkThenError(
      'data: {"type":"CUSTOM","name":"interaction_requested","value":{"interactionId":"interaction-1","runId":"server-r1","kind":"user_input","payload":{"questions":[{"question":"Continue?"}]}},"sequence":7}\n\n',
      new Error('waiting connection dropped'),
    )));
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [{
        round_id: 'server-r1',
        status: 'waiting_interaction',
        final_response: '',
        steps: [{
          step_number: 1,
          thinking: '',
          assistant_content: 'persisted before waiting',
          tool_calls: [],
          tool_results: [],
          status: 'completed',
        }],
        step_count: 1,
        created_at: '2026-07-17T01:00:00Z',
        last_event_sequence: 9,
        interrupt: {
          id: 'interaction-1',
          reason: 'input_required',
          payload: { questions: [{ question: 'Continue?' }] },
        },
      }],
    } as any);

    const subscription = startSubscribeStream({
      ...identity({ source: 'subscribe' }),
      serverRunId: 'server-r1',
      lastSequence: 6,
      onEnvelope: (envelope) => envelopes.push(envelope),
    });

    await expect(subscription.promise).rejects.toEqual(expect.objectContaining({
      name: NonTerminalStreamError.name,
      code: 'SSE_NON_TERMINAL_END',
      reason: 'history_waiting',
    }));
    expect(envelopes.find((item) => item.event.type === 'RUNTIME_HISTORY_SNAPSHOT'))
      .toMatchObject({
        authoritativeRecovery: true,
        sequence: 9,
        event: {
          rounds: [{
            round_id: 'server-r1',
            steps: [{ assistant_content: 'persisted before waiting' }],
          }],
        },
      });
    expect(subscription.getLatestSequence?.()).toBe(9);
  });

  it('treats an unsequenced SUBSCRIBE_FAILED event as a reconnectable transport failure', async () => {
    const envelopes: StreamEnvelope[] = [];
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(responseWithChunks([
      'data: {"type":"RUN_ERROR","message":"订阅失败","code":"SUBSCRIBE_FAILED"}\n\n',
    ])));
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [{
        round_id: 'server-r1',
        status: 'running',
        final_response: '',
        steps: [],
        step_count: 0,
        created_at: '2026-07-17T01:00:00Z',
        last_event_sequence: 4,
      }],
    } as any);

    const subscription = startSubscribeStream({
      ...identity({ source: 'subscribe' }),
      serverRunId: 'server-r1',
      lastSequence: 3,
      onEnvelope: (envelope) => envelopes.push(envelope),
    });

    await expect(subscription.promise).rejects.toMatchObject({
      code: 'SSE_NON_TERMINAL_END',
      reason: 'history_running',
    });
    expect(envelopes.some((item) => item.event.type === 'RUN_ERROR')).toBe(false);
    expect(envelopes.map((item) => item.event.type)).toEqual([
      'RUNTIME_HISTORY_SNAPSHOT',
    ]);
  });

  it('recovers history when a waiting subscription starts after the interaction cursor', async () => {
    const envelopes: StreamEnvelope[] = [];
    const onError = vi.fn();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(responseWithChunks([
      'data: {"type":"RUN_ERROR","message":"adapter failed after durable wait","code":"INTERNAL_ERROR"}\n\n',
    ])));
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [{
        round_id: 'server-r1',
        status: 'waiting_interaction',
        final_response: '',
        steps: [],
        step_count: 1,
        created_at: '2026-07-17T01:00:00Z',
        last_event_sequence: 7,
        interrupt: {
          id: 'interaction-1',
          reason: 'input_required',
          payload: { kind: 'user_input', questions: [{ question: 'Continue?' }] },
        },
      }],
    } as any);

    const subscription = startSubscribeStream({
      ...identity({ source: 'subscribe' }),
      serverRunId: 'server-r1',
      lastSequence: 7,
      durableInteractionObserved: true,
      onEnvelope: (envelope) => envelopes.push(envelope),
      onError,
    });

    await expect(subscription.promise).rejects.toMatchObject({
      code: 'SSE_NON_TERMINAL_END',
      reason: 'history_waiting',
    });
    expect(onError).toHaveBeenCalledWith(
      'adapter failed after durable wait',
      'INTERNAL_ERROR',
    );
    expect(envelopes.some((item) => item.event.type === 'RUN_ERROR')).toBe(false);
    expect(envelopes.map((item) => [item.event.type, item.event.name])).toEqual([
      ['RUNTIME_HISTORY_SNAPSHOT', undefined],
      ['CUSTOM', 'interaction_requested'],
    ]);
  });

  it('recovers the existing waiting Round when a new message hits INTERACTION_PENDING', async () => {
    const envelopes: StreamEnvelope[] = [];
    const onControlConflict = vi.fn();
    const onError = vi.fn();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(responseWithChunks([
      'data: {"type":"RUN_ERROR","message":"当前 Round 正在等待用户回答","code":"INTERACTION_PENDING"}\n\n',
    ])));
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [{
        round_id: 'server-waiting',
        status: 'waiting_interaction',
        final_response: '',
        steps: [],
        step_count: 1,
        created_at: '2026-07-17T01:00:00Z',
        last_event_sequence: 7,
        interrupt: {
          id: 'interaction-existing',
          reason: 'input_required',
          payload: {
            kind: 'user_input',
            questions: [{ question: 'Continue?' }],
          },
        },
      }],
    } as any);

    const subscription = startSendStream({
      ...identity(),
      content: [{ type: 'text', text: 'new message' }],
      idempotencyKey: 'idem-new',
      onEnvelope: (envelope) => envelopes.push(envelope),
      onControlConflict,
      onError,
    });

    await subscription.promise;

    expect(onControlConflict).toHaveBeenCalledWith(
      '当前 Round 正在等待用户回答',
      'INTERACTION_PENDING',
      'server-waiting',
    );
    expect(onError).toHaveBeenCalledWith(
      '当前 Round 正在等待用户回答',
      'INTERACTION_PENDING',
    );
    expect(envelopes.some((item) => item.event.type === 'RUN_ERROR')).toBe(false);
    expect(envelopes.slice(-2)).toMatchObject([
      {
        source: 'subscribe',
        authoritativeRecovery: true,
        event: { type: 'RUNTIME_HISTORY_SNAPSHOT' },
      },
      {
        source: 'subscribe',
        authoritativeRecovery: true,
        event: {
          type: 'CUSTOM',
          name: 'interaction_requested',
          value: { interactionId: 'interaction-existing', runId: 'server-waiting' },
        },
      },
    ]);
    expect(subscription.getHandoff?.()).toEqual({
      serverRunId: 'server-waiting',
      status: 'waiting_interaction',
      lastSequence: 7,
    });
  });

  it('does not terminalize a direct Round when an unsequenced error follows interaction_requested', async () => {
    const envelopes: StreamEnvelope[] = [];
    const onError = vi.fn();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(responseWithChunks([
      'data: {"type":"RUN_STARTED","threadId":"sess-a","runId":"server-r1","sequence":1}\n',
      'data: {"type":"CUSTOM","name":"interaction_requested","value":{"interactionId":"interaction-1","runId":"server-r1","kind":"user_input","payload":{"questions":[{"question":"Continue?"}]}},"sequence":2}\n',
      'data: {"type":"RUN_ERROR","message":"adapter failed after waiting","code":"INTERNAL_ERROR"}\n\n',
    ])));
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [{
        round_id: 'server-r1',
        status: 'waiting_interaction',
        final_response: '',
        steps: [],
        step_count: 1,
        created_at: '2026-07-17T01:00:00Z',
        last_event_sequence: 2,
        interrupt: {
          id: 'interaction-1',
          reason: 'input_required',
          payload: { kind: 'user_input', questions: [{ question: 'Continue?' }] },
        },
      }],
    } as any);

    const subscription = startSendStream({
      ...identity(),
      content: [{ type: 'text', text: 'hello' }],
      idempotencyKey: 'idem-a',
      onEnvelope: (envelope) => envelopes.push(envelope),
      onError,
    });

    await subscription.promise;

    expect(envelopes.some((item) => item.event.type === 'RUN_ERROR')).toBe(false);
    expect(onError).toHaveBeenCalledWith(
      'adapter failed after waiting',
      'INTERNAL_ERROR',
    );
    expect(subscription.getHandoff?.()).toEqual({
      serverRunId: 'server-r1',
      status: 'waiting_interaction',
      lastSequence: 2,
    });
    expect(envelopes.filter((item) => (
      item.event.type === 'CUSTOM' && item.event.name === 'interaction_requested'
    ))).toHaveLength(2);
  });

  it('resume accepted then disconnected recovers the same round and subscribes from the consumed sequence', async () => {
    const envelopes: StreamEnvelope[] = [];
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(responseWithChunkThenError(
        'data: {"type":"CUSTOM","name":"interaction_resolved","value":{"interactionId":"interaction-1","runId":"server-r1"},"sequence":6}\n\n',
        new Error('resume connection dropped'),
      ))
      .mockResolvedValueOnce(responseWithChunks([
        'data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"m1","delta":"done","sequence":7}\n',
        'data: {"type":"RUN_FINISHED","threadId":"sess-a","runId":"server-r1","result":{"finalResponse":"done"},"outcome":"success","sequence":8}\n',
      ]));
    vi.stubGlobal('fetch', fetchMock);
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [{
        round_id: 'server-r1',
        status: 'running',
        final_response: '',
        steps: [],
        step_count: 1,
        created_at: '2026-07-17T01:00:00Z',
        last_event_sequence: 6,
      }],
    } as any);

    const subscription = startResumeStream({
      ...identity({ source: 'resume' }),
      interruptId: 'interaction-1',
      answers: { Continue: 'Yes' },
      serverRunId: 'server-r1',
      lastSequence: 5,
      onEnvelope: (envelope) => envelopes.push(envelope),
    });

    await subscription.promise;

    expect(apiService.getSessionHistoryV2).toHaveBeenCalledWith(
      'sess-a',
      expect.any(AbortSignal),
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      '/api/chat/sess-a/round/server-r1/subscribe?last_sequence=6',
    );
    expect(envelopes.map((item) => [item.source, item.event.type, item.event.name])).toEqual([
      ['resume', 'CUSTOM', 'stream_accepted'],
      ['resume', 'CUSTOM', 'interaction_resolved'],
      ['subscribe', 'RUNTIME_HISTORY_SNAPSHOT', undefined],
      ['subscribe', 'TEXT_MESSAGE_CONTENT', undefined],
      ['subscribe', 'RUN_FINISHED', undefined],
    ]);
    expect(subscription.getLatestSequence?.()).toBe(8);
  });

  it('settles after a durable resume terminal even when the reader rejects afterward', async () => {
    const envelopes: StreamEnvelope[] = [];
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(responseWithChunkThenError(
      'data: {"type":"RUN_FINISHED","threadId":"sess-a","runId":"server-r1","result":{"finalResponse":"done"},"outcome":"success","sequence":8}\n\n',
      new Error('reader failed after terminal'),
    )));

    const subscription = startResumeStream({
      ...identity({ source: 'resume' }),
      interruptId: 'interaction-1',
      answers: { Continue: 'Yes' },
      serverRunId: 'server-r1',
      lastSequence: 7,
      onEnvelope: (envelope) => envelopes.push(envelope),
    });

    await expect(subscription.promise).resolves.toBeUndefined();

    expect(envelopes.map((item) => item.event.type)).toEqual([
      'CUSTOM',
      'RUN_FINISHED',
    ]);
    expect(apiService.getSessionHistoryV2).not.toHaveBeenCalled();
    expect(subscription.getLatestSequence?.()).toBe(8);
  });

  it('marks an equal-sequence terminal synthesized from history as authoritative recovery', async () => {
    const envelopes: StreamEnvelope[] = [];
    const fetchMock = vi.fn().mockResolvedValueOnce(responseWithChunkThenError(
      'data: {"type":"CUSTOM","name":"interaction_resolved","value":{"interactionId":"interaction-1","runId":"server-r1"},"sequence":6}\n\n',
      new Error('resume connection dropped'),
    ));
    vi.stubGlobal('fetch', fetchMock);
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [{
        round_id: 'server-r1',
        status: 'completed',
        final_response: 'done',
        steps: [],
        step_count: 1,
        created_at: '2026-07-17T01:00:00Z',
        completed_at: '2026-07-17T01:00:02Z',
        last_event_sequence: 6,
      }],
    } as any);

    const subscription = startResumeStream({
      ...identity({ source: 'resume' }),
      interruptId: 'interaction-1',
      answers: { Continue: 'Yes' },
      serverRunId: 'server-r1',
      lastSequence: 5,
      onEnvelope: (envelope) => envelopes.push(envelope),
    });

    await subscription.promise;

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(envelopes.slice(-2)).toMatchObject([
      {
        source: 'subscribe',
        authoritativeRecovery: true,
        event: {
          type: 'RUNTIME_HISTORY_SNAPSHOT',
          rounds: [{ round_id: 'server-r1' }],
        },
      },
      {
        source: 'subscribe',
        authoritativeRecovery: true,
        sequence: 6,
        event: { type: 'RUN_FINISHED', runId: 'server-r1' },
      },
    ]);
    expect(subscription.getLatestSequence?.()).toBe(6);
  });

  it('does not re-park the resolved interaction when a boundary error sees stale waiting history', async () => {
    vi.useFakeTimers();
    const envelopes: StreamEnvelope[] = [];
    const onError = vi.fn();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(responseWithChunks([
      'data: {"type":"CUSTOM","name":"interaction_resolved","value":{"interactionId":"interaction-1","runId":"server-r1"},"sequence":6}\n',
      'data: {"type":"RUN_ERROR","message":"continuation setup failed","code":"INTERNAL_ERROR"}\n\n',
    ])));
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [{
        round_id: 'server-r1',
        status: 'waiting_interaction',
        final_response: '',
        steps: [],
        step_count: 1,
        created_at: '2026-07-17T01:00:00Z',
        last_event_sequence: 7,
        interrupt: {
          id: 'interaction-1',
          reason: 'input_required',
          payload: { kind: 'user_input', questions: [{ question: 'Continue?' }] },
        },
      }],
    } as any);

    const subscription = startResumeStream({
      ...identity({ source: 'resume' }),
      interruptId: 'interaction-1',
      answers: { Continue: 'Yes' },
      serverRunId: 'server-r1',
      lastSequence: 5,
      onEnvelope: (envelope) => envelopes.push(envelope),
      onError,
    });
    const settled = expect(subscription.promise).rejects.toThrow('continuation setup failed');
    await vi.runAllTimersAsync();
    await settled;

    expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(3);
    expect(envelopes.filter((item) => (
      item.event.type === 'CUSTOM' && item.event.name === 'interaction_requested'
    ))).toHaveLength(0);
    expect(envelopes.some((item) => item.event.type === 'RUN_ERROR')).toBe(false);
    expect(onError).toHaveBeenCalledWith(
      'continuation setup failed',
      'INTERNAL_ERROR',
    );
    expect(subscription.getLatestSequence?.()).toBe(7);
  });

  it('accepts a different interaction requested after a resolved-boundary error', async () => {
    const envelopes: StreamEnvelope[] = [];
    const onError = vi.fn();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(responseWithChunks([
      'data: {"type":"CUSTOM","name":"interaction_resolved","value":{"interactionId":"interaction-1","runId":"server-r1"},"sequence":6}\n',
      'data: {"type":"RUN_ERROR","message":"continuation setup failed","code":"INTERNAL_ERROR"}\n\n',
    ])));
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [{
        round_id: 'server-r1',
        status: 'waiting_interaction',
        final_response: '',
        steps: [],
        step_count: 1,
        created_at: '2026-07-17T01:00:00Z',
        last_event_sequence: 8,
        interrupt: {
          id: 'interaction-2',
          reason: 'input_required',
          payload: { kind: 'user_input', questions: [{ question: 'Next?' }] },
        },
      }],
    } as any);

    const subscription = startResumeStream({
      ...identity({ source: 'resume' }),
      interruptId: 'interaction-1',
      answers: { Continue: 'Yes' },
      serverRunId: 'server-r1',
      lastSequence: 5,
      onEnvelope: (envelope) => envelopes.push(envelope),
      onError,
    });

    await expect(subscription.promise).resolves.toBeUndefined();

    expect(envelopes.filter((item) => (
      item.event.type === 'CUSTOM' && item.event.name === 'interaction_requested'
    )).map((item) => item.event.value.interactionId)).toEqual(['interaction-2']);
    expect(onError).toHaveBeenCalledWith(
      'continuation setup failed',
      'INTERNAL_ERROR',
    );
  });

  it.each([
    'NO_PENDING_INTERRUPT',
    'RESUME_CONFLICT',
    'AGENT_INIT_FAILED',
  ])('recovers authoritative waiting state instead of terminalizing a prelude %s error', async (code) => {
    const envelopes: StreamEnvelope[] = [];
    const onError = vi.fn();
    const fetchMock = vi.fn().mockResolvedValue(responseWithChunks([
      `data: {"type":"RUN_ERROR","message":"resume rejected","code":"${code}","runId":"server-r1"}\n\n`,
    ]));
    vi.stubGlobal('fetch', fetchMock);
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [{
        round_id: 'server-r1',
        status: 'waiting_interaction',
        final_response: '',
        steps: [],
        step_count: 1,
        created_at: '2026-07-17T01:00:00Z',
        last_event_sequence: 5,
        interrupt: {
          id: 'interaction-1',
          reason: 'input_required',
          payload: {
            kind: 'user_input',
            questions: [{ question: 'Continue?' }],
          },
        },
      }],
    } as any);

    const subscription = startResumeStream({
      ...identity({ source: 'resume' }),
      interruptId: 'interaction-1',
      answers: { Continue: 'Yes' },
      serverRunId: 'server-r1',
      lastSequence: 5,
      onEnvelope: (envelope) => envelopes.push(envelope),
      onError,
    });

    await subscription.promise;

    expect(apiService.getSessionHistoryV2).toHaveBeenCalledWith(
      'sess-a',
      expect.any(AbortSignal),
    );
    expect(envelopes.some((item) => item.event.type === 'RUN_ERROR')).toBe(false);
    expect(envelopes.slice(-2)).toMatchObject([
      {
        source: 'subscribe',
        authoritativeRecovery: true,
        event: {
          type: 'RUNTIME_HISTORY_SNAPSHOT',
          rounds: [{ round_id: 'server-r1' }],
        },
      },
      {
        source: 'subscribe',
        authoritativeRecovery: true,
        event: {
          type: 'CUSTOM',
          name: 'interaction_requested',
          value: { interactionId: 'interaction-1', runId: 'server-r1' },
        },
      },
    ]);
    expect(onError).toHaveBeenCalledWith('resume rejected', code);
  });

  it('settles without a synthetic failure when a new interaction arrived before disconnect', async () => {
    const envelopes: StreamEnvelope[] = [];
    const fetchMock = vi.fn().mockResolvedValue(responseWithChunkThenError(
      'data: {"type":"CUSTOM","name":"interaction_requested","value":{"interactionId":"interaction-2","runId":"server-r1","kind":"user_input","payload":{"questions":[{"question":"One more thing?"}]}},"sequence":9}\n\n',
      new Error('resume connection dropped'),
    ));
    vi.stubGlobal('fetch', fetchMock);
    vi.mocked(apiService.getSessionHistoryV2).mockRejectedValue(new Error('history unavailable'));

    const subscription = startResumeStream({
      ...identity({ source: 'resume' }),
      interruptId: 'interaction-1',
      answers: { Continue: 'Yes' },
      serverRunId: 'server-r1',
      lastSequence: 8,
      onEnvelope: (envelope) => envelopes.push(envelope),
    });

    await expect(subscription.promise).resolves.toBeUndefined();

    expect(envelopes.map((item) => [item.event.type, item.event.name])).toEqual([
      ['CUSTOM', 'stream_accepted'],
      ['CUSTOM', 'interaction_requested'],
    ]);
    expect(apiService.getSessionHistoryV2).not.toHaveBeenCalled();
  });
});
