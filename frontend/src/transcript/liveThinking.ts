import type { ChatRunRuntimeState } from '../runtime/chatRuntimeTypes';
import type { RoundData } from '../types';

/** A transient status preview must be backed by an open live segment, never history prose. */
export function selectLiveThinking(
  round: RoundData,
  run?: ChatRunRuntimeState,
  answering = false,
): { id: string; text: string } | null {
  if (round.status !== 'running' || !run || run.status !== 'streaming'
    || run.localOutputStopped || answering) return null;
  const textId = run.buffers.currentTextMessageId;
  if (textId && run.buffers.textSegmentStateByMessageId[textId]?.open
    && run.buffers.textByMessageId[textId]?.trim()) return null;
  const id = run.buffers.currentThinkingMessageId;
  if (!id || !run.buffers.thinkingSegmentStateByMessageId[id]?.open) return null;
  const text = run.buffers.thinkingByMessageId[id];
  return text?.trim() ? { id, text } : null;
}
