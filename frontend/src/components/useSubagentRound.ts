import { useCallback, useEffect, useRef, useState } from 'react';
import { apiService } from '../services/api';
import { startSubscribeStream, type RuntimeSubscription } from '../services/chatStreamClient';
import { chatRuntimeReducer } from '../runtime/chatRuntimeReducer';
import { initialChatRuntimeState, RUNTIME_HISTORY_SNAPSHOT } from '../runtime/chatRuntimeTypes';
import type { RoundData, SubagentTask } from '../types';
import { extractErrorMessage } from '../utils/errorMessages';

const terminalStatuses = new Set(['completed', 'failed', 'cancelled', 'max_steps_reached']);

interface SubagentRoundView {
  identity: string;
  round: RoundData | null;
  loading: boolean;
  error: string;
}

/** A read-only child transport. Its reducer never enters the parent runtime. */
export function useSubagentRound(
  sessionId: string | null | undefined,
  task: SubagentTask | null | undefined,
  active = true,
) {
  const childRunId = task?.child_run_id;
  const parentRunId = task?.parent_run_id;
  const identity = sessionId && task
    ? `${sessionId}:${task.edge_id}:${childRunId || 'requested'}` : '';
  const [view, setView] = useState<SubagentRoundView>({ identity: '', round: null, loading: false, error: '' });
  const [attempt, setAttempt] = useState(0);
  const epochRef = useRef(0);
  const retry = useCallback(() => setAttempt((value) => value + 1), []);

  useEffect(() => {
    if (!active || !sessionId || !childRunId) return;
    const epoch = ++epochRef.current;
    const controller = new AbortController();
    const clientRunKey = `subagent:${identity}`;
    let subscription: RuntimeSubscription | undefined;
    let disposed = false;
    const isCurrent = () => !disposed && !controller.signal.aborted && epoch === epochRef.current;
    setView((previous) => ({
      identity, round: previous.identity === identity ? previous.round : null, loading: true, error: '',
    }));

    const load = async () => {
      try {
        const snapshot = await apiService.getRoundSnapshot(sessionId, childRunId, controller.signal);
        if (!isCurrent()) return;
        if (snapshot.round_id !== childRunId || (snapshot.parent_run_id && snapshot.parent_run_id !== parentRunId)) {
          throw new Error('子任务详情与当前任务不匹配');
        }
        let runtime = chatRuntimeReducer(initialChatRuntimeState, {
          type: 'LOCAL_RUN_STARTED', sessionId, clientRunKey,
          tempRoundId: childRunId, source: 'subscribe', round: snapshot,
        });
        runtime = chatRuntimeReducer(runtime, {
          type: 'HISTORY_LOADED', sessionId, rounds: [snapshot], loadedAt: Date.now(), source: 'history',
        });
        const publish = () => {
          if (!isCurrent()) return;
          setView({ identity, round: runtime.sessions[sessionId]?.rounds.find((round) => round.round_id === childRunId) || null,
            loading: false, error: '' });
        };
        publish();
        if (terminalStatuses.has(snapshot.status)) return;
        subscription = startSubscribeStream({
          ownerSessionId: sessionId, clientRunKey, serverRunId: childRunId,
          transportEpoch: epoch, connectionId: `${clientRunKey}:${epoch}`,
          source: 'subscribe', snapshotScope: 'round', lastSequence: snapshot.last_event_sequence || 0,
          durableInteractionObserved: snapshot.status === 'waiting_interaction',
          onEnvelope: (envelope) => {
            if (!isCurrent()) return;
            if (envelope.ownerSessionId !== sessionId || envelope.clientRunKey !== clientRunKey
              || (envelope.serverRunId && envelope.serverRunId !== childRunId)) return;
            if (envelope.event.type === RUNTIME_HISTORY_SNAPSHOT
              && envelope.event.rounds.some((round: RoundData) => round.round_id !== childRunId)) return;
            runtime = chatRuntimeReducer(runtime, { type: 'STREAM_EVENT', envelope });
            publish();
          },
        });
        await subscription.promise;
      } catch (error) {
        if (!isCurrent()) return;
        const message = extractErrorMessage(error) || '无法加载子任务详情';
        setView((previous) => ({ ...previous, identity, loading: false, error: message }));
      }
    };
    void load();
    return () => {
      disposed = true;
      controller.abort();
      // Unmounting stops observation, not execution; never abortChat here.
      subscription?.abort();
    };
  }, [active, sessionId, childRunId, parentRunId, identity, attempt]);

  const sameTask = view.identity === identity;
  return {
    round: sameTask ? view.round : null,
    loading: active && Boolean(sessionId && childRunId) && (!sameTask || view.loading),
    error: sameTask ? view.error : '',
    retry,
  };
}
