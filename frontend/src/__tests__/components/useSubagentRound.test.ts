import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useSubagentRound } from '../../components/useSubagentRound';
import { apiService } from '../../services/api';
import { startSubscribeStream } from '../../services/chatStreamClient';
import type { RoundData, SubagentTask } from '../../types';

vi.mock('../../services/api', () => ({ apiService: { getRoundSnapshot: vi.fn() } }));
vi.mock('../../services/chatStreamClient', () => ({ startSubscribeStream: vi.fn() }));

const task = (id = 'a'): SubagentTask => ({
  edge_id: `edge-${id}`, parent_run_id: 'parent', child_run_id: `child-${id}`, tool_call_id: `tool-${id}`,
  agent_name: 'audit', description: '审阅', agent_type: null, model_id: null, status: 'running',
  created_at: '', started_at: null, completed_at: null,
});
const snapshot = (id = 'a', overrides: Partial<RoundData> = {}): RoundData => ({
  round_id: `child-${id}`, parent_run_id: 'parent', user_message: '审阅', final_response: null,
  status: 'running', created_at: '', steps: [], step_count: 0, last_event_sequence: 3, ...overrides,
});
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
};

describe('useSubagentRound', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(startSubscribeStream).mockImplementation(() => ({ abort: vi.fn(), promise: new Promise(() => {}) }));
  });
  afterEach(() => { vi.restoreAllMocks(); });

  it('hydrates then subscribes from the child cursor in a private runtime', async () => {
    vi.mocked(apiService.getRoundSnapshot).mockResolvedValue(snapshot());
    const { result, unmount } = renderHook(() => useSubagentRound('session', task()));
    await waitFor(() => expect(startSubscribeStream).toHaveBeenCalledOnce());
    const options = vi.mocked(startSubscribeStream).mock.calls[0][0];
    expect(options).toMatchObject({ serverRunId: 'child-a', snapshotScope: 'round', lastSequence: 3 });
    act(() => options.onEnvelope({ ...options, event: { type: 'TEXT_MESSAGE_START', messageId: 'answer', role: 'assistant' },
      receivedAt: Date.now(), sequence: 4 }));
    act(() => options.onEnvelope({ ...options, event: { type: 'TEXT_MESSAGE_CONTENT', messageId: 'answer', delta: '子任务结论' },
      receivedAt: Date.now(), sequence: 5 }));
    expect(result.current.round?.assistant_messages?.[0].content).toBe('子任务结论');
    expect(result.current.round?.parent_run_id).toBe('parent');
    expect(result.current.loading).toBe(false);
    unmount();
    expect(vi.mocked(startSubscribeStream).mock.results[0].value.abort).toHaveBeenCalledOnce();
  });

  it('switching tasks aborts old reads and rejects late snapshots and envelopes', async () => {
    const oldRead = deferred<RoundData>();
    vi.mocked(apiService.getRoundSnapshot).mockReturnValueOnce(oldRead.promise).mockResolvedValueOnce(snapshot('b'));
    const { result, rerender } = renderHook(({ selected }) => useSubagentRound('session', selected), { initialProps: { selected: task() } });
    const oldSignal = vi.mocked(apiService.getRoundSnapshot).mock.calls[0][2]!;
    rerender({ selected: task('b') });
    await waitFor(() => expect(result.current.round?.round_id).toBe('child-b'));
    expect(oldSignal.aborted).toBe(true);
    await act(async () => oldRead.resolve(snapshot()));
    expect(startSubscribeStream).toHaveBeenCalledTimes(1);
    const oldOptions = vi.mocked(startSubscribeStream).mock.calls[0][0];
    vi.mocked(apiService.getRoundSnapshot).mockResolvedValueOnce(snapshot('c', { status: 'completed' }));
    rerender({ selected: task('c') });
    await waitFor(() => expect(result.current.round?.round_id).toBe('child-c'));
    act(() => oldOptions.onEnvelope({ ...oldOptions, receivedAt: Date.now(), sequence: 9,
      event: { type: 'RUN_FINISHED', runId: 'child-b', result: { finalResponse: '旧结果' } } }));
    expect(result.current.round?.round_id).toBe('child-c');
    expect(result.current.round?.final_response).toBeNull();
    expect(startSubscribeStream).toHaveBeenCalledTimes(1);
  });

  it('inactive stops observing, and returning reloads a fresh snapshot before resubscribing', async () => {
    vi.mocked(apiService.getRoundSnapshot).mockResolvedValueOnce(snapshot());
    const selected = task();
    const { result, rerender } = renderHook(({ active }) => useSubagentRound('session', selected, active), { initialProps: { active: true } });
    await waitFor(() => expect(startSubscribeStream).toHaveBeenCalledOnce());
    rerender({ active: false });
    expect(vi.mocked(startSubscribeStream).mock.results[0].value.abort).toHaveBeenCalledOnce();
    expect(result.current.round?.round_id).toBe('child-a');
    vi.mocked(apiService.getRoundSnapshot).mockResolvedValueOnce(snapshot('a', { last_event_sequence: 12 }));
    rerender({ active: true });
    await waitFor(() => expect(startSubscribeStream).toHaveBeenCalledTimes(2));
    expect(vi.mocked(startSubscribeStream).mock.calls[1][0].lastSequence).toBe(12);
  });

  it('exposes load failures for retry, and completed snapshots do not start a stream', async () => {
    vi.mocked(apiService.getRoundSnapshot).mockRejectedValueOnce(new Error('网络断开'));
    const { result } = renderHook(() => useSubagentRound('session', task()));
    await waitFor(() => expect(result.current.error).toBe('网络断开'));
    expect(result.current.loading).toBe(false);
    expect(startSubscribeStream).not.toHaveBeenCalled();
    vi.mocked(apiService.getRoundSnapshot).mockResolvedValueOnce(snapshot('a', { status: 'completed', final_response: '完成' }));
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.round?.final_response).toBe('完成'));
    expect(result.current.error).toBe('');
    expect(startSubscribeStream).not.toHaveBeenCalled();
  });
});
