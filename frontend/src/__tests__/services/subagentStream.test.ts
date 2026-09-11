import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { apiService } from '../../services/api';
import { startSubscribeStream } from '../../services/chatStreamClient';
import type { StreamEnvelope } from '../../runtime/chatRuntimeTypes';

vi.mock('../../services/api', () => ({ apiService: {
  getAuthHeaders: vi.fn(() => ({})), getSessionHistoryV2: vi.fn(), getRoundSnapshot: vi.fn(),
} }));

const child = { round_id: 'child', parent_run_id: 'parent', status: 'completed',
  user_message: 'audit', final_response: 'result', steps: [], step_count: 0,
  created_at: '', last_event_sequence: 9 };
const eof = () => ({ ok: true, body: { getReader: () => ({ read: async () => ({ done: true }) }) } });
const subscribe = (envelopes: StreamEnvelope[]) => startSubscribeStream({
  ownerSessionId: 'session', serverRunId: 'child', clientRunKey: 'child-key',
  transportEpoch: 1, connectionId: 'child-connection', source: 'subscribe',
  lastSequence: 3, snapshotScope: 'round', onEnvelope: (envelope) => envelopes.push(envelope),
});

describe('child subscribe recovery', () => {
  beforeEach(() => { vi.resetAllMocks(); vi.stubGlobal('fetch', vi.fn().mockResolvedValue(eof())); });
  afterEach(() => { vi.unstubAllGlobals(); });

  it('recovers the child snapshot and terminal without ever fetching parent history', async () => {
    vi.mocked(apiService.getRoundSnapshot).mockResolvedValue(child);
    const envelopes: StreamEnvelope[] = [];
    await subscribe(envelopes).promise;
    expect(apiService.getRoundSnapshot).toHaveBeenCalledWith('session', 'child', expect.any(AbortSignal));
    expect(apiService.getSessionHistoryV2).not.toHaveBeenCalled();
    expect(envelopes.map((item) => item.event.type)).toEqual(['RUNTIME_HISTORY_SNAPSHOT', 'RUN_FINISHED']);
    expect(envelopes[0].event.rounds).toEqual([child]);
    expect(envelopes[1].event.runId).toBe('child');
    expect(envelopes[1].sequence).toBe(9);
  });

  it('aborts a recovery request and ignores its late snapshot', async () => {
    let resolve!: (value: typeof child) => void;
    vi.mocked(apiService.getRoundSnapshot).mockImplementation(() => new Promise((done) => { resolve = done; }));
    const envelopes: StreamEnvelope[] = [];
    const stream = subscribe(envelopes);
    await vi.waitFor(() => expect(apiService.getRoundSnapshot).toHaveBeenCalled());
    const signal = vi.mocked(apiService.getRoundSnapshot).mock.calls[0][2]!;
    stream.abort();
    expect(signal.aborted).toBe(true);
    resolve(child);
    await stream.promise;
    expect(envelopes).toEqual([]);
    expect(apiService.getSessionHistoryV2).not.toHaveBeenCalled();
  });
});
