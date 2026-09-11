import type { RoundData } from '../types';

/** A terminal snapshot cannot retroactively erase an old live-only message prefix. */
export function preserveUncommittedMessages(local: RoundData | undefined, server: RoundData): RoundData {
  if (!local?.assistant_messages || !server.assistant_messages) return server;
  const terminal = ['completed', 'failed', 'cancelled', 'max_steps_reached'].includes(server.status);
  let messages = server.assistant_messages;
  let changed = false;
  for (const message of local.assistant_messages) {
    if (message.content_committed || !message.content) continue;
    const matching = messages.find((item) => item.message_id === message.message_id);
    // Only the same message's committed END (including a failover END) covers its
    // old ephemeral prefix. A later unrelated event's watermark is insufficient.
    if (matching?.content_committed && matching.state !== 'streaming') continue;
    const retained = { ...message, phase: matching?.phase ?? message.phase,
      state: terminal ? 'interrupted' as const : message.state };
    messages = matching ? messages.map((item) => item === matching ? retained : item) : [...messages, retained];
    changed = true;
  }
  if (!changed) return server;
  messages = [...messages].sort((a, b) => (a.first_sequence || 0) - (b.first_sequence || 0));
  return { ...server, assistant_messages: messages };
}
