import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  startSendStream,
  startSubscribeStream,
} from '../../services/chatStreamClient';
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
