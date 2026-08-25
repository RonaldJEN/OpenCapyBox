import { beforeEach, describe, expect, it, vi } from 'vitest';

import { act, render, waitFor } from '../utils/test-utils';
import {
  ChatRuntimeProvider,
  useChatRuntime,
} from '../../runtime/ChatRuntimeProvider';
import {
  startResumeStream,
  startSendStream,
  startSubscribeStream,
} from '../../services/chatStreamClient';
import { apiService } from '../../services/api';
import type { RoundData } from '../../types';

vi.mock('../../services/api', () => ({
  apiService: {
    getSessionHistoryV2: vi.fn(),
    abortChat: vi.fn(),
  },
}));

vi.mock('../../services/chatStreamClient', () => ({
  startResumeStream: vi.fn(),
  startSendStream: vi.fn(),
  startSubscribeStream: vi.fn(() => ({
    abort: vi.fn(),
    promise: Promise.resolve(),
    getLatestSequence: () => 0,
  })),
}));

function deferred() {
  let resolve!: () => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<void>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function deferredValue<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function waitingRound(overrides: Partial<RoundData> = {}): RoundData {
  return {
    round_id: 'server-r1',
    user_message: 'hello',
    final_response: '',
    steps: [],
    step_count: 1,
    status: 'waiting_interaction',
    created_at: '2026-01-01T00:00:00.000Z',
    last_event_sequence: 7,
    interrupt: {
      id: 'interaction-1',
      reason: 'input_required',
      payload: { questions: [{ question: 'Continue?' }] },
    },
    ...overrides,
  };
}

let runtime: ReturnType<typeof useChatRuntime> | null = null;

function RuntimeProbe() {
  runtime = useChatRuntime();
  const projection = runtime.getSessionProjection('sess-a');
  return (
    <div
      data-testid="runtime-probe"
      data-error={projection.error}
      data-pending={projection.pendingInterrupt?.id || ''}
      data-rounds={projection.rounds.length}
      data-executing={runtime.getExecutingSessionIds().has('sess-a') ? 'yes' : 'no'}
      data-active-slot={runtime.getActiveSlotSessionIds().has('sess-a') ? 'yes' : 'no'}
    />
  );
}

async function loadHistory(rounds: RoundData[]) {
  vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
    session_id: 'sess-a',
    rounds,
    total: rounds.length,
  });
  await act(async () => {
    await runtime!.loadSessionHistory('sess-a');
  });
}

describe('ChatRuntimeProvider resume transport ownership', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(startSubscribeStream).mockReturnValue({
      abort: vi.fn(),
      promise: Promise.resolve(),
      getLatestSequence: () => 0,
    });
    runtime = null;
  });

  it('drops a late error from the replaced resume transport and clears the settled subscription', async () => {
    const first = deferred();
    const second = deferred();
    const firstAbort = vi.fn();
    const secondAbort = vi.fn();
    vi.mocked(startResumeStream)
      .mockReturnValueOnce({ abort: firstAbort, promise: first.promise })
      .mockReturnValueOnce({ abort: secondAbort, promise: second.promise });

    const view = render(
      <ChatRuntimeProvider>
        <RuntimeProbe />
      </ChatRuntimeProvider>,
    );
    await loadHistory([waitingRound()]);
    const interrupt = runtime!.getSessionProjection('sess-a').pendingInterrupt!;

    let firstRun!: Promise<void>;
    act(() => {
      firstRun = runtime!.resumeRun('sess-a', interrupt, { 'Continue?': 'Yes' });
    });
    await waitFor(() => expect(startResumeStream).toHaveBeenCalledTimes(1));
    expect(vi.mocked(startResumeStream).mock.calls[0][0]).toMatchObject({
      serverRunId: 'server-r1',
      lastSequence: 7,
    });

    let secondRun!: Promise<void>;
    act(() => {
      secondRun = runtime!.resumeRun('sess-a', interrupt, { 'Continue?': 'Yes' });
    });
    await waitFor(() => expect(startResumeStream).toHaveBeenCalledTimes(2));
    expect(firstAbort).toHaveBeenCalledTimes(1);

    await act(async () => {
      first.reject(new Error('old resume connection failed'));
      await firstRun;
    });
    expect(runtime!.getSessionProjection('sess-a').error).toBe('');
    expect(runtime!.getSessionProjection('sess-a').pendingInterrupt).toBeNull();

    await act(async () => {
      second.resolve();
      await secondRun;
    });
    view.unmount();
    expect(secondAbort).not.toHaveBeenCalled();
  });

  it('clears a failed resume subscription while restoring an unaccepted interaction', async () => {
    const failed = deferred();
    const retry = deferred();
    const failedAbort = vi.fn();
    const retryAbort = vi.fn();
    vi.mocked(startResumeStream)
      .mockReturnValueOnce({ abort: failedAbort, promise: failed.promise })
      .mockReturnValueOnce({ abort: retryAbort, promise: retry.promise });

    const view = render(
      <ChatRuntimeProvider>
        <RuntimeProbe />
      </ChatRuntimeProvider>,
    );
    await loadHistory([waitingRound()]);
    const interrupt = runtime!.getSessionProjection('sess-a').pendingInterrupt!;

    let resume!: Promise<void>;
    act(() => {
      resume = runtime!.resumeRun('sess-a', interrupt, { 'Continue?': 'Yes' });
    });
    await waitFor(() => expect(startResumeStream).toHaveBeenCalledTimes(1));
    await act(async () => {
      failed.reject(new Error('resume rejected'));
      await resume;
    });

    expect(runtime!.getSessionProjection('sess-a').pendingInterrupt?.id).toBe('interaction-1');
    expect(runtime!.getSessionProjection('sess-a').error).toBe('resume rejected');
    expect(runtime!.getSessionProjection('sess-a').rounds).toHaveLength(1);
    expect(runtime!.getSessionProjection('sess-a').rounds[0].status).toBe('waiting_interaction');
    expect(runtime!.state.runs['run:server-r1']).toMatchObject({
      serverRunId: 'server-r1',
      status: 'waiting',
    });

    let retryResume!: Promise<void>;
    act(() => {
      retryResume = runtime!.resumeRun(
        'sess-a',
        runtime!.getSessionProjection('sess-a').pendingInterrupt!,
        { 'Continue?': 'Yes' },
      );
    });
    await waitFor(() => expect(startResumeStream).toHaveBeenCalledTimes(2));
    expect(vi.mocked(startResumeStream).mock.calls[1][0]).toMatchObject({
      serverRunId: 'server-r1',
      lastSequence: 7,
    });
    expect(runtime!.getSessionProjection('sess-a').rounds).toHaveLength(1);
    await act(async () => {
      retry.resolve();
      await retryResume;
    });

    view.unmount();
    expect(failedAbort).not.toHaveBeenCalled();
    expect(retryAbort).not.toHaveBeenCalled();
  });

  it('subscribes a waiting round so another tab can resume it to completion', async () => {
    const waitingSubscription = deferred();
    vi.mocked(startSubscribeStream).mockReturnValue({
      abort: vi.fn(),
      promise: waitingSubscription.promise,
      getLatestSequence: () => 9,
    });
    const view = render(
      <ChatRuntimeProvider>
        <RuntimeProbe />
      </ChatRuntimeProvider>,
    );

    await loadHistory([waitingRound()]);

    expect(startSubscribeStream).toHaveBeenCalledTimes(1);
    const subscribeArgs = vi.mocked(startSubscribeStream).mock.calls[0][0];
    expect(subscribeArgs).toMatchObject({
      ownerSessionId: 'sess-a',
      serverRunId: 'server-r1',
      lastSequence: 7,
      source: 'subscribe',
    });
    expect(runtime!.getSessionProjection('sess-a').pendingInterrupt?.id).toBe('interaction-1');
    expect(runtime!.getSessionProjection('sess-a').sending).toBe(false);

    await act(async () => {
      subscribeArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: subscribeArgs.clientRunKey,
        transportEpoch: subscribeArgs.transportEpoch,
        connectionId: subscribeArgs.connectionId,
        source: 'subscribe',
        sequence: 8,
        receivedAt: Date.now(),
        event: {
          type: 'CUSTOM',
          name: 'interaction_resolved',
          value: { interactionId: 'interaction-1', runId: 'server-r1' },
        },
      });
    });
    expect(runtime!.getSessionProjection('sess-a').sending).toBe(true);
    expect(runtime!.getSessionProjection('sess-a').pendingInterrupt).toBeNull();

    await act(async () => {
      subscribeArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: subscribeArgs.clientRunKey,
        transportEpoch: subscribeArgs.transportEpoch,
        connectionId: subscribeArgs.connectionId,
        source: 'subscribe',
        sequence: 9,
        receivedAt: Date.now(),
        event: {
          type: 'RUN_FINISHED',
          threadId: 'sess-a',
          runId: 'server-r1',
          outcome: 'success',
          result: { finalResponse: 'done' },
        },
      });
      waitingSubscription.resolve();
      await waitingSubscription.promise;
    });

    expect(runtime!.getSessionProjection('sess-a').rounds[0]).toMatchObject({
      status: 'completed',
      final_response: 'done',
    });
    view.unmount();
  });

  it('re-subscribes when a waiting transport rejects without a Round terminal', async () => {
    vi.useFakeTimers();
    try {
      const first = deferred();
      const second = deferred();
      vi.mocked(startSubscribeStream)
        .mockReturnValueOnce({
          abort: vi.fn(),
          promise: first.promise,
          getLatestSequence: () => 7,
        })
        .mockReturnValueOnce({
          abort: vi.fn(),
          promise: second.promise,
          getLatestSequence: () => 7,
        });
      const view = render(
        <ChatRuntimeProvider>
          <RuntimeProbe />
        </ChatRuntimeProvider>,
      );

      await loadHistory([waitingRound()]);
      await act(async () => {
        first.reject(new Error('waiting subscribe disconnected'));
        await Promise.resolve();
      });
      expect(startSubscribeStream).toHaveBeenCalledTimes(1);
      expect(runtime!.getSessionProjection('sess-a').pendingInterrupt?.id).toBe('interaction-1');

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      expect(startSubscribeStream).toHaveBeenCalledTimes(2);
      expect(vi.mocked(startSubscribeStream).mock.calls[1][0]).toMatchObject({
        serverRunId: 'server-r1',
        lastSequence: 7,
      });
      expect(runtime!.getSessionProjection('sess-a').error).toBe('');
      view.unmount();
    } finally {
      vi.useRealTimers();
    }
  });

  it('invalidates a queued waiting retry before a deferred abort completes', async () => {
    vi.useFakeTimers();
    try {
      const failedSubscription = deferred();
      const abortRequest = deferred();
      const subscribeAbort = vi.fn();
      vi.mocked(startSubscribeStream).mockReturnValue({
        abort: subscribeAbort,
        promise: failedSubscription.promise,
        getLatestSequence: () => 7,
      });
      vi.mocked(apiService.abortChat).mockReturnValue(abortRequest.promise as any);
      const view = render(
        <ChatRuntimeProvider>
          <RuntimeProbe />
        </ChatRuntimeProvider>,
      );

      await loadHistory([waitingRound()]);
      await act(async () => {
        failedSubscription.reject(new Error('waiting subscribe disconnected'));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(startSubscribeStream).toHaveBeenCalledTimes(1);

      let stop!: Promise<void>;
      act(() => {
        stop = runtime!.stopSessionRun('sess-a');
      });
      await act(async () => {
        await Promise.resolve();
      });
      expect(subscribeAbort).not.toHaveBeenCalled();
      expect(runtime!.getSessionProjection('sess-a').rounds[0].status).toBe('cancelled');

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      expect(startSubscribeStream).toHaveBeenCalledTimes(1);

      const subscribeArgs = vi.mocked(startSubscribeStream).mock.calls[0][0];
      act(() => {
        subscribeArgs.onEnvelope({
          ownerSessionId: 'sess-a',
          clientRunKey: subscribeArgs.clientRunKey,
          transportEpoch: subscribeArgs.transportEpoch,
          connectionId: subscribeArgs.connectionId,
          source: 'subscribe',
          event: {
            type: 'RUN_FINISHED',
            runId: 'server-r1',
            outcome: 'success',
            result: { finalResponse: 'uncommitted stale success' },
          },
          receivedAt: Date.now(),
        });
      });
      expect(runtime!.getSessionProjection('sess-a').rounds[0].status).toBe('cancelled');

      await act(async () => {
        abortRequest.resolve();
        await stop;
      });
      expect(apiService.abortChat).toHaveBeenCalledWith('sess-a');
      view.unmount();
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not persist abort outcome_warning as runtime presentation state', async () => {
    vi.mocked(apiService.abortChat).mockResolvedValue({
      status: 'cancelled',
      request_id: 'abort-1',
      reason: 'force_aborted',
      outcome_warning: '远端操作可能已经执行，请先确认结果再重试',
    });
    const view = render(
      <ChatRuntimeProvider>
        <RuntimeProbe />
      </ChatRuntimeProvider>,
    );
    await loadHistory([waitingRound({ status: 'running', interrupt: undefined })]);

    await act(async () => {
      await runtime!.stopSessionRun('sess-a');
    });

    expect(runtime!.getSessionProjection('sess-a')).toMatchObject({
      error: '',
    });
    expect(runtime!.getSessionProjection('sess-a')).not.toHaveProperty('warning');
    view.unmount();
  });

  it('stops init polling when the live active-slot reader turns false', async () => {
    vi.useFakeTimers();
    try {
      let activeSlot = true;
      vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
        session_id: 'sess-a',
        rounds: [],
        total: 0,
      });
      const view = render(
        <ChatRuntimeProvider>
          <RuntimeProbe />
        </ChatRuntimeProvider>,
      );

      await act(async () => {
        await runtime!.loadSessionHistory('sess-a', {
          hasActiveSlot: true,
          isActiveSlotCurrent: () => activeSlot,
        });
      });
      expect(runtime!.getActiveSlotSessionIds().has('sess-a')).toBe(true);

      activeSlot = false;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1500);
        await Promise.resolve();
      });
      expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(2);
      expect(runtime!.getActiveSlotSessionIds().has('sess-a')).toBe(false);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(3000);
      });
      expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(2);
      view.unmount();
    } finally {
      vi.useRealTimers();
    }
  });

  it('switches a direct stream to waiting subscription after interaction_requested', async () => {
    const direct = deferred();
    vi.mocked(startSendStream).mockReturnValue({
      abort: vi.fn(),
      promise: direct.promise,
    });
    const view = render(
      <ChatRuntimeProvider>
        <RuntimeProbe />
      </ChatRuntimeProvider>,
    );

    let send!: Promise<void>;
    act(() => {
      send = runtime!.sendMessage({
        sessionId: 'sess-a',
        displayMessage: 'hello',
        content: [{ type: 'text', text: 'hello' }],
      });
    });
    await waitFor(() => expect(startSendStream).toHaveBeenCalledTimes(1));
    const sendArgs = vi.mocked(startSendStream).mock.calls[0][0];
    await act(async () => {
      sendArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: sendArgs.clientRunKey,
        transportEpoch: sendArgs.transportEpoch,
        connectionId: sendArgs.connectionId,
        source: 'direct',
        sequence: 1,
        receivedAt: Date.now(),
        event: {
          type: 'RUN_STARTED',
          threadId: 'sess-a',
          runId: 'server-direct',
        },
      });
      sendArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: sendArgs.clientRunKey,
        transportEpoch: sendArgs.transportEpoch,
        connectionId: sendArgs.connectionId,
        source: 'direct',
        sequence: 2,
        receivedAt: Date.now(),
        event: {
          type: 'CUSTOM',
          name: 'interaction_requested',
          value: {
            interactionId: 'interaction-direct',
            runId: 'server-direct',
            kind: 'user_input',
            payload: { questions: [{ question: 'Continue?' }] },
          },
        },
      });
      direct.resolve();
      await send;
    });

    expect(startSubscribeStream).toHaveBeenCalledTimes(1);
    expect(vi.mocked(startSubscribeStream).mock.calls[0][0]).toMatchObject({
      clientRunKey: sendArgs.clientRunKey,
      serverRunId: 'server-direct',
      lastSequence: 2,
      source: 'subscribe',
    });
    expect(runtime!.getSessionProjection('sess-a').pendingInterrupt?.id).toBe(
      'interaction-direct',
    );
    view.unmount();
  });

  it('restores the rejected draft and adopts history on INTERACTION_PENDING', async () => {
    const direct = deferred();
    const restoreDraft = vi.fn();
    vi.mocked(startSendStream).mockReturnValue({
      abort: vi.fn(),
      promise: direct.promise,
      getHandoff: () => ({
        serverRunId: 'server-waiting',
        status: 'waiting_interaction',
        lastSequence: 7,
      }),
    });
    const view = render(
      <ChatRuntimeProvider>
        <RuntimeProbe />
      </ChatRuntimeProvider>,
    );

    let send!: Promise<void>;
    act(() => {
      send = runtime!.sendMessage({
        sessionId: 'sess-a',
        displayMessage: 'stale draft',
        content: [{ type: 'text', text: 'stale draft' }],
        onRejectedBeforeAccept: restoreDraft,
      });
    });
    await waitFor(() => expect(startSendStream).toHaveBeenCalledTimes(1));
    const sendArgs = vi.mocked(startSendStream).mock.calls[0][0];
    const recoveredRound = waitingRound({
      round_id: 'server-waiting',
      interrupt: {
        id: 'interaction-existing',
        reason: 'input_required',
        payload: { questions: [{ question: 'Existing question?' }] },
      },
    });

    await act(async () => {
      sendArgs.onControlConflict?.(
        '当前 Round 正在等待用户回答',
        'INTERACTION_PENDING',
        'server-waiting',
      );
      sendArgs.onError?.('当前 Round 正在等待用户回答', 'INTERACTION_PENDING');
      sendArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: sendArgs.clientRunKey,
        transportEpoch: sendArgs.transportEpoch,
        connectionId: sendArgs.connectionId,
        source: 'subscribe',
        authoritativeRecovery: true,
        sequence: 7,
        receivedAt: Date.now(),
        event: {
          type: 'RUNTIME_HISTORY_SNAPSHOT',
          rounds: [recoveredRound],
          sequence: 7,
        },
      });
      sendArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: sendArgs.clientRunKey,
        transportEpoch: sendArgs.transportEpoch,
        connectionId: sendArgs.connectionId,
        source: 'subscribe',
        authoritativeRecovery: true,
        sequence: 7,
        receivedAt: Date.now(),
        event: {
          type: 'CUSTOM',
          name: 'interaction_requested',
          value: {
            interactionId: 'interaction-existing',
            runId: 'server-waiting',
            kind: 'user_input',
            payload: { questions: [{ question: 'Existing question?' }] },
          },
        },
      });
      direct.resolve();
      await send;
    });

    const projection = runtime!.getSessionProjection('sess-a');
    expect(restoreDraft).toHaveBeenCalledTimes(1);
    expect(projection.rounds).toHaveLength(1);
    expect(projection.rounds[0]).toMatchObject({
      round_id: 'server-waiting',
      status: 'waiting_interaction',
    });
    expect(projection.pendingInterrupt?.id).toBe('interaction-existing');
    expect(projection.error).toBe('当前 Round 正在等待用户回答');
    expect(projection.sending).toBe(false);
    expect(startSubscribeStream).toHaveBeenCalledTimes(1);
    expect(vi.mocked(startSubscribeStream).mock.calls[0][0]).toMatchObject({
      clientRunKey: sendArgs.clientRunKey,
      serverRunId: 'server-waiting',
      lastSequence: 7,
      source: 'subscribe',
    });
    view.unmount();
  });

  it('takes over a running direct handoff with a subscribe transport', async () => {
    const direct = deferred();
    vi.mocked(startSendStream).mockReturnValue({
      abort: vi.fn(),
      promise: direct.promise,
      getHandoff: () => ({
        serverRunId: 'server-running',
        status: 'running',
        lastSequence: 4,
      }),
    });
    const view = render(
      <ChatRuntimeProvider>
        <RuntimeProbe />
      </ChatRuntimeProvider>,
    );

    let send!: Promise<void>;
    act(() => {
      send = runtime!.sendMessage({
        sessionId: 'sess-a',
        displayMessage: 'hello',
        content: [{ type: 'text', text: 'hello' }],
      });
    });
    await waitFor(() => expect(startSendStream).toHaveBeenCalledTimes(1));
    const sendArgs = vi.mocked(startSendStream).mock.calls[0][0];
    await act(async () => {
      sendArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: sendArgs.clientRunKey,
        transportEpoch: sendArgs.transportEpoch,
        connectionId: sendArgs.connectionId,
        source: 'direct',
        sequence: 1,
        receivedAt: Date.now(),
        event: {
          type: 'RUN_STARTED',
          threadId: 'sess-a',
          runId: 'server-running',
        },
      });
      direct.resolve();
      await send;
    });

    expect(startSubscribeStream).toHaveBeenCalledTimes(1);
    expect(vi.mocked(startSubscribeStream).mock.calls[0][0]).toMatchObject({
      clientRunKey: sendArgs.clientRunKey,
      serverRunId: 'server-running',
      lastSequence: 4,
      source: 'subscribe',
    });
    expect(runtime!.getSessionProjection('sess-a').rounds[0].status).toBe('running');
    view.unmount();
  });

  it('separates accepted active slot from true executing state', async () => {
    const direct = deferred();
    vi.mocked(startSendStream).mockReturnValue({
      abort: vi.fn(),
      promise: direct.promise,
    });
    const view = render(
      <ChatRuntimeProvider>
        <RuntimeProbe />
      </ChatRuntimeProvider>,
    );

    let send!: Promise<void>;
    act(() => {
      send = runtime!.sendMessage({
        sessionId: 'sess-a',
        displayMessage: 'hello',
        content: [{ type: 'text', text: 'hello' }],
      });
    });
    await waitFor(() => expect(startSendStream).toHaveBeenCalledTimes(1));
    expect(view.getByTestId('runtime-probe')).toHaveAttribute('data-active-slot', 'yes');
    expect(view.getByTestId('runtime-probe')).toHaveAttribute('data-executing', 'no');

    const sendArgs = vi.mocked(startSendStream).mock.calls[0][0];
    await act(async () => {
      sendArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: sendArgs.clientRunKey,
        transportEpoch: sendArgs.transportEpoch,
        connectionId: sendArgs.connectionId,
        source: 'direct',
        sequence: 1,
        receivedAt: Date.now(),
        event: {
          type: 'RUN_STARTED',
          threadId: 'sess-a',
          runId: 'server-executing',
        },
      });
    });
    expect(view.getByTestId('runtime-probe')).toHaveAttribute('data-executing', 'yes');

    await act(async () => {
      sendArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: sendArgs.clientRunKey,
        transportEpoch: sendArgs.transportEpoch,
        connectionId: sendArgs.connectionId,
        source: 'direct',
        sequence: 2,
        receivedAt: Date.now(),
        event: {
          type: 'RUN_FINISHED',
          threadId: 'sess-a',
          runId: 'server-executing',
          outcome: 'success',
          result: { finalResponse: 'done' },
        },
      });
      direct.resolve();
      await send;
    });
    view.unmount();
  });

  it('clears stale subscribe retry timer before resume creates another waiting interaction', async () => {
    vi.useFakeTimers();
    try {
      const failedSubscription = Promise.reject(new Error('subscribe failed'));
      const resumed = deferred();
      vi.mocked(startSubscribeStream)
        .mockReturnValueOnce({
          abort: vi.fn(),
          promise: failedSubscription,
        })
        .mockReturnValue({
          abort: vi.fn(),
          promise: Promise.resolve(),
        });
      vi.mocked(startResumeStream).mockReturnValue({
        abort: vi.fn(),
        promise: resumed.promise,
      });
      const view = render(
        <ChatRuntimeProvider>
          <RuntimeProbe />
        </ChatRuntimeProvider>,
      );

      await loadHistory([waitingRound()]);
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(startSubscribeStream).toHaveBeenCalledTimes(1);

      const interrupt = runtime!.getSessionProjection('sess-a').pendingInterrupt!;
      let resume!: Promise<void>;
      act(() => {
        resume = runtime!.resumeRun('sess-a', interrupt, { 'Continue?': 'Yes' });
      });
      await act(async () => {
        await Promise.resolve();
      });
      const resumeArgs = vi.mocked(startResumeStream).mock.calls[0][0];
      await act(async () => {
        resumeArgs.onEnvelope({
          ownerSessionId: 'sess-a',
          clientRunKey: resumeArgs.clientRunKey,
          transportEpoch: resumeArgs.transportEpoch,
          connectionId: resumeArgs.connectionId,
          source: 'resume',
          sequence: 8,
          receivedAt: Date.now(),
          event: {
            type: 'CUSTOM',
            name: 'interaction_requested',
            value: {
              interactionId: 'interaction-next',
              runId: 'server-r1',
              kind: 'user_input',
              payload: { questions: [{ question: 'Next?' }] },
            },
          },
        });
        resumed.resolve();
        await resume;
      });

      expect(startSubscribeStream).toHaveBeenCalledTimes(2);
      expect(vi.mocked(startSubscribeStream).mock.calls[1][0]).toMatchObject({
        serverRunId: 'server-r1',
        lastSequence: 8,
      });
      view.unmount();
    } finally {
      vi.useRealTimers();
    }
  });

  it('restores authoritative waiting state after a 200 SSE prelude error', async () => {
    const resumeTransport = deferred();
    vi.mocked(startResumeStream).mockReturnValue({
      abort: vi.fn(),
      promise: resumeTransport.promise,
    });
    const view = render(
      <ChatRuntimeProvider>
        <RuntimeProbe />
      </ChatRuntimeProvider>,
    );
    await loadHistory([waitingRound()]);
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValueOnce({
      session_id: 'sess-a',
      rounds: [waitingRound()],
      total: 1,
    });

    const interrupt = runtime!.getSessionProjection('sess-a').pendingInterrupt!;
    let resume!: Promise<void>;
    act(() => {
      resume = runtime!.resumeRun('sess-a', interrupt, { 'Continue?': 'Yes' });
    });
    await waitFor(() => expect(startResumeStream).toHaveBeenCalledTimes(1));
    const resumeArgs = vi.mocked(startResumeStream).mock.calls[0][0];
    await act(async () => {
      resumeArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: resumeArgs.clientRunKey,
        transportEpoch: resumeArgs.transportEpoch,
        connectionId: resumeArgs.connectionId,
        source: 'resume',
        receivedAt: Date.now(),
        event: { type: 'CUSTOM', name: 'stream_accepted', value: {} },
      });
      resumeArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: resumeArgs.clientRunKey,
        transportEpoch: resumeArgs.transportEpoch,
        connectionId: resumeArgs.connectionId,
        source: 'resume',
        receivedAt: Date.now(),
        event: {
          type: 'RUN_ERROR',
          message: 'No pending interaction',
          code: 'NO_PENDING_INTERRUPT',
        },
      });
      resumeTransport.resolve();
      await resume;
    });

    expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(2);
    expect(runtime!.getSessionProjection('sess-a').rounds[0].status).toBe('waiting_interaction');
    expect(runtime!.getSessionProjection('sess-a').pendingInterrupt?.id).toBe('interaction-1');
    expect(runtime!.state.runs['run:server-r1'].status).toBe('waiting');
    expect(runtime!.getSessionProjection('sess-a').error).toBe('No pending interaction');
    view.unmount();
  });

  it('keeps an authoritative terminal when the resume reader rejects afterward', async () => {
    const resumeTransport = deferred();
    vi.mocked(startResumeStream).mockReturnValue({
      abort: vi.fn(),
      promise: resumeTransport.promise,
    });
    const view = render(
      <ChatRuntimeProvider>
        <RuntimeProbe />
      </ChatRuntimeProvider>,
    );
    await loadHistory([waitingRound()]);

    const interrupt = runtime!.getSessionProjection('sess-a').pendingInterrupt!;
    let resume!: Promise<void>;
    act(() => {
      resume = runtime!.resumeRun('sess-a', interrupt, { 'Continue?': 'Yes' });
    });
    await waitFor(() => expect(startResumeStream).toHaveBeenCalledTimes(1));
    const resumeArgs = vi.mocked(startResumeStream).mock.calls[0][0];
    await act(async () => {
      resumeArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: resumeArgs.clientRunKey,
        transportEpoch: resumeArgs.transportEpoch,
        connectionId: resumeArgs.connectionId,
        source: 'subscribe',
        authoritativeRecovery: true,
        sequence: 8,
        receivedAt: Date.now(),
        event: {
          type: 'RUN_ERROR',
          threadId: 'sess-a',
          runId: 'server-r1',
          message: 'provider failed after continuation',
          code: 'RUN_FAILED',
          sequence: 8,
        },
      });
      resumeTransport.reject(new Error('reader failed after durable terminal'));
      await resume;
    });

    expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(1);
    expect(runtime!.getSessionProjection('sess-a').pendingInterrupt).toBeNull();
    expect(runtime!.getSessionProjection('sess-a').rounds[0]).toMatchObject({
      status: 'failed',
      final_response: 'provider failed after continuation',
    });
    expect(runtime!.state.runs['run:server-r1'].status).toBe('error');
    view.unmount();
  });

  it('keeps the continuation running and re-subscribes from the resolved cursor when history fails', async () => {
    const resumeTransport = deferred();
    vi.mocked(startResumeStream).mockReturnValue({
      abort: vi.fn(),
      promise: resumeTransport.promise,
    });
    const view = render(
      <ChatRuntimeProvider>
        <RuntimeProbe />
      </ChatRuntimeProvider>,
    );
    await loadHistory([waitingRound()]);
    vi.mocked(apiService.getSessionHistoryV2).mockRejectedValueOnce(new Error('history unavailable'));

    const interrupt = runtime!.getSessionProjection('sess-a').pendingInterrupt!;
    let resume!: Promise<void>;
    act(() => {
      resume = runtime!.resumeRun('sess-a', interrupt, { 'Continue?': 'Yes' });
    });
    await waitFor(() => expect(startResumeStream).toHaveBeenCalledTimes(1));
    const resumeArgs = vi.mocked(startResumeStream).mock.calls[0][0];
    await act(async () => {
      resumeArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: resumeArgs.clientRunKey,
        transportEpoch: resumeArgs.transportEpoch,
        connectionId: resumeArgs.connectionId,
        source: 'resume',
        receivedAt: Date.now(),
        event: { type: 'CUSTOM', name: 'stream_accepted', value: {} },
      });
      resumeArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: resumeArgs.clientRunKey,
        transportEpoch: resumeArgs.transportEpoch,
        connectionId: resumeArgs.connectionId,
        source: 'resume',
        sequence: 8,
        receivedAt: Date.now(),
        event: {
          type: 'CUSTOM',
          name: 'interaction_resolved',
          value: { interactionId: 'interaction-1', runId: 'server-r1' },
        },
      });
      resumeTransport.reject(new Error('resume connection dropped'));
      await resume;
    });

    const projection = runtime!.getSessionProjection('sess-a');
    expect(projection.pendingInterrupt).toBeNull();
    expect(projection.rounds[0].status).toBe('running');
    expect(runtime!.state.runs['run:server-r1'].status).toBe('streaming');
    expect(startSubscribeStream).toHaveBeenCalledTimes(2);
    expect(vi.mocked(startSubscribeStream).mock.calls[1][0]).toMatchObject({
      serverRunId: 'server-r1',
      lastSequence: 8,
    });
    view.unmount();
  });

  it('treats authoritative running history as a started continuation when resolved was missed', async () => {
    const resumeTransport = deferred();
    vi.mocked(startResumeStream).mockReturnValue({
      abort: vi.fn(),
      promise: resumeTransport.promise,
    });
    const view = render(
      <ChatRuntimeProvider>
        <RuntimeProbe />
      </ChatRuntimeProvider>,
    );
    await loadHistory([waitingRound()]);
    vi.mocked(apiService.getSessionHistoryV2).mockRejectedValueOnce(new Error('history unavailable'));

    const interrupt = runtime!.getSessionProjection('sess-a').pendingInterrupt!;
    let resume!: Promise<void>;
    act(() => {
      resume = runtime!.resumeRun('sess-a', interrupt, { 'Continue?': 'Yes' });
    });
    await waitFor(() => expect(startResumeStream).toHaveBeenCalledTimes(1));
    const resumeArgs = vi.mocked(startResumeStream).mock.calls[0][0];
    const runningRound = waitingRound({
      status: 'running',
      interrupt: undefined,
      last_event_sequence: 8,
    });
    await act(async () => {
      resumeArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: resumeArgs.clientRunKey,
        transportEpoch: resumeArgs.transportEpoch,
        connectionId: resumeArgs.connectionId,
        source: 'resume',
        receivedAt: Date.now(),
        event: { type: 'CUSTOM', name: 'stream_accepted', value: {} },
      });
      resumeArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: resumeArgs.clientRunKey,
        transportEpoch: resumeArgs.transportEpoch,
        connectionId: resumeArgs.connectionId,
        source: 'subscribe',
        authoritativeRecovery: true,
        sequence: 8,
        receivedAt: Date.now(),
        event: {
          type: 'RUNTIME_HISTORY_SNAPSHOT',
          rounds: [runningRound],
          sequence: 8,
        },
      });
      resumeTransport.reject(new Error('recovered subscribe failed'));
      await resume;
    });

    const projection = runtime!.getSessionProjection('sess-a');
    expect(projection.pendingInterrupt).toBeNull();
    expect(projection.rounds[0].status).toBe('running');
    expect(runtime!.state.runs['run:server-r1'].status).toBe('streaming');
    expect(startSubscribeStream).toHaveBeenCalledTimes(2);
    expect(vi.mocked(startSubscribeStream).mock.calls[1][0]).toMatchObject({
      serverRunId: 'server-r1',
      lastSequence: 8,
    });
    view.unmount();
  });

  it('keeps a newly requested interaction when resume disconnects and history also fails', async () => {
    const resumeTransport = deferred();
    vi.mocked(startResumeStream).mockReturnValue({
      abort: vi.fn(),
      promise: resumeTransport.promise,
    });
    const view = render(
      <ChatRuntimeProvider>
        <RuntimeProbe />
      </ChatRuntimeProvider>,
    );
    await loadHistory([waitingRound()]);
    vi.mocked(apiService.getSessionHistoryV2).mockRejectedValueOnce(new Error('history unavailable'));

    const interrupt = runtime!.getSessionProjection('sess-a').pendingInterrupt!;
    let resume!: Promise<void>;
    act(() => {
      resume = runtime!.resumeRun('sess-a', interrupt, { 'Continue?': 'Yes' });
    });
    await waitFor(() => expect(startResumeStream).toHaveBeenCalledTimes(1));
    const resumeArgs = vi.mocked(startResumeStream).mock.calls[0][0];
    await act(async () => {
      resumeArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: resumeArgs.clientRunKey,
        transportEpoch: resumeArgs.transportEpoch,
        connectionId: resumeArgs.connectionId,
        source: 'resume',
        receivedAt: Date.now(),
        event: { type: 'CUSTOM', name: 'stream_accepted', value: {} },
      });
      resumeArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: resumeArgs.clientRunKey,
        transportEpoch: resumeArgs.transportEpoch,
        connectionId: resumeArgs.connectionId,
        source: 'resume',
        sequence: 8,
        receivedAt: Date.now(),
        event: {
          type: 'CUSTOM',
          name: 'interaction_requested',
          value: {
            interactionId: 'interaction-2',
            runId: 'server-r1',
            kind: 'user_input',
            payload: { questions: [{ question: 'One more thing?' }] },
          },
        },
      });
      resumeTransport.reject(new Error('resume connection dropped'));
      await resume;
    });

    expect(runtime!.getSessionProjection('sess-a').rounds[0].status).toBe('waiting_interaction');
    expect(runtime!.getSessionProjection('sess-a').pendingInterrupt?.id).toBe('interaction-2');
    expect(runtime!.state.runs['run:server-r1'].status).toBe('waiting');
    expect(startSubscribeStream).toHaveBeenCalledTimes(2);
    expect(vi.mocked(startSubscribeStream).mock.calls[1][0]).toMatchObject({
      serverRunId: 'server-r1',
      lastSequence: 8,
    });
    view.unmount();
  });

  it('does not let an in-flight running history snapshot erase a newer interaction event', async () => {
    const resumeTransport = deferred();
    vi.mocked(startResumeStream).mockReturnValue({
      abort: vi.fn(),
      promise: resumeTransport.promise,
    });
    const view = render(
      <ChatRuntimeProvider>
        <RuntimeProbe />
      </ChatRuntimeProvider>,
    );
    await loadHistory([waitingRound()]);

    const staleHistory = deferredValue<any>();
    vi.mocked(apiService.getSessionHistoryV2).mockReturnValueOnce(staleHistory.promise);
    let historyRequest!: Promise<void>;
    act(() => {
      historyRequest = runtime!.loadSessionHistory('sess-a');
    });

    const originalInterrupt = runtime!.getSessionProjection('sess-a').pendingInterrupt!;
    let resume!: Promise<void>;
    act(() => {
      resume = runtime!.resumeRun('sess-a', originalInterrupt, { 'Continue?': 'Yes' });
    });
    await waitFor(() => expect(startResumeStream).toHaveBeenCalledTimes(1));
    const resumeArgs = vi.mocked(startResumeStream).mock.calls[0][0];
    await act(async () => {
      resumeArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: resumeArgs.clientRunKey,
        transportEpoch: resumeArgs.transportEpoch,
        connectionId: resumeArgs.connectionId,
        source: 'resume',
        sequence: 8,
        receivedAt: Date.now(),
        event: {
          type: 'CUSTOM',
          name: 'interaction_requested',
          value: {
            interactionId: 'interaction-2',
            runId: 'server-r1',
            kind: 'user_input',
            payload: { questions: [{ question: 'One more thing?' }] },
          },
        },
      });
    });

    staleHistory.resolve({
      session_id: 'sess-a',
      rounds: [waitingRound({
        status: 'running',
        interrupt: undefined,
        last_event_sequence: 7,
      })],
      total: 1,
    });
    await act(async () => {
      await historyRequest;
    });

    const projection = runtime!.getSessionProjection('sess-a');
    expect(projection.pendingInterrupt?.id).toBe('interaction-2');
    expect(projection.rounds[0].status).toBe('waiting_interaction');
    expect(projection.activeRunKeys).toEqual([]);

    await act(async () => {
      resumeTransport.resolve();
      await resume;
    });
    view.unmount();
  });

  it('clears a stale terminal marker when history authoritatively revives the run', async () => {
    const failedResume = deferred();
    vi.mocked(startResumeStream).mockReturnValue({
      abort: vi.fn(),
      promise: failedResume.promise,
    });
    const view = render(
      <ChatRuntimeProvider>
        <RuntimeProbe />
      </ChatRuntimeProvider>,
    );
    await loadHistory([waitingRound()]);
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValueOnce({
      session_id: 'sess-a',
      rounds: [waitingRound({
        status: 'running',
        interrupt: undefined,
        last_event_sequence: 8,
      })],
      total: 1,
    });

    const interrupt = runtime!.getSessionProjection('sess-a').pendingInterrupt!;
    let resume!: Promise<void>;
    act(() => {
      resume = runtime!.resumeRun('sess-a', interrupt, { 'Continue?': 'Yes' });
    });
    await waitFor(() => expect(startResumeStream).toHaveBeenCalledTimes(1));
    const resumeArgs = vi.mocked(startResumeStream).mock.calls[0][0];
    await act(async () => {
      resumeArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: resumeArgs.clientRunKey,
        transportEpoch: resumeArgs.transportEpoch,
        connectionId: resumeArgs.connectionId,
        source: 'resume',
        receivedAt: Date.now(),
        event: { type: 'CUSTOM', name: 'stream_accepted', value: {} },
      });
      resumeArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: resumeArgs.clientRunKey,
        transportEpoch: resumeArgs.transportEpoch,
        connectionId: resumeArgs.connectionId,
        source: 'resume',
        receivedAt: Date.now(),
        event: {
          type: 'RUN_ERROR',
          message: 'resume state unknown',
          code: 'RESUME_STATUS_UNKNOWN',
        },
      });
      failedResume.reject(new Error('resume transport lost'));
      await resume;
    });

    expect(runtime!.getSessionProjection('sess-a').sending).toBe(true);
    expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(2);

    vi.mocked(apiService.abortChat).mockRejectedValueOnce(new Error('abort network failure'));
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValueOnce({
      session_id: 'sess-a',
      rounds: [waitingRound({
        status: 'running',
        interrupt: undefined,
        last_event_sequence: 8,
      })],
      total: 1,
    });
    await act(async () => {
      await runtime!.stopSessionRun('sess-a');
    });

    expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(3);
    expect(runtime!.getSessionProjection('sess-a').error).toBe(
      '停止请求失败，后端任务可能仍在运行',
    );
    view.unmount();
  });

  it('releases a settled terminal transport and safely recreates it from history', async () => {
    const terminalResume = deferred();
    vi.mocked(startResumeStream).mockReturnValue({
      abort: vi.fn(),
      promise: terminalResume.promise,
    });
    const view = render(
      <ChatRuntimeProvider>
        <RuntimeProbe />
      </ChatRuntimeProvider>,
    );
    await loadHistory([waitingRound()]);

    const interrupt = runtime!.getSessionProjection('sess-a').pendingInterrupt!;
    let resume!: Promise<void>;
    act(() => {
      resume = runtime!.resumeRun('sess-a', interrupt, { 'Continue?': 'Yes' });
    });
    await waitFor(() => expect(startResumeStream).toHaveBeenCalledTimes(1));
    const oldResumeArgs = vi.mocked(startResumeStream).mock.calls[0][0];
    await act(async () => {
      oldResumeArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: oldResumeArgs.clientRunKey,
        transportEpoch: oldResumeArgs.transportEpoch,
        connectionId: oldResumeArgs.connectionId,
        source: 'resume',
        sequence: 8,
        receivedAt: Date.now(),
        event: {
          type: 'RUN_FINISHED',
          threadId: 'sess-a',
          runId: 'server-r1',
          outcome: 'success',
          result: { finalResponse: 'done' },
        },
      });
      terminalResume.resolve();
      await resume;
    });

    expect(runtime!.getSessionProjection('sess-a').activeRunKeys).toEqual([]);
    expect(runtime!.state.runs['run:server-r1'].status).toBe('finished');
    await act(async () => {
      oldResumeArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: oldResumeArgs.clientRunKey,
        transportEpoch: oldResumeArgs.transportEpoch,
        connectionId: oldResumeArgs.connectionId,
        source: 'resume',
        sequence: 9,
        receivedAt: Date.now(),
        event: { type: 'RUN_ERROR', message: 'late stale error' },
      });
    });
    expect(runtime!.getSessionProjection('sess-a').error).toBe('');
    expect(runtime!.state.runs['run:server-r1'].debugMetadata?.droppedAfterTerminal).toBeUndefined();

    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValueOnce({
      session_id: 'sess-a',
      rounds: [waitingRound({
        status: 'running',
        interrupt: undefined,
        last_event_sequence: 8,
      })],
      total: 1,
    });
    await act(async () => {
      await runtime!.loadSessionHistory('sess-a');
    });

    expect(startSubscribeStream).toHaveBeenCalledTimes(2);
    const revivedArgs = vi.mocked(startSubscribeStream).mock.calls[1][0];
    expect(revivedArgs.transportEpoch).toBe(1);
    expect(revivedArgs.connectionId).not.toBe(oldResumeArgs.connectionId);
    expect(runtime!.getSessionProjection('sess-a').rounds).toHaveLength(1);

    await act(async () => {
      oldResumeArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: oldResumeArgs.clientRunKey,
        transportEpoch: oldResumeArgs.transportEpoch,
        connectionId: oldResumeArgs.connectionId,
        source: 'resume',
        sequence: 9,
        receivedAt: Date.now(),
        event: { type: 'RUN_ERROR', message: 'late error after revival' },
      });
      revivedArgs.onEnvelope({
        ownerSessionId: 'sess-a',
        clientRunKey: revivedArgs.clientRunKey,
        transportEpoch: revivedArgs.transportEpoch,
        connectionId: revivedArgs.connectionId,
        source: 'subscribe',
        sequence: 9,
        receivedAt: Date.now(),
        event: {
          type: 'CUSTOM',
          name: 'interaction_requested',
          value: {
            interactionId: 'interaction-after-revival',
            runId: 'server-r1',
            kind: 'user_input',
            payload: { questions: [{ question: 'Continue again?' }] },
          },
        },
      });
    });

    expect(runtime!.getSessionProjection('sess-a').error).toBe('');
    expect(runtime!.getSessionProjection('sess-a').pendingInterrupt?.id).toBe(
      'interaction-after-revival',
    );
    view.unmount();
  });

  it('keeps the live transport when a terminal history snapshot is behind its sequence', async () => {
    const liveResume = deferred();
    vi.mocked(startResumeStream).mockReturnValue({
      abort: vi.fn(),
      promise: liveResume.promise,
    });
    const view = render(
      <ChatRuntimeProvider>
        <RuntimeProbe />
      </ChatRuntimeProvider>,
    );
    await loadHistory([waitingRound()]);

    const interrupt = runtime!.getSessionProjection('sess-a').pendingInterrupt!;
    let resume!: Promise<void>;
    act(() => {
      resume = runtime!.resumeRun('sess-a', interrupt, { 'Continue?': 'Yes' });
    });
    await waitFor(() => expect(startResumeStream).toHaveBeenCalledTimes(1));
    const liveArgs = vi.mocked(startResumeStream).mock.calls[0][0];
    const emitLive = (event: any, sequence: number) => liveArgs.onEnvelope({
      ownerSessionId: 'sess-a',
      clientRunKey: liveArgs.clientRunKey,
      transportEpoch: liveArgs.transportEpoch,
      connectionId: liveArgs.connectionId,
      source: 'resume',
      sequence,
      receivedAt: Date.now(),
      event,
    });
    await act(async () => {
      emitLive({
        type: 'CUSTOM',
        name: 'interaction_resolved',
        value: { interactionId: 'interaction-1', runId: 'server-r1' },
      }, 10);
    });

    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValueOnce({
      session_id: 'sess-a',
      rounds: [waitingRound({
        status: 'completed',
        interrupt: undefined,
        final_response: '',
        completed_at: undefined,
        last_event_sequence: 9,
      })],
      total: 1,
    });
    await act(async () => {
      await runtime!.loadSessionHistory('sess-a');
    });
    expect(runtime!.state.runs['run:server-r1'].status).toBe('streaming');

    await act(async () => {
      emitLive({
        type: 'CUSTOM',
        name: 'interaction_requested',
        value: {
          interactionId: 'interaction-newer-than-history',
          runId: 'server-r1',
          kind: 'user_input',
          payload: { questions: [{ question: 'Still connected?' }] },
        },
      }, 11);
    });
    expect(runtime!.getSessionProjection('sess-a').pendingInterrupt?.id).toBe(
      'interaction-newer-than-history',
    );

    await act(async () => {
      liveResume.resolve();
      await resume;
    });
    view.unmount();
  });

});
