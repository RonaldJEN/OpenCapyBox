import { useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import type { AssistantFileReference, FileInfo, RoundData, SubagentTask } from '../types';
import type { ChatRunRuntimeState } from '../runtime/chatRuntimeTypes';
import { projectRoundTranscript, roundRenderKey, type TranscriptTextNode } from '../transcript/projectRoundTranscript';
import { selectLatestActivityKeys } from '../transcript/latestActivity';
import { getToolCategory, projectToolItems, type ToolGroupItem } from '../utils/displayBlocks';
import { AssistantTextMessage } from './AssistantTranscript';
import { ToolItemView } from './ReasoningPanel';
import { SubagentTaskGroup } from './SubagentTaskGroup';
import { ActivityDisclosureScope } from './useActivityDisclosure';
import { RunStatusBar } from './RunStatusBar';
import { apiService } from '../services/api';
const NO_FILES: AssistantFileReference[] = [];

type Entry = { key: string; sequence?: number; step: number; rank: number; process: boolean } & (
  { kind: 'text'; node: TranscriptTextNode } | { kind: 'tool'; item: ToolGroupItem }
  | { kind: 'file'; file: AssistantFileReference });

export function InlineRoundTranscript({ round, run, streaming, onOpenFile, renderFile, reveal, onRetryStop, onInspectProcess, onOpenSubtask }: {
  round: RoundData; run?: ChatRunRuntimeState; streaming: boolean;
  onOpenFile?: (file: FileInfo) => void; renderFile: (file: AssistantFileReference) => ReactNode;
  reveal?: { messageId?: string; nonce: number }; onRetryStop?: () => void;
  onInspectProcess?: () => void;
  onOpenSubtask?: (task: SubagentTask) => void;
}) {
  const settled = ['completed', 'failed', 'cancelled', 'max_steps_reached'].includes(round.status);
  const transcript = projectRoundTranscript(round, streaming);
  const files = round.assistant_file_references || NO_FILES;
  const finals = transcript.finalNodeIds;
  const answers = new Set(transcript.answerNodes.map((node) => node.id));
  const hasAnswer = answers.size > 0;
  // Without a proven final answer, retain partial/attachment-only delivery text visibly.
  const entries: Entry[] = transcript.nodes.map((node) => ({ kind: 'text', node, key: node.id,
    sequence: node.sequence, step: finals.has(node.id) ? Number.MAX_SAFE_INTEGER - 1 : node.stepNumber ?? Number.MAX_SAFE_INTEGER, rank: 1,
    process: answers.has(node.id) ? false : node.interrupted && (!settled || hasAnswer) ? true
      : node.phase === 'commentary' ? true : settled ? hasAnswer : true }));
  for (const item of projectToolItems(round.steps, streaming)) {
    // Progress prose comes only from assistant content. Live reasoning is allowed
    // only in RunStatusBar's transient preview, never in the transcript or copy.
    entries.push({ kind: 'tool', item, key: `tool:${item.id}`, sequence: item.sequence, step: item.stepNumber || 1, rank: 2, process: true });
  }
  for (const file of files) entries.push({ kind: 'file', file, key: `file:${file.ref_id}`, sequence: file.event_sequence ?? undefined,
    step: Number.MAX_SAFE_INTEGER, rank: 3, process: false });
  const ordered = entries.filter((entry) => entry.kind !== 'file' && !finals.has(entry.key)).every((entry) => entry.sequence !== undefined);
  entries.sort((a, b) => ordered ? (a.sequence ?? Number.MAX_SAFE_INTEGER) - (b.sequence ?? Number.MAX_SAFE_INTEGER) : a.step - b.step || a.rank - b.rank);
  const latestKeys = selectLatestActivityKeys(entries);
  const previousEntries = entries.filter((entry) => entry.process && !latestKeys.has(entry.key));
  const hasProcess = entries.some((entry) => entry.process);
  const scope = `${apiService.getUserId()}:${roundRenderKey(round)}`;
  // Native phase is available at START, even before an empty message has a DOM node.
  const visibleMessages = round.assistant_messages?.filter((message) => message.state !== 'superseded') || [];
  const latestMessage = visibleMessages[visibleMessages.length - 1];
  const answerStarted = latestMessage?.phase === 'final_answer' && latestMessage.state !== 'interrupted';
  const presentationPhase = settled ? 'settled' : answerStarted ? 'answering' : 'working';
  const working = presentationPhase === 'working';
  const [expanded, setExpanded] = useState(false);
  const phase = useRef({ scope, presentationPhase });
  const lastReveal = useRef<number>();
  const revealNonce = reveal?.nonce;
  const revealTargetPresent = Boolean(reveal
    && (!reveal.messageId || entries.some((entry) => entry.kind === 'text' && entry.node.messageId === reveal.messageId)));
  const revealProcess = Boolean(reveal
    && (!reveal.messageId || entries.some((entry) => entry.process && entry.kind === 'text' && entry.node.messageId === reveal.messageId)));
  const root = useRef<HTMLDivElement>(null);
  const prefix = useId();
  useLayoutEffect(() => {
    if (phase.current.scope === scope && phase.current.presentationPhase === presentationPhase) return;
    phase.current = { scope, presentationPhase };
    if (presentationPhase !== 'working' && document.activeElement instanceof HTMLElement
      && root.current?.contains(document.activeElement)
      && document.activeElement.closest('[data-process-row="true"]')) {
      root.current.querySelector<HTMLButtonElement>('[data-process-summary]')?.focus({ preventScroll: true });
    }
    // A running-time click never overrides the next completion transition.
    setExpanded(false);
  }, [scope, presentationPhase]);
  useLayoutEffect(() => {
    if (revealTargetPresent && revealNonce !== undefined && lastReveal.current !== revealNonce) {
      lastReveal.current = revealNonce;
      if (revealProcess) setExpanded(true);
    }
  }, [revealTargetPresent, revealProcess, revealNonce]);
  const toggle = () => {
    if (!expanded) onInspectProcess?.();
    setExpanded((open) => !open);
  };
  const rowId = (entry: Entry) => `${prefix}-${encodeURIComponent(entry.key)}`;
  const isVisible = (entry: Entry) => !entry.process || expanded || (working && latestKeys.has(entry.key));
  const currentProcess = entries.filter((entry) => entry.process && latestKeys.has(entry.key));
  const reserveLiveSpace = round.status === 'running' && working && !expanded && !hasAnswer && files.length === 0;
  const boundedTools = reserveLiveSpace && currentProcess[currentProcess.length - 1]?.kind === 'tool';
  const currentTextKey = currentProcess.find((entry) => entry.kind === 'text')?.key || '';
  const currentStep = Math.max(0, ...currentProcess.map((entry) => entry.step));
  const activityViewport = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    // The local tool preview owns its scroll only; the chat viewport stays with
    // useChatReadingPosition. New groups start at their beginning, not an old offset.
    if (boundedTools && activityViewport.current) activityViewport.current.scrollTop = 0;
  }, [scope, boundedTools, currentTextKey, currentStep]);
  // Keep parallel tasks together without reparenting any assistant text node.
  const subtaskGroups = new Map<string, { items: ToolGroupItem[]; visible: boolean }>();
  const groupedSubtasks = new Set<string>();
  const isSubtask = (entry: Entry): entry is Entry & { kind: 'tool' } => entry.kind === 'tool'
    && entry.item.toolDisplay?.provider !== 'mcp' && getToolCategory(entry.item.toolName) === 'subagent';
  for (let i = 0; i < entries.length; i++) {
    if (!isSubtask(entries[i])) continue;
    const group: Array<Entry & { kind: 'tool' }> = [];
    while (i < entries.length && isSubtask(entries[i])) group.push(entries[i++] as Entry & { kind: 'tool' });
    i--;
    subtaskGroups.set(group[0].key, { items: group.map(entry => entry.item), visible: group.some(isVisible) });
    group.slice(1).forEach(entry => groupedSubtasks.add(entry.key));
  }
  const rowVisible = (entry: Entry) => !groupedSubtasks.has(entry.key) && (subtaskGroups.get(entry.key)?.visible ?? isVisible(entry));
  const canToggle = working ? previousEntries.length > 0 : hasProcess;
  return <div ref={root} data-process-container data-testid="inline-round-transcript">
    <RunStatusBar round={round} run={run} answering={answerStarted} expanded={expanded} onToggle={canToggle ? toggle : undefined}
      controls={(working ? previousEntries : entries.filter((entry) => entry.process)).map(rowId).join(' ')} onRetryStop={onRetryStop} />
    <ActivityDisclosureScope.Provider value={scope}>
      <div ref={activityViewport} className={`chat-transcript-rows ${reserveLiveSpace ? 'chat-live-space' : ''} ${boundedTools ? 'chat-live-tools' : ''}`}
        tabIndex={boundedTools ? 0 : undefined} role={boundedTools ? 'region' : undefined} aria-label={boundedTools ? '当前工具进展' : undefined}>
        {entries.map((entry) => <div key={entry.key} id={rowId(entry)} hidden={!rowVisible(entry)}
          data-process-row={entry.process ? 'true' : undefined}
          data-command-row={entry.kind === 'tool' && entry.item.toolDisplay?.provider !== 'mcp' && getToolCategory(entry.item.toolName) === 'bash' ? 'true' : undefined}
          data-transcript-node={entry.kind === 'text' ? entry.node.id : undefined}
          data-message-id={entry.kind === 'text' ? entry.node.messageId : undefined}
          className={entry.kind === 'text' ? 'prose max-w-none' : entry.kind === 'file' ? 'not-prose pt-1' : 'not-prose text-sm text-claude-secondary'}>
          {rowVisible(entry) && (entry.kind === 'text'
            ? <AssistantTextMessage node={entry.node} fileReferences={files} onOpenFile={onOpenFile} />
            : entry.kind === 'file' ? renderFile(entry.file)
              : subtaskGroups.has(entry.key) ? <SubagentTaskGroup items={subtaskGroups.get(entry.key)!.items} tasks={round.subagent_tasks || []} onOpen={onOpenSubtask} />
                : <ToolItemView item={entry.item} disableMotion />)}
        </div>)}
      </div>
    </ActivityDisclosureScope.Provider>
  </div>;
}
