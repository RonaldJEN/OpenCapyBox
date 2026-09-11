import type { RoundData } from '../types';

export interface TranscriptTextNode {
  id: string;
  text: string;
  streaming: boolean;
  messageId?: string;
  interrupted?: boolean;
  sequence?: number;
  stepNumber?: number;
  phase?: 'commentary' | 'final_answer';
  /** The step is tied to a successful explicit present_files receipt. */
  delivery?: boolean;
}

export function roundRenderKey(round: RoundData): string {
  return round.idempotency_key || round.round_id;
}

// Only the legacy cancellation protocol owns this sentinel. Text events do not.
function legacyCancellation(text: string, round: RoundData): boolean {
  return round.status === 'cancelled' && text.trim() === 'Cancelled';
}

/** Legacy step projection: preserves supplied text, not unavailable message boundaries. */
export function projectRoundTranscript(round: RoundData, streaming = false) {
  const key = roundRenderKey(round);
  const nodes: TranscriptTextNode[] = [];
  const presentedTools = new Set((round.assistant_file_references || [])
    .filter((reference) => reference.operation === 'PRESENTED' && reference.tool_call_id)
    .map((reference) => reference.tool_call_id));
  const deliverySteps = new Set(round.steps
    .filter((step) => step.tool_calls.some((call) => call.name === 'present_files' && call.id && presentedTools.has(call.id)))
    .map((step) => step.step_number));
  const messageView = round.assistant_messages != null && round.transcript_coverage?.kind !== 'legacy';
  if (messageView) {
    for (const message of round.assistant_messages!) {
      if (!message.content.trim() || message.state === 'superseded') continue;
      nodes.push({ id: `${key}:message:${message.message_id}`, messageId: message.message_id,
        sequence: message.first_sequence ?? undefined, stepNumber: message.step_number,
        phase: message.phase || undefined, delivery: deliverySteps.has(message.step_number),
        text: message.content, streaming: streaming && message.state === 'streaming',
        interrupted: message.state === 'interrupted' });
    }
  } else for (const step of round.steps) {
    const text = step.assistant_content || '';
    if (!text.trim()) continue;
    if (!step.assistant_content_source && legacyCancellation(text, round)) continue;
    nodes.push({
      id: `${key}:step:${step.step_number}:text`, text,
      stepNumber: step.step_number,
      delivery: deliverySteps.has(step.step_number),
      streaming: streaming && step === round.steps[round.steps.length - 1] && step.status === 'streaming',
    });
  }

  const metadata = round.terminal_presentation;
  const finalText = round.final_response || '';
  const origin = metadata?.final_response_origin || 'unknown';
  const isLegacyFinal = origin === 'unknown'
    && round.status !== 'failed' && round.status !== 'max_steps_reached';
  const isAssistant = origin === 'assistant' || isLegacyFinal;
  const finalNodeIds = new Set<string>();
  const settled = ['completed', 'failed', 'cancelled', 'max_steps_reached'].includes(round.status);
  const aliasIds = round.final_message_ids?.length ? round.final_message_ids : round.final_message_id ? [round.final_message_id] : [];
  if (settled) for (const node of nodes) {
    if (node.messageId && aliasIds.includes(node.messageId) && !node.interrupted) finalNodeIds.add(node.id);
  }
  let aggregateAnswerId: string | undefined;
  let notice: { label: string; text: string } | undefined;
  if (finalText.trim() && !(origin !== 'assistant' && legacyCancellation(finalText, round))) {
    if (isAssistant) {
      // The only text-based de-duplication: final_response aliases the last legacy step.
      const finalAliasesMessages = aliasIds.length > 0 && aliasIds.every((id) => nodes.some((node) => node.messageId === id));
      if (!finalAliasesMessages && nodes[nodes.length - 1]?.text !== finalText) {
        nodes.push({ id: `${key}:final-fallback`, text: finalText, streaming: false });
      }
      const last = nodes[nodes.length - 1];
      if (last?.text === finalText && !last.interrupted) {
        finalNodeIds.add(last.id);
        if (!finalAliasesMessages && aliasIds.length > 0) aggregateAnswerId = last.id;
      }
    } else if (origin !== 'run_error') {
      notice = {
        label: origin === 'system_notice' ? '运行说明' : '运行结束说明（旧版记录）',
        text: finalText,
      };
    }
  }
  const error = metadata?.error?.message
    || (origin === 'run_error' ? finalText : undefined);
  // A partially restored alias list must not copy a known chunk again alongside
  // the complete authoritative fallback. Keep that chunk in the process history.
  const coveredByAggregate = (node: TranscriptTextNode) => Boolean(aggregateAnswerId
    && node.id !== aggregateAnswerId && node.messageId && aliasIds.includes(node.messageId));
  for (const node of nodes) if (coveredByAggregate(node)) finalNodeIds.delete(node.id);
  // This answer set owns both default display and the main reply copy action.
  // Progress/failed attempts remain in nodes for review, never in answer copy.
  const answerNodes = nodes.filter((node) => !node.interrupted && !coveredByAggregate(node) && (finalNodeIds.has(node.id)
    || node.phase === 'final_answer' || (node.delivery && node.phase !== 'commentary')));
  return { nodes, answerNodes, finalNodeIds, copyText: answerNodes.map((node) => node.text).join('\n\n'), notice, error };
}
