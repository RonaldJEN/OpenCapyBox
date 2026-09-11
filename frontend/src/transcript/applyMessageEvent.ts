import type { AssistantMessageData, RoundData } from '../types';
import type { StreamEnvelope } from '../runtime/chatRuntimeTypes';
const messagePhase = (value: unknown) => value === 'commentary' || value === 'final_answer' ? value : undefined;

/** Canonical message facts live on the existing Round in ChatRuntime. */
export function applyMessageEvent(round: RoundData, envelope: StreamEnvelope): RoundData {
  const { event, sequence } = envelope;
  const kind = event.type;
  if (kind === 'RUN_STARTED') return { ...round, started_at_ts: event.timestamp };
  const messages = round.assistant_messages || [];
  const step = round.steps[round.steps.length - 1]?.step_number || 1;
  if (kind === 'TEXT_MESSAGE_START' && event.role && event.role !== 'assistant') {
    return { ...round, assistant_messages: round.assistant_messages || [] };
  }
  if (kind === 'TEXT_MESSAGE_START' && event.messageId && (event.role || 'assistant') === 'assistant') {
    if (messages.some((message) => message.message_id === event.messageId)) return round;
    return { ...round,
      transcript_coverage: !round.assistant_messages && round.steps.some((item) => item.assistant_content)
        ? { kind: 'legacy', durable_through_sequence: sequence || 0 } : round.transcript_coverage,
      assistant_messages: [...messages, {
      message_id: event.messageId, step_number: step, content: '', state: 'streaming',
      phase: messagePhase(event.phase),
      first_sequence: sequence, last_sequence: sequence, content_committed: true,
    }] };
  }
  if (!round.assistant_messages) {
    return kind === 'RUN_ERROR' || kind === 'RUN_FINISHED' ? { ...round, finished_at_ts: event.timestamp } : round;
  }
  const update = (predicate: (message: AssistantMessageData) => boolean, change: (message: AssistantMessageData) => AssistantMessageData) => ({
    ...round, assistant_messages: messages.map((message) => predicate(message) ? change(message) : message),
  });
  if (kind === 'TEXT_MESSAGE_CONTENT') {
    return update((message) => message.message_id === event.messageId, (message) => ({ ...message,
      content: envelope.isAggregate ? event.delta || '' : message.content + (event.delta || ''),
      last_sequence: sequence ?? message.last_sequence, content_committed: typeof sequence === 'number',
    }));
  }
  if (kind === 'TEXT_MESSAGE_END') {
    return update((message) => message.message_id === event.messageId, (message) => ({ ...message,
      state: event.interrupted === true ? 'interrupted' : 'complete',
      phase: messagePhase(event.phase) ?? message.phase,
      last_sequence: sequence ?? message.last_sequence,
      content_committed: typeof sequence === 'number' || message.content_committed,
    }));
  }
  if (kind === 'CUSTOM' && event.name === 'failover_reset') {
    return update((message) => message === messages[messages.length - 1] && message.step_number === step,
      (message) => ({ ...message, state: 'interrupted' }));
  }
  if (kind === 'RUN_ERROR' || kind === 'RUN_FINISHED') {
    const finalIds = Array.isArray(event.finalMessageIds)
      ? event.finalMessageIds.filter((id: unknown): id is string => typeof id === 'string') : undefined;
    return { ...update((message) => message.state === 'streaming', (message) => ({ ...message, state: 'interrupted' })),
      final_message_id: event.finalMessageId ?? (finalIds?.length ? null : round.final_message_id),
      final_message_ids: finalIds ?? (event.finalMessageId ? null : round.final_message_ids),
      finished_at_ts: event.timestamp };
  }
  return round;
}
